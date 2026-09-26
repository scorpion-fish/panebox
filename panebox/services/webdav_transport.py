"""WebDAV transport for cloud backup (port of Services/WebDavBackupTransport.cs).

requests-based PROPFIND / MKCOL / PUT / GET / DELETE with Basic auth.
Security rules carried over verbatim:
- entry names come from the href's last segment, NEVER from <displayname>
  (a malicious displayname cannot traverse outside the remote folder);
- the remote path is split and per-segment URI-escaped, so a hostile folder
  name can't smuggle "../" into the request URL.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import requests

TIMEOUT_SECONDS = 300
_DAV_NS = {"d": "DAV:"}


class CloudBackupTransportException(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class RemoteEntry:
    name: str
    is_directory: bool
    size: int = 0
    modified_utc: Optional[str] = None


def parse_multistatus(body: bytes, request_url: str) -> list[RemoteEntry]:
    """Parse a Depth:1 PROPFIND multistatus body into directory entries.

    The entry describing the requested collection itself (its href matches the
    request URL) is skipped; only 200-status propstats contribute properties.
    """
    request_path = unquote(urlsplit(request_url).path)
    request_segments = tuple(seg for seg in request_path.split("/") if seg)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise CloudBackupTransportException(f"Invalid WebDAV response XML: {exc}") from exc
    if root.tag != f"{{{_DAV_NS['d']}}}multistatus":
        raise CloudBackupTransportException("WebDAV response is not a multistatus document")

    entries: list[RemoteEntry] = []
    for response_el in root.findall("d:response", _DAV_NS):
        href = (response_el.findtext("d:href", default="", namespaces=_DAV_NS) or "").strip()
        if not href:
            continue
        href_path = unquote(urlsplit(href).path)
        segments = tuple(seg for seg in href_path.split("/") if seg)
        if not segments:
            continue
        if segments == request_segments:
            # The requested collection itself — Depth:1 servers list it first.
            continue

        is_directory = False
        size = 0
        modified: Optional[str] = None
        for propstat in response_el.findall("d:propstat", _DAV_NS):
            status = propstat.findtext("d:status", default="", namespaces=_DAV_NS) or ""
            if " 200 " not in status and not status.endswith(" 200"):
                continue
            prop = propstat.find("d:prop", _DAV_NS)
            if prop is None:
                continue
            resourcetype = prop.find("d:resourcetype", _DAV_NS)
            if resourcetype is not None and resourcetype.find("d:collection", _DAV_NS) is not None:
                is_directory = True
            length = prop.findtext("d:getcontentlength", default=None, namespaces=_DAV_NS)
            if length and length.isdigit():
                size = int(length)
            modified = prop.findtext("d:getlastmodified", default=None, namespaces=_DAV_NS) or modified
        # Name from the href's last segment only — displayname is untrusted.
        entries.append(RemoteEntry(name=segments[-1], is_directory=is_directory, size=size, modified_utc=modified))
    return entries


class WebDavBackupTransport:
    def __init__(self, server_url: str, username: str = "", password: str = ""):
        parts = urlsplit(server_url.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise CloudBackupTransportException(f"The WebDAV server URL is not valid: {server_url!r}")
        self._scheme = parts.scheme
        self._host = parts.netloc
        self._base_segments = [seg for seg in parts.path.split("/") if seg]
        self._auth = (username, password) if username else None

    # ---- URL building ------------------------------------------------------------

    def _url(self, remote_path: str) -> str:
        segments = self._base_segments + [seg for seg in remote_path.split("/") if seg]
        # Per-segment escaping: a folder name can never smuggle path syntax.
        escaped = "/".join(quote(seg, safe="") for seg in segments)
        return urlunsplit((self._scheme, self._host, "/" + escaped, "", ""))

    def _request(self, method: str, url: str, **kwargs):
        try:
            return requests.request(
                method,
                url,
                auth=self._auth,
                timeout=TIMEOUT_SECONDS,
                allow_redirects=True,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise CloudBackupTransportException(f"Cloud backup request failed: {exc}") from exc

    def _raise_for_status(self, response, context: str) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise CloudBackupTransportException(
                "Cloud backup authentication failed — check the username and app password.",
                status,
            )
        if status == 404:
            raise CloudBackupTransportException(f"Cloud backup remote path not found ({context}).", status)
        if status == 507:
            raise CloudBackupTransportException("The cloud backup server reports insufficient storage.", status)
        detail = (response.text or "").strip().replace("\n", " ")[:200]
        raise CloudBackupTransportException(
            f"Cloud backup request failed with HTTP {status} ({context}).{(' ' + detail) if detail else ''}",
            status,
        )

    # ---- operations ----------------------------------------------------------------

    def probe(self) -> None:
        """Cheap endpoint check: PROPFIND with Depth 0 against the DAV root."""
        response = self._request("PROPFIND", self._url(""), headers={"Depth": "0"}, data=_PROPFIND_BODY)
        if response.status_code == 404:
            # Base path may not exist yet on a fresh server — MKCOL will create it.
            return
        self._raise_for_status(response, "probe")

    def ensure_directory(self, remote_path: str) -> None:
        """Progressive MKCOL: create each missing segment (405 = exists)."""
        segments = self._base_segments + [seg for seg in remote_path.split("/") if seg]
        built: list[str] = []
        for segment in segments:
            built.append(segment)
            url = urlunsplit((self._scheme, self._host, "/" + "/".join(quote(s, safe="") for s in built), "", ""))
            response = self._request("MKCOL", url)
            if response.status_code == 405 or 200 <= response.status_code < 300:
                continue
            self._raise_for_status(response, "mkcol")

    def list(self, remote_path: str) -> list[RemoteEntry]:
        response = self._request("PROPFIND", self._url(remote_path), headers={"Depth": "1"}, data=_PROPFIND_BODY)
        self._raise_for_status(response, "propfind")
        return parse_multistatus(response.content, response.url or self._url(remote_path))

    def upload(self, local_path: Path, remote_path: str) -> None:
        with open(local_path, "rb") as handle:
            response = self._request("PUT", self._url(remote_path), data=handle)
        self._raise_for_status(response, "put")

    def download(self, remote_path: str, destination: Path) -> None:
        response = self._request("GET", self._url(remote_path), stream=True)
        self._raise_for_status(response, "get")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(destination.name + f".part-{os.getpid()}")
        try:
            with open(tmp, "wb") as handle:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        handle.write(chunk)
            os.replace(tmp, destination)
        finally:
            tmp.unlink(missing_ok=True)

    def delete(self, remote_path: str) -> None:
        response = self._request("DELETE", self._url(remote_path))
        if response.status_code == 404:
            return  # already gone — delete is idempotent
        self._raise_for_status(response, "delete")


_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    "</d:prop></d:propfind>"
)
