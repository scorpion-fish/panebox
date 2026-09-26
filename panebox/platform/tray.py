"""Tray icon: StatusNotifierItem (KDE spec) + libnotify fallback.

GNOME ships no StatusNotifierWatcher, so the SNI path is taken only when the
watcher is on the bus; otherwise a persistent libnotify notification with an
"open" action acts as the tray surrogate (matches the plan's fallback).

The dbusmenu export is flat (one level) — enough for PaneBox's tray menu.
"""

from __future__ import annotations

import os
from typing import Callable, List, Optional

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

SNI_NAME_TEMPLATE = "org.kde.StatusNotifierItem-{pid}-1"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
MENU_PATH = "/MenuBar"

_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <method name="Activate">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
    </method>
    <method name="Scroll">
      <arg type="i" direction="in" name="delta"/>
      <arg type="s" direction="in" name="orientation"/>
    </method>
  </interface>
</node>
"""

_DBUSMENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg type="i" direction="in" name="revision"/>
      <arg type="i" direction="in" name="parentId"/>
      <arg type="as" direction="in" name="filter"/>
      <arg type="ai" direction="in" name="ids"/>
      <arg type="u" direction="out" name="revision"/>
      <arg type="(ia{sv}av)" direction="out" name="layout"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" direction="in" name="ids"/>
      <arg type="as" direction="in" name="filter"/>
      <arg type="a(ia{sv})" direction="out" name="properties"/>
    </method>
    <method name="GetProperty">
      <arg type="i" direction="in" name="id"/>
      <arg type="s" direction="in" name="name"/>
      <arg type="v" direction="out" name="value"/>
    </method>
    <method name="Event">
      <arg type="i" direction="in" name="id"/>
      <arg type="s" direction="in" name="eventId"/>
      <arg type="v" direction="in" name="data"/>
      <arg type="x" direction="in" name="timestamp"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" direction="in" name="events"/>
      <arg type="ai" direction="out" name="idErrors"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" direction="in" name="id"/>
      <arg type="b" direction="out" name="needUpdate"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" direction="in" name="ids"/>
      <arg type="ab" direction="out" name="updatesNeeded"/>
      <arg type="ai" direction="out" name="idErrors"/>
    </method>
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
  </interface>
</node>
"""


class TrayMenuItem:
    def __init__(self, action_id: str, label: str, disabled: bool = False, visible: bool = True):
        self.action_id = action_id
        self.label = label
        self.disabled = disabled
        self.visible = visible
        self.menu_id = 0  # assigned by TrayController.set_menu


class TrayController:
    """Owns the tray surface. `actions` maps action_id -> callable."""

    def __init__(
        self,
        actions: dict[str, Callable[[], None]],
        on_activate: Optional[Callable[[], None]] = None,
        log: Callable[[str], None] = lambda _msg: None,
    ):
        self.actions = actions
        self.on_activate = on_activate
        self.log = log
        self.menu: List[TrayMenuItem] = []
        self.tooltip = ""
        self.icon_name = "folder"
        self.mode = "none"  # "sni" | "notify" | "none"
        self._bus_id = 0
        self._connection: Optional[Gio.DBusConnection] = None
        self._sni_info = Gio.DBusNodeInfo.new_for_xml(_SNI_XML).interfaces[0]
        self._menu_info = Gio.DBusNodeInfo.new_for_xml(_DBUSMENU_XML).interfaces[0]
        self._notify = None
        self._notify_handler = None

    # ---- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._connection = bus
        if self._watcher_available(bus):
            self._start_sni(bus)
        else:
            self._start_notify_fallback()

    def stop(self) -> None:
        if self._bus_id:
            Gio.bus_unown_name(self._bus_id)
            self._bus_id = 0
        if self._connection is not None:
            for path, iface in (
                (self._object_path(), self._sni_info.name),
                (MENU_PATH, self._menu_info.name),
            ):
                try:
                    self._connection.unregister_object(_REGISTRY.pop((path, iface), None))
                except Exception:
                    pass
        if self._notify is not None:
            try:
                self._notify.close()
            except Exception:
                pass
            self._notify = None
        self.mode = "none"

    # ---- public ------------------------------------------------------------------

    def set_menu(self, items: List[TrayMenuItem]) -> None:
        self.menu = items
        # Stable dbusmenu ids (root = 0), allocated whenever the menu changes.
        _MENU_IDS.clear()
        _MENU_IDS[0] = "root"
        for index, item in enumerate(items, start=1):
            _MENU_IDS[index] = item.action_id
            item.menu_id = index

    def set_tooltip(self, text: str) -> None:
        self.tooltip = text
        if self._notify is not None:
            try:
                self._notify.update(text, "", "panebox")
            except Exception:
                pass

    # ---- SNI ------------------------------------------------------------------------

    def _object_path(self) -> str:
        return f"/org/kde/StatusNotifierItem/{os.getpid()}"

    @staticmethod
    def _watcher_available(bus: Gio.DBusConnection) -> bool:
        try:
            result = bus.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                "org.freedesktop.DBus.Peer",
                "Ping",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            return result is not None
        except GLib.Error:
            return False

    def _start_sni(self, bus: Gio.DBusConnection) -> None:
        name = SNI_NAME_TEMPLATE.format(pid=os.getpid())
        self._bus_id = Gio.bus_own_name_on_connection(
            bus, name, Gio.BusNameOwnerFlags.NONE, self._on_name_acquired, self._on_name_lost
        )

    def _on_name_acquired(self, connection: Gio.DBusConnection, name: str) -> None:
        try:
            token = connection.register_object(
                self._object_path(),
                self._sni_info,
                self._on_sni_method,
                self._on_sni_property,
                None,
            )
            _REGISTRY[(self._object_path(), self._sni_info.name)] = token
            token = connection.register_object(
                MENU_PATH,
                self._menu_info,
                self._on_menu_method,
                self._on_menu_property,
                None,
            )
            _REGISTRY[(MENU_PATH, self._menu_info.name)] = token
        except Exception as exc:
            self.log(f"[Tray] SNI registration failed: {exc}")
            self._start_notify_fallback()
            return
        self.mode = "sni"
        try:
            connection.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                WATCHER_NAME,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", [name]),
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except GLib.Error as exc:
            self.log(f"[Tray] watcher registration failed: {exc}")

    def _on_name_lost(self, connection, name: str) -> None:
        self.log("[Tray] SNI name lost")
        if self.mode != "sni":
            self._start_notify_fallback()

    def _on_sni_method(self, connection, sender, path, iface, method, params, invocation):
        if method == "Activate":
            if self.on_activate:
                self.on_activate()
        elif method in ("SecondaryActivate", "Scroll"):
            pass
        invocation.return_value(None)

    def _on_sni_property(self, connection, sender, path, iface, prop):
        if prop == "Category":
            return GLib.Variant("s", "SystemServices")
        if prop == "Id":
            return GLib.Variant("s", f"panebox-{os.getpid()}")
        if prop == "Title":
            return GLib.Variant("s", "PaneBox")
        if prop == "Status":
            return GLib.Variant("s", "Active")
        if prop == "IconName":
            return GLib.Variant("s", self.icon_name)
        if prop in ("AttentionIconName", "OverlayIconName"):
            return GLib.Variant("s", "")
        if prop == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", (self.tooltip or "PaneBox", [], "", ""))
        if prop == "Menu":
            return GLib.Variant("o", MENU_PATH)
        if prop == "ItemIsMenu":
            return GLib.Variant("b", False)
        return None

    # ---- dbusmenu ---------------------------------------------------------------------

    def _layout_value(self):
        """Menu tree for one GLib.Variant call. PyGObject composes struct
        children from plain tuples, but each `av` element must be a ready-made
        Variant (the override's `v` leaf rejects tuples)."""
        children = [
            GLib.Variant(
                "(ia{sv}av)",
                (
                    item.menu_id,
                    {
                        "label": GLib.Variant("s", item.label),
                        "enabled": GLib.Variant("b", not item.disabled),
                        "visible": GLib.Variant("b", True),
                    },
                    [],
                ),
            )
            for item in self.menu
            if item.visible
        ]
        return (0, {"children-display": GLib.Variant("s", "submenu")}, children)

    def _on_menu_method(self, connection, sender, path, iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (1, self._layout_value())))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            ids = params[0]
            invocation.return_value(GLib.Variant("(abai)", ([False] * len(ids), [])))
        elif method == "Event":
            item_id, event_id, _data, _ts = params.unpack()
            if event_id == "clicked":
                action_id = _MENU_IDS.get(item_id)
                if action_id:
                    handler = self.actions.get(action_id)
                    if handler:
                        GLib.idle_add(handler)
            invocation.return_value(None)
        elif method == "GetGroupProperties":
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([])))
        elif method == "GetProperty":
            invocation.return_value(GLib.Variant("v", GLib.Variant("i", 0)))
        elif method == "EventGroup":
            invocation.return_value(GLib.Variant("(ai)", ([])))
        else:
            invocation.return_value(None)

    def _on_menu_property(self, connection, sender, path, iface, prop):
        if prop == "Version":
            return GLib.Variant("u", 2)
        if prop == "TextDirection":
            return GLib.Variant("s", "ltr")
        if prop == "Status":
            return GLib.Variant("s", "normal")
        if prop == "IconThemePath":
            return GLib.Variant("as", [])
        return None

    # ---- libnotify fallback ---------------------------------------------------------------

    def _start_notify_fallback(self) -> None:
        try:
            gi.require_version("Notify", "0.7")
            from gi.repository import Notify
        except Exception:
            self.mode = "none"
            self.log("[Tray] no SNI watcher and libnotify unavailable; tray disabled")
            return
        try:
            if not Notify.is_initted():
                Notify.init("PaneBox")
            notification = Notify.Notification.new(self.tooltip or "PaneBox", "", "panebox")
            notification.set_timeout(Notify.EXPIRES_NEVER)
            notification.add_action("default", "Open", lambda *_a: self.on_activate and self.on_activate())
            notification.show()
            self._notify = notification
            self.mode = "notify"
            self.log("[Tray] using persistent-notification fallback (no StatusNotifierWatcher)")
        except Exception as exc:
            self.mode = "none"
            self.log(f"[Tray] notification fallback failed: {exc}")


# registration-object tokens + menu-id dispatch (module-level: D-Bus callbacks
# carry no user_data in PyGObject)
_REGISTRY: dict[tuple[str, str], int] = {}
_MENU_IDS: dict[int, str] = {0: "root"}
