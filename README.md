# PaneBox for Linux

A feature-equivalent Linux port of the Windows desktop organizer DeskBox
(WinUI 3, C#/WinRT source): real-folder-backed file widgets pinned to the
desktop layer, plus Todo, Quick Capture, Glance (clock/lunar), Weather, Music
and Search feature widgets — tray control, hotkeys, per-monitor layout memory,
12-language i18n, and resilient JSON settings.

License: GPL-3.0 (see [LICENSE](LICENSE)), same as DeskBox — this is a
port of that project, so it keeps its license. Contributing:
[CONTRIBUTING.md](CONTRIBUTING.md) — Google Python Style Guide, ruff,
pytest.

"Feature parity" means the feature list, not 1:1 file parity. Where Linux
differs from Windows, the behavior is reimplemented on the closest native
mechanism and listed under [Platform divergences](#platform-divergences).

## Acknowledgements

PaneBox is a Linux port of [DeskBox](https://github.com/Tianyu199509/DeskBox),
the Windows desktop organizer by [Tianyu199509](https://github.com/Tianyu199509).
Every feature here was traced back to the original app, and a lot of the
details worth keeping came from reading its code. Thank you for open-sourcing
DeskBox — this project would not exist without it.

## Requirements

- Python 3.12+ (developed on 3.14) with PyGObject (`gi`), GTK 4.16+
- `python3-pil`, `python3-requests`, `python3-watchdog`, `python3-dateutil`
- X11 or XWayland (desktop-layer widgets are EWMH X11 windows — the app forces
  `GDK_BACKEND=x11` itself)
- Optional, auto-detected: `pactl` (music volume), `libnotify` (reminders),
  `xdg-open` / `org.freedesktop.FileManager1` (reveal in files)

## Run

```bash
python3 main.py               # run normally
python3 main.py --settings    # ask the running instance to open settings
python3 main.py --toggle      # ask the running instance to toggle widgets
```

Settings live in `~/.config/panebox/` (`settings.json`, `widget-layout.json`,
atomic writes with `.bak` + corrupt-file quarantine/recovery). Data lives under
`~/.local/share/panebox/`; the app log rotates at
`~/.local/share/panebox/panebox.log` (1 MiB x 2 backups). Managed storage
defaults to `~/PaneBox`.

## Tests

```bash
python3 -m pytest tests/          # everything (GUI tests auto-skip without X)
Xvfb :99 &                        # or: enable the GUI tests
PANEBOX_SMOKE_DISPLAY=:99 python3 -m pytest tests/
```

GUI tests need `Xvfb :99` (1280×800×24) and set `GSK_RENDERER=cairo`
themselves. 476 tests currently pass (services, models, state machines,
localization key parity, Xvfb surface smoke). Note: a running PaneBox
instance owns the D-Bus name and collides with the GUI smoke tests —
stop it first.

## Packages

`bash packaging/build_all.sh` builds all three artifacts into `dist/`
(icons are generated with PIL; the AppImage type-2 runtime is taken from an
existing AppImage, default `~/Downloads/Joplin-3.6.14.AppImage`):

- `panebox_<ver>_all.deb` — `sudo apt install ./panebox_0.4.1_all.deb`
  (Depends pulls python3-gi, gir1.2-gtk-4.0 and friends)
- `PaneBox-<ver>-x86_64.AppImage` — `./PaneBox-0.4.1-x86_64.AppImage`;
  the payload is interpreted, so the host needs the same system GTK/PyGObject
  packages (AppRun prints the install line when PyGObject is missing)
- `panebox_<ver>_amd64.snap` — classic confinement against system packages:
  `sudo snap install --dangerous --classic ./panebox_0.4.1_amd64.snap`

## Status

- **M1 — done**: X11 desktop layer, widget shell (move/resize/snap), file
  widgets (sorting, DnD, clipboard, trash), tray + toggle-all,
  settings window, resilient stores.
- **M2 — done**: Todo ✅, Quick Capture ✅, Glance ✅, Weather ✅, Music ✅,
  Search ✅ (index + engine + popup + desktop widget + actions), Feature
  Widgets settings page ✅ (per-feature enable gates + per-widget settings
  with live surface refresh, search hotkey capture).
- **M3 — done**: capsule mode ✅ (collapse behaviors Expanded/Click/Smart
  with hover-expand + auto-collapse + click-to-pin, Smart/Minimal/Summary
  content with privacy masking, anchor-aware expansion, Aligned/Independent
  width modes, capsule drag placement capture, bar arrangement with persisted
  order, per-kind summaries); widget groups ✅ (merge into one surface with an
  8-member cap, Tabs/Stack navigation with per-group overrides, tab strip with
  hover switching, wheel gesture switching (120-px step, one switch per
  gesture), Ctrl+Tab cycling, member reorder, detach with cascade placement,
  dissolve with confirmation, group settings page, restore keeps one window
  per group); file stacks ✅ (auto stacking by Kind/DateAdded/DateModified/
  custom rules with threshold 2/3/5, all eight kind categories + five date
  buckets, manual stacks with convert/merge/split/dissolve lifecycle,
  per-widget overrides incl. enable/follow-defaults, per-stack rename/move/
  disable-group/restore, inline expand or popover open mode with Grid3/Grid5/
  adaptive layouts, custom-rules editor, settings page rows); organize desktop ✅
  (scan with exclusion reasons + 100 MiB/200-item quick-batch cap, classifier for
  Shortcuts/Documents/Images/Media/Packages/Other with Pdf/Word/Excel/
  PowerPoint/Text/Audio/Video subtypes, per-widget routing rules ranked
  extension > subtype > category with exclusions, plan preview with per-bucket
  selection + new/existing destination routing + retained-items opt-in
  (folders, >100 MB files, batch overflow), preview reconciliation that never
  silently selects newly discovered files, crash-recovery journal with
  device+inode identity receipts saved before every move, startup recovery +
  undo/abandon lifecycle, per-item source revalidation, retention policy
  24 entries / 500 receipts / 2500 budget with undo-lifecycle protection,
  tray + settings entries); WebDAV cloud backup ✅ (scoped snapshots over a
  requests-based WebDAV transport — PROPFIND/MKCOL/PUT/GET/DELETE with
  per-segment quoting and 401/403/404/407 error mapping; integrity manifest
  written last with per-file length + sha256 verified on every extraction;
  three backup domains — todo data, quick-capture data, widget style — with
  attachment/image payloads deliberately outside every domain; upload
  verification by listing with 0.8/2/4 s retries degrading to
  success-with-unverified-flag, never failure; retention pruning keeps
  max(1, N) newest snapshots; credential stored via libsecret keyed by
  provider + endpoint origin + username with stale-key sweep; destination
  identity change resets last-success/failure stamps (first observation after
  startup keeps persisted stamps); scheduled runs gate on interval +
  10-minute spacing floor, checked every 5 minutes; staged restore: snapshot
  download → manifest peek → domain-checkbox dialog with merge/overwrite
  modes → validation, staging, orphan-todo-widget remap planning (source
  liveness from the widget-style document, source-orphan debris dropped,
  leftover orphans merged), managed-attachment path rebase → pending marker;
  apply at next launch before widget surfaces return — additive merge by item
  id where a remote item wins only when strictly newer and a tombstone never
  beats a live local item, or replace mode deleting in-domain local items;
  pre-restore safety backup capped at 5, bounded 3 apply attempts then
  abandon, per-widget remap/merge/unmapped/attachment-ref counts surfaced in
  the settings status; settings page with provider/connection/password/test
  connection, data-type switches, schedule, snapshot list with restore/
  delete); diagnostics bundle ✅ (Advanced-page export writes a
  `PaneBox-Diagnostics-<stamp>.zip` — atomic tmp+rename, `-2` collision
  suffix — containing a runtime snapshot (schema v5: version, distribution
  channel, OS/arch, ui culture, hotkey state, settings load-recovery state,
  widget hosts with bounds/collapsed/position-locked, monitor topology), a
  bilingual README, and a sanitized 2 MiB tail of the app log; the privacy
  filter redacts sensitive key=value assignments (bools survive), Windows
  and POSIX paths, emails, SIDs, and account/machine names before anything
  leaves the machine — settings.json, widget content stores, file
  inventories and attachments are never included); update check ✅
  (background check 45 s after startup gated on the auto-check setting,
  official manifest first with GitHub latest-release fallback, C#
  version-parse semantics — optional v prefix, prerelease/build suffixes
  stripped, ≤4 numeric parts — result stamped into lastUpdateCheckAt,
  one-time tray notification on availability, Settings → Advanced page
  with auto-check toggle, manual check button, live status line, and a
  manual-download link).

## Platform divergences

Windows mechanism → Linux replacement. All divergences are intentional;
behavior parity is preserved up to the platform boundary.

| Windows | Linux port | Notes |
| --- | --- | --- |
| WorkerW desktop attach | EWMH `_NET_WM_WINDOW_TYPE_DESKTOP` + sticky/skip-taskbar | Works on X11 sessions and via XWayland on GNOME/Wayland |
| Explorer context menu / reveal | GTK popover; `FileManager1.ShowItems` / `xdg-open` | |
| Recycle Bin (Shell API) | GIO `trash://` + `~/.local/share/Trash/info` parsing | Identity = device + inode |
| `.lnk` shortcuts | `.desktop` files | |
| RegisterHotKey | XGrabKey (ctypes) | Grabs only fire while an X11 window has focus on GNOME-Wayland; tray fallback |
| Tray icon on GNOME/Wayland (no StatusNotifierWatcher) | every widget's "…" title-bar menu carries the tray entries (new widget, new folder mapping, add feature widget, open storage, settings) | A persistent libnotify notification with an "open" action is also posted; install the AppIndicator extension for a real tray icon |
| MSN Weather | Open-Meteo primary, MSN secondary | Same WMO code mapper |
| Windows Geolocator | IP geolocation + city picker (offline city list) | |
| Credential Manager | libsecret | Falls back to a chmod-600 JSON file under the data root when the keyring is unavailable (headless/test) |
| Bing wallpaper (Glance) | same endpoints | |
| ChineseLunisolarCalendar (BCL) | ported lunar table 1900–2100 | Other traditional calendars: none for v1 |
| QuickLook (Space preview) | `org.gnome.NautilusPreviewer` D-Bus | best-effort |
| Everything IPC search index | own file index (`$HOME` walk, watchdog refresh, XDG app scan) | Depth-8/200k-file caps, skip-list for noisy dirs; see Search specifics below |
| Stack "Applications" category (`.exe`/`.msi`/…) | Linux extension table (`.desktop`, `.AppImage`, `.sh`, `.py`, `.run`, `.flatpakref`, …) | Same first-match category resolution; documents/media/archives tables are verbatim |
| Drag a file onto a stack tile to add it | context menu on the stack (add/remove members) + "Start stacking" from a selection | GTK4 drop targets don't expose per-child drop routing inside a `FlowBox`; membership ops are otherwise identical |
| Auto-organization watcher (background organizing on idle/desktop change) | not ported | Organize stays a user-confirmed action from the tray/settings; the journal + undo lifecycle is complete |
| Restore auto-restarts the app | restore stages now, applies at next launch | PaneBox on Windows relaunches itself after staging a restore; the Linux port asks the user to restart (stated in the confirm dialog) — apply runs before widget surfaces load, so nothing is lost by the manual step |
| Local full "DataBackup" (settings + content ZIP export/import) | not ported | Only the scoped cloud-backup flow (todo / quick-capture / widget-style domains over WebDAV) is ported; Windows' unscoped whole-profile ZIP is documented here as out of scope |
| `widgetCollapsedStyle` / `widgetCompactMediaCornerMode` in style sync | skipped | Those keys don't exist on the Linux widget-shell settings slice; the remaining 23 shell keys + 4 per-widget keys sync as on Windows |
| Update download + installer pipeline (exe/MSIX, hash check, pre-update backup) | check-only | The port compares versions (manifest → GitHub fallback) and links the official download page; Linux installs arrive through the distribution channel |
| Diagnostics EventLog / Windows account+SID redaction | own stderr log + POSIX redaction | Same bundle shape; the filter additionally redacts `/home/...` paths and libsecret-free account names |
| Public/shared desktop scope (all-users items, elevation) | single personal desktop | Linux has no shared desktop directory in the XDG model; the public source is reported unavailable, as on a Windows box without a public desktop |

### Search widget specifics

| Windows | Linux port | Notes |
| --- | --- | --- |
| Everything IPC provider | `SearchFileIndex`: bounded `$HOME` walk + `.desktop` app directories | AND-term matching, name-beats-path scoring (100/80/60/25), folders −4; depth 8, 200k files, noisy dirs (`.git`, `node_modules`, caches) skipped; refresh every 300s or on watchdog events |
| Start Menu `.lnk` apps | XDG `applications` dirs (incl. Flatpak exports) | ≤40 launchers in recommendations |
| Win32 hotkey (Alt+D default, off) | same XGrabKey layer as the global hotkey | Modifier encoding Shift/Ctrl/Alt masks |
| Popup show/hide composition animations | instant map/unmap | WinUI 167ms/4px and 83ms/−2px storyboards have no GTK equivalent |
| Multi-select batch operations | single selection + context menu (open, open location, copy path, attach to todo, save to note) | Not ported |
| Custom popup bounds (`searchPopupCustomX/Y/W/H`) | not ported | The settings slice shipped without those fields; popup is always work-area centered |
| Creation time column | file mtime | Some Linux filesystems expose birth time inconsistently |

### Music widget specifics

| Windows | Linux port | Notes |
| --- | --- | --- |
| SMTC media sessions | MPRIS (`org.mpris.MediaPlayer2.*`) | "System current" player = most-recently-active heuristic (playback-status/metadata/seek signal timestamps); a preferred source can be pinned |
| WASAPI session volume | `pactl` (default sink + sink-inputs) | Best-effort: app-stream matching is by application name/binary; unavailable streams show "volume unavailable" |
| Segoe MDL2 Assets glyphs | Unicode glyphs (▶ ⏸ ⏮ ⏭ 🔊 ⇄ ⟳ →) | Font-dependent rendering |
| InlineVolumePanel flyout | one volume popover per layout | GTK popovers cannot be shared between menu buttons |
| Composition animations (vinyl spin, tonearm swing) | `Gtk.DrawingArea` draw func + timers | Same 360°/5s rotation cadence |
| WinUI marquee (TranslateTransform) | CSS `transform: translate()` ticks | Paint-only scrolling — margin animation would re-run size negotiation every tick |
