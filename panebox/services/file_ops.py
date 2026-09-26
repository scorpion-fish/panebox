"""File operations — Linux port of the FileService core.

Cancellable chunked transfers with per-item error classification, Gio trash,
".desktop" link shortcuts, name dedupe ("name (2)"), rename sanitization and
extension-change confirmation policy, FileManager1 reveal.
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

COPY_CHUNK_SIZE = 1024 * 1024


class ErrorKind:
    NOT_FOUND = "NotFound"
    ACCESS_DENIED = "AccessDenied"
    IN_USE = "InUse"
    NOT_ENOUGH_SPACE = "NotEnoughSpace"
    SOURCE_CHANGED = "SourceChanged"
    CANCELED = "Canceled"
    UNKNOWN = "Unknown"


@dataclass
class TransferItemError:
    source: str
    kind: str
    message: str = ""


@dataclass
class TransferReport:
    completed: list[tuple[str, str]] = field(default_factory=list)  # (src, dst)
    skipped: list[TransferItemError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.skipped


def classify_error(exc: BaseException) -> str:
    if isinstance(exc, CancelFlag):
        return ErrorKind.CANCELED
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return ErrorKind.NOT_FOUND
    if isinstance(exc, PermissionError):
        return ErrorKind.ACCESS_DENIED
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return ErrorKind.NOT_ENOUGH_SPACE
    if isinstance(exc, OSError) and exc.errno in (errno.EBUSY, errno.EAGAIN):
        return ErrorKind.IN_USE
    return ErrorKind.UNKNOWN


class CancelFlag(Exception):
    """Raised inside transfer loops when the caller cancelled."""


class CancelToken:
    def __init__(self):
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def check(self) -> None:
        if self._cancelled:
            raise CancelFlag()


# ---- naming -----------------------------------------------------------------


def sanitize_file_system_name(name: Optional[str]) -> str:
    sanitized = (name or "").strip()
    for ch in ("/", "\x00"):
        sanitized = sanitized.replace(ch, "-")
    return sanitized.strip().rstrip(".")


def split_extension(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if not dot or stem == "":
        return name, ""
    return stem, "." + ext if dot else ""


def resolve_rename_destination(
    original_file_name: str,
    sanitized_name: str,
    is_folder: bool,
    is_shortcut: bool,
    show_file_extensions: bool,
) -> tuple[str, bool]:
    """Returns (resolved_name, requires_extension_change_confirmation)."""
    if is_folder:
        return sanitized_name, False

    original_stem, original_ext = split_extension(original_file_name)

    if is_shortcut or not show_file_extensions:
        if original_ext and not sanitized_name.casefold().endswith(original_ext.casefold()):
            return sanitized_name + original_ext, False
        return sanitized_name, False

    input_stem, input_ext = split_extension(sanitized_name)
    if input_ext.casefold() == original_ext.casefold():
        return sanitized_name, False
    if input_ext:
        # Typed a different extension: honor after caller confirmation.
        return sanitized_name, True
    # No extension typed: treat as name-only edit.
    return sanitized_name + original_ext, False


def get_available_path(desired_path: str, reserved: Optional[set[str]] = None) -> str:
    if reserved is None:
        reserved = set()

    def free(path: str) -> bool:
        return not os.path.exists(path) and path not in reserved

    desired = os.path.abspath(desired_path)
    if free(desired):
        reserved.add(desired)
        return desired

    directory = os.path.dirname(desired) or os.getcwd()
    name = os.path.basename(desired)
    stem, ext = split_extension(name)
    index = 2
    while True:
        candidate = os.path.join(directory, f"{stem} ({index}){ext}")
        if free(candidate):
            reserved.add(candidate)
            return candidate
        index += 1


def is_path_under_directory(candidate_path: str, directory_path: str) -> bool:
    candidate = os.path.realpath(candidate_path)
    directory = os.path.realpath(directory_path)
    if candidate == directory:
        return False
    return candidate.startswith(directory + os.sep)


# ---- enumeration ----------------------------------------------------------------


def should_display_entry(name: str) -> bool:
    """Hidden dotfiles are refused, mirroring PaneBox's hidden-file policy."""
    return not name.startswith(".")


def enumerate_directory(path: str) -> list[dict]:
    """Visible direct children with lightweight metadata (no icon work)."""
    entries: list[dict] = []
    try:
        names = os.listdir(path)
    except OSError:
        return []
    for name in sorted(names):
        if not should_display_entry(name):
            continue
        full = os.path.join(path, name)
        try:
            stat = os.lstat(full)
        except OSError:
            continue
        entries.append(
            {
                "path": full,
                "name": name,
                "is_folder": os.path.isdir(full) and not os.path.islink(full),
                "is_symlink": os.path.islink(full),
                "size": 0 if os.path.isdir(full) else stat.st_size,
                "last_modified": stat.st_mtime,
                "is_shortcut": name.endswith(".desktop") and os.path.isfile(full),
            }
        )
    return entries


# ---- transfers --------------------------------------------------------------------


def transfer_items(
    source_paths: Iterable[str],
    destination_folder: str,
    move: bool,
    cancel: Optional[CancelToken] = None,
    progress: Optional[Callable[[str, str], None]] = None,
    reserved: Optional[set[str]] = None,
) -> TransferReport:
    """Copy or move items into a folder, deduping names, skipping failures."""
    report = TransferReport()
    reserved = reserved if reserved is not None else set()
    cancel = cancel or CancelToken()
    for source in source_paths:
        try:
            cancel.check()
            _transfer_one(source, destination_folder, move, cancel, reserved, report)
            if progress and report.completed:
                progress(*report.completed[-1])
        except CancelFlag:
            report.skipped.append(TransferItemError(source, ErrorKind.CANCELED))
            break
        except Exception as exc:  # per-item isolation
            report.skipped.append(TransferItemError(source, classify_error(exc), str(exc)))
    return report


def _transfer_one(
    source: str,
    destination_folder: str,
    move: bool,
    cancel: CancelToken,
    reserved: set[str],
    report: TransferReport,
) -> None:
    if not os.path.exists(source):
        raise FileNotFoundError(source)
    name = os.path.basename(source.rstrip("/"))
    # Same-folder move or same-volume drop: same name means same item.
    destination = os.path.join(destination_folder, name)
    if os.path.realpath(source) == os.path.realpath(destination) and move:
        report.completed.append((source, destination))
        return
    destination = get_available_path(destination, reserved)
    os.makedirs(destination_folder, exist_ok=True)
    if move:
        shutil.move(source, destination)
    elif os.path.isdir(source):
        shutil.copytree(source, destination, symlinks=True)
    else:
        _copy_file_chunked(source, destination, cancel)
    report.completed.append((source, destination))


def _copy_file_chunked(source: str, destination: str, cancel: CancelToken) -> None:
    with open(source, "rb") as src, open(destination, "wb") as dst:
        while True:
            cancel.check()
            chunk = src.read(COPY_CHUNK_SIZE)
            if not chunk:
                break
            dst.write(chunk)
    shutil.copystat(source, destination)


def trash_items(paths: Iterable[str], cancel: Optional[CancelToken] = None) -> TransferReport:
    """Move items to the freedesktop trash via GIO."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio

    report = TransferReport()
    for path in paths:
        try:
            if cancel:
                cancel.check()
            file = Gio.File.new_for_path(path)
            if not file.trash(None):
                raise PermissionError(f"trash refused {path}")
            report.completed.append((path, path))
        except CancelFlag:
            report.skipped.append(TransferItemError(path, ErrorKind.CANCELED))
            break
        except Exception as exc:
            report.skipped.append(TransferItemError(path, classify_error(exc), str(exc)))
    return report


def delete_permanently(paths: Iterable[str], cancel: Optional[CancelToken] = None) -> TransferReport:
    report = TransferReport()
    for path in paths:
        try:
            if cancel:
                cancel.check()
            if os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            report.completed.append((path, path))
        except CancelFlag:
            report.skipped.append(TransferItemError(path, ErrorKind.CANCELED))
            break
        except Exception as exc:
            report.skipped.append(TransferItemError(path, classify_error(exc), str(exc)))
    return report


def rename_entry(path: str, new_name: str) -> str:
    """Sanitize + dedupe + apply. Case-only renames apply directly."""
    parent = os.path.dirname(path)
    target = os.path.join(parent, new_name)
    if target != path and os.path.exists(target):
        target = get_available_path(target)
    os.rename(path, target)
    return target


# ---- shortcuts (.desktop links) ------------------------------------------------------


def create_shortcut(target_path: str, destination_folder: str, display_name: Optional[str] = None) -> str:
    """Write a Name/URL .desktop link (Linux .lnk equivalent)."""
    import urllib.parse

    name = display_name or os.path.basename(target_path.rstrip("/"))
    stem, _ = split_extension(name)
    desired = os.path.join(destination_folder, stem + ".desktop")
    final = get_available_path(desired)
    uri = urllib.parse.quote(os.path.abspath(target_path))
    icon = "folder" if os.path.isdir(target_path) else "text-x-generic"
    content = f"[Desktop Entry]\nType=Link\nName={name}\nURL=file://{uri}\nIcon={icon}\n"
    Path(final).write_text(content, encoding="utf-8")
    os.chmod(final, 0o755)
    return final


def shortcut_target(path: str) -> Optional[str]:
    """Resolve a Type=Link .desktop to its local target path."""
    import urllib.parse

    if not path.endswith(".desktop"):
        return None
    try:
        keyfile = GLib_KeyFile_load(path)
        if keyfile is None:
            return None
        url = keyfile.get_string("Desktop Entry", "URL")
    except Exception:
        return None
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.unquote(parsed.path) if parsed.scheme == "file" else url


def GLib_KeyFile_load(path: str):
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    keyfile = GLib.KeyFile()
    try:
        if not keyfile.load_from_file(path, GLib.KeyFileFlags.NONE):
            return None
    except GLib.Error:
        return None
    return keyfile


def shortcut_display_name(path: str) -> Optional[str]:
    keyfile = GLib_KeyFile_load(path)
    if keyfile is None:
        return None
    try:
        return keyfile.get_string("Desktop Entry", "Name") or None
    except Exception:
        return None


# ---- reveal -----------------------------------------------------------------------------


def reveal_in_file_manager(paths: list[str]) -> None:
    """org.freedesktop.FileManager1.ShowItems; fallback xdg-open parent."""
    import subprocess

    if not paths:
        return
    uris = ["file://" + os.path.abspath(p) for p in paths]
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(
            "org.freedesktop.FileManager1",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1",
            "ShowItems",
            GLib_Variant("as", uris + [""]),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        return
    except Exception:
        pass
    subprocess.Popen(
        ["xdg-open", os.path.dirname(os.path.abspath(paths[0]))],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def GLib_Variant(kind: str, value):
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    return GLib.Variant(kind, value)


# ---- managed storage -------------------------------------------------------------------------


def mint_managed_folder(storage_root: str, desired_name: str) -> str:
    os.makedirs(storage_root, exist_ok=True)
    sanitized = sanitize_file_system_name(desired_name) or "Folder"
    desired = os.path.join(storage_root, sanitized)
    final = get_available_path(desired)
    os.makedirs(final, exist_ok=True)
    return final


def desktop_directory() -> str:
    """XDG desktop dir via GLib, then ~/Desktop, then home."""
    home = os.path.expanduser("~")
    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        path = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
        if path and os.path.isdir(path):
            return path
    except Exception:
        pass
    fallback = os.path.join(home, "Desktop")
    if os.path.isdir(fallback):
        return fallback
    return home
