"""Context anchors (WS1) — a turn's ambient desktop/meeting context, looked
up sideways at extraction time.

`_attendee_priors_for_turn` already threads calendar attendees into person
resolution; this module generalizes that exact pattern to PROJECTS and
TOOLS: which apps/windows held focus while the words were spoken, which
meeting was live, and which existing entities those signals name.

Pure read, no writes (alias recurrence recording excepted — bind-only, and
an anchor can never mint an entity: unknown candidates stay candidates).
Consumers:

  * observational (default on): the extractor stamps the returned dict onto
    the anchor event's meta["context_anchor"] — measurable in Console,
    replayable in goldens, changes nothing downstream.
  * derived edges: candidates from a DOMINANT app (share >=
    derived_edge_min_share) that resolved to a real entity merge into the
    context-attribution stamp (origin="context" about-edges — revertible,
    never asserted, never KG-dual-written).
  * prior (opt-in, QUILL_EXTRACT_CONTEXT=1): a bounded, hint-only block in
    the extractor prompt. Eval-gated; see extractor._extract_text.

Perf: activities are read via one indexed query path (recent_activities)
and the raw-event fallback is a bounded time-range scan on the events(time)
index — the whole lookup stays well under the 20 ms extraction budget.
"""
from __future__ import annotations

import os
import re

from app.storage import Store, get_store

# Segments of a window title that can be entity candidates must look like
# names, not sentences or filenames.
_SEG_OK = re.compile(r"^[A-Za-z0-9][\w .\-]{1,48}$")
_FILEY = re.compile(r"\.\w{1,5}$")

_MAX_TITLE_SEGS = 4


def enabled() -> bool:
    return os.getenv("QUILL_CONTEXT_ANCHOR", "1") not in ("0", "false", "False")


def _cfg():
    try:
        from app.config import settings
        return getattr(settings, "context_anchor", None)
    except Exception:
        return None


def _max_apps() -> int:
    return int(getattr(_cfg(), "max_apps", 3) or 3)


def _min_share() -> float:
    return float(getattr(_cfg(), "min_share", 0.15) or 0.15)


def derived_edge_min_share() -> float:
    return float(getattr(_cfg(), "derived_edge_min_share", 0.6) or 0.6)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _clean_title(title: str) -> str:
    """Self-window suppression + browser-suffix strip (shared rationale with
    context_attribution._clean_title)."""
    t = (title or "").strip()
    if not t:
        return ""
    try:
        from app.services import surface_filters
        if surface_filters.is_self_window(t):
            return ""
    except Exception:
        pass
    try:
        from app.services.meeting_session import _strip_browser_suffix
        t = _strip_browser_suffix(t)
    except Exception:
        pass
    return t


def title_candidates(window_title: str) -> list[str]:
    """The non-app segments of a window title ("storage.py - nexus_v1 -
    Cursor" → ["nexus_v1"]) — activity.app_of's convention inverted. The
    last dash-segment is the app; filenames and sentence-shaped segments
    are dropped."""
    t = _clean_title(window_title)
    if not t:
        return []
    parts = [s.strip() for s in re.split(r"\s+[-–—]\s+", t)]
    if len(parts) < 2 or len(parts) > _MAX_TITLE_SEGS:
        return []
    out = []
    for seg in parts[:-1]:  # the trailing segment is the app name
        if not _SEG_OK.match(seg) or _FILEY.search(seg):
            continue
        if len(seg.split()) > 4:
            continue
        out.append(seg)
    return out


def _apps_from_activities(store: Store, t0: float, t1: float) -> list[dict]:
    span = max(1.0, t1 - t0)
    try:
        blocks = store.recent_activities(limit=200)
    except Exception:
        return []
    out: list[dict] = []
    for a in blocks:
        ov = _overlap(t0, t1, float(a.get("start") or 0),
                      float(a.get("end") or 0))
        if ov <= 0:
            continue
        wins = a.get("windows") or []
        out.append({
            "app": a.get("app") or "desktop",
            "window": wins[-1] if wins else "",
            "windows": wins[-3:],
            "share": round(min(1.0, ov / span), 3),
        })
    out.sort(key=lambda x: -x["share"])
    return out


def _apps_from_events(store: Store, t0: float, t1: float) -> list[dict]:
    """Fallback when no activity block has been rolled up yet (the worker
    rebuilds activities lazily — a turn can settle before its block exists).
    Share ≈ per-app fraction of the window's desktop frames."""
    try:
        rows = store.events_in_window(t0 - 30.0, t1 + 30.0,
                                      source="desktop.screen", limit=200)
    except Exception:
        return []
    if not rows:
        return []
    from app.services.activity import app_of
    per: dict[str, dict] = {}
    for ev in rows:
        win = str((ev.get("meta") or {}).get("window") or "")
        app = app_of(win)
        slot = per.setdefault(app, {"app": app, "window": win,
                                    "windows": [], "n": 0})
        slot["n"] += 1
        slot["window"] = win or slot["window"]
        if win and win not in slot["windows"]:
            slot["windows"] = (slot["windows"] + [win])[-3:]
    total = sum(s["n"] for s in per.values()) or 1
    out = [{"app": s["app"], "window": s["window"], "windows": s["windows"],
            "share": round(s["n"] / total, 3)} for s in per.values()]
    out.sort(key=lambda x: -x["share"])
    return out


def _meeting_for_window(store: Store, t0: float, t1: float) -> dict | None:
    # 1) live session (available from the first utterance).
    try:
        from app.services import meeting_session as ms
        st = ms.current()
        if st and st.get("title"):
            return {"title": str(st["title"]),
                    "session_id": st.get("id"),
                    "attendees": list(st.get("attendees") or [])}
    except Exception:
        pass
    # 2) stored session overlapping the window (extraction lags capture).
    try:
        for s in store.recent_sessions(limit=200):
            meta = s.get("meeting_meta") or {}
            title = (meta.get("title") or "").strip()
            if title and float(s["start"]) <= t1 and float(s["end"]) >= t0:
                return {"title": title, "session_id": s.get("id"),
                        "attendees": list(meta.get("attendees") or [])}
    except Exception:
        pass
    return None


def _identifier_norms(store: Store, t0: float, t1: float) -> list[str]:
    """Verbatim OCR identifiers (WS2) stamped on frames inside the window —
    they outrank title parsing (they are exact text, not convention)."""
    try:
        rows = store.events_in_window(t0 - 30.0, t1 + 30.0,
                                      source="desktop.screen", limit=200)
    except Exception:
        return []
    out, seen = [], set()
    for ev in rows:
        for ident in (ev.get("meta") or {}).get("identifiers") or []:
            if (ident or {}).get("kind") not in ("repo", "title_segment",
                                                 "path"):
                continue
            n = str(ident.get("norm") or "").strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
    return out


def anchors_for_window(store: Store | None, t_start: float,
                       t_end: float) -> dict:
    """Context candidates for a time window. Pure read; no writes. Empty
    capture → empty anchors, never an exception."""
    empty = {"apps": [], "meeting": None, "entities": []}
    if not enabled():
        return empty
    store = store if store is not None else get_store()
    try:
        t0 = float(t_start)
        t1 = float(t_end if t_end is not None else t_start)
    except (TypeError, ValueError):
        return empty
    if t1 < t0:
        t0, t1 = t1, t0

    apps = _apps_from_activities(store, t0, t1)
    if not apps:
        apps = _apps_from_events(store, t0, t1)
    apps = [a for a in apps if a["share"] >= _min_share()][:_max_apps()]

    meeting = _meeting_for_window(store, t0, t1)

    # Entity candidates: window-title segments (scored by app share),
    # meeting-title pattern hits, and OCR identifiers (verbatim — top score,
    # wins ties over title convention). Resolution is bind-only via the
    # alias-aware resolver; unknowns stay candidates with entity_id None.
    from app.services import entity_alias
    cands: dict[str, dict] = {}

    # Ties go to the more verbatim signal: an OCR identifier IS the text on
    # screen; a title segment is only convention.
    _SOURCE_RANK = {"identifier": 2, "window_title": 1, "meeting_title": 0}

    def _add(name: str, source: str, score: float):
        key = name.strip().lower()
        if not key:
            return
        cur = cands.get(key)
        if cur is not None and (cur["score"], _SOURCE_RANK.get(cur["source"], 0)) \
                >= (score, _SOURCE_RANK.get(source, 0)):
            return
        cands[key] = {"name": name.strip(), "source": source,
                      "score": round(float(score), 3)}

    for a in apps:
        for w in (a.get("windows") or [a.get("window") or ""]):
            for seg in title_candidates(w):
                _add(seg, "window_title", a["share"])
    if meeting:
        _add(meeting["title"], "meeting_title", 1.0)
    for norm in _identifier_norms(store, t0, t1):
        _add(norm, "identifier", 1.0)

    entities: list[dict] = []
    for c in cands.values():
        eid = None
        try:
            if c["source"] == "meeting_title":
                # A meeting title is prose — scan it for known entity
                # names/aliases instead of resolving the whole string.
                eid = _entity_in_text(store, c["name"])
            else:
                eid = entity_alias.resolve(c["name"], store=store,
                                           ts=t1, record=True)
        except Exception:
            eid = None
        row = dict(c)
        row["entity_id"] = int(eid) if eid else None
        if eid:
            try:
                ent = store.get_entity(int(eid))
                if ent:
                    row["name"] = ent.get("name") or row["name"]
                    row["kind"] = ent.get("kind")
            except Exception:
                pass
        entities.append(row)
    entities.sort(key=lambda e: (e["entity_id"] is None, -e["score"]))
    return {"apps": apps, "meeting": meeting, "entities": entities}


def _entity_in_text(store: Store, text: str) -> int | None:
    """First existing entity whose name/alias pattern matches inside `text`
    (the meeting-title path — same vocabulary as graph.rebuild)."""
    try:
        from app.services.graph import _entity_patterns
        for e in store.all_entities():
            if e.get("hidden"):
                continue
            pats = _entity_patterns(e)
            if pats and any(p.search(text) for p in pats):
                return int(e["id"])
    except Exception:
        return None
    return None


def derived_context_entities(anchors: dict) -> list[dict]:
    """Anchor entities strong enough for a code-attached derived edge:
    name-resolved AND carried by a dominant signal (score >=
    derived_edge_min_share). Shaped like context_attribution hits
    ({id, name, via_title}) so stamp_fact consumes them unchanged."""
    out = []
    floor = derived_edge_min_share()
    for e in (anchors or {}).get("entities") or []:
        if not e.get("entity_id") or float(e.get("score") or 0) < floor:
            continue
        out.append({"id": int(e["entity_id"]), "name": e.get("name"),
                    "via_title": f"{e.get('source')}:{e.get('name')}"})
    return out


def prompt_block(anchors: dict) -> str:
    """The bounded, hint-only context block for the extractor prompt
    (QUILL_EXTRACT_CONTEXT=1). Wording mirrors the spelling-only vocabulary
    hint: context may be irrelevant, and must never source a fact."""
    if not anchors:
        return ""
    parts: list[str] = []
    meeting = anchors.get("meeting")
    if meeting and meeting.get("title"):
        parts.append(f'active meeting: "{meeting["title"]}"')
    apps = anchors.get("apps") or []
    if apps:
        seen = []
        for a in apps[:3]:
            w = _clean_title(a.get("window") or "")
            seen.append(f"{a['app']} ({w})" if w else a["app"])
        parts.append("on-screen: " + "; ".join(seen))
    named = [e["name"] for e in (anchors.get("entities") or [])
             if e.get("entity_id")][:3]
    if named:
        parts.append("likely projects: " + ", ".join(named))
    if not parts:
        return ""
    return ("Context (may be irrelevant — never invent facts from it, and "
            "never emit a task/commitment/claim that is not in the "
            "transcript): " + "; ".join(parts) + ".")
