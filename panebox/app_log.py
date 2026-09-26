"""Rotating application log.

One file per install: ``<DATA_ROOT>/panebox.log`` (``constants.LOG_FILE``),
1 MiB x 2 rotated backups — ≥ the 2 MiB sanitized tail the diagnostics
bundle exports. Everything also goes to stderr, matching the pre-logging
terminal behavior (and the smoke tests' log watching).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from .constants import APP_VERSION, LOG_FILE

MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 2
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO) -> Path:
    """Install handlers on the "panebox" logger tree. Safe to call twice."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("panebox")
    root.setLevel(level)
    if not root.handlers:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(file_handler)
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.INFO)
        stderr_handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(stderr_handler)
    root.info("---- PaneBox %s session start (pid %s) ----", APP_VERSION, os.getpid())
    return LOG_FILE


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"panebox.{name}")
