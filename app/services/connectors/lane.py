"""Connector lane — answer mail/calendar questions from a connected
connector instead of driving the provider's web UI.

Why this exists (live, 2026-09-22): a user with the Google connector already
connected asked "check my gmail"; the router sent it to the browser agent,
which opened mail.google.com, hit Google's sign-in wall, and — on a hosted
seat whose browser is headless — asked three times for a sign-in nobody could
perform. The seat already had the inbox headers through the Gmail API.

The lane sits between the router and the browser: when the route names a
mail/calendar task for a site a connected connector covers, the connector's
own read-only fetch (`fetch_items`, same scopes as background capture:
headers and subjects, never a body) becomes context for a direct answer.
Nothing here writes to a provider, and a connector the user toggled off for
this conversation (connectors/session.py) is never consulted.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

# Web hosts each connector stands in for. A wall on one of these hosts is a
# wall the connector would have avoided — the orchestrator's stop message
# names the connector so the user's next move is concrete.
_HOSTS: dict[str, tuple[str, ...]] = {
    "google": ("mail.google.com", "gmail.com", "calendar.google.com",
               "accounts.google.com", "myaccount.google.com"),
    "outlook": ("outlook.live.com", "outlook.office.com",
                "outlook.office365.com", "login.microsoftonline.com",
                "login.live.com", "office.com", "www.office.com"),
}

# Router `site` values that name a provider (the router prompt suggests
# "gmail" / "calendar" / "crm" / "web"; models also write the brand).
_SITE_TO_CONNECTOR = (
    (("gmail", "google mail", "google calendar", "gcal", "google"), "google"),
    (("outlook", "office365", "office 365", "microsoft", "o365", "hotmail",
      "live.com", "exchange"), "outlook"),
)

# Mail/calendar-shaped intents. The router labels are free-form verb_noun
# strings (check_email, read_inbox, summarize_calendar, list_meetings…), so
# match on the noun with the verbs left open — an action that would WRITE
# (send/draft/reply/schedule) stays on its normal path, where the approval
# gate lives.
_MAIL_NOUN_RE = re.compile(
    r"(?i)\b(?:e-?mails?|mail|inbox|messages?)\b")
_CAL_NOUN_RE = re.compile(
    r"(?i)\b(?:calendar|meetings?|schedule|agenda|events?|appointments?)\b")
_WRITE_VERB_RE = re.compile(
    r"(?i)\b(?:send|draft|compose|reply|forward|write|schedule|book|create|"
    r"invite|delete|archive|move|cancel|update)\b")
_READ_VERB_RE = re.compile(
    r"(?i)\b(?:check|read|list|show|summar\w*|what|any|new|recent|latest|"
    r"scan|review|find|search|look|see|get|unread|today|tomorrow|this|next|"
    r"upcoming|inbox)\b")

DEFAULT_DAYS = 3.0
MAX_LINES = 60


def _norm(s: Any) -> str:
    return re.sub(r"[_\-]+", " ", str(s or "")).strip().lower()


def connector_for_host(host: str) -> dict | None:
    """{"id", "label", "connected"} for the connector that covers a web host."""
    h = (host or "").strip().lower()
    if not h:
        return None
    from app.services.connectors import registry
    for cid, hosts in _HOSTS.items():
        if any(h == x or h.endswith("." + x) for x in hosts):
            c = registry.get(cid)
            if c is None:
                return None
            try:
                connected = bool(c.connected())
            except Exception:
                connected = False
            return {"id": cid, "label": c.label, "connected": connected}
    return None


def wants(route: dict | None) -> dict | None:
    """Does this route describe a READ of mail or calendar that a connector
    could serve? Returns {"mail": bool, "calendar": bool, "site": id|None}
    or None when the browser is the right tool (writes, other sites)."""
    r = route or {}
    if r.get("surface") not in (None, "browser") and not r.get("requires_browser"):
        return None
    intent = _norm(r.get("intent"))
    site = _norm(r.get("site"))
    text = f"{intent} {site}"
    if _WRITE_VERB_RE.search(intent):
        return None
    site_id = None
    for names, cid in _SITE_TO_CONNECTOR:
        if any(n in site for n in names):
            site_id = cid
            break
    mail = bool(_MAIL_NOUN_RE.search(text)) or site in ("gmail", "outlook", "hotmail")
    cal = bool(_CAL_NOUN_RE.search(text)) or "calendar" in site
    if not (mail or cal):
        return None
    if not (_READ_VERB_RE.search(intent) or site_id or site):
        return None
    return {"mail": mail, "calendar": cal, "site": site_id}


def candidates(want: dict) -> list:
    """Connected connectors, active for this chat, that can serve `want`.
    A site-specific ask uses only that provider's connector."""
    from app.services.connectors import registry, session
    try:
        active = session.active_ids()
    except Exception:
        active = set()
    out = []
    for c in registry.all():
        if getattr(c, "availability", "") != "ready":
            continue
        if not callable(getattr(c, "fetch_items", None)):
            continue
        if want.get("site") and c.id != want["site"]:
            continue
        if c.id not in active:
            continue
        out.append(c)
    return out


def _fmt_ts(ts: Any) -> str:
    try:
        if isinstance(ts, str):
            return ts[:16].replace("T", " ")
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "?"


def _mail_line(it: dict) -> str:
    frm = (it.get("from") or "").strip()
    subj = (it.get("title") or "(no subject)").strip()
    return f"- {_fmt_ts(it.get('ts'))} · from {frm or '?'} · {subj}"


def _cal_line(it: dict) -> str:
    title = (it.get("title") or it.get("summary") or "(untitled)").strip()
    who = ", ".join(str(p) for p in (it.get("people") or [])[:6])
    span = _fmt_ts(it.get("start") or it.get("ts"))
    end = it.get("end")
    if end:
        span += f" → {_fmt_ts(end)}"
    return f"- {span} · {title}" + (f" · with {who}" if who else "")


def _sort_key(it: dict) -> float:
    ts = it.get("ts") or it.get("start") or 0
    if isinstance(ts, str):
        try:
            import datetime as _dt
            return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _days() -> float:
    try:
        return max(0.5, float(os.environ.get("QUILL_CONNECTOR_LANE_DAYS", DEFAULT_DAYS)))
    except ValueError:
        return DEFAULT_DAYS


def _stored_items(cid: str, since: float) -> list[dict]:
    """Fallback when the live fetch fails: what background capture already
    landed (connectors/scheduler.py: source '<connector>.<kind>')."""
    try:
        from app.storage import store
        rows = store.recent_events(source_substr=f"{cid}.", limit=200, since=since)
    except Exception:
        return []
    out = []
    for r in rows:
        meta = r.get("meta") or {}
        kind = "calendar" if r.get("source", "").endswith(".calendar") else "mail"
        out.append({"kind": kind, "ts": r.get("time"),
                    "title": meta.get("title") or r.get("summary") or "",
                    "from": meta.get("from") or "", "people": meta.get("people") or [],
                    "start": meta.get("start"), "end": meta.get("end")})
    return out


def context_block(route: dict | None, *, now: float | None = None) -> tuple[str, dict] | None:
    """The connector-served context for a mail/calendar read, or None when
    the browser should handle the route after all.

    Returns (block_text, meta) where meta = {"ids", "labels", "mail",
    "calendar", "days", "errors"}."""
    want = wants(route)
    if not want:
        return None
    conns = candidates(want)
    if not conns:
        return None
    now = float(now if now is not None else time.time())
    days = _days()
    since = now - days * 86400
    mail: list[dict] = []
    cal: list[dict] = []
    errors: list[str] = []
    for c in conns:
        try:
            items, _cursor = c.fetch_items(cursor={"since": since}, now=now)
        except Exception as exc:
            errors.append(f"{c.label}: {type(exc).__name__}: {str(exc)[:120]}")
            items = _stored_items(c.id, since)
        for it in items or []:
            (cal if it.get("kind") == "calendar" else mail).append(it)
    mail.sort(key=_sort_key, reverse=True)
    cal.sort(key=_sort_key)
    lines = [f"CONNECTOR DATA (read-only, last {days:g} days, via "
             f"{', '.join(c.label for c in conns)}; headers and titles only — "
             "no message bodies are available, say so if asked for one):"]
    if want["mail"]:
        lines.append(f"Mail ({len(mail)} messages, newest first):")
        lines += [_mail_line(it) for it in mail[:MAX_LINES]] or ["- (none in window)"]
        if len(mail) > MAX_LINES:
            lines.append(f"- … {len(mail) - MAX_LINES} older messages omitted")
    if want["calendar"]:
        lines.append(f"Calendar ({len(cal)} events):")
        lines += [_cal_line(it) for it in cal[:MAX_LINES]] or ["- (none in window)"]
    if errors:
        lines.append("Fetch problems (tell the user plainly): " + "; ".join(errors))
    meta = {"ids": [c.id for c in conns], "labels": [c.label for c in conns],
            "mail": len(mail), "calendar": len(cal), "days": days,
            "errors": errors}
    return "\n\n" + "\n".join(lines) + "\n", meta
