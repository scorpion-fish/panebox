#!/usr/bin/env python3
"""PaneBox — Linux desktop organizer (port of the Windows PaneBox).

Usage:
  python3 main.py               run normally
  python3 main.py --settings    ask the running instance to open settings
  python3 main.py --toggle      ask the running instance to toggle widgets
"""

from __future__ import annotations

import os
import sys

# Desktop-layer widgets are X11 windows (EWMH); force the X11 backend so the
# app behaves identically on Wayland sessions via XWayland.
os.environ.setdefault("GDK_BACKEND", "x11")

# The default GSK renderer (ngl) initializes on GNOME/XWayland's 32-bit ARGB
# visuals but paints nothing — every widget comes up invisible. Cairo is what
# the test rig uses; set GSK_RENDERER yourself to override.
os.environ.setdefault("GSK_RENDERER", "cairo")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    from panebox.app import PaneBoxApplication

    app = PaneBoxApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
