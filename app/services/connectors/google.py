"""Google connector — Gmail + Calendar metadata via exhaust_ingest."""
from __future__ import annotations

import threading
from typing import Any

from app.services.connectors.base import oauth_redirect_base, public_base_url


class GoogleConnector:
    id = "google"
    label = "Google (Gmail + Calendar)"
    tool_names = ("Gmail", "Google Calendar")
    availability = "ready"
    kind = "directory"
    category = "calendar"
    # Event/fact source prefixes this connector contributes to memory; the
    # per-chat toggle (connectors/session.py) reads these off the registry so
    # a new connector needs no edit to session.py.
    source_prefixes = ("exhaust.gmail", "exhaust.calendar",
                       "google.mail", "google.calendar")
    # Background sync (connectors/scheduler.py): metadata only — headers and
    # subjects for mail, titles and attendees for calendar. Never a body.
    sync_interval_s = 300
    sync_blurb = ("new Gmail message headers (sender, recipients, subject) "
                  "and Google Calendar events")
    description = (
        "Read-only Gmail headers and Google Calendar events. "
        "You sign in on Google's screen — Sparrow never sees your password."
    )
    capabilities = (
        "Import contacts from recent mail headers",
        "Import upcoming and recent calendar events",
        "Seed People and your next-meeting view",
    )

    def configured(self) -> bool:
        from app.services import exhaust_ingest as ex
        return ex.oauth_configured()

    def connected(self) -> bool:
        from app.services import exhaust_ingest as ex
        return ex.connected()

    def status(self) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        st = ex.status()
        return {
            "id": self.id,
            "label": self.label,
            "tool_names": list(self.tool_names),
            "availability": self.availability,
            "kind": self.kind,
            "category": self.category,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "configured": st.get("oauth_configured", False),
            "connected": st.get("connected", False),
            "enabled": st.get("enabled", True),
            "days": st.get("days"),
            "scopes": st.get("scopes"),
            "progress": st.get("progress"),
            "ledger": st.get("ledger"),
            "oauth_mode": "redirect" if public_base_url() else "loopback",
            "public_base": public_base_url(),
            "redirect_base": oauth_redirect_base(),
            "error": None,
        }

    def begin_connect(self, *, public_base: str | None = None,
                      return_path: str | None = None) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        base = (public_base or "").strip().rstrip("/") or public_base_url()
        if base and base.lower().startswith("https://"):
            return ex.start_oauth_redirect(
                base, redirect_base=oauth_redirect_base(),
                return_path=return_path)
        # Desktop / no public HTTPS — existing loopback (blocks until done).
        result = ex.start_oauth_loopback()
        if result.get("ok"):
            result = {**result, "mode": "loopback"}
        return result

    def complete_connect(self, code: str, state: str,
                         *, redirect_uri: str) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        return ex.complete_oauth_redirect(code, state, redirect_uri=redirect_uri)

    def sync(self) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        if not ex.connected():
            return {"ok": False, "error": "not connected"}
        if ex.progress().get("running"):
            return {"ok": True, "running": True, **ex.progress()}

        def _worker():
            ex.run_ingest(fetch=True)

        t = threading.Thread(target=_worker, name="exhaust-ingest", daemon=True)
        t.start()
        return {"ok": True, "started": True}

    def disconnect(self) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        return ex.clear_tokens()

    # --- background capture (spec F1) --------------------------------------
    def fetch_items(self, *, cursor: dict | None = None,
                    now: float | None = None) -> tuple[list[dict], dict]:
        """Items since the cursor's `since` timestamp (first run: a bounded
        backfill). Mail items are metadata-only SYSTEM events — headers and
        subject, no body, same gmail.metadata scope; calendar items carry
        start/end/attendees so the completion detector can read a call back."""
        import os
        import time as _time
        from app.services import exhaust_ingest as ex
        now = float(now if now is not None else _time.time())
        cursor = dict(cursor or {})
        backfill_days = float(os.environ.get("QUILL_CONNECTOR_BACKFILL_DAYS", "7"))
        since = cursor.get("since")
        try:
            since = float(since) if since is not None else now - backfill_days * 86400
        except (TypeError, ValueError):
            since = now - backfill_days * 86400
        # Overlap the window by an hour so a message that landed while we
        # were fetching is not missed; the ledger dedupes the overlap.
        window_days = max(1.0 / 24.0, (now - since + 3600) / 86400)
        items: list[dict] = []
        for m in ex.fetch_gmail_headers(days=window_days, now=now,
                                        include_subject=True):
            items.append(self._mail_item(m))
        for e in ex.fetch_calendar_events(days=window_days, now=now):
            items.append(self._calendar_item(e))
        return items, {**cursor, "since": now}

    @staticmethod
    def _addr_names(raw: str) -> list[str]:
        from app.services import exhaust_ingest as ex
        out = []
        for a in ex.parse_rfc2822_addr(raw or ""):
            name = (a.get("name") or "").strip() or (a.get("email") or "")
            if name and name not in out:
                out.append(name)
        return out

    def _mail_item(self, m: dict) -> dict:
        h = m.get("headers") or {}
        subject = (h.get("subject") or "").strip()
        frm = h.get("from") or ""
        to = h.get("to") or ""
        cc = h.get("cc") or ""
        people = self._addr_names(frm) + self._addr_names(to) + self._addr_names(cc)
        lines = [f"From: {frm}" if frm else "", f"To: {to}" if to else "",
                 f"Cc: {cc}" if cc else "", f"Subject: {subject}" if subject else ""]
        return {"kind": "mail", "external_id": m.get("id"),
                "thread_id": m.get("thread_id"), "ts": m.get("ts"),
                "title": subject, "text": "\n".join(l for l in lines if l),
                "people": people, "from": frm, "to": to, "cc": cc}

    def _calendar_item(self, e: dict) -> dict:
        attendees = [a.get("name") or a.get("email") for a in (e.get("attendees") or [])
                     if (a.get("name") or a.get("email"))]
        org = e.get("organizer") or {}
        title = (e.get("title") or "").strip()
        return {"kind": "calendar", "external_id": e.get("id"), "ts": e.get("start"),
                "title": title, "text": f"{title} ({', '.join(attendees)})" if attendees
                else title, "people": attendees, "start": e.get("start"),
                "end": e.get("end"), "attendees": e.get("attendees") or [],
                "organizer": org, "summary": title}

    def lookup(self, need: str, *, timeout_s: float = 10.0) -> list[dict]:
        """Option A search assist: recent mail whose subject matches the need
        (metadata only). Bounded to this connected source."""
        import time as _time
        from app.services import exhaust_ingest as ex
        from app.services.slots import coverage
        deadline = _time.time() + float(timeout_s)
        out = []
        try:
            for m in ex.fetch_gmail_headers(days=30, include_subject=True):
                if _time.time() > deadline:
                    break
                item = self._mail_item(m)
                if coverage(need, item.get("title") or "") >= 0.6:
                    out.append(item)
        except Exception as exc:
            print(f"[connectors] google lookup skipped ({exc}).")
        return out[:10]

    def purge(self) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        return ex.purge()


google = GoogleConnector()
