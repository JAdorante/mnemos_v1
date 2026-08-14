"""iCloud calendar sync (read-only) — the user's real schedule as memory.

Uses the credentials the guided connect flow stored (icloud_account) to walk
Apple's CalDAV chain — principal -> calendar home (a per-user pXX host) ->
calendars -> time-window REPORT — and lands each upcoming event as an
observed-tier memory event (source=phone.calendar), deduped by content hash so
re-syncs are idempotent and an edited event re-lands once. Chat grounding,
reflection, and anticipation then see the actual schedule, not just what was
overheard about it.

Read-only by construction: nothing here issues PUT/DELETE. Recurring events
are expanded server-side (CALDAV:expand) so instances carry real dates; if a
server rejects expand, the raw-master fallback still yields the events, just
with the series' original start.

v1 limits (documented, not hidden): removed events are not retracted from
memory (the old event stays as history); recurrence fallback shows master
dates; only VEVENT calendars are read (no Reminders — Apple took those off
CalDAV years ago).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from app.config import settings
from app.events import Event, Modality, bus
from app.services import confidence as _conf
from app.services import icloud_account

SOURCE = "phone.calendar"
ROOT = "https://caldav.icloud.com/"
_DAV = "{DAV:}"
_CAL = "{urn:ietf:params:xml:ns:caldav}"
_TS_FMT = "%Y%m%dT%H%M%SZ"

_lock = threading.Lock()
_thread: threading.Thread | None = None
_last: dict = {}   # {"ts", "ok", "calendars", "events_seen", "new", "error"}


def _state_path() -> Path:
    return Path(settings.icloud.state_path)


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --- CalDAV plumbing --------------------------------------------------------
def _req(method: str, url: str, auth: tuple, body: str, depth: str = "0"):
    return requests.request(
        method, url, auth=auth, data=body.encode("utf-8"),
        headers={"Depth": depth, "Content-Type": "text/xml; charset=utf-8"},
        timeout=30, allow_redirects=True)


def _href_of(elem) -> str:
    h = elem.find(f".//{_DAV}href")
    return (h.text or "").strip() if h is not None else ""


def discover(auth: tuple) -> tuple[str, list[dict]]:
    """principal -> home -> [{href, name}] of VEVENT-bearing calendars."""
    r = _req("PROPFIND", ROOT,
             auth, '<?xml version="1.0"?><propfind xmlns="DAV:">'
                   '<prop><current-user-principal/></prop></propfind>')
    r.raise_for_status()
    tree = ET.fromstring(r.text)
    principal = _href_of(tree.find(f".//{_DAV}current-user-principal"))
    if not principal:
        raise RuntimeError("no principal href in CalDAV response")
    base = re.match(r"(https://[^/]+)", r.url).group(1)

    r2 = _req("PROPFIND", base + principal,
              auth, '<?xml version="1.0"?><propfind xmlns="DAV:" '
                    'xmlns:c="urn:ietf:params:xml:ns:caldav">'
                    '<prop><c:calendar-home-set/></prop></propfind>')
    r2.raise_for_status()
    home = _href_of(ET.fromstring(r2.text).find(f".//{_CAL}calendar-home-set"))
    if not home:
        raise RuntimeError("no calendar-home-set in CalDAV response")
    home_url = home if home.startswith("http") else base + home
    home_base = re.match(r"(https://[^/]+)", home_url).group(1).replace(":443", "")

    r3 = _req("PROPFIND", home_url,
              auth, '<?xml version="1.0"?><propfind xmlns="DAV:">'
                    '<prop><displayname/><resourcetype/></prop></propfind>',
              depth="1")
    r3.raise_for_status()
    home_path = re.sub(r"^https://[^/]+", "", home_url).replace(":443", "")
    cals = []
    for resp in ET.fromstring(r3.text).findall(f"{_DAV}response"):
        href = _href_of(resp)
        name_el = resp.find(f".//{_DAV}displayname")
        name = (name_el.text or "").strip() if name_el is not None else ""
        if not href or href == home_path:
            continue
        # Scheduling in/outboxes and notification folders are not calendars.
        if href.rstrip("/").endswith(("inbox", "outbox", "notification")):
            continue
        if resp.find(f".//{_DAV}resourcetype/{_CAL}calendar") is None:
            continue
        cals.append({"href": href, "name": name or href.rstrip("/").split("/")[-1]})
    return home_base, cals


def _query_body(start: str, end: str, expand: bool) -> str:
    exp = f'<c:expand start="{start}" end="{end}"/>' if expand else ""
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<c:calendar-query xmlns:d="DAV:" '
            'xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f'<d:prop><c:calendar-data>{exp}</c:calendar-data></d:prop>'
            '<c:filter><c:comp-filter name="VCALENDAR">'
            '<c:comp-filter name="VEVENT">'
            f'<c:time-range start="{start}" end="{end}"/>'
            '</c:comp-filter></c:comp-filter></c:filter>'
            '</c:calendar-query>')


def query_events(base: str, cal: dict, auth: tuple,
                 start: dt.datetime, end: dt.datetime) -> list[dict]:
    """Window-query one calendar; expand recurrences, fall back to masters."""
    s, e = start.strftime(_TS_FMT), end.strftime(_TS_FMT)
    r = _req("REPORT", base + cal["href"], auth, _query_body(s, e, True), "1")
    if r.status_code not in (200, 207):
        r = _req("REPORT", base + cal["href"], auth, _query_body(s, e, False), "1")
        if r.status_code not in (200, 207):
            raise RuntimeError(f"calendar-query failed ({r.status_code}) "
                               f"for {cal['name']!r}")
    out = []
    for block in _ics_blocks(r.text):
        ev = parse_vevent(block)
        if ev:
            ev["calendar"] = cal["name"]
            out.append(ev)
    return out


# --- iCalendar parsing (minimal, tested) ------------------------------------
def _unfold(text: str) -> str:
    """RFC 5545 line unfolding: a line starting with space/tab continues."""
    return re.sub(r"\r?\n[ \t]", "", text)


def _ics_blocks(text: str) -> list[str]:
    return re.findall(r"BEGIN:VEVENT.*?END:VEVENT", _unfold(text), re.S)


def _parse_dt(prop: str, value: str) -> tuple[dt.datetime | dt.date | None, bool]:
    """('DTSTART;TZID=US/Pacific', '20260718T090000') -> (aware dt | date, all_day)."""
    value = value.strip()
    if "VALUE=DATE" in prop or re.fullmatch(r"\d{8}", value):
        try:
            return dt.datetime.strptime(value, "%Y%m%d").date(), True
        except ValueError:
            return None, False
    tz = None
    m = re.search(r"TZID=([^;:]+)", prop)
    if m:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m.group(1).strip())
        except Exception:
            tz = None
    try:
        if value.endswith("Z"):
            return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=dt.timezone.utc), False
        parsed = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
        return (parsed.replace(tzinfo=tz) if tz else parsed), False
    except ValueError:
        return None, False


def _parse_cal_address(head: str, val: str) -> dict:
    """ATTENDEE/ORGANIZER line → {name, email}."""
    cn = ""
    m = re.search(r"CN=([^;:]+)", head or "", re.I)
    if m:
        cn = m.group(1).strip().strip('"')
        # RFC 5545 escaped commas in CN
        cn = cn.replace("\\,", ",").replace("\\;", ";")
    email = (val or "").strip()
    if email.lower().startswith("mailto:"):
        email = email[7:]
    # Drop angle brackets some servers emit
    email = email.strip("<>").strip()
    return {"name": cn, "email": email}


def parse_vevent(block: str) -> dict | None:
    """One unfolded VEVENT -> {uid, summary, start, end, all_day, location,
    organizer, attendees}.

    Attendees/organizer feed Meeting Layer P1 session join + people priors.
    """
    props: dict[str, tuple[str, str]] = {}
    attendees: list[dict] = []
    organizer: dict | None = None
    for line in block.splitlines():
        if ":" not in line:
            continue
        head, _, val = line.partition(":")
        name = head.split(";", 1)[0].upper()
        if name in ("UID", "SUMMARY", "LOCATION", "DTSTART", "DTEND",
                    "RECURRENCE-ID", "STATUS", "DESCRIPTION", "URL"):
            props[name] = (head, val)
        elif name == "ATTENDEE":
            a = _parse_cal_address(head, val)
            if a.get("email") or a.get("name"):
                attendees.append(a)
        elif name == "ORGANIZER":
            organizer = _parse_cal_address(head, val)
    if props.get("STATUS", ("", ""))[1].strip().upper() == "CANCELLED":
        return None
    start, all_day = _parse_dt(*props.get("DTSTART", ("", "")))
    if start is None:
        return None
    end, _ = _parse_dt(*props.get("DTEND", ("", "")))
    uid = props.get("UID", ("", ""))[1].strip()
    rec = props.get("RECURRENCE-ID", ("", ""))[1].strip()
    location = props.get("LOCATION", ("", ""))[1].strip()
    description = (props.get("DESCRIPTION", ("", ""))[1] or "").replace("\\n", "\n")
    url_prop = props.get("URL", ("", ""))[1].strip()
    join_url, provider = "", "unknown"
    try:
        from app.services.meeting_session import extract_conference_link
        join_url, provider = extract_conference_link(url_prop, location, description)
    except Exception:
        pass
    return {
        "uid": uid + (f"#{rec}" if rec else ""),
        "summary": props.get("SUMMARY", ("", ""))[1].strip() or "(untitled)",
        "location": location,
        "description": description,
        "url": url_prop,
        "join_url": join_url,
        "provider": provider,
        "start": start, "end": end, "all_day": all_day,
        "organizer": organizer,
        "attendees": attendees,
    }


# --- event text + sync ------------------------------------------------------
def _when_text(ev: dict) -> str:
    s = ev["start"]
    if ev["all_day"]:
        return s.strftime("%a %b %d (all day)")
    txt = s.strftime("%a %b %d %H:%M")
    e = ev.get("end")
    if isinstance(e, dt.datetime):
        txt += "-" + e.strftime("%H:%M")
    return txt


def _fingerprint(ev: dict) -> tuple[str, str]:
    """(stable key, content hash) — hash change means the event was edited."""
    key = f"{ev['calendar']}|{ev['uid']}"
    blob = json.dumps([
        ev["summary"], str(ev["start"]), str(ev.get("end")),
        ev["location"], ev["all_day"],
        ev.get("organizer"), ev.get("attendees") or [],
        ev.get("join_url") or "", ev.get("url") or "",
    ], sort_keys=True)
    return key, hashlib.sha1(blob.encode("utf-8")).hexdigest()


def sync(publish=None) -> dict:
    """One sync pass. Returns {ok, calendars, events_seen, new, error?}."""
    global _last
    if not settings.icloud.sync_enabled:
        return {"ok": False, "error": "sync disabled (QUILL_ICLOUD_SYNC=0)"}
    user, pwd = icloud_account._read_saved()
    if not (user and pwd):
        return {"ok": False, "error": "iCloud not connected"}
    auth = (user, pwd)
    publish = publish or bus.publish_nowait
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=settings.icloud.past_days)
    end = now + dt.timedelta(days=settings.icloud.ahead_days)
    try:
        base, cals = discover(auth)
        seen = 0
        fresh = 0
        state = _load_state()
        known = state.get("hashes") or {}
        # Meeting Layer P1: keep calendar_events index warm even when the
        # memory-event fingerprint is unchanged (attendee priors need it).
        try:
            from app.storage import get_store
            from app.services import meeting_join as _mj
            _cal_store = get_store()
        except Exception:
            _cal_store = None
            _mj = None  # type: ignore

        for cal in cals:
            for ev in query_events(base, cal, auth, start, end):
                seen += 1
                if _cal_store is not None and _mj is not None:
                    try:
                        _mj.upsert_from_sync_event(_cal_store, ev)
                    except Exception:
                        pass
                key, digest = _fingerprint(ev)
                if known.get(key) == digest:
                    continue
                known[key] = digest
                fresh += 1
                when = _when_text(ev)
                text = (f"Calendar ({ev['calendar']}): {ev['summary']} — {when}"
                        + (f" @ {ev['location']}" if ev["location"] else ""))
                out = Event(time=time.time(), modality=Modality.SYSTEM,
                            raw=text, summary=f"[calendar] {text}", source=SOURCE,
                            meta={"origin": "icloud", "calendar": ev["calendar"],
                                  "uid": ev["uid"], "start": str(ev["start"]),
                                  "end": str(ev.get("end") or ""),
                                  "all_day": ev["all_day"],
                                  "summary": ev["summary"],
                                  "location": ev.get("location") or "",
                                  "organizer": ev.get("organizer"),
                                  "attendees": ev.get("attendees") or [],
                                  "join_url": ev.get("join_url") or "",
                                  "provider": ev.get("provider") or "",
                                  "description": (ev.get("description") or "")[:2000],
                                  "url": ev.get("url") or ""})
                _conf.attach(out, _conf.OBSERVED)
                publish(out)
        if fresh:
            state["hashes"] = known
        state["last_sync"] = time.time()
        _save_state(state)
        _last = {"ts": time.time(), "ok": True, "calendars": len(cals),
                 "events_seen": seen, "new": fresh}
        if fresh:
            print(f"[icloud_calendar] sync: {fresh} new/changed of {seen} "
                  f"events in window ({len(cals)} calendars).")
        return {"ok": True, **{k: _last[k] for k in
                               ("calendars", "events_seen", "new")}}
    except Exception as exc:
        _last = {"ts": time.time(), "ok": False, "error": str(exc)}
        print(f"[icloud_calendar] sync failed ({exc}).")
        return {"ok": False, "error": str(exc)}


# --- write-back (create / delete) -------------------------------------------
# CalDAV PUT/DELETE. Deliberately NARROW: solo personal events only — this never
# writes an ATTENDEE line, so it can't email or invite anyone. Each write is a
# human-initiated command (the caller IS the approval), never autonomous.
def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(
        ",", "\\,").replace("\n", "\\n").replace("\r", "")


def _to_utc(value: str) -> dt.datetime:
    """Parse an ISO datetime; a naive one is read as this machine's local time
    (DST-correct for that date) and converted to UTC."""
    d = dt.datetime.fromisoformat(value)
    if d.tzinfo is None:
        d = d.astimezone()          # attach local tz for that date
    return d.astimezone(dt.timezone.utc)


def _find_calendar(cals: list[dict], name: str) -> dict | None:
    want = (name or "").strip().lower()
    for c in cals:
        if c["name"].lower() == want:
            return c
    return None


def _uid() -> str:
    # No Date.now/random constraints here (normal runtime); uuid is fine.
    import uuid
    return "mnemos-" + uuid.uuid4().hex


def build_ics(uid: str, summary: str, start: str, end: str | None,
              duration_min: int, location: str, all_day: bool,
              stamp: dt.datetime) -> str:
    """Compose a minimal single-VEVENT iCalendar object (no attendees)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//mnemos//calendar//EN", "BEGIN:VEVENT",
             f"UID:{uid}",
             f"DTSTAMP:{stamp.strftime(_TS_FMT)}",
             f"SUMMARY:{_ics_escape(summary)}"]
    if location:
        lines.append(f"LOCATION:{_ics_escape(location)}")
    if all_day:
        d0 = dt.date.fromisoformat(start[:10])
        d1 = (dt.date.fromisoformat(end[:10]) if end
              else d0 + dt.timedelta(days=1))
        lines.append(f"DTSTART;VALUE=DATE:{d0.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{d1.strftime('%Y%m%d')}")
    else:
        s = _to_utc(start)
        e = _to_utc(end) if end else s + dt.timedelta(minutes=max(1, duration_min))
        lines.append(f"DTSTART:{s.strftime(_TS_FMT)}")
        lines.append(f"DTEND:{e.strftime(_TS_FMT)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def get_event(href_or_uid: str, calendar: str = "Home") -> dict:
    """CalDAV GET read-back by href or bare UID (plan 5.1)."""
    href_or_uid = (href_or_uid or "").strip()
    if not href_or_uid:
        return {"ok": False, "error": "href/uid required"}
    user, pwd = icloud_account._read_saved()
    if not (user and pwd):
        return {"ok": False, "error": "iCloud not connected"}
    auth = (user, pwd)
    try:
        base, cals = discover(auth)
        if href_or_uid.endswith(".ics") and "/" in href_or_uid:
            url = base + href_or_uid if not href_or_uid.startswith("http") \
                else href_or_uid
            href = href_or_uid
        else:
            cal = _find_calendar(cals, calendar) or (cals[0] if cals else None)
            if cal is None:
                return {"ok": False, "error": "calendar not found"}
            href = cal["href"] + href_or_uid + ".ics"
            url = base + href
        r = requests.get(url, auth=auth, timeout=30,
                         headers={"Accept": "text/calendar"})
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "href": href,
                    "error": f"GET HTTP {r.status_code}"}
        body = r.text or ""
        uid_m = re.search(r"(?im)^UID:(.+)$", body)
        uid = (uid_m.group(1).strip() if uid_m else href_or_uid)
        return {"ok": True, "uid": uid, "href": href, "status": 200,
                "ics": body[:4000]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def create_event(summary: str, start: str, *, end: str | None = None,
                 duration_min: int = 60, calendar: str = "Home",
                 location: str = "", all_day: bool = False) -> dict:
    """Create one personal event (no attendees) via CalDAV PUT. Human-initiated.

    Plan 5.1: after PUT, GET the event by href — `verified` only when read-back
    succeeds; otherwise `outcome_uncertain` with the PUT still ok.
    """
    if not settings.icloud.sync_enabled:
        return {"ok": False, "error": "iCloud sync disabled"}
    user, pwd = icloud_account._read_saved()
    if not (user and pwd):
        return {"ok": False, "error": "iCloud not connected"}
    summary = (summary or "").strip()
    if not summary:
        return {"ok": False, "error": "summary is required"}
    if not start:
        return {"ok": False, "error": "start is required"}
    auth = (user, pwd)
    try:
        base, cals = discover(auth)
        cal = _find_calendar(cals, calendar) or _find_calendar(cals, "Home") \
            or (cals[0] if cals else None)
        if cal is None:
            return {"ok": False, "error": "no writable calendar found"}
        uid = _uid()
        stamp = dt.datetime.now(dt.timezone.utc)
        ics = build_ics(uid, summary, start, end, duration_min, location,
                        all_day, stamp)
        href = cal["href"] + uid + ".ics"
        url = base + href
        r = requests.put(url, auth=auth, data=ics.encode("utf-8"),
                         headers={"Content-Type": "text/calendar; charset=utf-8",
                                  "If-None-Match": "*"},
                         timeout=30)
        if r.status_code not in (200, 201, 204):
            return {"ok": False,
                    "error": f"calendar rejected the write (HTTP {r.status_code})"}
        when = (start if all_day else str(_to_utc(start).astimezone()))
        print(f"[icloud_calendar] created event {summary!r} in {cal['name']}")
        out = {"ok": True, "uid": uid, "calendar": cal["name"],
               "summary": summary, "when": when, "href": href}
        # Evidence-anchored read-back (plan 5.1)
        try:
            from app.services import outcome_verify as ov
            ev = ov.verify_calendar_event(href, uid=uid, calendar=cal["name"])
            out["verify"] = ev.as_dict()
            out["step_status"] = ev.status
        except Exception as exc:
            out["verify"] = {"ok": False, "source": "calendar_get",
                             "note": str(exc), "status": "outcome_uncertain"}
            out["step_status"] = "outcome_uncertain"
        return out
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def delete_event(href_or_uid: str, calendar: str = "Home") -> dict:
    """Delete an event by its href (from create_event) or bare UID. Best-effort."""
    user, pwd = icloud_account._read_saved()
    if not (user and pwd):
        return {"ok": False, "error": "iCloud not connected"}
    auth = (user, pwd)
    try:
        base, cals = discover(auth)
        if href_or_uid.endswith(".ics") and "/" in href_or_uid:
            url = base + href_or_uid
        else:
            cal = _find_calendar(cals, calendar) or (cals[0] if cals else None)
            if cal is None:
                return {"ok": False, "error": "calendar not found"}
            url = base + cal["href"] + href_or_uid + ".ics"
        r = requests.delete(url, auth=auth, timeout=30)
        ok = r.status_code in (200, 204, 404)
        return {"ok": ok, "status": r.status_code}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def sync_status() -> dict:
    st = _load_state()
    return {"enabled": settings.icloud.sync_enabled,
            "connected": icloud_account.status()["connected"],
            "last_sync": st.get("last_sync"),
            "last_result": dict(_last),
            "interval_s": settings.icloud.sync_interval_s}


def start_background() -> bool:
    """Periodic sync thread — one per process, best-effort, quiet when idle."""
    global _thread
    if not settings.icloud.sync_enabled:
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return True

        def _loop() -> None:
            while True:
                if icloud_account.status()["connected"]:
                    sync()
                time.sleep(max(300.0, float(settings.icloud.sync_interval_s)))

        _thread = threading.Thread(target=_loop, name="icloud-calendar-sync",
                                   daemon=True)
        _thread.start()
    return True
