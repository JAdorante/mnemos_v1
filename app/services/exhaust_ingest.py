"""Gmail + Calendar *metadata* cold-start for the people graph (Workstream 2).

Read-only OAuth (installed-app loopback). Headers and attendees only — never
bodies, never attachments. Derivation is pure (no LLM). People v2 mints
candidates + asserted ``works_at``; commitments/claims are policy-denied.

Ingest is one-shot plus ``POST /exhaust/refresh``. ``POST /exhaust/purge``
removes every exhaust-sourced event, edge, and person that exhaust created
and nobody else cited.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from app.config import settings
from app.events import Event, Modality
from app.services.people_pipeline import (
    _FREE_MAIL,
    ingest_email_network,
    org_from_email_domain,
    parse_email_parties,
)

SCOPES = (
    "https://www.googleapis.com/auth/gmail.metadata",
    "https://www.googleapis.com/auth/calendar.events.readonly",
)
SOURCE_GMAIL = "exhaust.gmail"
SOURCE_CAL = "exhaust.calendar"

_lock = threading.Lock()
_progress: dict[str, Any] = {
    "running": False, "contacts": 0, "events": 0, "messages": 0, "error": None,
}


def enabled() -> bool:
    return os.environ.get("QUILL_EXHAUST_INGEST", "1") not in (
        "0", "false", "False")


def _ledger_path() -> Path:
    return Path(settings.exhaust.ledger_path)


def _token_path() -> Path:
    return Path(settings.exhaust.token_path)


def _load_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (FileNotFoundError, ValueError, OSError):
        return default


def _save_json(path: Path, data) -> None:
    from app.atomic_json import write_json
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)


def progress() -> dict[str, Any]:
    with _lock:
        return dict(_progress)


def _set_progress(**kw) -> None:
    with _lock:
        _progress.update(kw)


# ---------------------------------------------------------------------------
# Pure derivation (unit-testable, no I/O)
# ---------------------------------------------------------------------------
def parse_rfc2822_addr(raw: str) -> list[dict[str, str]]:
    """Turn a From/To/Cc header into [{name, email}]."""
    from email.utils import getaddresses
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, email in getaddresses([raw or ""]):
        email = (email or "").strip().lower()
        if not email or "@" not in email or email in seen:
            continue
        seen.add(email)
        name = (name or "").strip(" \"'")
        if not name:
            local = email.split("@", 1)[0]
            name = re.sub(r"[._+]+", " ", local).title()
        out.append({"name": name, "email": email})
    return out


def header_blob(headers: dict[str, str]) -> str:
    """Reconstruct a From/To/Cc block so People v2's email parser can run."""
    lines = []
    for key in ("from", "to", "cc", "date", "message-id"):
        val = headers.get(key) or headers.get(key.title()) or ""
        if val:
            lines.append(f"{key.title()}: {val}")
    return "\n".join(lines)


def derive_contact_stats(
    messages: Iterable[dict],
    events: Iterable[dict],
    *,
    self_emails: Iterable[str] = (),
) -> dict[str, dict[str, Any]]:
    """Per-email: interaction_count, last_seen, direction_ratio, co_attendance.

    ``direction_ratio`` = outbound / max(total, 1) where outbound means this
    address appeared on From (we wrote) vs To/Cc (they wrote / we received).
    Calendar-only people have interaction_count 0 and co_attendance > 0.
    """
    self_set = {e.strip().lower() for e in self_emails if e}
    stats: dict[str, dict[str, Any]] = {}

    def row(email: str) -> dict[str, Any]:
        r = stats.get(email)
        if r is None:
            r = {
                "email": email, "name": "",
                "interaction_count": 0, "last_seen": 0.0,
                "outbound": 0, "inbound": 0, "direction_ratio": 0.0,
                "co_attendance": 0, "from_calendar": False,
                "message_ids": [], "event_ids": [],
            }
            stats[email] = r
        return r

    for msg in messages:
        headers = msg.get("headers") or {}
        ts = float(msg.get("ts") or 0)
        mid = str(msg.get("id") or "")
        from_p = parse_rfc2822_addr(headers.get("from") or "")
        to_p = parse_rfc2822_addr(
            " ".join(headers.get(k) or "" for k in ("to", "cc")))
        from_emails = {p["email"] for p in from_p}
        we_sent = bool(from_emails & self_set) if self_set else False
        parties = from_p + to_p
        for p in parties:
            if p["email"] in self_set:
                continue
            r = row(p["email"])
            if p.get("name") and (not r["name"] or len(p["name"]) > len(r["name"])):
                r["name"] = p["name"]
            r["interaction_count"] += 1
            r["last_seen"] = max(float(r["last_seen"] or 0), ts)
            if we_sent:
                r["outbound"] += 1
            else:
                r["inbound"] += 1
            if mid:
                r["message_ids"].append(mid)
        # If we don't know self, treat From as inbound (they wrote us).
        if not self_set:
            for p in from_p:
                row(p["email"])["inbound"] += 0  # already counted as interaction

    for ev in events:
        ts = float(ev.get("start") or ev.get("ts") or 0)
        eid = str(ev.get("id") or "")
        people = list(ev.get("attendees") or [])
        org = ev.get("organizer")
        if isinstance(org, dict) and org.get("email"):
            people = [org, *people]
        emails_here = []
        for p in people:
            email = (p.get("email") or "").strip().lower()
            if not email or email in self_set:
                continue
            r = row(email)
            if p.get("name") and (not r["name"] or len(p["name"]) > len(r["name"])):
                r["name"] = p["name"]
            r["from_calendar"] = True
            r["co_attendance"] += 1
            r["last_seen"] = max(float(r["last_seen"] or 0), ts)
            if eid:
                r["event_ids"].append(eid)
            emails_here.append(email)
        # pair-wise co-attendance is recorded later as edges; count is per person

    for r in stats.values():
        total = max(int(r["outbound"]) + int(r["inbound"]), 1)
        r["direction_ratio"] = round(int(r["outbound"]) / total, 4)
        r["interaction_strength"] = round(
            min(1.0, (0.04 * r["interaction_count"])
                + (0.08 * r["co_attendance"])),
            4,
        )
    return stats


def co_attendance_pairs(events: Iterable[dict], *,
                        self_emails: Iterable[str] = ()
                        ) -> dict[tuple[str, str], int]:
    """Undirected (email_a, email_b) -> shared meeting count. a < b."""
    self_set = {e.strip().lower() for e in self_emails if e}
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for ev in events:
        emails = []
        people = list(ev.get("attendees") or [])
        org = ev.get("organizer")
        if isinstance(org, dict) and org.get("email"):
            people = [org, *people]
        for p in people:
            email = (p.get("email") or "").strip().lower()
            if email and email not in self_set and email not in emails:
                emails.append(email)
        emails.sort()
        for i, a in enumerate(emails):
            for b in emails[i + 1:]:
                counts[(a, b)] += 1
    return dict(counts)


def assert_metadata_scopes(scope_string: str) -> None:
    """Refuse any grant that is not exactly the metadata/read-only pair."""
    got = {s.strip() for s in (scope_string or "").split() if s.strip()}
    allowed = set(SCOPES)
    extra = got - allowed
    if extra:
        raise PermissionError(
            f"OAuth grant includes disallowed scopes: {sorted(extra)}")
    missing = allowed - got
    if missing:
        raise PermissionError(
            f"OAuth grant missing required scopes: {sorted(missing)}")


# ---------------------------------------------------------------------------
# OAuth (installed-app, loopback, urllib only — no Google SDK)
# ---------------------------------------------------------------------------
def oauth_configured() -> bool:
    return bool(settings.exhaust.client_id and settings.exhaust.client_secret)


def _auth_url(redirect: str, state: str) -> str:
    q = urlencode({
        "client_id": settings.exhaust.client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return f"https://accounts.google.com/o/oauth2/v2/auth?{q}"


def _exchange_code(code: str, redirect: str) -> dict:
    body = urlencode({
        "code": code,
        "client_id": settings.exhaust.client_id,
        "client_secret": settings.exhaust.client_secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }).encode()
    req = Request("https://oauth2.googleapis.com/token", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert_metadata_scopes(data.get("scope") or " ".join(SCOPES))
    return data


def _refresh_token(refresh: str) -> dict:
    body = urlencode({
        "refresh_token": refresh,
        "client_id": settings.exhaust.client_id,
        "client_secret": settings.exhaust.client_secret,
        "grant_type": "refresh_token",
    }).encode()
    req = Request("https://oauth2.googleapis.com/token", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data


def load_tokens() -> dict:
    return _load_json(_token_path(), {})


def connected() -> bool:
    tok = load_tokens()
    return bool(tok.get("access_token") or tok.get("refresh_token"))


def start_oauth_loopback() -> dict[str, Any]:
    """Open the system browser and wait on localhost for the redirect.

    Blocks the calling thread (onboarding kicks this off in a worker).
    Degrades with a clear error when client id/secret are missing.
    """
    if not enabled():
        return {"ok": False, "error": "exhaust ingest disabled"}
    if not oauth_configured():
        return {"ok": False, "error": "GOOGLE_OAUTH_CLIENT_ID not set",
                "skip": True}
    import secrets
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import webbrowser

    state = secrets.token_urlsafe(16)
    result: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            result["code"] = (qs.get("code") or [None])[0]
            result["state"] = (qs.get("state") or [None])[0]
            result["error"] = (qs.get("error") or [None])[0]
            body = b"<html><body>You can close this tab.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # noqa: A003
            return

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    redirect = f"http://127.0.0.1:{port}/"
    try:
        webbrowser.open(_auth_url(redirect, state))
        httpd.handle_request()
    finally:
        httpd.server_close()
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    if result.get("state") != state:
        return {"ok": False, "error": "oauth state mismatch"}
    if not result.get("code"):
        return {"ok": False, "error": "no oauth code"}
    try:
        tokens = _exchange_code(result["code"], redirect)
    except PermissionError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"token exchange failed: {exc}"}
    tokens["obtained_at"] = time.time()
    _save_json(_token_path(), tokens)
    return {"ok": True, "scopes": tokens.get("scope")}


def _access_token() -> str:
    tok = load_tokens()
    if not tok:
        raise RuntimeError("not connected")
    obtained = float(tok.get("obtained_at") or 0)
    expires = float(tok.get("expires_in") or 3600)
    if tok.get("refresh_token") and (time.time() - obtained > expires - 60):
        fresh = _refresh_token(tok["refresh_token"])
        tok.update(fresh)
        tok["obtained_at"] = time.time()
        _save_json(_token_path(), tok)
    return str(tok["access_token"])


def _google_get(url: str) -> dict:
    req = Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {_access_token()}")
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_gmail_headers(*, days: int | None = None,
                        now: float | None = None) -> list[dict]:
    """Last N days of Gmail metadata (From/To/Cc/Date/Message-ID only)."""
    days = int(days if days is not None else settings.exhaust.days)
    ts = time.time() if now is None else float(now)
    after = int(ts - days * 86400)
    q = quote(f"after:{after}")
    out: list[dict] = []
    page = None
    while True:
        url = (
            "https://gmail.googleapis.com/gmail/v1/users/me/messages"
            f"?q={q}&maxResults=100"
        )
        if page:
            url += f"&pageToken={quote(page)}"
        data = _google_get(url)
        for m in data.get("messages") or []:
            mid = m.get("id")
            if not mid:
                continue
            got = _google_get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                f"{quote(mid)}?format=metadata"
                "&metadataHeaders=From&metadataHeaders=To"
                "&metadataHeaders=Cc&metadataHeaders=Date"
                "&metadataHeaders=Message-ID"
            )
            payload = got.get("payload") or {}
            headers = {
                (h.get("name") or "").lower(): (h.get("value") or "")
                for h in (payload.get("headers") or [])
                if (h.get("name") or "").lower() in (
                    "from", "to", "cc", "date", "message-id")
            }
            # Guard: Gmail metadata format must never include a body.
            if payload.get("body", {}).get("data") or payload.get("parts"):
                raise RuntimeError("gmail returned body data; aborting ingest")
            msg_ts = float(got.get("internalDate") or 0) / 1000.0
            if not msg_ts and headers.get("date"):
                try:
                    msg_ts = parsedate_to_datetime(headers["date"]).timestamp()
                except Exception:
                    msg_ts = ts
            out.append({"id": headers.get("message-id") or mid,
                        "headers": headers, "ts": msg_ts})
        page = data.get("nextPageToken")
        if not page:
            break
        if len(out) >= 5000:
            break
    return out


def fetch_calendar_events(*, days: int | None = None,
                          now: float | None = None) -> list[dict]:
    days = int(days if days is not None else settings.exhaust.days)
    ts = time.time() if now is None else float(now)
    tmin = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(ts - days * 86400))
    tmax = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(ts + 1 * 86400))
    url = (
        "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        f"?timeMin={quote(tmin)}&timeMax={quote(tmax)}&singleEvents=true"
        "&maxResults=250&fields=items(id,summary,start,end,attendees,"
        "organizer,recurrence,status),nextPageToken"
    )
    out: list[dict] = []
    page = None
    while True:
        page_url = url + (f"&pageToken={quote(page)}" if page else "")
        data = _google_get(page_url)
        for it in data.get("items") or []:
            if it.get("status") == "cancelled":
                continue
            start = _rfc3339(it.get("start") or {})
            end = _rfc3339(it.get("end") or {}) or start
            attendees = []
            for a in it.get("attendees") or []:
                email = (a.get("email") or "").lower()
                if not email:
                    continue
                attendees.append({
                    "email": email,
                    "name": a.get("displayName") or "",
                })
            org = it.get("organizer") or {}
            organizer = None
            if org.get("email"):
                organizer = {
                    "email": str(org["email"]).lower(),
                    "name": org.get("displayName") or "",
                }
            out.append({
                "id": it.get("id"),
                "title": it.get("summary") or "",
                "start": start, "end": end,
                "attendees": attendees,
                "organizer": organizer,
                "recurrence": it.get("recurrence") or [],
            })
        page = data.get("nextPageToken")
        if not page:
            break
    return out


def _rfc3339(block: dict) -> float:
    raw = block.get("dateTime") or block.get("date") or ""
    if not raw:
        return 0.0
    try:
        from datetime import datetime, timezone
        if len(raw) == 10:
            dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt.timestamp()
        raw = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Persist through existing People v2 + calendar_events
# ---------------------------------------------------------------------------
def _provenance_event(store, *, source: str, raw: str, summary: str,
                      ts: float, meta: dict) -> int:
    ev = Event(
        time=ts, modality=Modality.SYSTEM, raw=raw, summary=summary,
        source=source, confidence=0.9, meta={
            **meta, "epistemic": "observed", "exhaust": True,
        },
    )
    return int(store.insert(ev))


def apply_stats(
    store,
    stats: dict[str, dict[str, Any]],
    pairs: dict[tuple[str, str], int],
    *,
    messages: list[dict] | None = None,
    events: list[dict] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Mint people/orgs/edges. Returns ledger + counts."""
    from app.services import people_pipeline as pp

    ts = time.time() if now is None else float(now)
    ledger = _load_json(_ledger_path(), {"people": [], "events": [],
                                         "relations": [], "calendar_ids": []})
    people_ids: dict[str, int] = {}
    n_people = 0
    n_org = 0

    # One provenance event per source class (not per message — keep the
    # timeline readable). Individual message/event ids ride in meta + ledger.
    gmail_eid = _provenance_event(
        store, source=SOURCE_GMAIL,
        raw="Gmail metadata ingest (From/To/Cc/Date/Message-ID).",
        summary="Seeded people from email headers (no bodies).",
        ts=ts, meta={"n_messages": len(messages or [])},
    )
    cal_eid = _provenance_event(
        store, source=SOURCE_CAL,
        raw="Google Calendar metadata ingest (attendees/title/times).",
        summary="Seeded people from calendar attendees (no event bodies).",
        ts=ts, meta={"n_events": len(events or [])},
    )
    ledger["events"] = list(dict.fromkeys(
        list(ledger.get("events") or []) + [gmail_eid, cal_eid]))

    # Reuse People v2 email ingest on reconstructed header blobs (batched).
    if messages:
        blob = "\n\n".join(header_blob(m.get("headers") or {}) for m in messages)
        ingest_email_network(
            blob, store=store, event_id=gmail_eid,
            event_source=SOURCE_GMAIL, window="exhaust.gmail", now=ts)

    for email, st in stats.items():
        name = (st.get("name") or email.split("@")[0]).strip()
        pid = None
        try:
            pid = store.find_person_by_contact("email", email)
        except Exception:
            pid = None
        if not pid and name:
            res = pp.resolve_person_mention(
                name, store=store, event_id=cal_eid if st.get("from_calendar")
                else gmail_eid,
                event_source=SOURCE_CAL if st.get("from_calendar") else SOURCE_GMAIL,
                window="exhaust", text=f"From: {name} <{email}>",
                grammatical_role="exhaust_contact", now=ts,
                relationship_boost=0.8)
            pid = res.person_id
        if not pid:
            try:
                pid = store.insert_person(name, ts=st.get("last_seen") or ts,
                                          promotion_state="candidate")
            except Exception:
                continue
        people_ids[email] = int(pid)
        n_people += 1
        try:
            store.upsert_contact_point(
                person_id=int(pid), type_="email",
                value_display=email, value_normalized=email,
                confidence=0.95, attribution_method="exhaust_header",
                verification_status="attributed",
                source_event_id=cal_eid if st.get("from_calendar") else gmail_eid,
                evidence_quote=f"{name} <{email}>",
                discourse_role="exhaust",
                ts=st.get("last_seen") or ts, created_by="system",
                pipeline_version="exhaust_v1")
        except Exception:
            pass
        try:
            store.set_person_interaction_strength(
                int(pid), float(st.get("interaction_strength") or 0))
        except Exception:
            pass
        if st.get("from_calendar"):
            try:
                store.set_person_attr(
                    int(pid), "exhaust_from_calendar", "1",
                    None, st.get("last_seen") or ts)
            except Exception:
                pass
        org_name = org_from_email_domain(email)
        if org_name:
            try:
                eid = store.resolve_entity(org_name, "org", ts=ts)
                store.add_relation(
                    "person", int(pid), "works_at", "entity", int(eid),
                    origin="asserted", ts=ts,
                    source_event_id=gmail_eid,
                    quote=f"{name} <{email}>", source_class="exhaust")
                n_org += 1
                ledger.setdefault("relations", []).append(
                    ["person", int(pid), "works_at", "entity", int(eid)])
            except Exception:
                pass
        ledger.setdefault("people", []).append(int(pid))

    for (a, b), weight in pairs.items():
        pa, pb = people_ids.get(a), people_ids.get(b)
        if not pa or not pb:
            continue
        try:
            store.add_relation(
                "person", int(pa), "co_attended", "person", int(pb),
                weight=float(weight), origin="asserted", ts=ts,
                source_event_id=cal_eid, source_class="exhaust",
                quote=f"co-attended {weight} meeting(s)")
            store.add_relation(
                "person", int(pb), "co_attended", "person", int(pa),
                weight=float(weight), origin="asserted", ts=ts,
                source_event_id=cal_eid, source_class="exhaust")
            ledger.setdefault("relations", []).append(
                ["person", int(pa), "co_attended", "person", int(pb)])
        except Exception:
            pass

    for ev in events or []:
        try:
            store.upsert_calendar_event(
                event_id=str(ev.get("id")),
                calendar="google-primary",
                uid=str(ev.get("id")),
                title=ev.get("title") or "",
                start=float(ev.get("start") or ts),
                end=float(ev.get("end") or ev.get("start") or ts),
                all_day=False,
                organizer=ev.get("organizer"),
                attendees=ev.get("attendees") or [],
                source_event_id=cal_eid,
                updated_at=ts,
                provider="google",
            )
            ledger.setdefault("calendar_ids", []).append(str(ev.get("id")))
        except Exception:
            pass

    ledger["people"] = list(dict.fromkeys(int(x) for x in ledger.get("people") or []))
    _save_json(_ledger_path(), ledger)
    return {
        "ok": True,
        "people": n_people,
        "org_edges": n_org,
        "co_attended": len(pairs),
        "gmail_event_id": gmail_eid,
        "calendar_event_id": cal_eid,
    }


def _self_emails(store) -> list[str]:
    emails = []
    try:
        from app.services import self_profile
        pid = self_profile.self_person_id(store)
        if pid:
            for c in store.list_contact_points(int(pid)) or []:
                if (c.get("type") or c.get("type_")) == "email":
                    emails.append((c.get("value_normalized") or c.get("value") or "").lower())
    except Exception:
        pass
    try:
        ident = json.loads(Path(settings.onboarding.profile_path).read_text(
            encoding="utf-8"))
        for key in ("primary_email", "secondary_email"):
            v = ((ident.get("identity") or {}).get(key) or "").strip().lower()
            if v:
                emails.append(v)
    except Exception:
        pass
    return [e for e in emails if e]


def run_ingest(
    *,
    store=None,
    messages: list[dict] | None = None,
    events: list[dict] | None = None,
    fetch: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """One-shot ingest. ``fetch=False`` uses the provided fixtures (tests)."""
    if not enabled() and fetch:
        return {"ok": False, "error": "QUILL_EXHAUST_INGEST=0"}
    from app.storage import get_store
    store = store or get_store()
    _set_progress(running=True, contacts=0, events=0, messages=0, error=None)
    try:
        if fetch:
            if not connected():
                return {"ok": False, "error": "not connected", "need_oauth": True}
            messages = fetch_gmail_headers(now=now)
            _set_progress(messages=len(messages))
            events = fetch_calendar_events(now=now)
            _set_progress(events=len(events))
        messages = messages or []
        events = events or []
        self_emails = _self_emails(store)
        stats = derive_contact_stats(messages, events, self_emails=self_emails)
        pairs = co_attendance_pairs(events, self_emails=self_emails)
        _set_progress(contacts=len(stats), events=len(events),
                      messages=len(messages))
        out = apply_stats(store, stats, pairs, messages=messages,
                          events=events, now=now)
        _set_progress(running=False)
        return {**out, "contacts": len(stats),
                "messages": len(messages), "calendar_events": len(events)}
    except Exception as exc:
        _set_progress(running=False, error=str(exc))
        return {"ok": False, "error": str(exc)}


def purge(store=None) -> dict[str, Any]:
    """Remove exhaust-sourced rows/edges recorded in the ledger."""
    from app.storage import get_store
    store = store or get_store()
    ledger = _load_json(_ledger_path(), {})
    n_rel = 0
    n_people = 0
    n_events = 0
    for rel in ledger.get("relations") or []:
        try:
            if len(rel) >= 5:
                store.delete_relation(rel[0], int(rel[1]), rel[2], rel[3], int(rel[4]))
                n_rel += 1
        except Exception:
            pass
    for src in (SOURCE_GMAIL, SOURCE_CAL):
        try:
            dropped = store.purge_source(src)
            n_events += len(dropped.get("events") or [])
        except Exception:
            pass
    for pid in ledger.get("people") or []:
        try:
            # Only drop candidates that have no non-exhaust evidence.
            p = store.get_person(int(pid))
            if not p:
                continue
            attrs = store.person_attrs(int(pid))
            # Keep if the user confirmed/merged (has user attrs beyond exhaust).
            user_keys = {k for k in attrs if not str(k).startswith("exhaust")}
            if user_keys:
                store.clear_person_attr(int(pid), "exhaust_from_calendar")
                continue
            store.delete_person(int(pid))
            n_people += 1
        except Exception:
            pass
    for cid in ledger.get("calendar_ids") or []:
        try:
            with store._lock:
                store._conn.execute(
                    "DELETE FROM calendar_events WHERE id = ?", (str(cid),))
                store._conn.commit()
        except Exception:
            pass
    _save_json(_ledger_path(), {"people": [], "events": [],
                                "relations": [], "calendar_ids": []})
    return {"ok": True, "people": n_people, "relations": n_rel,
            "events": n_events}


def status() -> dict[str, Any]:
    return {
        "enabled": enabled(),
        "oauth_configured": oauth_configured(),
        "connected": connected(),
        "days": settings.exhaust.days,
        "scopes": list(SCOPES),
        "progress": progress(),
        "ledger": _ledger_path().is_file(),
    }
