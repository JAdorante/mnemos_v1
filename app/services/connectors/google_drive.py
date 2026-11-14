"""Google Drive connector — files you own or that are shared with you, read-only.

Google's OAuth on the shared base. By default it uses the same Google OAuth
client as the Gmail + Calendar connector (``GOOGLE_OAUTH_CLIENT_ID/SECRET``;
``GOOGLE_DRIVE_OAUTH_CLIENT_ID/SECRET`` override it) but it is a *second*
grant with its own token file: the Gmail connector's scope guard refuses any
scope it did not ask for, so Drive could not ride on that token, and keeping
the two apart means disconnecting one leaves the other alone. The redirect
URI ``/oauth/google_drive/callback`` has to be registered on the client next
to ``/oauth/google/callback``.

Scope: ``drive.readonly`` by default, which reads file text (Docs, Sheets and
Slides through the export endpoint; plain-text files directly).
``GOOGLE_DRIVE_OAUTH_SCOPES=.../drive.metadata.readonly`` narrows the ask to
names, owners and edit times, and the connector then lands metadata only.
Google echoes the granted scope, so a grant that carries anything else is
refused. ``access_type=offline`` + ``prompt=consent`` make Google return a
refresh token on every sign-in; the base refreshes on expiry.

Reads: ``GET /drive/v3/files`` with a query on ``modifiedTime`` or
``sharedWithMeTime`` since the cursor (shared drives included), newest edit
first; folders and shortcuts are skipped. Text comes from one export per
changed Doc/Sheet/Slides (top ``BODY_CAP`` characters, at most
``QUILL_GOOGLE_DRIVE_MAX_BODIES`` per check). Items land through
connectors/scheduler.py as ``google_drive.document`` (Docs, Sheets, Slides)
and ``google_drive.file`` (everything else: PDFs, images, Office files, by
name and metadata only). Nothing here writes to Drive.
"""
from __future__ import annotations

import os
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import settings
from app.services.connectors.oauth_base import (
    USER_AGENT,
    OAuthApiError,
    OAuthConnectorBase,
    clip,
    iso_utc,
    parse_iso,
)

API = "https://www.googleapis.com/drive/v3"
CONTENT_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
METADATA_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
BODY_CAP = 1500
PAGE = 100
# Bytes read from an export or download before clipping: well past BODY_CAP
# even for CSV with wide rows, without pulling a 10 MB export per file.
MAX_TEXT_BYTES = 64 * 1024
# Non-Google text files above this size are landed by name only.
MAX_DOWNLOAD_BYTES = 1024 * 1024

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
# Google-native types and the export format that carries their text.
EXPORTS: dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
_TYPE_LABELS: dict[str, str] = {
    "application/vnd.google-apps.document": "doc",
    "application/vnd.google-apps.presentation": "slides",
    "application/vnd.google-apps.spreadsheet": "sheet",
    "application/vnd.google-apps.form": "form",
    "application/vnd.google-apps.drawing": "drawing",
    "application/pdf": "PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word file",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel file",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint file",
    "text/plain": "text file",
    "text/markdown": "Markdown file",
    "text/csv": "CSV file",
}
_TEXT_MIMES = ("text/plain", "text/markdown", "text/x-markdown", "text/csv",
               "application/json")
FIELDS = ("nextPageToken,files(id,name,mimeType,modifiedTime,createdTime,"
          "sharedWithMeTime,webViewLink,size,description,parents,"
          "owners(displayName,emailAddress),"
          "lastModifyingUser(displayName,emailAddress))")


class GoogleDriveConnector(OAuthConnectorBase):
    id = "google_drive"
    label = "Google Drive"
    tool_names = ("Google Drive", "Google Docs")
    category = "productivity"
    description = (
        "Read-only files in your Google Drive and files shared with you: "
        "names, who edited them, and the text of Docs, Sheets and Slides. "
        "You sign in on Google's screen — Sparrow never sees your password."
    )
    capabilities = (
        "Remember Docs, Sheets and Slides as they change",
        "Track files shared with you and who edited them",
        "Find a file by what it says, not just its name",
    )
    source_prefixes = ("google_drive.document", "google_drive.file")
    sync_interval_s = 600
    sync_blurb = ("Google Drive files that changed or were shared with you, "
                  "with the text of Docs, Sheets and Slides")

    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scopes = (CONTENT_SCOPE,)
    extra_authorize_params = {"access_type": "offline", "prompt": "consent"}
    # Google desktop clients accept http://127.0.0.1:<any port>.
    loopback_host = "127.0.0.1"
    loopback_port = 0
    client_id_env = "GOOGLE_OAUTH_CLIENT_ID"
    client_secret_env = "GOOGLE_OAUTH_CLIENT_SECRET"
    env_token_var = ""

    def _cfg(self):
        return settings.google_drive

    def setup_hint(self) -> str:
        return ("Set GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET (shared "
                "with Gmail) or GOOGLE_DRIVE_OAUTH_CLIENT_ID + "
                "GOOGLE_DRIVE_OAUTH_CLIENT_SECRET on this install, and register "
                "/oauth/google_drive/callback on the client.")

    def assert_scopes(self, granted: Any) -> None:
        # Google may narrow the grant to the metadata scope when that is what
        # was asked; anything wider than the two read scopes is refused.
        if granted in (None, ""):
            return
        got = {s for s in str(granted).replace(",", " ").split() if s}
        extra = got - set(self.requested_scopes()) - {CONTENT_SCOPE, METADATA_SCOPE}
        if extra:
            raise PermissionError(
                f"OAuth grant includes disallowed scopes: {sorted(extra)}")

    def content_scope_granted(self) -> bool:
        """Does the stored grant let us read file text, not just names?"""
        scope = str(self.load_tokens().get("scope") or "")
        if scope:
            return CONTENT_SCOPE in scope.split()
        return CONTENT_SCOPE in self.requested_scopes()

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["content_scope_granted"] = bool(out["connected"]
                                            and self.content_scope_granted())
        return out

    def _whoami(self) -> dict[str, Any]:
        data, _ = self._request(f"{API}/about?fields=user(displayName,emailAddress)")
        user = (data or {}).get("user") or {}
        return {"account": user.get("emailAddress") or "",
                "name": user.get("displayName") or ""}

    # --- reads ------------------------------------------------------------------------
    def _max_pages(self) -> int:
        try:
            return max(1, int(os.environ.get("QUILL_GOOGLE_DRIVE_MAX_PAGES", "5")))
        except ValueError:
            return 5

    def _max_bodies(self) -> int:
        try:
            return max(0, int(os.environ.get("QUILL_GOOGLE_DRIVE_MAX_BODIES", "50")))
        except ValueError:
            return 50

    def list_changed(self, *, since: float) -> list[dict]:
        """Files modified or shared with the user since the cursor, newest
        edit first. Folders and shortcuts are dropped."""
        stamp = iso_utc(since)
        q = (f"(modifiedTime > '{stamp}' or sharedWithMeTime > '{stamp}') "
             "and trashed = false")
        out: list[dict] = []
        token = None
        for _ in range(self._max_pages()):
            params: dict[str, str] = {
                "q": q, "orderBy": "modifiedTime desc", "pageSize": str(PAGE),
                "fields": FIELDS, "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            data, _ = self._request(f"{API}/files?{urlencode(params)}")
            data = data or {}
            for f in data.get("files") or []:
                if not isinstance(f, dict) or not f.get("id"):
                    continue
                if f.get("mimeType") in (FOLDER, SHORTCUT):
                    continue
                out.append(f)
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def _fetch_text(self, url: str) -> str:
        """A bounded, authenticated read of a non-JSON body (export or
        download), decoded as UTF-8."""
        req = Request(url, method="GET")
        req.add_header("User-Agent", USER_AGENT)
        for k, v in self._auth_headers().items():
            req.add_header(k, v)
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read(MAX_TEXT_BYTES)
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise OAuthApiError(exc.code, detail or exc.reason) from None
        return raw.decode("utf-8", "replace").lstrip("﻿")

    def file_text(self, f: dict) -> str:
        """The text of one file, clipped: an export for Google-native types,
        a direct download for small plain-text files, empty otherwise."""
        fid = str(f.get("id") or "")
        mime = str(f.get("mimeType") or "")
        if not fid:
            return ""
        if mime in EXPORTS:
            # A text/plain export of a Doc starts with a byte-order mark.
            q = urlencode({"mimeType": EXPORTS[mime]})
            raw = self._fetch_text(f"{API}/files/{fid}/export?{q}").lstrip("\ufeff")
            return clip(raw, BODY_CAP)
        if mime in _TEXT_MIMES or mime.startswith("text/"):
            try:
                size = int(f.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if 0 < size <= MAX_DOWNLOAD_BYTES:
                q = urlencode({"alt": "media", "supportsAllDrives": "true"})
                return clip(self._fetch_text(f"{API}/files/{fid}?{q}"), BODY_CAP)
        return ""

    # --- mapping ----------------------------------------------------------------------
    @staticmethod
    def _person(u: Any) -> str:
        if not isinstance(u, dict):
            return ""
        return str(u.get("displayName") or u.get("emailAddress") or "").strip()

    def _item(self, f: dict, *, text: str) -> dict:
        name = clip(f.get("name"), 200) or "Untitled"
        mime = str(f.get("mimeType") or "")
        kind = "document" if mime in EXPORTS else "file"
        label = _TYPE_LABELS.get(mime) or (mime.rsplit("/", 1)[-1] if mime else "file")
        editor = self._person(f.get("lastModifyingUser"))
        owners = [p for p in (self._person(o) for o in (f.get("owners") or [])) if p]
        shared = bool(f.get("sharedWithMeTime"))
        desc = clip(f.get("description"), 300)
        bits = [f"Edited by {editor}" if editor else "",
                f"Owner {', '.join(owners)}" if owners and owners != [editor] else "",
                "shared with you" if shared else ""]
        head = f"Google Drive {label} {name}" + (f": {desc}" if desc else "")
        lines = [l for l in (head, " · ".join(b for b in bits if b),
                             str(f.get("webViewLink") or "")) if l]
        if text:
            lines += ["", text]
        people: list[str] = []
        for p in [editor] + owners:
            if p and p not in people:
                people.append(p)
        parents = [str(p) for p in (f.get("parents") or []) if p]
        ts = (parse_iso(f.get("modifiedTime")) or parse_iso(f.get("sharedWithMeTime"))
              or parse_iso(f.get("createdTime")))
        return {
            "kind": kind, "external_id": f"file:{f.get('id')}",
            "thread_id": parents[0] if parents else None,
            "ts": ts, "title": name, "text": "\n".join(lines),
            "people": people, "url": f.get("webViewLink"),
            "meta": {"mime_type": mime, "editor": editor, "owners": owners,
                     "shared_with_me": shared, "folder_id": parents[0] if parents else None,
                     "created_time": f.get("createdTime"),
                     "modified_time": f.get("modifiedTime")},
        }

    # --- background capture ---------------------------------------------------------
    def fetch_items(self, *, cursor: dict | None = None,
                    now: float | None = None) -> tuple[list[dict], dict]:
        from app.services.connectors.oauth_base import FETCH_OVERLAP_S
        now = float(now if now is not None else time.time())
        cursor = dict(cursor or {})
        since = self._since(cursor, now)
        files = self.list_changed(since=since)
        items: list[dict] = []
        budget = self._max_bodies() if self.content_scope_granted() else 0
        for f in files:
            text = ""
            wants_text = str(f.get("mimeType") or "") in EXPORTS \
                or str(f.get("mimeType") or "").startswith("text/") \
                or str(f.get("mimeType") or "") in _TEXT_MIMES
            if budget > 0 and wants_text:
                budget -= 1
                try:
                    text = self.file_text(f)
                except Exception as exc:
                    print(f"[connectors] google_drive file text skipped ({exc}).")
            items.append(self._item(f, text=text))
        self._note_fetch(items, now)
        return items, {**cursor, "since": now - FETCH_OVERLAP_S}


google_drive = GoogleDriveConnector()
