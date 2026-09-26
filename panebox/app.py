"""PaneBoxApplication — startup pipeline, tray, hotkeys, toggle-all, shutdown.

Single instance comes free: Gtk.Application's D-Bus name registration forwards
activate/actions of second launches to the primary.
"""

from __future__ import annotations

import logging
import os
import threading
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import i18n
from .app_log import setup_logging
from .constants import ensure_dirs
from .i18n import fmt, t
from .platform.autostart import ensure_autostart_default, write_autostart
from .platform.event_thread import X11EventThread
from .platform.hotkey import GlobalHotkeyController, modifiers_from_x11
from .platform.tray import TrayController, TrayMenuItem
from .services.organize_service import OrganizeService
from .services.quick_capture_clipboard import QuickCaptureClipboardMonitor
from .services.settings_service import SettingsService
from .services.todo_reminder import TodoReminderNotification, TodoReminderService
from .services.todo_store import TodoWidgetStore
from .services.widget_manager import FileWidgetPathConflict, WidgetManager

APP_ID = "org.panebox.PaneBox"
CSS_PATH = Path(__file__).parent / "ui" / "app.css"


class PaneBoxApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        from .constants import migrate_legacy_dirs

        migrate_legacy_dirs()  # pre-rename deskbox dirs → panebox, once
        self.settings_service = SettingsService()
        self.widget_manager: Optional[WidgetManager] = None
        self.todo_reminder: Optional[TodoReminderService] = None
        self.quick_capture_clipboard: Optional[QuickCaptureClipboardMonitor] = None
        self.tray: Optional[TrayController] = None
        self.event_thread: Optional[X11EventThread] = None
        self.hotkey: Optional[GlobalHotkeyController] = None
        self.search_hotkey: Optional[GlobalHotkeyController] = None
        self.search_popup = None  # SearchPopupWindow singleton
        self.settings_window = None  # arrives with the settings view (M1.6)
        self.organize_service = None  # OrganizeService (M3)
        self.organize_window = None  # OrganizeWindow singleton
        self.cloud_backup_service = None  # CloudBackupService (M3)
        self.update_service = None  # UpdateCheckService (M3)
        self._update_notification_shown = False
        self._restore_source = 0
        self._cloud_tick_source = 0
        self._restored = False  # set once the deferred widget restore has run

    # ---- lifecycle ---------------------------------------------------------------

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        log_file = setup_logging()
        self._log(f"[Startup] logging to {log_file}")
        try:
            ensure_dirs()
            self.settings_service.use_scheduler(lambda ms, fn: GLib.timeout_add(ms, fn))
            self.settings_service.load()
            i18n.set_language(self._resolved_language())
            self._load_css()
        except Exception as exc:
            self._log(f"[Startup] settings load failed: {exc}\n{traceback.format_exc()}")

        core = self.settings_service.settings.core
        try:
            core.autoStartDefaultApplied = ensure_autostart_default(core.autoStart, core.autoStartDefaultApplied)
        except Exception as exc:
            self._log(f"[Startup] autostart default failed: {exc}")

        self._register_actions()
        self._install_signal_handlers()
        self.hold()

    def _install_signal_handlers(self) -> None:
        """SIGTERM/SIGINT → graceful quit (the default handler kills without
        the shutdown flush, losing the debounced layout save)."""
        import signal

        def on_signal(signum, _frame):
            GLib.idle_add(self.quit)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, on_signal)
            except (OSError, ValueError):
                pass

    def do_activate(self) -> None:
        self._setup_runtime()
        self._revive_on_reactivate()

    def _revive_on_reactivate(self) -> None:
        """Second launch while the app is resident must show something.

        Closing the last widget leaves the instance alive (tray/notification),
        so a re-run forwards activate here — without this it looks dead.
        Widgets merely hidden (toggle-all) get raised; none left → recreate
        the default file widget at the first-run spot.
        """
        manager = self.widget_manager
        if manager is None or not self._restored:
            return  # initial restore (+300ms) hasn't run yet — not a re-launch
        if manager.has_visible_widgets:
            return
        if manager.runtimes:
            manager.raise_toggle.raise_all()
            self._log("[Activate] raised hidden widgets")
            return
        try:
            manager.create_file_widget(name=t("Widget.DefaultDesktopName"), place_for_first_run=True)
            self._log("[Activate] recreated default file widget")
        except Exception as exc:
            self._log(f"[Activate] revive failed: {exc}")

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        args = command_line.get_arguments()
        if "--settings" in args:
            self.activate_action("settings")
            return 0
        if "--toggle" in args:
            self.activate_action("toggle-widgets")
            return 0
        self.activate()
        return 0

    def do_shutdown(self) -> None:
        if self._restore_source:
            GLib.source_remove(self._restore_source)
            self._restore_source = 0
        if self._cloud_tick_source:
            GLib.source_remove(self._cloud_tick_source)
            self._cloud_tick_source = 0
        if self.todo_reminder is not None:
            self.todo_reminder.stop()
            self.todo_reminder = None
        if self.quick_capture_clipboard is not None:
            self.quick_capture_clipboard.stop()
            self.quick_capture_clipboard = None
        if self.widget_manager is not None:
            try:
                self.widget_manager.shutdown()
            except Exception as exc:
                self._log(f"[Shutdown] widget manager: {exc}")
            self.widget_manager = None
        if self.hotkey is not None:
            self.hotkey.clear()
            self.hotkey = None
        if self.search_hotkey is not None:
            self.search_hotkey.clear()
            self.search_hotkey = None
        if self.search_popup is not None:
            try:
                self.search_popup.vm.dispose()
            except Exception:
                pass
            self.search_popup.destroy()
            self.search_popup = None
        if self.event_thread is not None:
            self.event_thread.stop()
            self.event_thread = None
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        try:
            self.settings_service.flush_pending_save()
        except Exception as exc:
            self._log(f"[Shutdown] settings flush: {exc}")
        Gtk.Application.do_shutdown(self)

    # ---- runtime -------------------------------------------------------------------

    def _setup_runtime(self) -> None:
        if self.widget_manager is not None:
            return
        self.widget_manager = WidgetManager(self.settings_service, application=self)
        self.widget_manager.on_tray_state_text = self._refresh_tray_tooltip
        self.widget_manager.on_widgets_changed = self._on_widgets_changed

        self.event_thread = X11EventThread()
        self.event_thread.start()
        self.event_thread.set_active_window_callback(self.widget_manager.raise_toggle.on_active_window_changed)

        self.hotkey = GlobalHotkeyController(self.event_thread, log=self._log)
        core = self.settings_service.settings.core
        status = self.hotkey.apply(
            enabled=core.globalHotkeyEnabled,
            activation_kind=core.globalHotkeyActivationKind,
            modifiers=core.globalHotkeyModifiers,
            keysym=core.globalHotkeyKey,
            on_triggered=lambda: self.activate_action("toggle-widgets"),
        )
        self._log(f"[Hotkey] {status.state} {status.detail}".rstrip())

        search_slice = self.settings_service.settings.search
        self.search_hotkey = GlobalHotkeyController(self.event_thread, log=self._log)
        status = self.search_hotkey.apply(
            enabled=search_slice.searchHotkeyEnabled,
            activation_kind="KeyPress",
            modifiers=modifiers_from_x11(search_slice.searchHotkeyModifiers),
            keysym=search_slice.searchHotkeyKey,
            on_triggered=lambda: self.open_search_popup(),
        )
        self._log(f"[Hotkey] search {status.state} {status.detail}".rstrip())

        self._start_tray()

        # Crash recovery for an interrupted desktop organization must run
        # BEFORE widget surfaces are restored (it may drop uncommitted widgets).
        self.organize_service = OrganizeService(self.settings_service)
        try:
            recovered = self.organize_service.recover_pending()
            if recovered:
                self._log(f"[Organize] recovered {recovered} pending item(s)")
        except Exception as exc:
            self._log(f"[Organize] recovery failed (continuing): {exc}")

        # A staged cloud-backup restore applies BEFORE surfaces come back, so
        # todo/quick-capture widgets load the merged stores on creation.
        self._apply_pending_restore()

        # Restore after the work-area settles (monitors may still be probing).
        self._restore_source = GLib.timeout_add(300, self._restore_widgets_once)

        # Scheduled cloud backups: checked every 5 minutes (interval gating and
        # the 10-minute spacing floor live inside the service).
        self._cloud_tick_source = GLib.timeout_add(5 * 60 * 1000, self._tick_cloud_backup)

        # Background update check: once, shortly after startup (45 s, as on
        # Windows), gated on the auto-check setting.
        GLib.timeout_add(45_000, self._background_update_check)

    def _restore_widgets_once(self) -> bool:
        self._restore_source = 0
        if self.widget_manager is None:
            return False
        self._restored = True
        try:
            self.widget_manager.restore_widgets()
            self.widget_manager.ensure_initial_file_widget(interactive=True)
            self.widget_manager.capture_geometries()
        except Exception as exc:
            self._log(f"[Restore] failed: {exc}\n{traceback.format_exc()}")
        self.todo_reminder = TodoReminderService(
            settings_service=self.settings_service,
            store_factory=lambda widget_id: TodoWidgetStore(widget_id),
            notify=self._notify_todo_reminder,
            log=self._log,
        )
        self.todo_reminder.start(lambda ms, fn: GLib.timeout_add(ms, fn))
        self.quick_capture_clipboard = QuickCaptureClipboardMonitor(
            settings_service=self.settings_service,
            quick_capture_service=self.widget_manager.quick_capture_service,
            log=self._log,
        )
        self.quick_capture_clipboard.refresh()
        return False

    # ---- cloud backup -----------------------------------------------------------------

    def _apply_pending_restore(self) -> None:
        """Build the backup services and apply a restore staged last session."""
        from .platform.secrets import CredentialStore
        from .services.cloud_backup import CloudBackupService
        from .services.data_backup import DataBackupService

        data_backup = DataBackupService(settings_service=self.settings_service, log=self._log)
        self.cloud_backup_service = CloudBackupService(
            self.settings_service,
            data_backup=data_backup,
            credentials=CredentialStore(log=self._log),
            log=self._log,
        )
        self.cloud_backup_service.refresh_options()  # observe identity, keep stamps
        try:
            result = data_backup.apply_pending_restore()
        except Exception as exc:
            self._log(f"[CloudBackup] restore apply crashed (continuing): {exc}")
            return
        if result is None:
            return
        if result.success:
            self._log(
                f"[CloudBackup] restore applied: {result.todo_stores} todo store(s), "
                f"{result.merged_items} merged item(s), quick-capture={result.quick_capture}, "
                f"widget-style={result.widget_style}"
            )
        elif result.abandoned:
            self._log(f"[CloudBackup] restore abandoned after repeated failures: {result.error}")
        else:
            self._log(f"[CloudBackup] restore apply failed (will retry next launch): {result.error}")

    def _tick_cloud_backup(self) -> bool:
        """Periodic scheduled-backup check; failures only log (retry next tick)."""
        if self.cloud_backup_service is None:
            return False
        try:
            from .constants import APP_VERSION

            result = self.cloud_backup_service.tick_scheduled(app_version=APP_VERSION)
        except Exception as exc:
            self._log(f"[CloudBackup] scheduled tick crashed (continuing): {exc}")
            return True
        if result is None:
            return True
        if result.outcome == "Uploaded":
            note = " (unverified)" if result.unverified else ""
            self._log(f"[CloudBackup] scheduled backup uploaded {result.snapshot_name}{note}")
        elif result.outcome not in ("NotConfigured", "NoScope"):
            self._log(f"[CloudBackup] scheduled backup: {result.outcome} {result.message}".rstrip())
        return True

    # ---- update check -------------------------------------------------------------------

    def _ensure_update_service(self):
        if self.update_service is None:
            from .services.update_check import UpdateCheckService

            self.update_service = UpdateCheckService(log=self._log)
        return self.update_service

    def _check_updates(self):
        return self._ensure_update_service().check()

    def _background_update_check(self) -> bool:
        if not self.settings_service.settings.core.autoCheckForUpdates:
            return False

        def worker():
            result = self._check_updates()
            GLib.idle_add(lambda: self._handle_update_result(result))

        threading.Thread(target=worker, daemon=True, name="update-check").start()
        return False  # once

    def _handle_update_result(self, result) -> None:
        core = self.settings_service.settings.core
        core.lastUpdateCheckAt = result.checked_at_utc
        self.settings_service.save_debounced()
        if result.is_update_available:
            self._log(f"[Update] new version available: {result.remote_version}")
            if not self._update_notification_shown:
                self._update_notification_shown = True
                self._notify(
                    t("Tray.UpdateAvailableTitle"),
                    fmt("Tray.UpdateAvailableMessage", result.remote_version),
                )
        elif result.status == "Failed":
            self._log(f"[Update] background check failed: {result.error}")
        if self.settings_window is not None:
            try:
                self.settings_window._update_check_result = result
                if self.settings_window._update_status is not None:
                    self.settings_window._update_status.set_text(self.settings_window._update_status_text())
            except Exception:
                pass

    def _on_widgets_changed(self) -> None:
        if self.todo_reminder is not None:
            self.todo_reminder.refresh()
        if self.quick_capture_clipboard is not None:
            self.quick_capture_clipboard.refresh()
        self._rebuild_tray_menu()

    def _refresh_todo_surfaces(self) -> None:
        """Reload live Todo widgets after a store change made outside them
        (notification complete/snooze)."""
        if self.widget_manager is None:
            return
        for runtime in self.widget_manager.runtimes.values():
            surface = runtime.surface
            if runtime.config.widgetKind == "Todo" and hasattr(surface, "store"):
                try:
                    surface.data = surface.store.load()
                    surface.refresh()
                except Exception as exc:
                    self._log(f"[TodoReminder] refresh surface: {exc}")

    def _notify_todo_reminder(self, notification: TodoReminderNotification) -> None:
        try:
            gi.require_version("Notify", "0.7")
            from gi.repository import Notify

            if not Notify.is_initted():
                Notify.init("PaneBox")
            note = Notify.Notification.new(notification.title, notification.message, "panebox")
            note.add_action(
                "complete",
                t("Todo.Menu.MarkCompleted"),
                lambda *_a: self._todo_notification_action(notification, "complete"),
                None,
            )
            note.add_action(
                "snooze",
                t("Todo.Menu.Snooze"),
                lambda *_a: self._todo_notification_action(notification, "snooze"),
                None,
            )
            note.show()
        except Exception:
            self._log(f"[TodoReminder] {notification.title}: {notification.message}")

    def _todo_notification_action(self, notification: TodoReminderNotification, action: str) -> None:
        service = self.todo_reminder
        if service is None:
            return
        if action == "complete":
            service.complete(notification.widget_id, notification.item_id)
        else:
            service.snooze(notification.widget_id, notification.item_id, timedelta(minutes=10))
        self._refresh_todo_surfaces()

    # ---- tray ------------------------------------------------------------------------

    def _start_tray(self) -> None:
        actions = {
            "organize": lambda: self.open_organize_window(),
            "new-widget": self._tray_new_widget,
            "map-folder": self._tray_map_folder,
            "add-feature": self._tray_add_feature,
            "open-storage": self._tray_open_storage,
            "settings": lambda: self.open_settings(),
            "exit": self.quit,
        }
        self.tray = TrayController(actions, on_activate=lambda: self.activate_action("toggle-widgets"), log=self._log)
        self.tray.icon_name = "folder"
        self._rebuild_tray_menu()
        self._refresh_tray_tooltip()
        try:
            self.tray.start()
        except Exception as exc:
            self._log(f"[Tray] start failed (continuing without tray): {exc}")

    def _rebuild_tray_menu(self) -> None:
        if self.tray is None:
            return
        can_create = self.widget_manager is not None
        self.tray.set_menu(
            [
                TrayMenuItem("organize", t("Tray.OrganizeDesktop")),
                TrayMenuItem("new-widget", t("Common.NewWidget"), disabled=not can_create),
                TrayMenuItem("map-folder", t("Common.NewFolderMapping"), disabled=not can_create),
                TrayMenuItem("add-feature", t("Common.AddFeatureWidget"), disabled=not can_create),
                TrayMenuItem("open-storage", t("Tray.OpenManagedStorage")),
                TrayMenuItem("settings", t("Tray.Settings")),
                TrayMenuItem("exit", t("Tray.Exit")),
            ]
        )

    def _refresh_tray_tooltip(self) -> None:
        if self.tray is None:
            return
        raised = bool(self.widget_manager and self.widget_manager.raise_toggle.raised)
        self.tray.set_tooltip(t("Tray.TooltipRaised") if raised else t("Tray.Tooltip"))

    def _tray_new_widget(self) -> None:
        if self.widget_manager is None:
            return
        try:
            self.widget_manager.create_file_widget()
        except Exception as exc:
            self._log(f"[Tray] new widget failed: {exc}")

    def _tray_map_folder(self) -> None:
        if self.widget_manager is None:
            return
        dialog = Gtk.FileChooserNative.new(
            t("Common.NewFolderMapping"), None, Gtk.FileChooserAction.SELECT_FOLDER, None, None
        )
        dialog.set_modal(True)

        def respond(_dlg, response: int):
            if response == Gtk.ResponseType.ACCEPT:
                folder = dialog.get_file()
                path = folder.get_path() if folder is not None else None
                if path:
                    try:
                        self.widget_manager.create_file_widget(mapped_folder=path)
                    except FileWidgetPathConflict as conflict:
                        self._show_mapping_conflict(conflict)
                    except Exception as exc:
                        self._log(f"[Tray] map folder failed: {exc}")
            dialog.destroy()

        dialog.connect("response", respond)
        dialog.show()

    def _show_mapping_conflict(self, conflict: FileWidgetPathConflict) -> None:
        if conflict.kind == "ExistingWidget" and conflict.widget is not None:
            message = i18n.fmt("Widget.MapFolder.ConflictWidgetHint", conflict.widget.name)
        else:
            message = t("Widget.MapFolder.ConflictRootHint")
        self._notify(t("Common.NewFolderMapping"), message)

    def _tray_add_feature(self) -> None:
        if self.widget_manager is None:
            return
        from .views.feature_picker import FeaturePickerWindow

        FeaturePickerWindow(self, on_pick=self._create_feature_widget).present()

    def _create_feature_widget(self, kind: str) -> None:
        if self.widget_manager is None:
            return
        try:
            self.widget_manager.create_feature_widget(kind)
        except Exception as exc:
            self._log(f"[Tray] add feature widget failed: {exc}")

    def _tray_open_storage(self) -> None:
        from .services import file_ops

        root = self.widget_manager.storage_root() if self.widget_manager else ""
        if root:
            os.makedirs(root, exist_ok=True)
            file_ops.reveal_in_file_manager([root])

    def _notify(self, title: str, message: str) -> None:
        try:
            gi.require_version("Notify", "0.7")
            from gi.repository import Notify

            if not Notify.is_initted():
                Notify.init("PaneBox")
            Notify.Notification.new(title, message, "panebox").show()
        except Exception:
            self._log(f"[Notify] {title}: {message}")

    # ---- actions ------------------------------------------------------------------------

    def _register_actions(self) -> None:
        def action(name: str, callback, enabled: bool = True):
            act = Gio.SimpleAction.new(name, None)
            act.set_enabled(enabled)
            act.connect("activate", lambda *_a: callback())
            self.add_action(act)

        action("toggle-widgets", lambda: self._toggle_widgets())
        action("new-widget", lambda: self.widget_manager and self.widget_manager.create_file_widget())
        action("settings", lambda: self.open_settings())
        action("quit", self.quit)

    def _toggle_widgets(self) -> None:
        if self.widget_manager is None:
            return
        raised = self.widget_manager.toggle_widgets()
        self._log(f"[Toggle] widgets raised={raised}")

    # ---- search popup ------------------------------------------------------------

    def open_search_popup(self, initial_query: Optional[str] = None) -> None:
        """Singleton search overlay (C# SearchPopupService.Show)."""
        if self.widget_manager is None:
            return
        if self.search_popup is None:
            from .views.search_popup import SearchPopupWindow

            manager = self.widget_manager
            self.search_popup = SearchPopupWindow(
                self.settings_service,
                manager.search_engine,
                manager.search_history,
                hooks=self._search_hooks(),
                application=self,
                todo_store_factory=lambda widget_id: TodoWidgetStore(widget_id),
                quick_capture_service=manager.quick_capture_service,
            )
        self.search_popup.open_search(initial_query)

    def _search_hooks(self) -> dict:
        return {
            "new-todo": lambda: self._focus_or_create_feature_widget("Todo"),
            "new-note": lambda: self._focus_or_create_feature_widget("QuickCapture"),
            "open-settings": lambda: self.open_settings(),
            "toggle-widgets": lambda: self.activate_action("toggle-widgets"),
            "toggle-theme": self._toggle_theme,
            "reveal_content": self._reveal_search_content,
        }

    def _focus_or_create_feature_widget(self, kind: str) -> None:
        """Search action target: present the existing widget, or create one."""
        if self.widget_manager is None:
            return
        for runtime in self.widget_manager.runtimes.values():
            if runtime.config.widgetKind == kind and not runtime.config.isDisabled:
                runtime.window.present()
                return
        try:
            self.widget_manager.create_feature_widget(kind)
        except Exception as exc:
            self._log(f"[Search] create {kind} widget failed: {exc}")

    def _toggle_theme(self) -> None:
        core = self.settings_service.settings.core
        core.theme = "Dark" if core.theme != "Dark" else "Light"
        self.settings_service.save_debounced()
        self._apply_theme(core.theme)
        if self.widget_manager is not None:
            self.widget_manager.apply_appearance()

    def _reveal_search_content(self, item) -> None:
        """Todo/Note result activation → present the owning widget window."""
        if self.widget_manager is None:
            return
        target = self.widget_manager.runtimes.get(item.todo_widget_id) if item.todo_widget_id else None
        if target is None and item.quick_capture_item_id:
            target = next(
                (rt for rt in self.widget_manager.runtimes.values() if rt.config.widgetKind == "QuickCapture"),
                None,
            )
        if target is not None:
            target.window.present()

    # ---- settings window -----------------------------------------------------------

    def open_organize_window(self) -> None:
        from .views.organize_window import OrganizeWindow

        if self.organize_service is None:
            self.organize_service = OrganizeService(self.settings_service)
        if self.organize_window is None:
            self.organize_window = OrganizeWindow(
                self,
                self.organize_service,
                on_committed=self._on_organize_committed,
            )
        self.organize_window.present()

    def _on_organize_committed(self) -> None:
        # Materialize widgets created by the transaction on the live desktop.
        if self.widget_manager is not None:
            self.widget_manager.restore_widgets()
            self._rebuild_tray_menu()

    def undo_latest_organization(self) -> str:
        from .i18n import t

        if self.organize_service is None:
            self.organize_service = OrganizeService(self.settings_service)
        self.organize_service.undo_latest()
        if self.widget_manager is not None:
            self.widget_manager.restore_widgets()
        return t("DesktopOrganization.Undo.Success")

    def open_settings(self, section: Optional[str] = None) -> None:
        from .views.settings_window import SettingsWindow

        if self.settings_window is None:
            self.settings_window = SettingsWindow(self, self.settings_service, self._settings_hooks())
        self.settings_window.present()
        if section:
            self.settings_window.show_section(section)

    def _settings_hooks(self):
        from .views.settings_window import SettingsHooks

        return SettingsHooks(
            save=self.settings_service.save_debounced,
            apply_theme=self._apply_theme,
            apply_language=self._apply_language,
            apply_hotkey=self._reapply_hotkey,
            apply_autostart=self.set_autostart,
            apply_appearance=lambda: self.widget_manager and self.widget_manager.apply_appearance(),
            log=self._log,
            set_feature_enabled=self._set_feature_enabled,
            refresh_feature=lambda kind: self.widget_manager and self.widget_manager.refresh_feature(kind),
            refresh_groups=lambda: self.widget_manager and self.widget_manager._refresh_group_strips(),
            dissolve_all_groups=lambda: bool(self.widget_manager and self.widget_manager.dissolve_all_groups()),
            open_organize=lambda: self.open_organize_window(),
            undo_latest_organization=lambda: self.undo_latest_organization(),
            cloud_backup=self.cloud_backup_service,
            export_diagnostics=self._export_diagnostics,
            check_updates=self._check_updates,
        )

    def _export_diagnostics(self, destination: str):
        from .services.diagnostics import DiagnosticsBundleService, collect_snapshot

        snapshot = collect_snapshot(
            self.settings_service,
            widget_manager=self.widget_manager,
            hotkey=self.hotkey,
            search_hotkey=self.search_hotkey,
            log=self._log,
        )
        return DiagnosticsBundleService(log=self._log).export(destination, snapshot)

    def _set_feature_enabled(self, kind: str, enabled: bool) -> None:
        if self.widget_manager is not None:
            self.widget_manager.set_feature_enabled(kind, enabled)

    def _apply_theme(self, theme: str) -> None:
        try:
            gtk_settings = Gtk.Settings.get_default()
            if gtk_settings is not None:
                # System follows the XSETTINGS/platform theme; Light/Dark force it.
                gtk_settings.set_property("gtk-application-prefer-dark-theme", theme == "Dark")
        except Exception as exc:
            self._log(f"[Theme] apply failed: {exc}")

    def _apply_language(self) -> None:
        """Language switch: tray menu + widget surfaces retranslate."""
        self._rebuild_tray_menu()
        self._refresh_tray_tooltip()
        if self.widget_manager is not None:
            for runtime in self.widget_manager.runtimes.values():
                surface = runtime.shell.content.get_first_child()
                if surface is not None and hasattr(surface, "queue_rebuild"):
                    surface.queue_rebuild()

    def _reapply_hotkey(self):
        if self.hotkey is None:

            class _Disabled:
                state = "disabled"
                detail = ""

            return _Disabled()
        core = self.settings_service.settings.core
        status = self.hotkey.apply(
            enabled=core.globalHotkeyEnabled,
            activation_kind=core.globalHotkeyActivationKind,
            modifiers=core.globalHotkeyModifiers,
            keysym=core.globalHotkeyKey,
            on_triggered=lambda: self.activate_action("toggle-widgets"),
        )
        if self.search_hotkey is not None:
            search = self.settings_service.settings.search
            search_status = self.search_hotkey.apply(
                enabled=search.searchHotkeyEnabled,
                activation_kind="KeyPress",
                modifiers=modifiers_from_x11(search.searchHotkeyModifiers),
                keysym=search.searchHotkeyKey,
                on_triggered=lambda: self.open_search_popup(),
            )
            status.detail = f"{status.detail} search:{search_status.state}".rstrip()
        return status

    # ---- misc -----------------------------------------------------------------------------

    def set_autostart(self, enabled: bool) -> None:
        write_autostart(enabled)
        self.settings_service.settings.core.autoStart = enabled
        self.settings_service.save_debounced()

    def _resolved_language(self) -> Optional[str]:
        language = self.settings_service.settings.core.language
        if language and language != "System":
            return language
        return None  # set_language(None) resolves from the environment

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(str(CSS_PATH))
        except Exception as exc:
            self._log(f"[Theme] css load failed: {exc}")
            return
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _log(self, message: str) -> None:
        logging.getLogger("panebox.app").info(message)
