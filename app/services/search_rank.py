"""One ranking for Sparrow's memory search ("Google-style search", Sparrow 1.0
notes, 2026-11-08).

memory.search orders hits by similarity plus a small recency bonus. That
finds the right item but often below the handful of lines grounding can show:
on the Sparrow 1.0 holdout worlds the evidence sat at ranks 5-13 for 7 of the
11 questions still missed once hits rendered as passages. `rerank` re-scores
a wider candidate list with the signals the notes name, each bounded so
similarity still decides between unrelated items:

  person      the question names someone, and the item is from / to / with
              them (participant) or mentions them
  tag         the question names a project / org / entity the item carries
  recency     the question asks for the latest or current state
  references  how many facts were filed from the item (a link count)
  approved    a fact the user approved or edited

It scores any candidate list from the signals alone, so results from another
scope (a teammate's Sparrow) can be ranked by the same function.
QUILL_SEARCH_RERANK=1 turns it on for chat grounding (grounding.compose).
"""
from __future__ import annotations

import math
import os
import re
from typing import Any

WEIGHTS = {
    "person": 0.15, "person_mention": 0.08, "tag": 0.10,
    "recency": 0.10, "references": 0.05, "approved": 0.05,
}

_RECENCY_CUE = re.compile(
    r"\b(latest|most recent|recent(ly)?|now|current(ly)?|still|anymore|"
    r"newest|last|so far|yet|updated?|changed?)\b", re.I)
_WORD = re.compile(r"[a-z0-9][a-z0-9&.-]*")   # no apostrophe: "Rachel's" -> rachel
_GENERIC = frozenset({
    "project", "team", "group", "company", "inc", "llc", "the", "and",
    "new", "phase", "review", "meeting", "office", "center", "services",
})


def env_weights() -> dict[str, float]:
    """QUILL_SEARCH_RERANK_WEIGHTS="person=0.2,tag=0" overrides WEIGHTS
    (for tuning; unknown keys are ignored)."""
    out: dict[str, float] = {}
    for kv in os.environ.get("QUILL_SEARCH_RERANK_WEIGHTS", "").split(","):
        k, _, v = kv.partition("=")
        if k.strip() in WEIGHTS:
            try:
                out[k.strip()] = float(v)
            except ValueError:
                pass
    return out


def enabled() -> bool:
    return os.environ.get("QUILL_SEARCH_RERANK", "0") not in ("0", "false", "False")


def _words(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def query_context(query: str, store=None) -> dict[str, Any]:
    """What the question names: people (full name or a first name), tagged
    entities (full name or a distinctive first word), and a recency cue."""
    q = (query or "").lower()
    qw = _words(q)
    people: list[dict] = []
    entities: list[str] = []
    if store is not None:
        try:
            for p in store.all_people():
                name = (p.get("name") or "").strip()
                if (not name or p.get("hide_from_people")
                        or p.get("canonical_person_id")):
                    continue
                parts = name.lower().split()
                first = parts[0] if parts else ""
                if name.lower() in q or (len(first) >= 3 and first in qw):
                    people.append({"name": name, "first": first})
        except Exception:
            pass
        try:
            for e in store.all_entities():
                name = (e.get("name") or "").strip()
                low = name.lower()
                if len(low) < 3:
                    continue
                head = low.split()[0]
                if low in q or (len(head) >= 4 and head not in _GENERIC
                                and head in qw):
                    entities.append(low)
        except Exception:
            pass
    return {"people": people, "entities": entities,
            "recency_cue": bool(_RECENCY_CUE.search(q))}


def _hit_blob(h: dict) -> str:
    meta = h.get("meta") or {}
    parts = [h.get("summary"), h.get("raw"), h.get("chunk_text"),
             " ".join(map(str, h.get("entities") or [])),
             str(meta.get("project") or "")]
    return " ".join(str(p) for p in parts if p).lower()


def _participants(h: dict) -> str:
    meta = h.get("meta") or {}
    bits = [*map(str, h.get("people") or [])]
    for k in ("from", "to", "cc", "attendees", "organizer", "speakers"):
        v = meta.get(k)
        bits += map(str, v) if isinstance(v, (list, tuple)) else [str(v or "")]
    return " ".join(bits).lower()


def features(h: dict, ctx: dict, *, store=None, t_range=(0.0, 0.0)) -> dict:
    blob = _hit_blob(h)
    who = _participants(h)
    f = {"person": 0.0, "person_mention": 0.0, "tag": 0.0, "recency": 0.0,
         "references": 0.0, "approved": 0.0}
    for p in ctx["people"]:
        full, first = p["name"].lower(), p["first"]
        if full in who or (first and re.search(rf"\b{re.escape(first)}\b", who)):
            f["person"] = 1.0
        elif full in blob or (first and re.search(rf"\b{re.escape(first)}\b", blob)):
            f["person_mention"] = 1.0
    if any(e in blob or e.split()[0] in _words(blob) for e in ctx["entities"]):
        f["tag"] = 1.0
    lo, hi = t_range
    t = float(h.get("time") or h.get("ts") or 0.0)
    if ctx["recency_cue"] and hi > lo and t:
        f["recency"] = (t - lo) / (hi - lo)
    if store is not None:
        try:
            if h.get("is_fact") and h.get("fact_id") is not None:
                fact = store.get_fact(int(h["fact_id"])) or {}
                f["approved"] = 1.0 if fact.get("review") in ("approved", "edited") else 0.0
            elif t:
                eid = h.get("event_id") or next(iter(store.event_ids_at(t, limit=1)), None)
                if eid is not None:
                    n = len(store.fact_spans_for_event(int(eid)))
                    f["references"] = min(1.0, math.log1p(n) / math.log1p(8))
        except Exception:
            pass
    return f


def rerank(hits: list[dict], query: str, *, store=None,
           weights: dict[str, float] | None = None) -> list[dict]:
    """Re-score `hits` (memory.search payloads, best first) and return them
    best first. Each payload gains `rank_score` and `rank_features`."""
    if not hits:
        return hits
    w = {**WEIGHTS, **env_weights(), **(weights or {})}
    ctx = query_context(query, store)
    times = [float(h.get("time") or h.get("ts") or 0.0) for h in hits]
    times = [t for t in times if t]
    t_range = (min(times), max(times)) if times else (0.0, 0.0)
    import time
    from app.services.memory import recency_adjusted
    now = time.time()
    scored = []
    for pos, h in enumerate(hits):
        f = features(h, ctx, store=store, t_range=t_range)
        t = float(h.get("time") or h.get("ts") or 0.0)
        # memory.search's own order (similarity + small recency bonus) is the
        # base, so a question naming nobody and nothing ranks as before.
        base = recency_adjusted(float(h.get("score") or 0.0),
                                (now - t) / 86400.0 if t else 3650.0)
        s = base + sum(w[k] * v for k, v in f.items())
        h["rank_score"] = round(s, 4)
        h["rank_features"] = {k: round(v, 3) for k, v in f.items() if v}
        scored.append((-s, pos, h))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [h for _, _, h in scored]
