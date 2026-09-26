"""File widget controller — the WidgetViewModel equivalent (logic, no GTK).

Owns one file widget's live state: current folder (mapped root or an embedded
navigation target), sorted entries, manual-order book, watcher, added-at
tracking, and every file operation. Views subscribe to on_entries_changed and
call controller methods; confirmation flows return the dialog payload before
executing.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional

from ..constants import SortMode
from ..i18n import fmt, t
from ..models.widget_config import WidgetConfig
from . import file_ops
from .file_sort import FileEntry, ManualOrderBook, sync_config_items
from .folder_watcher import FolderWatcher
from ..app_log import get_logger

_LOG = get_logger("dnd")


@dataclass
class Confirmation:
    title: str
    message: str
    confirm_label: str
    danger: bool = False
    checkbox_label: str = ""  # optional "also delete folder contents" style


@dataclass
class DropIntent:
    COPY = "copy"
    MOVE = "move"
    SHORTCUT = "shortcut"


def _uri_to_path(uri: str) -> str:
    """file:// URI → local path, percent-decoding exactly like GIO does.

    Nautilus/other file managers deliver RFC-escaped URIs (spaces → %20,
    non-ASCII → %xx), which a plain removeprefix leaves undecodable.
    """
    if not isinstance(uri, str) or not uri:
        return ""
    if not uri.startswith("file://"):
        return uri
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        path = Gio.File.new_for_uri(uri).get_path()
        if path:
            return path
    except Exception:
        pass
    from urllib.parse import unquote

    return unquote(uri.removeprefix("file://"))


class FileWidgetController:
    """One file widget's state + operations."""

    def __init__(
        self,
        config: WidgetConfig,
        storage_root: str,
        on_entries_changed: Optional[Callable[[], None]] = None,
        on_config_changed: Optional[Callable[[WidgetConfig], None]] = None,
        show_file_extensions: bool = True,
        watcher_factory=None,
        settings_service=None,
    ):
        self.config = config
        self.storage_root = storage_root
        self.on_entries_changed = on_entries_changed
        self.on_config_changed = on_config_changed
        self.show_file_extensions = show_file_extensions
        self.settings_service = settings_service  # stack settings + live reload
        self._watcher_factory = watcher_factory or FolderWatcher

        self.folder_path = self.resolve_folder_path()
        self.current_path = self.folder_path
        self.entries: List[FileEntry] = []
        self.manual = ManualOrderBook()
        self.cut_paths: List[str] = []  # internal cut marker
        self._watcher: Optional[FolderWatcher] = None
        self._load_manual_order()
        self._expanded_stack_key: Optional[str] = None
        self._projection_cache = None

    # ---- paths ------------------------------------------------------------------

    def resolve_folder_path(self) -> str:
        if self.config.mappedFolderPath:
            return os.path.expanduser(self.config.mappedFolderPath)
        name = self.config.managedFolderName or self.config.name or "Widget"
        return os.path.join(self.storage_root, name)

    @property
    def is_managed(self) -> bool:
        # PaneBox sets MappedFolderPath even for managed widgets; the folder
        # name is the real managed-ness discriminator (WidgetManager.cs:1015).
        return bool(self.config.managedFolderName)

    @property
    def is_at_root(self) -> bool:
        return os.path.realpath(self.current_path) == os.path.realpath(self.folder_path)

    def display_name_for(self, entry: FileEntry) -> str:
        """Shortcuts show their Name=; extensions follow the global setting."""
        if entry.extension == ".desktop":
            name = file_ops.shortcut_display_name(entry.path)
            if name:
                return name
        if self.show_file_extensions:
            return entry.name
        stem, ext = file_ops.split_extension(entry.name)
        return stem

    # ---- state ----------------------------------------------------------------------

    def start(self) -> None:
        self.refresh()
        self._start_watcher()

    def dispose(self) -> None:
        self._stop_watcher()

    def _start_watcher(self) -> None:
        self._stop_watcher()
        if not os.path.isdir(self.current_path):
            return
        self._watcher = self._watcher_factory(self.current_path, lambda _paths: self._on_folder_changed())
        self._watcher.start()

    def _stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def _on_folder_changed(self) -> None:
        self.refresh()
        if self.on_entries_changed:
            self.on_entries_changed()

    def refresh(self) -> None:
        self._projection_cache = None
        raw = file_ops.enumerate_directory(self.current_path)
        self.entries = [
            FileEntry(
                path=item["path"],
                name=item["name"],
                is_folder=item["is_folder"],
                size=item["size"],
                last_modified=item["last_modified"],
                extension=file_ops.split_extension(item["name"])[1],
                is_symlink=item["is_symlink"],
                is_shortcut=item["is_shortcut"],
            )
            for item in raw
        ]
        if self.is_at_root and self.config.sortMode == SortMode.MANUAL:
            self._reconcile_manual_order()
        if self.is_at_root:
            self._track_added_at()
        self._sort()

    def _sort(self) -> None:
        from .file_sort import sort_entries

        if self.config.sortMode == SortMode.MANUAL and self.is_at_root:
            for entry in self.entries:
                entry.sort_order = self.manual.order_for(entry.path)
            self.entries = sort_entries(self.entries, SortMode.MANUAL, False)
        else:
            self.entries = sort_entries(self.entries, self.config.sortMode, self.config.sortDescending)

    def _reconcile_manual_order(self) -> None:
        live = {e.path for e in self.entries}
        self.manual.paths = [p for p in self.manual.paths if p in live]
        for entry in self.entries:
            if not self.manual.contains(entry.path):
                self.manual.upsert(entry.path)

    def _load_manual_order(self) -> None:
        for item in self.config.items:
            self.manual.paths.append(item.path)

    def _persist_manual_order(self) -> None:
        if not self.is_at_root:
            return
        if sync_config_items(list(self.manual.paths), self.config.items, True):
            self._notify_config()

    def _track_added_at(self) -> None:
        for entry in self.entries:
            if not self.config.fileAddedAtTrackingInitialized or entry.path not in self.config.fileAddedAtByPath:
                self.config.record_added(entry.path)
        if not self.config.fileAddedAtTrackingInitialized:
            self.config.fileAddedAtTrackingInitialized = True
            self._notify_config()

    def _notify_config(self) -> None:
        if self.on_config_changed:
            self.on_config_changed(self.config)

    # ---- navigation -------------------------------------------------------------------

    def navigate(self, path: str) -> bool:
        if not os.path.isdir(path):
            return False
        if not self._in_scope(path):
            return False
        self.current_path = path
        self.start()
        return True

    def _in_scope(self, path: str) -> bool:
        root = os.path.realpath(self.folder_path)
        target = os.path.realpath(path)
        return target == root or target.startswith(root + os.sep)

    def go_back(self) -> bool:
        if self.is_at_root:
            return False
        parent = os.path.dirname(self.current_path)
        return self.navigate(parent if self._in_scope(parent) else self.folder_path)

    def crumbs(self) -> List[str]:
        root = os.path.realpath(self.folder_path)
        current = os.path.realpath(self.current_path)
        if current == root:
            return [self.config.name or os.path.basename(root)]
        relative = os.path.relpath(current, root)
        return [self.config.name or os.path.basename(root)] + relative.split(os.sep)

    # ---- open ------------------------------------------------------------------------

    def open(self, entry: FileEntry) -> None:
        path = entry.path
        if entry.extension == ".desktop":
            target = file_ops.shortcut_target(path)
            if target and os.path.exists(target):
                self._launch_default(target)
                return
        if entry.is_folder:
            self.navigate(path)
            return
        self._launch_default(path)

    @staticmethod
    def _launch_default(path: str) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio

            file = Gio.File.new_for_path(path)
            Gio.AppInfo.launch_default_for_uri(file.get_uri(), None)
        except Exception:
            subprocess.Popen(["xdg-open", path])

    # ---- rename ---------------------------------------------------------------------

    def plan_rename(self, entry: FileEntry, typed_name: str) -> Confirmation | None:
        """Returns a confirmation prompt when the extension would change."""
        sanitized = file_ops.sanitize_file_system_name(typed_name)
        _resolved, needs_confirm = file_ops.resolve_rename_destination(
            entry.name,
            sanitized,
            entry.is_folder,
            entry.extension == ".desktop",
            self.show_file_extensions,
        )
        if needs_confirm:
            return Confirmation(
                title=t("Common.Rename"),
                message=t("Widget.Rename.ExtensionChangeWarning"),
                confirm_label=t("Common.Ok"),
            )
        return None

    def rename(self, entry: FileEntry, typed_name: str) -> Optional[str]:
        sanitized = file_ops.sanitize_file_system_name(typed_name)
        if not sanitized:
            raise ValueError(t("Widget.Validation.NameRequired"))
        resolved, _confirm = file_ops.resolve_rename_destination(
            entry.name,
            sanitized,
            entry.is_folder,
            entry.extension == ".desktop",
            self.show_file_extensions,
        )
        if resolved == entry.name:
            return None
        target = os.path.join(os.path.dirname(entry.path), resolved)
        if os.path.exists(target) and target != entry.path:
            raise ValueError(t("Widget.Validation.TargetExists"))
        if self._watcher is not None:
            self._watcher.pause()
        try:
            new_path = file_ops.rename_entry(entry.path, resolved)
            if self.manual.contains(entry.path):
                self.manual.remove(entry.path)
                self.manual.upsert(new_path)
        finally:
            if self._watcher is not None:
                self._watcher.resume()
        self.refresh()
        if self.on_entries_changed:
            self.on_entries_changed()
        return new_path

    # ---- delete -------------------------------------------------------------------------

    def plan_delete(self, entries: List[FileEntry]) -> Confirmation:
        folders = [e for e in entries if e.is_folder]
        if len(entries) == 1:
            title = fmt("Widget.DeleteItemsTitleOne", entries[0].name)
        else:
            title = fmt("Widget.DeleteItemsTitleMany", len(entries))
        message = ""
        if len(folders) == 1 and len(entries) == 1:
            message = t("Widget.DeleteFolderNoteOne")
        elif folders:
            message = fmt("Widget.DeleteFolderNoteMany", len(folders))
        return Confirmation(
            title=title,
            message=message,
            confirm_label=t("Widget.MoveToRecycleBin"),
            danger=True,
        )

    def trash(self, entries: List[FileEntry]) -> file_ops.TransferReport:
        with self._watcher_pause():
            report = file_ops.trash_items([e.path for e in entries])
        for src, _dst in report.completed:
            self.manual.remove(src)
        self.refresh()
        if self.on_entries_changed:
            self.on_entries_changed()
        return report

    def plan_permanent_delete(self, entries: List[FileEntry]) -> Confirmation:
        title = t("Widget.PermanentDelete.Title")
        body = fmt("Widget.PermanentDelete.Body", len(entries))
        return Confirmation(
            title=title,
            message=body,
            confirm_label=t("Widget.PermanentDelete.Confirm"),
            danger=True,
        )

    def delete_permanently(self, entries: List[FileEntry]) -> file_ops.TransferReport:
        with self._watcher_pause():
            report = file_ops.delete_permanently([e.path for e in entries])
        for src, _dst in report.completed:
            self.manual.remove(src)
        self.refresh()
        if self.on_entries_changed:
            self.on_entries_changed()
        return report

    def _watcher_pause(self):
        return self._watcher if self._watcher is not None else _NullPause()

    # ---- clipboard -------------------------------------------------------------------------

    def cut(self, entries: List[FileEntry]) -> None:
        self.cut_paths = [e.path for e in entries]
        self._set_clipboard_uris(self.cut_paths, cut=True)

    def copy(self, entries: List[FileEntry]) -> None:
        self.cut_paths = []
        self._set_clipboard_uris([e.path for e in entries], cut=False)

    def _set_clipboard_uris(self, paths: List[str], cut: bool) -> None:
        try:
            import gi

            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk

            # text/uri-list style lines (what Nautilus and friends exchange)
            provider = Gdk.ContentProvider.new_for_value("".join("file://" + p + "\r\n" for p in paths))
            clipboard = Gdk.Display.get_default().get_clipboard()
            clipboard.set_content(provider)
        except Exception:
            pass

    def can_paste(self) -> bool:
        try:
            import gi

            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk

            formats = Gdk.Display.get_default().get_clipboard().get_formats()
            text = formats.to_string() or ""
            return "uri-list" in text or "text/plain" in text or formats.contain_gtype(str)
        except Exception:
            return False

    def paste(self, paths: Optional[List[str]] = None) -> Optional[file_ops.TransferReport]:
        """Paste clipboard uris into the current folder (move when cut).

        `paths` comes from the view's async Gdk.FileList read; when omitted,
        the plain-text clipboard is parsed (file:// or /-absolute lines).
        """
        if paths is None:
            try:
                import gi

                gi.require_version("Gdk", "4.0")
                from gi.repository import Gdk

                text = Gdk.Display.get_default().get_clipboard().read_text_sync(None)
            except Exception:
                return None
            if not text:
                return None
            paths = [
                _uri_to_path(line) for line in text.splitlines() if line.startswith("file://") or line.startswith("/")
            ]
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return None
        move = bool(self.cut_paths) and all(p in self.cut_paths for p in paths)
        with self._watcher_pause():
            report = file_ops.transfer_items(paths, self.current_path, move=move)
        if move:
            self.cut_paths = []
            for src, _dst in report.completed:
                self.manual.remove(src)
        self.refresh()
        if self.on_entries_changed:
            self.on_entries_changed()
        return report

    # ---- misc ops ---------------------------------------------------------------------------

    def create_shortcut(self, entry: FileEntry) -> str:
        with self._watcher_pause():
            path = file_ops.create_shortcut(entry.path, self.current_path)
        self.refresh()
        if self.on_entries_changed:
            self.on_entries_changed()
        return path

    def new_folder(self) -> str:
        sanitized = file_ops.sanitize_file_system_name(t("Widget.NewFolderName"))
        desired = os.path.join(self.current_path, sanitized)
        target = file_ops.get_available_path(desired)
        os.makedirs(target, exist_ok=True)
        self.manual.upsert(target)
        self._persist_manual_order()
        self.refresh()
        if self.on_entries_changed:
            self.on_entries_changed()
        return target

    def copy_paths(self, entries: List[FileEntry]) -> int:
        try:
            import gi

            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk

            Gdk.Display.get_default().get_clipboard().set_text("\n".join(e.path for e in entries))
            return len(entries)
        except Exception:
            return 0

    def move_back_to_desktop(self, entries: List[FileEntry]) -> file_ops.TransferReport:
        desktop = file_ops.desktop_directory()
        with self._watcher_pause():
            report = file_ops.transfer_items([e.path for e in entries], desktop, move=True)
        for src, _dst in report.completed:
            self.manual.remove(src)
        self.refresh()
        if self.on_entries_changed:
            self.on_entries_changed()
        return report

    def reveal(self, entries: List[FileEntry]) -> None:
        file_ops.reveal_in_file_manager([e.path for e in entries])

    # ---- drop ---------------------------------------------------------------------------------

    def handle_drop(
        self, uris: List[str], intent: str, preferred_index: Optional[int] = None
    ) -> file_ops.TransferReport:
        paths = [_uri_to_path(u) for u in uris]
        paths = [p for p in paths if p and os.path.exists(p) and file_ops.should_display_entry(os.path.basename(p))]
        if not paths:
            if uris:
                _LOG.warning("[DnD] dropped URIs resolved to no usable paths: %r", uris)
            return file_ops.TransferReport()
        if intent == DropIntent.SHORTCUT:
            for path in paths:
                file_ops.create_shortcut(path, self.current_path)
            report = file_ops.TransferReport(completed=[(p, p) for p in paths])
        else:
            move = intent == DropIntent.MOVE
            if move:
                # Refuse dropping a folder into itself.
                paths = [p for p in paths if not file_ops.is_path_under_directory(self.current_path, p)]
            with self._watcher_pause():
                report = file_ops.transfer_items(paths, self.current_path, move=move)
            if self.config.sortMode == SortMode.MANUAL:
                for src, dst in report.completed:
                    self.manual.upsert(dst, preferred_index=preferred_index)
                self._persist_manual_order()
        self.refresh()
        if self.on_entries_changed:
            self.on_entries_changed()
        return report

    # ---- view state -----------------------------------------------------------------------------

    def set_view_mode(self, mode: str) -> None:
        if self.config.viewMode != mode:
            self.config.viewMode = mode
            self._notify_config()

    def set_sort_mode(self, mode: str) -> None:
        if mode == self.config.sortMode:
            self.config.sortDescending = not self.config.sortDescending
        else:
            self.config.sortMode = mode
            self.config.sortDescending = False
        if mode != SortMode.MANUAL:
            # Leaving manual mode drops the persisted order.
            pass
        self._sort()
        self._notify_config()
        if self.on_entries_changed:
            self.on_entries_changed()

    def reorder(self, entry: FileEntry, target_index: int) -> bool:
        """Manual-mode reorder (drag inside the widget)."""
        if self.config.sortMode != SortMode.MANUAL or not self.is_at_root:
            return False
        if self.manual.move(entry.path, target_index):
            self._persist_manual_order()
            self.refresh()
            if self.on_entries_changed:
                self.on_entries_changed()
            return True
        return False

    # ---- stacks (WidgetViewModel.Stacks) -----------------------------------------

    def _file_settings(self):
        if self.settings_service is not None:
            return self.settings_service.settings.fileWidget
        from ..models.settings_slices import FileWidgetSettingsSlice

        return FileWidgetSettingsSlice()

    def stack_settings(self) -> dict:
        """Per-widget overrides resolved against the global defaults."""
        from . import stack_grouping, stack_settings

        fw = self._file_settings()
        return {
            "enabled": stack_settings.resolve_enabled(self.config, fw.fileStacksEnabled),
            "auto_stacking": fw.fileStackAutoStacking,
            "group_by": stack_settings.resolve_group_by(self.config, fw.fileStackGroupBy),
            "threshold": stack_settings.resolve_threshold(self.config, fw.fileStackThreshold),
            "order_by": stack_settings.resolve_order_by(self.config, fw.fileStackOrderBy),
            "open_mode": stack_settings.resolve_open_mode(self.config, fw.fileStackOpenMode),
            "popover_layout": stack_grouping.normalize_popover_layout(fw.fileStackPopoverLayout),
            "custom_rules": list(fw.fileStackCustomRules or []),
            "unmatched_behavior": fw.fileStackUnmatchedBehavior,
        }

    @property
    def stacks_enabled(self) -> bool:
        return self.stack_settings()["enabled"]

    def stack_projection(self):
        """Current display projection (cached until entries/settings change)."""
        from . import stack_settings as stack_settings_service
        from .stack_grouping import project

        if self._projection_cache is not None:
            return self._projection_cache
        snapshot = self.stack_settings()
        if not snapshot["enabled"]:
            self._projection_cache = project(self.entries, stacks_enabled=False)
            return self._projection_cache
        self._projection_cache = project(
            self.entries,
            stacks_enabled=True,
            auto_stacking=snapshot["auto_stacking"],
            group_by=snapshot["group_by"],
            order_by=snapshot["order_by"],
            threshold=snapshot["threshold"],
            custom_rules=snapshot["custom_rules"],
            unmatched_behavior=snapshot["unmatched_behavior"],
            added_at_by_path=self.config.fileAddedAtByPath,
            customizations=stack_settings_service.load_customizations(self.config),
            expanded_key=self._expanded_stack_key,
        )
        return self._projection_cache

    def visible_units(self) -> list:
        """Display units for the surface: stack tiles, loose items, children."""
        if not self.stacks_enabled:
            from .stack_grouping import DisplayUnit, loose_order_key

            return [DisplayUnit(loose_order_key(e), entry=e) for e in self.entries]
        return self.stack_projection().visible_units

    def stack_unit(self, key: str):
        return self.stack_projection().unit_for_key(key)

    def _invalidate_projection(self, *, notify: bool = True) -> None:
        self._projection_cache = None
        if notify and self.on_entries_changed:
            self.on_entries_changed()

    def invalidate_stack_projection(self) -> None:
        """Settings changed (global stack defaults) → recompute lazily."""
        self._projection_cache = None

    def _apply_stack_operation(self, mutator) -> bool:
        """Run a manual-stack operation, persist customizations, rebuild."""
        from . import stack_settings as stack_settings_service

        customizations = stack_settings_service.load_customizations(self.config)
        projection = self.stack_projection()
        changed = mutator(customizations, projection)
        if changed:
            stack_settings_service.persist_customizations(self.config, customizations)
            self._projection_cache = None
            self._notify_config()
            if self.on_entries_changed:
                self.on_entries_changed()
        return changed

    def toggle_stack(self, key: str) -> bool:
        unit = self.stack_unit(key)
        if unit is None or not unit.is_stack:
            return False
        self._expanded_stack_key = None if unit.expanded else key
        self._invalidate_projection()
        return True

    def expand_stack(self, key: str) -> bool:
        unit = self.stack_unit(key)
        if unit is None or not unit.is_stack:
            return False
        if not unit.expanded:
            self._expanded_stack_key = key
            self._invalidate_projection()
        return True

    def set_stack_open_mode(self, mode: str) -> None:
        from . import stack_settings as stack_settings_service

        stack_settings_service.set_open_mode_override(self.config, mode)
        self._notify_config()
        self._invalidate_projection()

    def set_widget_stacks_enabled(self, enabled: bool) -> None:
        """Per-widget master switch ('Enable for this widget')."""
        from . import stack_settings as stack_settings_service

        stack_settings_service.set_enabled_override(self.config, enabled)
        self._expanded_stack_key = None
        self._notify_config()
        self._invalidate_projection()

    def follow_global_stack_defaults(self) -> None:
        from . import stack_settings as stack_settings_service

        stack_settings_service.clear_overrides(self.config)
        self._notify_config()
        self._invalidate_projection()

    def disable_stack_group(self, key: str) -> bool:
        from .stack_grouping import set_stack_disabled

        def mutate(customizations, projection):
            if projection.unit_for_key(key) is None:
                return False
            set_stack_disabled(key, True, customizations)
            if self._expanded_stack_key == key:
                self._expanded_stack_key = None
            return True

        return self._apply_stack_operation(mutate)

    def restore_disabled_groups(self) -> bool:
        from . import stack_settings as stack_settings_service

        customizations = stack_settings_service.load_customizations(self.config)
        if not customizations.disabled:
            return False
        customizations.disabled.clear()
        stack_settings_service.persist_customizations(self.config, customizations)
        self._invalidate_projection()
        self._notify_config()
        return True

    def has_disabled_groups(self) -> bool:
        from . import stack_settings as stack_settings_service

        return bool(stack_settings_service.get_disabled_stacks(self.config))

    def rename_stack(self, key: str, name: str) -> bool:

        def mutate(customizations, projection):
            unit = projection.unit_for_key(key)
            if unit is None or not unit.is_stack:
                return False
            stripped = name.strip()
            if not stripped:
                customizations.name_overrides.pop(key, None)
            else:
                customizations.name_overrides[key] = stripped
            return True

        return self._apply_stack_operation(mutate)

    def move_stack(self, key: str, delta: int) -> bool:
        from .stack_grouping import move_stack as move

        return self._apply_stack_operation(
            lambda customizations, projection: move(key, delta, customizations, projection)
        )

    def dissolve_stack(self, key: str) -> bool:
        from .stack_grouping import dissolve_stack as dissolve

        def mutate(customizations, projection):
            changed = dissolve(key, customizations, projection)
            if changed and self._expanded_stack_key == key:
                self._expanded_stack_key = None
            return changed

        return self._apply_stack_operation(mutate)

    def create_manual_stack_from(self, paths: List[str]) -> bool:
        from .stack_grouping import create_manual_stack as create

        return self._apply_stack_operation(
            lambda customizations, projection: create(self.entries, paths, customizations, projection) is not None
        )

    def add_paths_to_stack(self, key: str, paths: List[str]) -> bool:
        from .stack_grouping import add_items_to_stack as add

        return self._apply_stack_operation(
            lambda customizations, projection: add(key, self.entries, customizations, projection, paths)
        )

    def remove_paths_from_stack(self, key: str, paths: List[str]) -> bool:
        from .stack_grouping import remove_items_from_stack as remove

        def mutate(customizations, projection):
            changed = remove(key, self.entries, customizations, projection, paths)
            if changed and self._expanded_stack_key == key and projection.unit_for_key(key) is None:
                self._expanded_stack_key = None
            return changed

        return self._apply_stack_operation(mutate)

    def stack_member_paths(self, key: str) -> List[str]:
        unit = self.stack_unit(key)
        return [e.path for e in unit.stack.items] if unit is not None and unit.is_stack else []

    def containing_stack_key(self, path: str) -> Optional[str]:
        for unit in self.stack_projection().visible_units:
            if unit.child_of and unit.entry and unit.entry.path == path:
                return unit.child_of
        return None


class _NullPause:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None
