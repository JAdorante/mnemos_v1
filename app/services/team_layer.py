"""Team-layer on top of the peer channel.

Named groups of *already paired* peers, relationship policy packs, an offline
mailbox, presence, shared loop IDs on handoffs, and meeting-attendee pairing
offers.

This does not share memory. Every ask still goes through peer_channel's
disclosure gate; each member answers from their own store. The coordinator-
shaped pieces here are local: a directory of groups you created, plus
reachability so a closed laptop does not drop the question.

See services/peer_channel.py for pairing, tokens, and the ask/answer path.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import settings

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None

# Relationship templates. `personal` is never auto (enforced again at write).
POLICY_PACKS: dict[str, dict[str, str]] = {
    "teammate": {
        "availability": "offer", "work": "offer", "contact": "offer",
        "personal": "offer", "other": "offer",
    },
    "manager": {
        "availability": "auto", "work": "auto", "contact": "offer",
        "personal": "offer", "other": "offer",
    },
    "company": {
        "availability": "offer", "work": "deny", "contact": "deny",
        "personal": "deny", "other": "deny",
    },
    "vendor": {
        "availability": "deny", "work": "deny", "contact": "deny",
        "personal": "deny", "other": "deny",
    },
}

PACK_BLURB = {
    "teammate": "Same squad — ask you on every topic (the default).",
    "manager": "Auto-share schedule and work; still ask on contact/personal.",
    "company": "Company directory — free/busy only; everything else declined.",
    "vendor": "Decline everything automatically.",
}

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_ASK_HASH_RE = re.compile(
    r"^\s*ask\s+#(?P<slug>[A-Za-z][\w-]{0,40})\s*[:,]\s*(?P<q>.{3,})$", re.I)
_ASK_THE_TEAM_RE = re.compile(
    r"^\s*ask\s+(?:the\s+)?(?P<who>.{1,40}?)\s+team\s*[:,]\s*(?P<q>.{3,})$",
    re.I)
_ASK_HASH_TO_RE = re.compile(
    r"^\s*ask\s+#(?P<slug>[A-Za-z][\w-]{0,40})\s+to\s+(?P<q>.{3,})$", re.I)
_ASK_TEAM_TO_RE = re.compile(
    r"^\s*ask\s+(?:the\s+)?(?P<who>.{1,40}?)\s+team\s+to\s+(?P<q>.{3,})$",
    re.I)


def _peer_cfg():
    return getattr(settings, "peer", None)


def _path(attr: str, default_name: str) -> Path:
    cfg = _peer_cfg()
    try:
        p = getattr(cfg, attr, None) if cfg is not None else None
        if p:
            return Path(str(p))
    except Exception:
        pass
    data = Path(getattr(settings, "data_dir", None)
                or __import__("os").environ.get("QUILL_DATA_DIR", "data"))
    return data / default_name


def _teams_path() -> Path:
    return _path("teams_path", "peer_teams.json")


def _mailbox_path() -> Path:
    return _path("mailbox_path", "peer_mailbox.json")


def _loops_path() -> Path:
    return _path("loops_path", "peer_loops.json")


def _load(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (FileNotFoundError, ValueError, OSError):
        return default


def _save(path: Path, data) -> None:
    from app.atomic_json import write_json
    write_json(path, data)


# --- TLS / URL guard --------------------------------------------------------
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def url_transport(url: str) -> dict[str, Any]:
    """Whether a peer callback URL is acceptable.

    HTTPS or loopback is always fine. Non-local HTTP warns; when
    QUILL_PEER_REQUIRE_TLS=1 it is refused.
    """
    raw = (url or "").strip()
    try:
        parsed = urlparse(raw)
    except Exception:
        return {"ok": False, "error": "invalid url", "tls": False,
                "local": False, "warning": None}
    host = (parsed.hostname or "").lower()
    local = host in _LOCAL_HOSTS
    tls = parsed.scheme == "https"
    require = bool(getattr(_peer_cfg(), "require_tls", False))
    warning = None
    if not tls and not local:
        warning = ("Peer channel over a non-local URL should use HTTPS "
                   "(set QUILL_PEER_BASE_URL to an https:// address).")
        if require:
            return {"ok": False, "tls": False, "local": False,
                    "warning": warning,
                    "error": "non-local HTTP blocked (QUILL_PEER_REQUIRE_TLS=1)"}
    return {"ok": True, "tls": tls, "local": local, "warning": warning}


def my_transport() -> dict[str, Any]:
    try:
        from app.services import peer_channel
        return url_transport(peer_channel.my_base_url())
    except Exception:
        return {"ok": True, "tls": False, "local": True, "warning": None}


# --- policy packs -----------------------------------------------------------
def list_packs() -> list[dict]:
    return [{"id": k, "policy": dict(v), "blurb": PACK_BLURB.get(k, "")}
            for k, v in POLICY_PACKS.items()]


def apply_pack(peer_id: str, pack: str) -> dict:
    pack = (pack or "").strip().lower()
    if pack not in POLICY_PACKS:
        return {"ok": False, "error": f"unknown pack {pack!r}"}
    from app.services import peer_channel
    res = peer_channel.set_policy(peer_id, POLICY_PACKS[pack], pack=pack)
    return res


# --- named teams ------------------------------------------------------------
def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:40]


def list_teams() -> list[dict]:
    with _lock:
        reg = _load(_teams_path(), {})
    out = []
    for slug, rec in sorted(reg.items()):
        out.append({
            "slug": slug,
            "name": rec.get("name") or slug,
            "peer_ids": list(rec.get("peer_ids") or []),
            "created_at": rec.get("created_at"),
        })
    return out


def get_team(slug: str) -> dict | None:
    key = _slugify(slug) if slug else ""
    if not key:
        return None
    with _lock:
        rec = _load(_teams_path(), {}).get(key)
    if not rec:
        # Allow lookup by display name.
        with _lock:
            reg = _load(_teams_path(), {})
        for s, r in reg.items():
            if str(r.get("name") or "").casefold() == slug.strip().casefold():
                rec, key = r, s
                break
        else:
            return None
    return {"slug": key, "name": rec.get("name") or key,
            "peer_ids": list(rec.get("peer_ids") or []),
            "created_at": rec.get("created_at")}


def upsert_team(name: str, peer_ids: list[str] | None = None,
                slug: str | None = None) -> dict:
    display = (name or "").strip()[:60]
    if not display:
        return {"ok": False, "error": "name required"}
    key = _slugify(slug or display)
    if not _SLUG_RE.match(key):
        return {"ok": False, "error": "slug must be like 'platform' or 'design-ops'"}
    ids = [str(p).strip() for p in (peer_ids or []) if str(p).strip()]
    with _lock:
        reg = _load(_teams_path(), {})
        prev = reg.get(key) or {}
        rec = {
            "name": display,
            "peer_ids": ids or list(prev.get("peer_ids") or []),
            "created_at": prev.get("created_at") or time.time(),
        }
        # If caller passed peer_ids (even empty), take them as the new set.
        if peer_ids is not None:
            rec["peer_ids"] = ids
        reg[key] = rec
        _save(_teams_path(), reg)
    return {"ok": True, "team": get_team(key)}


def set_team_members(slug: str, peer_ids: list[str]) -> dict:
    t = get_team(slug)
    if t is None:
        return {"ok": False, "error": "unknown team"}
    return upsert_team(t["name"], peer_ids=peer_ids, slug=t["slug"])


def delete_team(slug: str) -> dict:
    key = (get_team(slug) or {}).get("slug")
    if not key:
        return {"ok": False, "error": "unknown team"}
    with _lock:
        reg = _load(_teams_path(), {})
        if key not in reg:
            return {"ok": False, "error": "unknown team"}
        del reg[key]
        _save(_teams_path(), reg)
    return {"ok": True}


def parse_group_ask(text: str) -> dict | None:
    """Fan-out intent: 'ask #platform: …' or 'ask the platform team: …'.

    Returns a dict even when the team is unknown (so chat does not fall
    through to the browser agent). None only when this is not group syntax.
    """
    text = text or ""
    to_form = False
    m = _ASK_HASH_RE.match(text) or _ASK_THE_TEAM_RE.match(text)
    if m is None:
        m = _ASK_HASH_TO_RE.match(text) or _ASK_TEAM_TO_RE.match(text)
        to_form = m is not None
    if not m:
        return None
    who = (m.groupdict().get("slug") or m.groupdict().get("who") or "").strip()
    question = (m.group("q") or "").strip()
    if not who or not question.rstrip("?").strip():
        return None
    kind = "handoff" if to_form else "question"
    if not to_form:
        hand = re.match(r"to\s+(.+)$", question, re.I | re.S)
        if hand:
            kind, question = "handoff", hand.group(1).strip()
    team = get_team(who)
    if team is None:
        slug = _slugify(who.lstrip("#"))
        return {"fanout": True, "unknown": True, "team_slug": slug,
                "team_name": who.lstrip("#"), "peer_ids": [],
                "peer_name": f"{who.lstrip('#')} team",
                "question": question, "kind": kind}
    return {"fanout": True, "unknown": False, "team_slug": team["slug"],
            "team_name": team["name"], "peer_ids": list(team["peer_ids"]),
            "peer_name": f"{team['name']} team",
            "question": question, "kind": kind}


def fanout_ask(team_slug: str, question: str, kind: str = "question") -> dict:
    """Ask every paired member. Each ask is independent; rollup is later."""
    team = get_team(team_slug)
    if team is None:
        return {"ok": False, "error": "unknown team"}
    from app.services import peer_channel
    paired = {p["peer_id"] for p in peer_channel.peers()}
    members = [pid for pid in team["peer_ids"] if pid in paired]
    if not members:
        return {"ok": False, "error": "no paired members on that team",
                "team_slug": team["slug"], "team_name": team["name"]}
    team_ask_id = uuid.uuid4().hex[:12]
    results = []
    for pid in members:
        res = peer_channel.ask(pid, question, kind,
                               team_slug=team["slug"],
                               team_ask_id=team_ask_id)
        results.append({"peer_id": pid, **{k: res.get(k) for k in
                       ("ok", "status", "ask_id", "peer", "error", "answer")}})
    return {"ok": True, "team_slug": team["slug"], "team_name": team["name"],
            "team_ask_id": team_ask_id, "asked": len(members),
            "results": results}


def _chat_team_run(team_slug: str, question: str, kind: str) -> None:
    from app.services.peer_channel import _notify_chat
    res = fanout_ask(team_slug, question, kind)
    name = res.get("team_name") or team_slug
    if not res.get("ok"):
        _notify_chat(f"Couldn't ask the {name} team "
                     f"({res.get('error', 'unknown error')}).")
        return
    n = int(res.get("asked") or 0)
    verb = "Handing off to" if kind == "handoff" else "Asked"
    _notify_chat(f"{verb} the {name} team ({n} teammate"
                 f"{'' if n == 1 else 's'}) — I'll roll up answers as they land.")


def chat_team_ask_async(team_slug: str, question: str,
                        kind: str = "question") -> None:
    threading.Thread(target=_chat_team_run,
                     args=(team_slug, question, kind), daemon=True).start()


def maybe_rollup(team_ask_id: str | None) -> str | None:
    """When every ask in a fan-out is terminal, return a one-line rollup."""
    if not team_ask_id:
        return None
    from app.services import peer_channel
    rows = [r for r in peer_channel.answers()
            if r.get("team_ask_id") == team_ask_id]
    if not rows:
        return None
    terminal = {"answered", "declined", "error", "accepted"}
    pendingish = {"pending", "sent", "queued"}
    if any((r.get("status") or "") in pendingish for r in rows):
        return None
    bits = []
    n_ans = 0
    for r in rows:
        st = r.get("status") or "?"
        who = r.get("peer_name") or "teammate"
        if st == "answered":
            n_ans += 1
            ans = (r.get("answer") or "").strip()
            bits.append(f"- {who}: “{ans[:180]}”" if ans else f"- {who}: answered")
        elif st == "declined":
            bits.append(f"- {who}: declined")
        else:
            bits.append(f"- {who}: {st}")
    body = "\n".join(bits)
    return (f"{n_ans} of {len(rows)} on that team answered:\n{body}")


# --- mailbox (sender-side offline queue) ------------------------------------
def mailbox_enqueue(item: dict) -> None:
    row = {
        "ask_id": item.get("ask_id"),
        "peer_id": item.get("peer_id"),
        "question": item.get("question"),
        "kind": item.get("kind") or "question",
        "loop_id": item.get("loop_id"),
        "team_slug": item.get("team_slug"),
        "team_ask_id": item.get("team_ask_id"),
        "created_at": item.get("created_at") or time.time(),
        "attempts": int(item.get("attempts") or 0),
    }
    with _lock:
        box = _load(_mailbox_path(), [])
        if any(b.get("ask_id") == row["ask_id"] for b in box):
            return
        box.append(row)
        _save(_mailbox_path(), box[-200:])


def mailbox_list(peer_id: str | None = None) -> list[dict]:
    with _lock:
        box = _load(_mailbox_path(), [])
    if peer_id:
        box = [b for b in box if b.get("peer_id") == peer_id]
    return box


def mailbox_remove(ask_id: str) -> None:
    with _lock:
        box = [b for b in _load(_mailbox_path(), [])
               if b.get("ask_id") != ask_id]
        _save(_mailbox_path(), box)


def flush_mailbox(peer_id: str | None = None) -> dict:
    """Retry queued asks. Returns counts. Never raises."""
    items = mailbox_list(peer_id)
    flushed = 0
    still = 0
    from app.services import peer_channel
    for item in items:
        try:
            res = peer_channel.retry_queued(item)
        except Exception:
            still += 1
            continue
        if res.get("ok") and res.get("status") != "queued":
            mailbox_remove(item["ask_id"])
            flushed += 1
        else:
            still += 1
            with _lock:
                box = _load(_mailbox_path(), [])
                for b in box:
                    if b.get("ask_id") == item.get("ask_id"):
                        b["attempts"] = int(b.get("attempts") or 0) + 1
                _save(_mailbox_path(), box)
    return {"ok": True, "flushed": flushed, "remaining": still}


# --- presence ---------------------------------------------------------------
def presence_of(last_seen: float | None) -> str:
    if not last_seen:
        return "unknown"
    stale = float(getattr(_peer_cfg(), "presence_stale_s", 90) or 90)
    return "online" if (time.time() - float(last_seen)) <= stale else "offline"


def handle_ping(peer: dict) -> dict:
    from app.services import peer_channel
    peer_channel._touch(peer.get("peer_id") or "", None)
    # Their ping means they are reachable — try to deliver queued asks TO them.
    try:
        flush_mailbox(peer.get("peer_id"))
    except Exception:
        pass
    return {"ok": True, "name": peer_channel.instance_name(),
            "ts": time.time()}


def ping_peer(peer_id: str) -> dict:
    from app.services import peer_channel
    with peer_channel._lock:
        rec = peer_channel._load(peer_channel._peers_path(), {}).get(peer_id)
    if rec is None:
        return {"ok": False, "error": "unknown peer"}
    timeout = float(getattr(_peer_cfg(), "ping_timeout_s", 5) or 5)
    try:
        res = peer_channel._post_json(
            f"{rec['base_url']}/peer/ping", {},
            token=rec.get("outbound_token"), timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if res.get("ok"):
        peer_channel._touch(peer_id, None)
        try:
            flush_mailbox(peer_id)
        except Exception:
            pass
        return {"ok": True, "peer": rec.get("name")}
    return {"ok": False, "error": res.get("error") or "ping refused"}


def ping_all() -> dict:
    from app.services import peer_channel
    results = []
    for p in peer_channel.peers():
        results.append({"peer_id": p["peer_id"], **ping_peer(p["peer_id"])})
    return {"ok": True, "results": results}


def attach() -> None:
    """Heartbeat thread: ping peers and flush the mailbox."""
    global _thread
    import os
    import sys
    cfg = _peer_cfg()
    if cfg is not None and not getattr(cfg, "enabled", True):
        return
    if float(getattr(cfg, "ping_interval_s", 30) or 0) <= 0:
        return
    # Unittest imports FastAPI startup; don't hammer LAN peers during tests
    # unless the test opted in.
    if "unittest" in sys.modules and os.environ.get("QUILL_PEER_HEARTBEAT") != "1":
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_ping_loop, name="peer-presence",
                               daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def _ping_loop() -> None:
    cfg = _peer_cfg()
    interval = float(getattr(cfg, "ping_interval_s", 30) or 30)
    interval = max(10.0, min(interval, 300.0))
    # First tick soon after boot so queued asks drain without waiting a full
    # interval — but not immediately, so uvicorn can finish binding.
    if _stop.wait(3.0):
        return
    while True:
        try:
            ping_all()
        except Exception as exc:
            print(f"[peer] presence ping skipped ({exc}).")
        if _stop.wait(interval):
            return


# --- shared loops -----------------------------------------------------------
def upsert_loop(*, loop_id: str, peer_id: str, peer_name: str = "",
                task: str = "", status: str = "offered",
                ask_id: str | None = None, side: str = "local") -> dict:
    loop_id = (loop_id or "").strip()
    if not loop_id:
        loop_id = uuid.uuid4().hex[:12]
    with _lock:
        rows = _load(_loops_path(), [])
        found = next((r for r in rows if r.get("loop_id") == loop_id), None)
        if found is None:
            found = {"loop_id": loop_id, "created_at": time.time()}
            rows.append(found)
        found.update({
            "peer_id": peer_id, "peer_name": peer_name, "task": task[:500],
            "status": status, "ask_id": ask_id, "side": side,
            "updated_at": time.time(),
        })
        _save(_loops_path(), rows[-200:])
    return dict(found)


def mark_loop(loop_id: str, status: str) -> dict | None:
    if not loop_id:
        return None
    with _lock:
        rows = _load(_loops_path(), [])
        for r in rows:
            if r.get("loop_id") == loop_id:
                r["status"] = status
                r["updated_at"] = time.time()
                _save(_loops_path(), rows)
                return dict(r)
    return None


def loops(status: str | None = None) -> list[dict]:
    with _lock:
        rows = list(_load(_loops_path(), []))
    if status:
        rows = [r for r in rows if r.get("status") == status]
    rows.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
    return rows


# --- meeting pairing offers -------------------------------------------------
def _self_keys() -> set[str]:
    keys: set[str] = set()
    try:
        from app.services.identity import user_identity
        u = user_identity() or {}
        for k in ("name", "primary_email", "secondary_email"):
            v = str(u.get(k) or "").strip().casefold()
            if v:
                keys.add(v)
                keys.add(v.split()[0])
    except Exception:
        pass
    try:
        from app.services import peer_channel
        n = peer_channel.instance_name().casefold()
        if n:
            keys.add(n)
            keys.add(n.split()[0])
    except Exception:
        pass
    return {k for k in keys if k}


def _attendee_keys(att: dict) -> set[str]:
    keys: set[str] = set()
    name = str(att.get("name") or att.get("display_name") or "").strip()
    email = str(att.get("email") or att.get("mailto") or "").strip()
    if name:
        keys.add(name.casefold())
        keys.add(name.casefold().split()[0])
    if email:
        keys.add(email.casefold())
    return {k for k in keys if k}


def _paired_keys() -> set[str]:
    from app.services import peer_channel
    keys: set[str] = set()
    for p in peer_channel.peers():
        for raw in (p.get("name"), p.get("person_name")):
            n = str(raw or "").strip().casefold()
            if n:
                keys.add(n)
                keys.add(n.split()[0])
        pid = p.get("person_id")
        if pid is not None:
            try:
                keys |= peer_channel._person_alias_keys(int(pid))
            except Exception:
                pass
    return keys


def _collect_attendees(hours: float = 18.0) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []

    def _add(att: dict) -> None:
        if not isinstance(att, dict):
            return
        name = str(att.get("name") or att.get("display_name") or "").strip()
        email = str(att.get("email") or att.get("mailto") or "").strip()
        if not name and not email:
            return
        sig = (email or name).casefold()
        if sig in seen:
            return
        seen.add(sig)
        out.append({"name": name, "email": email})

    try:
        from app.services import meeting_session
        for a in meeting_session.attendees_live() or []:
            _add(a)
    except Exception:
        pass
    now = time.time()
    since = now - hours * 3600.0
    try:
        from app.storage import get_store
        store = get_store()
        for sess in store.recent_sessions(limit=30):
            if float(sess.get("start") or 0) < since:
                continue
            meta = sess.get("meeting_meta") or {}
            for a in meta.get("attendees") or []:
                _add(a if isinstance(a, dict) else {"name": str(a)})
        try:
            for ev in store.list_calendar_events(start_min=since, start_max=now + 3600,
                                                 limit=40):
                for a in ev.get("attendees") or []:
                    _add(a if isinstance(a, dict) else {"name": str(a)})
        except Exception:
            pass
    except Exception:
        pass
    return out


def pairing_offers(hours: float = 18.0) -> list[dict]:
    """Attendees from live / recent meetings who are not yet paired (and not you)."""
    self_keys = _self_keys()
    paired = _paired_keys()
    offers = []
    for att in _collect_attendees(hours):
        keys = _attendee_keys(att)
        if keys & self_keys:
            continue
        if keys & paired:
            continue
        if not att.get("name") and not att.get("email"):
            continue
        offers.append({
            "name": att.get("name") or "",
            "email": att.get("email") or "",
            "status": "unpaired",
        })
    return offers[:20]


def status_bits() -> dict:
    """Extra snapshot fields merged into GET /peer/status."""
    transport = my_transport()
    return {
        "packs": list_packs(),
        "teams": list_teams(),
        "mailbox": mailbox_list()[-20:],
        "loops": loops()[:20],
        "pairing_offers": pairing_offers(),
        "require_tls": bool(getattr(_peer_cfg(), "require_tls", False)),
        "tls": transport,
    }
