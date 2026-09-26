"""Credential store (port of the Windows Credential Locker usage in
CloudBackupService): libsecret keyring via the Secret-1 typelib, with a
plain-file fallback for headless sessions where no Secret Service runs.

The keyring stays authoritative for writes when available; reads consult the
keyring first and the fallback file second, so a credential saved before the
keyring disappeared still resolves.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from ..constants import DATA_ROOT

SCHEMA_NAME = "org.panebox.PaneBox.credentials"
SCHEMA_ATTRIBUTE = "key"


class CredentialStore:
    def __init__(self, fallback_path: Optional[Path] = None, log=None):
        self._fallback_path = Path(fallback_path) if fallback_path else DATA_ROOT / "credentials.json"
        self._log = log or (lambda message: None)
        self._schema = None
        self._schema_ready = False
        self._file_cache: Optional[dict] = None

    # ---- API -----------------------------------------------------------------

    def get_password(self, key: str) -> Optional[str]:
        secret = self._keyring_lookup(key)
        if secret is not None:
            return secret
        return self._file_read().get(key, {}).get("password")

    def set_password(self, key: str, value: str, label: str = "PaneBox") -> bool:
        if self._keyring_store(key, value, label):
            self._file_remove(key)
            return True
        self._file_write(key, value, label)
        return True  # the credential is stored — just not in the keyring

    def delete_password(self, key: str) -> bool:
        deleted = self._keyring_delete(key)
        deleted = self._file_remove(key) or deleted
        return deleted

    def delete_keys_with_prefix(self, prefix: str) -> int:
        """Sweep stale keys (e.g. old provider-prefixed cloud-backup keys)."""
        removed = self._keyring_delete_prefix(prefix)
        data = self._file_read()
        stale = [k for k in data if k.startswith(prefix)]
        for key in stale:
            data.pop(key, None)
        if stale:
            self._file_flush(data)
        return removed + len(stale)

    def list_keys(self) -> list[str]:
        """Every stored key, keyring first then the file fallback."""
        keys = set(self._keyring_list_keys())
        keys.update(self._file_read().keys())
        return sorted(keys)

    # ---- libsecret -----------------------------------------------------------

    def _ensure_schema(self):
        if self._schema_ready:
            return self._schema
        self._schema_ready = True
        try:
            import gi

            gi.require_version("Secret", "1")
            from gi.repository import Secret

            self._Secret = Secret
            self._schema = Secret.Schema.new(
                SCHEMA_NAME,
                Secret.SchemaFlags.DONT_MATCH_NAME,
                {SCHEMA_ATTRIBUTE: Secret.SchemaAttributeType.STRING},
            )
        except Exception as exc:
            self._log(f"[Secrets] libsecret unavailable, using file fallback: {exc}")
            self._Secret = None
            self._schema = None
        return self._schema

    def _keyring_lookup(self, key: str) -> Optional[str]:
        schema = self._ensure_schema()
        if schema is None:
            return None
        try:
            return self._Secret.password_lookup_sync(schema, {SCHEMA_ATTRIBUTE: key}, None)
        except Exception as exc:
            self._log(f"[Secrets] lookup failed: {exc}")
            return None

    def _keyring_store(self, key: str, value: str, label: str) -> bool:
        schema = self._ensure_schema()
        if schema is None:
            return False
        try:
            return bool(
                self._Secret.password_store_sync(
                    schema,
                    {SCHEMA_ATTRIBUTE: key},
                    self._Secret.COLLECTION_DEFAULT,
                    label,
                    value,
                    None,
                )
            )
        except Exception as exc:
            self._log(f"[Secrets] store failed (falling back to file): {exc}")
            return False

    def _keyring_delete(self, key: str) -> bool:
        schema = self._ensure_schema()
        if schema is None:
            return False
        try:
            return bool(self._Secret.password_clear_sync(schema, {SCHEMA_ATTRIBUTE: key}, None))
        except Exception as exc:
            self._log(f"[Secrets] delete failed: {exc}")
            return False

    def _keyring_delete_prefix(self, prefix: str) -> int:
        removed = 0
        for key in self._keyring_list_keys():
            if key.startswith(prefix) and self._keyring_delete(key):
                removed += 1
        return removed

    def _keyring_list_keys(self) -> list[str]:
        schema = self._ensure_schema()
        if schema is None:
            return []
        try:
            items = self._Secret.password_search_sync(
                schema,
                {},
                self._Secret.SearchFlags.ALL,
                None,
            )
        except Exception as exc:
            self._log(f"[Secrets] search failed: {exc}")
            return []
        keys: list[str] = []
        for item in items or []:
            try:
                attributes = item.get_attributes()
                key = attributes.get(SCHEMA_ATTRIBUTE) if attributes else None
                if key:
                    keys.append(str(key))
            except Exception:
                continue
        return keys

    # ---- file fallback ---------------------------------------------------------

    def _file_read(self) -> dict:
        if self._file_cache is not None:
            return self._file_cache
        try:
            raw = json.loads(self._fallback_path.read_text(encoding="utf-8"))
            data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            data = {}
        self._file_cache = data
        return data

    def _file_flush(self, data: dict) -> None:
        try:
            self._fallback_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self._fallback_path.parent), prefix=".credentials-")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._fallback_path)
            self._file_cache = data
        except OSError as exc:
            self._log(f"[Secrets] fallback write failed: {exc}")

    def _file_write(self, key: str, value: str, label: str) -> None:
        data = self._file_read()
        data[key] = {"password": value, "label": label}
        self._file_flush(data)

    def _file_remove(self, key: str) -> bool:
        data = self._file_read()
        if key not in data:
            return False
        data.pop(key, None)
        self._file_flush(data)
        return True
