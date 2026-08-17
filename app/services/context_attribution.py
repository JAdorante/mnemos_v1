"""Context-based project attribution — facts inherit the room they were born in.

The graph rebuild links facts to entities only by literal name match over the
fact text (`graph.rebuild` "about" edges), so a task minted during a meeting
titled "Nexus weekly" was never attributed to Nexus unless the sentence said
the word. This module resolves the ambient context of a turn — the
live/overlapping meeting session title and the anchor event's focused-window
title — against EXISTING entities (bind-only, never mints) and stamps
fact→entity `about` edges with origin="context".

Design constraints:
  * distinguishable: origin="context" at weight 0.5 vs the rebuild's
    origin="derived" text-match edges at 1.0 — independently revertible, and
    (not being asserted/user) never dual-written into the KG belief store.
  * stable across rebuilds: graph.rebuild clears origin="derived" only, and
    add_relation's cross-origin conflict takes MAX(weight), so a nightly
    rebuild can neither wipe nor inflate a context edge.
  * self-suppressed: the app's own windows ("Mnemos — Chat") are not context
    (surface_filters.is_self_window), and browser suffixes are stripped so
    "… - Google Chrome" can't bind a Chrome tool entity from every tab.

Kill-switch: QUILL_CONTEXT_ATTRIB=0.
"""
from __future__ import annotations

import json
import os
import time

PREDICATE = "about"
ORIGIN = "context"
_WEIGHT = 0.5
_CONFIDENCE = 0.5
# A fact born in one meeting shouldn't fan out to a dozen entities because
# the title happens to be dense — keep the strongest few context anchors.
_MAX_ENTITIES = 5


def enabled() -> bool:
    return os.getenv("QUILL_CONTEXT_ATTRIB", "1") not in ("0", "false", "False")


def _clean_title(title: str) -> str:
    """Strip browser suffix + drop the app's own surfaces entirely."""
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


def _titles_for_turn(store, *, turn_start=None, turn_end=None,
                     anchor_event_id=None) -> list[str]:
    titles: list[str] = []
    # 1) Live meeting session (available from the first utterance).
    try:
        from app.services import meeting_session as ms
        st = ms.current()
        if st and st.get("title"):
            titles.append(str(st["title"]))
    except Exception:
        pass
    # 2) Stored session overlapping the turn (extraction lags capture, so the
    #    live session may already be gone by the time facts materialize).
    if turn_start is not None:
        try:
            t0 = float(turn_start)
            t1 = float(turn_end if turn_end is not None else turn_start)
            for s in store.recent_sessions(limit=200):
                meta = s.get("meeting_meta") or {}
                title = (meta.get("title") or "").strip()
                if title and float(s["start"]) <= t1 and float(s["end"]) >= t0:
                    titles.append(title)
                    break
        except Exception:
            pass
    # 3) The anchor event's focused-window title (desktop-born facts).
    if anchor_event_id:
        try:
            ev = store.get_event(int(anchor_event_id))
            meta = ev.get("meta") if ev else None
            if isinstance(meta, str):
                meta = json.loads(meta or "{}")
            w = str((meta or {}).get("window") or "")
            if w:
                titles.append(w)
        except Exception:
            pass
    seen: set[str] = set()
    out: list[str] = []
    for t in titles:
        t = _clean_title(t)
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
    return out


def context_entities_for_turn(store, *, turn_start=None, turn_end=None,
                              anchor_event_id=None) -> list[dict]:
    """[{id, name, via_title}] — existing entities named in the turn's
    ambient context titles. Bind-only: a title can never mint an entity."""
    if not enabled():
        return []
    titles = _titles_for_turn(
        store, turn_start=turn_start, turn_end=turn_end,
        anchor_event_id=anchor_event_id)
    if not titles:
        return []
    try:
        # Same name+alias patterns (and junk guards) the rebuild uses for its
        # text-match "about" edges — context and text attribution stay in
        # vocabulary lockstep.
        from app.services.graph import _entity_patterns
        entities = store.all_entities()
    except Exception:
        return []
    hits: list[dict] = []
    for e in entities:
        if e.get("hidden"):
            continue
        pats = _entity_patterns(e)
        if not pats:
            continue
        for t in titles:
            if any(p.search(t) for p in pats):
                hits.append({"id": int(e["id"]), "name": e["name"],
                             "via_title": t})
                break
        if len(hits) >= _MAX_ENTITIES:
            break
    return hits


def stamp_fact(store, fact_id: int, ctx: list[dict], *,
               anchor_event_id=None, now=None) -> int:
    """Write fact→entity `about` edges (origin="context") for one fact."""
    if not ctx:
        return 0
    ts = now if now is not None else time.time()
    n = 0
    for hit in ctx:
        try:
            store.add_relation(
                "fact", int(fact_id), PREDICATE, "entity", int(hit["id"]),
                weight=_WEIGHT, origin=ORIGIN, confidence=_CONFIDENCE,
                source_event_id=anchor_event_id, ts=ts)
            n += 1
        except Exception as exc:
            print(f"[context_attrib] stamp skipped ({exc}).")
    return n
