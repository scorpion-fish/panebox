"""SettingsService: load/normalize, debounced save, widget CRUD + tombstones."""

from __future__ import annotations

import json

from panebox.constants import MAX_TOPOLOGY_PROFILES
from panebox.models.layout_slice import (
    WidgetSurfaceLayoutProfile,
    WidgetTopologyLayoutProfile,
)
from panebox.models.widget_config import WidgetConfig
from panebox.services.resilient_json_store import LoadState
from panebox.services.settings_service import SettingsService


class FakeScheduler:
    """Deterministic stand-in for GLib.timeout_add: fires only when pumped."""

    def __init__(self):
        self.pending = []  # list of tick callbacks
        self.calls = []

    def __call__(self, ms, tick):
        self.calls.append(ms)
        self.pending.append(tick)
        return len(self.pending)

    def pump(self):
        ticks, self.pending = self.pending, []
        for tick in ticks:
            tick()


def make_service(fresh_config_root) -> SettingsService:
    service = SettingsService(config_root=fresh_config_root)
    scheduler = FakeScheduler()
    service.use_scheduler(scheduler)
    service.scheduler = scheduler
    return service


def make_widget(name: str = "W") -> WidgetConfig:
    return WidgetConfig(name=name, widgetKind="File", mappedFolderPath="/tmp/x")


# ---- load ---------------------------------------------------------------


def test_fresh_load_seeds_defaults(fresh_config_root):
    service = SettingsService(config_root=fresh_config_root)
    service.load()
    assert service.load_state == LoadState.DEFAULTS_FOR_MISSING_FILE
    assert service.settings.core.theme == "System"
    assert service.settings.fileWidget.defaultManagedStorageRootPath
    assert service.layout.widgets == []


def test_normalization_clamps_out_of_range_values(fresh_config_root):
    (fresh_config_root / "settings.json").write_text(
        json.dumps({"widgetShell": {"widgetOpacity": 3.0, "defaultWidgetWidth": 10}}),
        encoding="utf-8",
    )
    service = SettingsService(config_root=fresh_config_root)
    service.load()
    shell = service.settings.widgetShell
    assert shell.widgetOpacity == 1.0
    assert shell.defaultWidgetWidth >= 50


# ---- debounced save -------------------------------------------------------


def test_debounced_save_coalesces_into_one_timer(fresh_config_root):
    service = make_service(fresh_config_root)
    service.load()
    service.settings.core.theme = "Dark"
    service.save_debounced()
    service.settings.core.theme = "Light"
    service.save_debounced()
    assert len(service.scheduler.calls) == 1, "pending timer must be reused"

    service.scheduler.pump()
    saved = json.loads((fresh_config_root / "settings.json").read_text())
    assert saved["core"]["theme"] == "Light", "flush must see the latest value"


def test_flush_pending_save_writes_without_timer(fresh_config_root):
    service = make_service(fresh_config_root)
    service.load()
    service.settings.core.autoStart = True
    service.save_debounced()
    service.flush_pending_save()
    saved = json.loads((fresh_config_root / "settings.json").read_text())
    assert saved["core"]["autoStart"] is True


def test_round_trip_restores_widgets(fresh_config_root):
    service = make_service(fresh_config_root)
    service.load()
    widget = make_widget("First")
    service.add_widget(widget)
    service.flush_pending_save()

    second = make_service(fresh_config_root)
    second.load()
    restored = second.widgets()
    assert len(restored) == 1
    assert restored[0].name == "First"
    assert restored[0].widgetKind == "File"


# ---- widget CRUD + tombstones ----------------------------------------------


def test_remove_widget_writes_tombstone_and_prunes_groups(fresh_config_root):
    from panebox.models.widget_group_config import WidgetGroupConfig

    service = make_service(fresh_config_root)
    service.load()
    a, b, c = make_widget("A"), make_widget("B"), make_widget("C")
    service.add_widget(a)
    service.add_widget(b)
    service.add_widget(c)
    service.layout.widgetGroups.append(
        WidgetGroupConfig(surfaceId="g1", memberIds=[a.id, b.id, c.id], activeMemberId=b.id)
    )

    service.remove_widget(b.id)
    assert [w.id for w in service.layout.widgets] == [a.id, c.id]
    assert b.id in service.layout.deletedWidgetIds
    group = service.layout.widgetGroups[0]
    assert group.memberIds == [a.id, c.id]
    assert group.activeMemberId == ""

    # A group falling below two members dissolves.
    service.remove_widget(c.id)
    assert service.layout.widgetGroups == []

    # Tombstoned ids refuse re-writes.
    service.update_widget(b)
    assert b.id not in [w.id for w in service.layout.widgets]


def test_add_widget_clears_stale_tombstone(fresh_config_root):
    service = make_service(fresh_config_root)
    service.load()
    widget = make_widget()
    service.add_widget(widget)
    service.remove_widget(widget.id)
    service.add_widget(widget)  # recreated with the same id
    assert widget.id not in service.layout.deletedWidgetIds


# ---- topology profiles ------------------------------------------------------


def test_stamp_topology_use_and_prune(fresh_config_root):
    service = make_service(fresh_config_root)
    service.load()
    for index in range(MAX_TOPOLOGY_PROFILES + 3):
        key = f"v3-{index:012d}"
        service.layout.widgetTopologyLayouts[key] = WidgetTopologyLayoutProfile(
            lastUsedAtUtc=f"2026-01-{index + 1:02d}T00:00:00Z",
            surfaces={"w": WidgetSurfaceLayoutProfile(x=float(index), y=0, width=100, height=100)},
        )
    active = f"v3-{(MAX_TOPOLOGY_PROFILES + 2):012d}"
    service.layout.activeWidgetTopologyKey = active

    from panebox.platform.workarea import TopologyLayoutService

    TopologyLayoutService(service.layout).prune_profiles()
    assert len(service.layout.widgetTopologyLayouts) == MAX_TOPOLOGY_PROFILES
    assert active in service.layout.widgetTopologyLayouts, "active key must survive pruning"

    service.stamp_topology_use(active)
    assert service.layout.widgetTopologyLayouts[active].lastUsedAtUtc != ""
