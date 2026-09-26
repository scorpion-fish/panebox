"""Widget manager — owns the live widget runtimes and their persistence.

Port of Services/WidgetManager.cs core surface for M1: restore-on-startup with
topology layout profiles, create managed/mapped file widgets (dedupe " (n)",
path-conflict guard), close with tombstones, rename, geometry capture into
both the widget config and the active topology profile, toggle-all raise via
RaiseToggleController, and per-window snap-target providers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..constants import DATA_ROOT, WidgetKind
from ..i18n import t
from ..models.layout_slice import WidgetSurfaceLayoutProfile, now_iso
from ..models.widget_config import (
    CURRENT_BOUNDS_COORDINATE_VERSION,
    WidgetConfig,
)
from ..platform.move_resize import MoveResizeController
from ..platform.raise_toggle import RaiseToggleController
from ..platform.widget_window import WidgetWindow
from ..services import feature_widgets, file_ops
from ..services import capsule as capsule_policy
from ..services.glance_image_service import GlanceImageService
from ..services.quick_capture_service import QuickCaptureService
from ..services.search_engine import SearchEngineService
from ..services.search_history import SearchHistoryService
from ..services.snap import Rect, SnapTarget
from ..views.feature_placeholder import FeaturePlaceholder
from ..views.file_widget import FileSurface
from ..views.glance_widget import GlanceSurface
from ..views.music_widget import MusicSurface
from ..views.quick_capture_widget import QuickCaptureSurface
from ..views.search_widget import SearchSurface
from ..views.shell import WidgetShell
from ..views.todo_widget import TodoSurface
from ..views.capsule import CapsuleSurface
from ..views.weather_widget import WeatherSurface
from .file_widget_controller import FileWidgetController
from .settings_service import LoadState, SettingsService
from .widget_manager_groups import WidgetManagerGroupsMixin

# InitialFileWidgetPlacementPolicy.cs
# First-run placement reserve: a menu-bar-height gap from the work-area
# edges so the widget never kisses the panel or the screen border.
INITIAL_RIGHT_MARGIN_DIPS = 36.0
INITIAL_TOP_MARGIN_DIPS = 36.0


class FileWidgetPathConflict(Exception):
    """Raise when a folder is already mapped by another widget / storage root."""

    def __init__(self, kind: str, widget: Optional[WidgetConfig] = None):
        super().__init__(kind)
        self.kind = kind  # "ExistingWidget" | "StorageRoot"
        self.widget = widget


@dataclass
class WidgetRuntime:
    config: WidgetConfig
    window: WidgetWindow
    shell: WidgetShell
    controller: Optional[FileWidgetController]
    surface: object
    mover: MoveResizeController
    # capsule (collapsed) state
    capsule: Optional[object] = None
    capsule_mover: Optional[MoveResizeController] = None
    pinned: bool = False
    expand_source: int = 0
    collapse_source: int = 0


class WidgetManager(WidgetManagerGroupsMixin):
    def __init__(self, settings: SettingsService, application=None):
        self.settings = settings
        self.application = application  # Gtk.Application, for add_window
        self.runtimes: dict[str, WidgetRuntime] = {}
        self.quick_capture_service = QuickCaptureService()  # shared across QC widgets
        self.glance_image_service = GlanceImageService(str(DATA_ROOT / "data" / "cache" / "glance"))  # shared cache
        self.search_history = SearchHistoryService()  # shared: popup + search widget
        self.search_engine = SearchEngineService(
            settings_service=settings,
            quick_capture_service=self.quick_capture_service,
        )
        # Content edits invalidate the search snapshot (1s min refresh).
        self.quick_capture_service.add_changed_listener(self.search_engine.content_changed)
        self.raise_toggle = RaiseToggleController(
            windows_provider=self._live_windows,
            on_state_changed=self._on_raise_state_changed,
        )
        self.on_tray_state_text: Optional[Callable[[], None]] = None
        self.on_widgets_changed: Optional[Callable[[], None]] = None

    def _notify_widgets_changed(self) -> None:
        if self.on_widgets_changed is not None:
            try:
                self.on_widgets_changed()
            except Exception:
                pass

    # ---- helpers ------------------------------------------------------------

    def _request_search_popup(self, query=None) -> None:
        """Search surface activation → application popup (if attached)."""
        open_popup = getattr(self.application, "open_search_popup", None)
        if callable(open_popup):
            open_popup(initial_query=query)

    def storage_root(self) -> str:
        root = self.settings.settings.fileWidget.defaultManagedStorageRootPath
        return os.path.expanduser(root) if root else os.path.expanduser("~/PaneBox")

    def _live_windows(self) -> List[WidgetWindow]:
        # Toggle-all operates on desktop surfaces: the active member of each
        # group plus every ungrouped widget. Hidden group members stay hidden.
        return [rt.window for rt in self.runtimes.values() if self.is_surface_window(rt.config.id)]

    def _on_raise_state_changed(self, raised: bool) -> None:
        if self.on_tray_state_text:
            self.on_tray_state_text()

    @property
    def has_visible_widgets(self) -> bool:
        return any(rt.window.get_visible() for rt in self.runtimes.values())

    # ---- restore -----------------------------------------------------------

    def _restorable(self, config: WidgetConfig) -> bool:
        """File widgets restore unless disabled; feature widgets follow the
        featureWidgetEnabledStates gate."""
        if config.isDisabled:
            return False
        if config.widgetKind == WidgetKind.FILE:
            return True
        return feature_widgets.is_feature_widget(config.widgetKind) and feature_widgets.is_enabled(
            self.settings.layout, config.widgetKind
        )

    def restore_widgets(self) -> None:
        from ..platform.workarea import (
            TopologyLayoutService,
            find_monitor_at,
            list_monitors,
            primary_monitor,
        )

        # Repair persisted group state first: it rewrites member geometry to
        # the group's surface before any rect is computed.
        self.normalize_groups_for_restore()
        monitors = list_monitors()
        topology = TopologyLayoutService(self.settings.layout)
        profile = topology.find_compatible(monitors)

        surface_rects: dict[str, tuple[float, float, float, float]] = {}
        for config in self.settings.layout.widgets:
            if not self._restorable(config):
                continue
            width, height = config.width, config.height
            if profile is not None and config.id in profile.surfaces:
                surface = profile.surfaces[config.id]
                surface_rects[config.id] = (surface.x, surface.y, surface.width, surface.height)
            elif config.needsInitialPlacement or (
                config.x == 100.0 and config.y == 100.0 and not config.positionAnchor
            ):
                work = primary_monitor(monitors)
                scale = work.scale
                x = work.work_x + work.work_width - width * scale - INITIAL_RIGHT_MARGIN_DIPS * scale
                y = work.work_y + INITIAL_TOP_MARGIN_DIPS * scale
                surface_rects[config.id] = (x, y, width, height)
            else:
                surface_rects[config.id] = (config.x, config.y, width, height)

        if profile is not None and profile.surfaces:
            from ..platform.workarea import topology_signature, topology_signature_from_profiles

            if topology_signature_from_profiles(profile) == topology_signature(monitors):
                # Same monitor set as when saved: restore verbatim (reprojecting
                # identical topologies drifts the position every boot).
                pass
            else:
                surface_rects = topology.reproject(monitors, surface_rects)

        created = 0
        for config in self.settings.layout.widgets:
            if not self._restorable(config):
                continue
            rect = surface_rects.get(config.id)
            x, y = (rect[0], rect[1]) if rect else (config.x, config.y)
            w, h = (rect[2], rect[3]) if rect else (config.width, config.height)
            monitor = find_monitor_at(monitors, x + w / 2, y + h / 2) or primary_monitor(monitors)
            cx, cy = monitor.clamp_rect(x, y, w, h)
            # Grouped members other than the active one keep their runtime but
            # not a mapped window.
            present = config.isVisible and self.is_surface_window(config.id)
            self.create_runtime(config, cx, cy, w, h, present=present)
            created += 1
        _ = created
        self._notify_widgets_changed()

    # ---- runtime creation ----------------------------------------------------

    def create_runtime(
        self,
        config: WidgetConfig,
        x: float,
        y: float,
        width: float,
        height: float,
        present: bool = True,
    ) -> WidgetRuntime:
        window = WidgetWindow(title=config.name)
        window.set_default_size(int(width), int(height))
        if self.application is not None:
            self.application.add_window(window)

        shell = WidgetShell(
            title=config.name,
            glyph=feature_widgets.descriptor_for(config.widgetKind)["glyph"]
            if feature_widgets.is_feature_widget(config.widgetKind)
            else "▤",
            on_renamed=lambda name, cfg=config: self.rename_widget(cfg.id, name),
            on_close=lambda cfg=config: self.close_widget(cfg.id),
            on_collapse=lambda cfg=config: self.toggle_collapse(cfg.id),
            on_menu=lambda button, cfg=config: self.open_widget_menu(cfg.id, button),
        )

        controller: Optional[FileWidgetController] = None
        surface: object
        if config.widgetKind == WidgetKind.FILE:
            controller = FileWidgetController(
                config,
                storage_root=self.storage_root(),
                show_file_extensions=self.settings.settings.fileWidget.showFileExtensions,
                settings_service=self.settings,
            )
            controller.on_config_changed = self._make_config_persist(config)
            surface = FileSurface(controller, settings_service=self.settings)
            controller.on_entries_changed = surface.queue_rebuild  # type: ignore[attr-defined]
        elif config.widgetKind == WidgetKind.TODO:
            surface = TodoSurface(config, self.settings, application=self.application)
        elif config.widgetKind == WidgetKind.QUICK_CAPTURE:
            surface = QuickCaptureSurface(
                config, self.settings, self.quick_capture_service, application=self.application
            )
        elif config.widgetKind == WidgetKind.GLANCE:
            surface = GlanceSurface(config, self.settings, self.glance_image_service, application=self.application)
        elif config.widgetKind == WidgetKind.WEATHER:
            surface = WeatherSurface(config, self.settings, application=self.application)
        elif config.widgetKind == WidgetKind.MUSIC:
            surface = MusicSurface(config, self.settings, application=self.application)
        elif config.widgetKind == WidgetKind.SEARCH:
            surface = SearchSurface(
                config,
                self.settings,
                self.search_history,
                search_requested=self._request_search_popup,
                application=self.application,
            )
        else:
            surface = FeaturePlaceholder(config, self.settings)

        shell.set_content(surface)
        window.set_child(shell)

        runtime = WidgetRuntime(
            config=config,
            window=window,
            shell=shell,
            controller=controller,
            surface=surface,
            mover=None,
        )  # type: ignore[arg-type]
        mover = MoveResizeController(
            window,
            snap_enabled=lambda: self.settings.settings.widgetShell.resizeSnapEnabled,
            snap_spacing=lambda: self.settings.settings.widgetShell.widgetSnapSpacing,
            work_area=lambda rt=runtime: self._work_area_for(rt),
            snap_targets=lambda rt=runtime: self._snap_targets_for(rt),
            on_geometry_committed=lambda *xy, rt=runtime: self._on_geometry_committed(rt, *xy),
            move_locked=lambda cfg=config: cfg.isPositionLocked,
            size_locked=lambda cfg=config: cfg.isSizeLocked,
        )
        mover.attach(shell.title_bar, surface)
        runtime.mover = mover
        key = Gtk.EventControllerKey()
        key.connect(
            "key-pressed",
            lambda _k, keyval, _kc, state, cfg=config: self._group_key_pressed(cfg, keyval, state),
        )
        window.add_controller(key)

        window.set_window_geometry(int(x), int(y), int(width), int(height))
        if present:
            window.present()
        self.runtimes[config.id] = runtime
        # Collapse availability follows the resolved behavior; a collapsed
        # config restores collapsed unless the behavior now forbids it.
        behavior = self.collapse_behavior_for(config)
        shell.set_collapse_available(behavior != capsule_policy.COLLAPSE_EXPANDED)
        self._refresh_group_strips()
        if config.isCollapsed:
            if behavior == capsule_policy.COLLAPSE_EXPANDED:
                config.isCollapsed = False  # CollapseBehaviorDisabled transition
                self.settings.update_widget(config)
            else:
                self._apply_collapse_state(runtime)
        return runtime

    def _make_config_persist(self, config: WidgetConfig) -> Callable[[WidgetConfig], None]:
        def persist(_changed: WidgetConfig) -> None:
            self.settings.update_widget(config)
            self.settings.save_debounced()

        return persist

    # ---- create / close ---------------------------------------------------------

    def create_managed_folder_name(self, name: str) -> str:
        """Dedupe against existing folders and widget folder names (" (n)")."""
        root = self.storage_root()
        existing = {rt.controller.folder_path for rt in self.runtimes.values() if rt.controller is not None}
        candidate = name
        index = 2
        while os.path.exists(os.path.join(root, candidate)) or os.path.join(root, candidate) in existing:
            candidate = f"{name} ({index})"
            index += 1
        return candidate

    def create_file_widget(
        self,
        name: Optional[str] = None,
        mapped_folder: Optional[str] = None,
        place_for_first_run: bool = False,
    ) -> WidgetConfig:
        if mapped_folder:
            folder = os.path.normpath(os.path.expanduser(mapped_folder))
            if os.path.realpath(folder) == os.path.realpath(self.storage_root()):
                raise FileWidgetPathConflict("StorageRoot")
            for rt in self.runtimes.values():
                # Feature widgets (Todo / QuickCapture / …) have no controller
                # folder; the scan must skip them — dereferencing it crashed the
                # whole mapping with "NoneType has no attribute folder_path" and
                # the chooser silently did nothing.
                if rt.controller is None:
                    continue
                if os.path.realpath(rt.controller.folder_path) == os.path.realpath(folder):
                    raise FileWidgetPathConflict("ExistingWidget", rt.config)
            config = WidgetConfig(
                name=os.path.basename(folder.rstrip("/")) or folder,
                isDefaultTitle=False,
                widgetKind=WidgetKind.FILE,
                mappedFolderPath=folder,
                boundsCoordinateVersion=CURRENT_BOUNDS_COORDINATE_VERSION,
                width=self.settings.settings.widgetShell.defaultWidgetWidth,
                height=self.settings.settings.widgetShell.defaultWidgetHeight,
            )
            x, y = self._next_cascade_position()
        else:
            name = name or t("Widget.DefaultName")
            folder_name = self.create_managed_folder_name(name)
            folder = os.path.join(self.storage_root(), folder_name)
            os.makedirs(folder, exist_ok=True)
            config = WidgetConfig(
                name=name,
                isDefaultTitle=False,
                widgetKind=WidgetKind.FILE,
                mappedFolderPath=folder,
                followsDefaultStoragePath=True,
                managedFolderName=folder_name,
                boundsCoordinateVersion=CURRENT_BOUNDS_COORDINATE_VERSION,
                width=self.settings.settings.widgetShell.defaultWidgetWidth,
                height=self.settings.settings.widgetShell.defaultWidgetHeight,
            )
            if place_for_first_run:
                x, y = self._initial_placement(config)
            else:
                x, y = self._next_cascade_position()

        config.x, config.y = x, y
        self.settings.add_widget(config)
        self.create_runtime(config, x, y, config.width, config.height)
        self.capture_geometries()
        return config

    def create_feature_widget(self, kind: str) -> WidgetConfig:
        """FeatureWidgetEntryFactory: mint a widget from the descriptor table."""
        descriptor = feature_widgets.descriptor_for(kind)
        if descriptor is None:
            raise ValueError(f"unknown feature widget kind: {kind}")
        config = WidgetConfig(
            name=t(descriptor["title_key"]),
            isDefaultTitle=True,
            widgetKind=kind,
            boundsCoordinateVersion=CURRENT_BOUNDS_COORDINATE_VERSION,
            width=float(descriptor["width"]),
            height=float(descriptor["height"]),
        )
        config.x, config.y = self._next_cascade_position()
        feature_widgets.set_enabled(self.settings.layout, kind, True)
        self.settings.add_widget(config)
        self.create_runtime(config, config.x, config.y, config.width, config.height)
        self.capture_geometries()
        self._notify_widgets_changed()
        return config

    def _initial_placement(self, config: WidgetConfig) -> tuple[float, float]:
        """Right-aligned placement (InitialFileWidgetPlacementPolicy)."""
        from ..platform.workarea import list_monitors, primary_monitor

        monitor = primary_monitor(list_monitors())
        scale = monitor.scale or 1.0
        w = max(50.0, config.width) * scale
        x = monitor.work_x + monitor.work_width - w - INITIAL_RIGHT_MARGIN_DIPS * scale
        y = monitor.work_y + INITIAL_TOP_MARGIN_DIPS * scale
        return max(monitor.work_x, x), max(monitor.work_y, y)

    def _next_cascade_position(self) -> tuple[float, float]:
        from ..platform.workarea import list_monitors, primary_monitor

        monitor = primary_monitor(list_monitors())
        count = len(self.runtimes)
        return (
            monitor.work_x + 80 + 32 * (count % 8),
            monitor.work_y + 80 + 32 * (count % 8),
        )

    def set_feature_enabled(self, kind: str, enabled: bool) -> None:
        """FeatureWidgets settings toggle (C# FeatureWidgetSettingsSection).

        Enable: restore a gated-out config of that kind, or mint a fresh one.
        Disable: close every runtime — close_widget clears the gate once the
        last config is gone; forced here too so a no-runtime flip still sticks.
        """
        if not feature_widgets.is_feature_widget(kind):
            return
        if enabled:
            feature_widgets.set_enabled(self.settings.layout, kind, True)
            configs = [w for w in self.settings.layout.widgets if w.widgetKind == kind and not w.isDisabled]
            if configs:
                for config in configs:
                    if config.id not in self.runtimes:
                        self.create_runtime(config, config.x, config.y, config.width, config.height)
            else:
                self.create_feature_widget(kind)  # mints + saves + notifies
                return
            self.settings.save_debounced()
            self._notify_widgets_changed()
        else:
            for runtime in [r for r in self.runtimes.values() if r.config.widgetKind == kind]:
                self.close_widget(runtime.config.id)
            feature_widgets.set_enabled(self.settings.layout, kind, False)
            self.settings.save_debounced()

    # Surface refresh entry points per kind (live settings application).
    _FEATURE_REFRESH: tuple[tuple[str, ...], ...] = (
        ("refresh", "queue_rebuild"),  # QuickCapture
        ("refresh", "queue_rebuild"),  # Todo
        ("refresh",),  # Music
        ("refresh",),  # Weather
        ("refresh",),  # Search
        ("apply_settings",),  # Glance
    )

    def refresh_feature(self, kind: str) -> None:
        """Push changed settings into every live surface of that kind."""
        if kind not in feature_widgets.FEATURE_KINDS:
            return
        methods = self._FEATURE_REFRESH[feature_widgets.FEATURE_KINDS.index(kind)]
        for runtime in [r for r in self.runtimes.values() if r.config.widgetKind == kind]:
            for name in methods:
                method = getattr(runtime.surface, name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:
                        pass

    def close_widget(self, widget_id: str) -> None:
        self._teardown_group_membership(widget_id)
        runtime = self.runtimes.pop(widget_id, None)
        if runtime is None:
            return
        if runtime.controller is not None:
            runtime.controller.dispose()
        if hasattr(runtime.surface, "dispose"):
            try:
                runtime.surface.dispose()
            except Exception:
                pass
        runtime.window.destroy()
        config = next((w for w in self.settings.layout.widgets if w.id == widget_id), None)
        if config is not None:
            self.settings.remove_widget(widget_id)
            # Deleting the last widget of a feature kind flips its enabled state.
            if config is not None and feature_widgets.is_feature_widget(config.widgetKind):
                still_present = any(w.widgetKind == config.widgetKind for w in self.settings.layout.widgets)
                if not still_present:
                    feature_widgets.set_enabled(self.settings.layout, config.widgetKind, False)
        profile = self._active_profile()
        if profile is not None:
            profile.surfaces.pop(widget_id, None)
        self.settings.save_debounced()
        self._notify_widgets_changed()

    def rename_widget(self, widget_id: str, name: str) -> bool:
        runtime = self.runtimes.get(widget_id)
        if runtime is None or not name.strip():
            return False
        runtime.config.name = name.strip()
        runtime.config.isDefaultTitle = False
        runtime.shell.set_title(runtime.config.name)
        runtime.window.set_title(runtime.config.name)
        self.settings.update_widget(runtime.config)
        self.settings.save_debounced()
        return True

    # ---- geometry ----------------------------------------------------------------

    def _work_area_for(self, runtime: WidgetRuntime) -> Optional[Rect]:
        from ..platform.workarea import find_monitor_at, list_monitors, primary_monitor

        x, y, w, h = runtime.window.window_geometry()
        monitor = find_monitor_at(list_monitors(), x + w / 2, y + h / 2) or primary_monitor(list_monitors())
        return Rect(monitor.work_x, monitor.work_y, monitor.work_width, monitor.work_height)

    def _snap_targets_for(self, runtime: WidgetRuntime) -> List[SnapTarget]:
        targets: List[SnapTarget] = []
        for other in self.runtimes.values():
            if other is runtime or not other.window.xid:
                continue
            if not self.is_surface_window(other.config.id):
                continue  # hidden group members mirror the active geometry
            x, y, w, h = other.window.window_geometry()
            targets.append(SnapTarget(bounds=Rect(x, y, w, h), window_handle=other.window.xid))
        return targets

    def _on_geometry_committed(self, runtime: WidgetRuntime, x: int, y: int, w: int, h: int) -> None:
        if runtime.config.isCollapsed:
            # Capsule drags persist the compact placement; the config keeps the
            # expanded geometry so expanding returns to the remembered spot.
            work, scale = self._monitor_rect_and_scale(runtime)
            capsule_policy.capture_compact_placement(runtime.config, capsule_policy.Rect(x, y, w, h), work, scale)
            self.settings.update_widget(runtime.config)
            self.settings.save_debounced()
            return
        runtime.config.x, runtime.config.y = float(x), float(y)
        runtime.config.width, runtime.config.height = float(w), float(h)
        self._sync_group_geometry_from(runtime)
        profile = self._active_profile()
        if profile is not None:
            surface = profile.surfaces.get(runtime.config.id)
            if surface is not None:
                surface.x, surface.y = float(x), float(y)
                surface.width, surface.height = float(w), float(h)
        self.settings.update_widget(runtime.config)
        self.settings.save_debounced()

    # ---- capsule (collapse) -------------------------------------------------------

    def collapse_behavior_for(self, config: WidgetConfig) -> str:
        ws = self.settings.settings.widgetShell
        return capsule_policy.resolve_collapse_behavior(config, ws.widgetCollapseBehavior)

    def effective_content_mode(self, config: WidgetConfig) -> str:
        ws = self.settings.settings.widgetShell
        return capsule_policy.resolve_privacy_content_mode(
            ws.widgetCompactContentMode,
            ws.widgetCompactHideSensitiveContent,
            config.normalized_kind(),
        )

    def _monitor_rect_and_scale(self, runtime: WidgetRuntime) -> tuple[capsule_policy.Rect, float]:
        from ..platform.workarea import find_monitor_at, list_monitors, primary_monitor

        x, y, w, h = runtime.window.window_geometry()
        monitor = find_monitor_at(list_monitors(), x + w / 2, y + h / 2) or primary_monitor(list_monitors())
        return (
            capsule_policy.Rect(monitor.work_x, monitor.work_y, monitor.work_width, monitor.work_height),
            monitor.scale or 1.0,
        )

    def toggle_collapse(self, widget_id: str, collapsed: Optional[bool] = None) -> None:
        runtime = self.runtimes.get(widget_id)
        if runtime is None:
            return
        config = runtime.config
        target = (not config.isCollapsed) if collapsed is None else collapsed
        behavior = self.collapse_behavior_for(config)
        if target and behavior == capsule_policy.COLLAPSE_EXPANDED:
            return  # "Always expanded" refuses to collapse
        if target == config.isCollapsed:
            return
        was_collapsed = config.isCollapsed
        config.isCollapsed = target
        if not target:
            runtime.pinned = False
        self._apply_collapse_state(runtime, was_collapsed=was_collapsed)
        self.settings.update_widget(config)
        self.settings.save_debounced()
        self._rearrange_capsule_bars()

    def _apply_collapse_state(self, runtime: WidgetRuntime, was_collapsed: Optional[bool] = None) -> None:
        config = runtime.config
        window = runtime.window
        work, scale = self._monitor_rect_and_scale(runtime)
        mode = self.effective_content_mode(config)
        if was_collapsed is None:
            was_collapsed = getattr(runtime, "_presented_collapsed", False)

        if config.isCollapsed:
            x, y, _w, _h = window.window_geometry()
            bounds = capsule_policy.resolve_compact_bounds(
                config,
                capsule_policy.Rect(x, y, 0, 0),
                scale,
                mode,
                align_to_expanded_width=False,
            )
            capsule_view = self._capsule_for(runtime)
            capsule_view.set_content_mode(mode)
            capsule_view.refresh()
            self._cancel_collapse_timers(runtime)
            window.set_child(capsule_view)
            window.set_window_geometry(bounds.x, bounds.y, bounds.width, bounds.height)
            runtime._presented_collapsed = True  # type: ignore[attr-defined]
        else:
            cx, cy, cw, ch = window.window_geometry()
            compact = capsule_policy.Rect(cx, cy, cw, ch)
            reason = capsule_policy.resolve_transition_reason(
                self.collapse_behavior_for(config), capsule_policy.COLLAPSE_EXPANDED
            )
            if capsule_policy.should_capture_compact_placement(
                reason,
                was_target_collapsed=True,
                compact_bounds_active=was_collapsed,
                transition_active=False,
            ):
                capsule_policy.capture_compact_placement(config, compact, work, scale)
            width_mode = self.settings.settings.widgetShell.widgetCompactWidthMode
            expanded_size = capsule_policy.resolve_expanded_size_for_width_mode(
                compact, capsule_policy.Size(int(config.width), int(config.height)), width_mode
            )
            anchors = [capsule_policy.from_position_anchor(config.positionAnchor)]
            anchors = [a for a in anchors if a] or list(capsule_policy.DEFAULT_ANCHOR_ORDER)
            direction = capsule_policy.resolve_effective_expansion_direction(
                config, self.settings.settings.widgetShell.widgetCompactExpansionDirection
            )
            layout = capsule_policy.resolve_adaptive_expansion(compact, expanded_size, work, anchors, direction)
            self._cancel_collapse_timers(runtime)
            window.set_child(runtime.shell)
            window.set_window_geometry(
                layout.expanded_bounds.x,
                layout.expanded_bounds.y,
                layout.expanded_bounds.width,
                layout.expanded_bounds.height,
            )
            config.x = layout.expanded_bounds.x / scale
            config.y = layout.expanded_bounds.y / scale
            config.width = layout.expanded_bounds.width / scale
            config.height = layout.expanded_bounds.height / scale
            runtime._presented_collapsed = False  # type: ignore[attr-defined]
            self._attach_expanded_pointer_tracking(runtime)

    def _capsule_for(self, runtime: WidgetRuntime) -> CapsuleSurface:
        if runtime.capsule is None:
            config = runtime.config

            def summary(_cfg=config) -> str:
                return self._capsule_summary(_cfg)

            view = CapsuleSurface(
                config,
                summary_provider=summary,
                on_activate=lambda rt=runtime: self._capsule_activated(rt),
                on_pointer_entered=lambda rt=runtime: self._capsule_hover(rt, True),
                on_pointer_left=lambda rt=runtime: self._capsule_hover(rt, False),
            )
            runtime.capsule = view
            runtime.capsule_mover = MoveResizeController(
                runtime.window,
                snap_enabled=lambda: False,  # capsules don't snap to widget edges
                snap_spacing=lambda: 0.0,
                work_area=lambda rt=runtime: self._work_area_for(rt),
                snap_targets=lambda _rt: [],
                on_geometry_committed=lambda *xy, rt=runtime: self._on_geometry_committed(rt, *xy),
                move_locked=lambda cfg=config: cfg.isPositionLocked,
                size_locked=lambda _cfg: True,  # capsule size follows content mode
            )
            runtime.capsule_mover.attach(view, view)  # whole pill drags; no content area
        return runtime.capsule  # type: ignore[return-value]

    def _capsule_activated(self, runtime: WidgetRuntime) -> None:
        behavior = self.collapse_behavior_for(runtime.config)
        if behavior == capsule_policy.COLLAPSE_SMART:
            runtime.pinned = True  # click pins an expanded Smart widget open
        self.toggle_collapse(runtime.config.id, collapsed=False)

    def _capsule_hover(self, runtime: WidgetRuntime, entered: bool) -> None:
        if self.collapse_behavior_for(runtime.config) != capsule_policy.COLLAPSE_SMART:
            return
        if runtime.expand_source:
            GLib.source_remove(runtime.expand_source)
            runtime.expand_source = 0
        if entered:
            delay = self.settings.settings.widgetShell.widgetCompactExpandDelayMs
            runtime.expand_source = GLib.timeout_add(max(0, delay), lambda rt=runtime: self._hover_expand_fire(rt))

    def _hover_expand_fire(self, runtime: WidgetRuntime) -> bool:
        runtime.expand_source = 0
        capsule_view = runtime.capsule
        inside = getattr(capsule_view, "is_pointer_inside", False) if capsule_view else False
        if not inside or not runtime.config.isCollapsed:
            return GLib.SOURCE_REMOVE
        runtime.pinned = False
        self.toggle_collapse(runtime.config.id, collapsed=False)
        return GLib.SOURCE_REMOVE

    def _attach_expanded_pointer_tracking(self, runtime: WidgetRuntime) -> None:
        """Smart widgets auto-collapse when the pointer leaves the window."""
        if getattr(runtime, "_expanded_motion_attached", False):
            return
        runtime._expanded_motion_attached = True  # type: ignore[attr-defined]
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a, rt=runtime: self._clear_collapse_timer(rt))
        motion.connect("leave", lambda *_a, rt=runtime: self._schedule_auto_collapse(rt))
        runtime.window.add_controller(motion)

    def _schedule_auto_collapse(self, runtime: WidgetRuntime) -> None:
        if self.collapse_behavior_for(runtime.config) != capsule_policy.COLLAPSE_SMART:
            return
        if runtime.config.isCollapsed or runtime.pinned:
            return
        self._clear_collapse_timer(runtime)
        delay = self.settings.settings.widgetShell.widgetCompactCollapseDelayMs
        runtime.collapse_source = GLib.timeout_add(max(0, delay), lambda rt=runtime: self._auto_collapse_fire(rt))

    def _auto_collapse_fire(self, runtime: WidgetRuntime) -> bool:
        runtime.collapse_source = 0
        if self.collapse_behavior_for(runtime.config) != capsule_policy.COLLAPSE_SMART:
            return GLib.SOURCE_REMOVE
        if runtime.config.isCollapsed or runtime.pinned:
            return GLib.SOURCE_REMOVE
        self.toggle_collapse(runtime.config.id, collapsed=True)
        return GLib.SOURCE_REMOVE

    def _clear_collapse_timer(self, runtime: WidgetRuntime) -> None:
        if runtime.collapse_source:
            GLib.source_remove(runtime.collapse_source)
            runtime.collapse_source = 0

    def _cancel_collapse_timers(self, runtime: WidgetRuntime) -> None:
        self._clear_collapse_timer(runtime)
        if runtime.expand_source:
            GLib.source_remove(runtime.expand_source)
            runtime.expand_source = 0

    # ---- capsule bars ---------------------------------------------------------------

    _BAR_EDGE_ANCHORS = {
        "Top": ("LeftTop", "Horizontal"),
        "Bottom": ("LeftBottom", "Horizontal"),
        "Left": ("LeftTop", "Vertical"),
        "Right": ("RightTop", "Vertical"),
    }

    def collapsed_ids(self) -> list[str]:
        return [rt.config.id for rt in self.runtimes.values() if rt.config.isCollapsed]

    def _rearrange_capsule_bars(self) -> None:
        """Bar arrangement mode: collapsed widgets share one row/column."""
        ws = self.settings.settings.widgetShell
        if capsule_policy.normalize_arrangement_mode(ws.widgetCapsuleArrangementMode) != "Bar":
            return
        ids = self.collapsed_ids()
        if not ids:
            return
        order = [i for i in (ws.widgetCapsuleBarOrder or []) if i in ids]
        order += [i for i in ids if i not in order]
        items = []
        for widget_id in order:
            runtime = self.runtimes.get(widget_id)
            if runtime is None:
                continue
            x, y, w, h = runtime.window.window_geometry()
            items.append(capsule_policy.CapsuleArrangementItem(widget_id, w, h))
        if not items:
            return

        placement = capsule_policy.normalize_bar_placement(ws.widgetCapsuleBarPlacement)
        first = self.runtimes.get(items[0].id)
        fx, fy, _fw, _fh = first.window.window_geometry() if first is not None else (0, 0, 0, 0)
        work, _scale = (
            self._monitor_rect_and_scale(first) if first is not None else (capsule_policy.Rect(0, 0, 1920, 1040), 1.0)
        )
        if placement == "Floating":
            anchor = capsule_policy.Point(fx, fy)
            position_anchor, direction = "LeftTop", ws.widgetCapsuleBarDirection
        else:
            position_anchor, edge_direction = self._BAR_EDGE_ANCHORS[placement]
            direction = (
                edge_direction
                if capsule_policy.normalize_bar_direction(ws.widgetCapsuleBarDirection) != "Auto"
                else ws.widgetCapsuleBarDirection
            )
            direction = capsule_policy.normalize_bar_direction(direction) or edge_direction
            if direction == "Auto":
                direction = edge_direction
            mid_x = work.x + work.width // 2
            mid_y = work.y + work.height // 2
            anchor = {
                "Top": capsule_policy.Point(mid_x, work.y),
                "Bottom": capsule_policy.Point(mid_x, work.bottom),
                "Left": capsule_policy.Point(work.x, mid_y),
                "Right": capsule_policy.Point(work.right, mid_y),
            }[placement]

        slots = capsule_policy.calculate_capsule_arrangement(
            items, work, anchor, position_anchor, direction, int(ws.widgetCapsuleBarSpacing)
        )
        for widget_id, bounds in slots.items():
            runtime = self.runtimes.get(widget_id)
            if runtime is None:
                continue
            runtime.window.set_window_geometry(bounds.x, bounds.y, bounds.width, bounds.height)
            capsule_policy.capture_compact_placement(runtime.config, bounds, work, _scale or 1.0)
        ws.widgetCapsuleBarOrder = order
        self.settings.save_debounced()

    # ---- capsule summaries -------------------------------------------------------------

    def _capsule_summary(self, config: WidgetConfig) -> str:
        """One-line capsule summary per kind (C# Smart/Summary content)."""
        from ..i18n import fmt

        ws = self.settings.settings.widgetShell
        kind = config.normalized_kind()
        if capsule_policy.normalize_content_mode(ws.widgetCompactContentMode) == "Minimal":
            return ""
        capsule_policy.hides_sensitive_content(ws.widgetCompactHideSensitiveContent, kind)
        try:
            if kind == WidgetKind.TODO:
                from datetime import datetime

                from .todo_store import TodoWidgetStore

                data = TodoWidgetStore(config.id).load()
                now = datetime.now()
                today = overdue = 0
                for item in data.items:
                    due = item.dueDate
                    if item.isCompleted or due is None:
                        continue
                    if due.date() == now.date():
                        today += 1
                    elif due < now:
                        overdue += 1
                return fmt("Widget.Compact.TodoSummary", today, overdue)
            if kind == WidgetKind.QUICK_CAPTURE:
                data = self.quick_capture_service.store.load()
                return fmt("Widget.Compact.QuickCaptureCount", len(data.items))
            if kind == WidgetKind.FILE:
                runtime = self.runtimes.get(config.id)
                count = len(runtime.controller.entries) if runtime and runtime.controller else len(config.items)
                return fmt("Widget.Compact.FileCount", count)
            if kind == WidgetKind.GLANCE:
                from datetime import datetime

                return datetime.now().strftime("%H:%M")
            # Music/Weather/Search capsule summaries stay empty for v1: the
            # live services are async and the pill is rebuilt on every refresh.
            return ""
        except Exception:
            return ""

    def capture_geometries(self) -> None:
        """Snapshot live window geometry into config + topology profile."""
        from ..platform.workarea import TopologyLayoutService, list_monitors

        surfaces: dict[str, WidgetSurfaceLayoutProfile] = {}
        for runtime in self.runtimes.values():
            if not runtime.window.xid:
                continue
            if not runtime.window.get_mapped():
                continue  # restore-then-capture race: pre-map geometry reads 0,0
            if not self.is_surface_window(runtime.config.id):
                continue  # hidden group members carry the group layout already
            x, y, w, h = runtime.window.window_geometry()
            if x == 0 and y == 0 and (runtime.config.x or runtime.config.y):
                # The map-time move_resize goes out on idle and the WM honors
                # it asynchronously; a capture in that window would read the
                # pre-move (0,0) and wipe the persisted target position.
                continue
            runtime.config.x, runtime.config.y = float(x), float(y)
            runtime.config.width, runtime.config.height = float(w), float(h)
            surfaces[runtime.config.id] = WidgetSurfaceLayoutProfile(
                x=float(x),
                y=float(y),
                width=float(w),
                height=float(h),
                boundsCoordinateVersion=CURRENT_BOUNDS_COORDINATE_VERSION,
            )
        if not surfaces:
            return
        monitors = list_monitors()
        topology = TopologyLayoutService(self.settings.layout)
        key, profile = topology.seed_profile(monitors, surfaces)
        profile.lastUsedAtUtc = now_iso()
        self.settings.stamp_topology_use(key)
        self.settings.save_debounced()

    def _active_profile(self):
        key = self.settings.layout.activeWidgetTopologyKey
        if key:
            return self.settings.layout.widgetTopologyLayouts.get(key)
        return None

    # ---- toggle-all --------------------------------------------------------------

    def toggle_widgets(self) -> bool:
        return self.raise_toggle.toggle()

    # ---- appearance ------------------------------------------------------------

    def apply_appearance(self) -> None:
        """Push shell appearance settings to every live widget (Settings changes)."""
        shell = self.settings.settings.widgetShell
        fw = self.settings.settings.fileWidget
        density = (
            shell.layoutDensity
            if shell.layoutDensity in ("compact", "relaxed", "standard")
            else shell.layoutDensity.lower()
        )
        for runtime in self.runtimes.values():
            try:
                runtime.window.set_opacity(min(1.0, max(0.10, shell.widgetOpacity)))
            except Exception:
                pass
            for css in ("density-compact", "density-relaxed"):
                if density == css.split("-")[1]:
                    runtime.shell.add_css_class(css)
                else:
                    runtime.shell.remove_css_class(css)
            if runtime.controller is not None:
                runtime.controller.show_file_extensions = fw.showFileExtensions
                runtime.controller.invalidate_stack_projection()
            surface = runtime.surface
            if hasattr(surface, "icon_size"):
                surface.icon_size = int(shell.iconSize)
            if hasattr(surface, "queue_rebuild"):
                surface.queue_rebuild()

    # ---- first-run ----------------------------------------------------------------

    def ensure_initial_file_widget(self, interactive: bool) -> Optional[WidgetConfig]:
        """InitialFileWidgetSetupPolicy.Evaluate — creates 我的桌面 on first run.

        The setup re-arms when the layout ends up with no widgets at all (the
        user closed everything): a cold start must not boot into an empty
        desktop with only the tray fallback left. Feature widgets alone still
        count as "resolved" — no file widget is forced back onto them.
        """
        core = self.settings.settings.core
        if core.hasResolvedInitialFileWidgetSetup and self.settings.layout.widgets:
            return None
        if self.settings.load_state == LoadState.DEFAULTS_AFTER_FAILURE:
            return None
        if not interactive:
            return None
        has_file_widget = any(
            w.widgetKind == WidgetKind.FILE and w.id not in self.settings.layout.deletedWidgetIds
            for w in self.settings.layout.widgets
        )
        if has_file_widget:
            core.hasResolvedInitialFileWidgetSetup = True
            self.settings.save_debounced()
            return None
        core.hasResolvedInitialFileWidgetSetup = True
        try:
            return self.create_file_widget(name=t("Widget.DefaultDesktopName"), place_for_first_run=True)
        except Exception:
            if not has_file_widget:
                core.hasResolvedInitialFileWidgetSetup = False
            raise

    # ---- shutdown -----------------------------------------------------------------

    def shutdown(self) -> None:
        self.raise_toggle.lower_all()
        self.capture_geometries()
        for runtime in list(self.runtimes.values()):
            if runtime.controller is not None:
                runtime.controller.dispose()
            if hasattr(runtime.surface, "dispose"):
                try:
                    runtime.surface.dispose()
                except Exception:
                    pass
            runtime.window.destroy()
        self.runtimes.clear()
        try:
            self.search_engine.dispose()
        except Exception:
            pass
        _ = file_ops  # imported for callers extending this module
