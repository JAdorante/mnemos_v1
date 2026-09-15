"""CAL — the model reader, run over the episodes the cheap path left blank.

The layer has two readers. Titles, URLs and identifiers resolve through the
binding table for free; that is `replay.anchors_for`. When a stretch of work
carries none of those — a mail client whose title is the user's own name, a
PDF open for half an hour — a model reads what was captured and picks one of
the projects the graph already knows, or says null. That is `escalate.choose`,
and until now nothing fed it.

This is the feed, as an OFFLINE pass: replay a day, then hand every unbound
episode to the model with its evidence. Three rules keep it honest:

  candidates are earned    the model chooses among entities the stretch's own
                           text MENTIONS, ranked by how often. Fifty-nine
                           graph nodes is not a choice; three is. No mention,
                           no call, no label.
  the moment is evidence   the prompt is window titles plus the lines around
                           each mention — not a summary someone wrote later.
  nothing is written       an episode named here is named in memory for the
                           scorer. The ratchet (minting) is a separate,
                           deliberate step this module does not take.
"""
from __future__ import annotations

import json
import re
from collections import Counter

from app.services.context import escalate as esc
from app.services.context import surfaces as sf
from app.services.context.resolver import Candidate

MIN_MENTIONS = 2          # one mention is a sentence, not a subject
MOMENT_CHARS = 1200       # an episode gets twice an event's budget
# An idea is a thing someone had, not a thing someone works on; it cannot
# name a stretch any more than a tool can.
_UNNAMEABLE = sf._UNNAMEABLE_KINDS | {"idea"}
SNIPPET = 70              # chars either side of a mention
SNIPPETS_PER_CANDIDATE = 2


def episode_text(store, event_ids) -> tuple[str, Counter]:
    """Everything captured across the episode's events, and its windows."""
    ids = [int(e) for e, _inh in (event_ids or [])]
    if not ids:
        return "", Counter()
    q = ",".join("?" * len(ids))
    with store._lock:
        rows = store._conn.execute(
            f"SELECT raw, summary, meta FROM events WHERE id IN ({q}) "
            "ORDER BY time ASC", ids).fetchall()
    parts: list[str] = []
    windows: Counter = Counter()
    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            meta = {}
        w = str(meta.get("window") or "").strip()
        if w:
            windows[w] += 1
        # Raw text is the evidence; summaries are someone's paraphrase and
        # window titles have already had their chance in the cheap path.
        parts.append(r["raw"] or r["summary"] or "")
    return "\n".join(p for p in parts if p), windows


def _sightings(store) -> dict:
    """entity id → (first_seen, last_seen). `all_entities` omits first_seen."""
    try:
        with store._lock:
            rows = store._conn.execute(
                "SELECT id, first_seen, last_seen FROM entities").fetchall()
        return {int(r["id"]): (float(r["first_seen"] or 0),
                               float(r["last_seen"] or 0)) for r in rows}
    except Exception:
        return {}


def _single_sighting(e: dict, seen: dict) -> bool:
    a, b = seen.get(int(e["id"]), (0.0, 0.0))
    return bool(a) and a == b


def candidates_for(store, text: str, *, top: int = esc.MAX_OPTIONS) -> list:
    """Entities the text mentions, most-mentioned first: [(Candidate, n)].

    Tools, places and ideas cannot name a stretch (`_UNNAMEABLE`), so they
    are not offered — a model told to pick between "Firefox" and a project
    will pick Firefox for the wrong stretch surprisingly often. Nor is an
    entity the graph has seen exactly once: that is a name mined out of one
    document (`MVP`, `manufacturing`), the same bar `episodes.MIN_OWN_EVIDENCE`
    holds a frame to. Measured on the first labelled day, letting those in
    put "company" and "MVP" above the real organisation and the model,
    rightly, refused the whole list.
    """
    if not (text or "").strip():
        return []
    from app.services.graph import _entity_patterns
    try:
        ents = [e for e in store.all_entities() if not e.get("hidden")]
    except Exception:
        ents = []
    seen = _sightings(store)
    out = []
    for e in ents:
        if (e.get("kind") or "") in _UNNAMEABLE:
            continue
        if _single_sighting(e, seen):
            continue
        n = sum(len(p.findall(text)) for p in _entity_patterns(e))
        if n >= MIN_MENTIONS:
            name = str(e.get("name") or e.get("canonical_name") or "")
            out.append((Candidate("entity", int(e["id"]), name,
                                  features={"f_mentions": float(n)}), n))
    out.sort(key=lambda cn: (-cn[1], cn[0].name.lower()))
    return out[:top]


def moment_for(text: str, windows: Counter, candidates, *,
               budget: int = MOMENT_CHARS) -> str:
    """What the model is shown: the windows, then the lines around each
    candidate's mentions. Evidence, not a summary."""
    lines = []
    if windows:
        lines.append("Windows: " + "; ".join(
            f"{w[:60]} (x{n})" for w, n in windows.most_common(3)))
    flat = re.sub(r"\s+", " ", text or "")
    for cand, _n in candidates:
        pat = re.compile(r"\b" + re.escape(cand.name) + r"\b", re.I)
        shown = 0
        for m in pat.finditer(flat):
            a, b = max(0, m.start() - SNIPPET), min(len(flat), m.end() + SNIPPET)
            lines.append(f"…{flat[a:b].strip()}…")
            shown += 1
            if shown >= SNIPPETS_PER_CANDIDATE:
                break
    return "\n".join(lines)[:budget]


def choose_debiased(moment: str, options, *, ask=None) -> esc.Escalation:
    """Ask in both orders; accept only what both agree on.

    A 7B model reads a numbered list with a position bias: on the first
    labelled day the same moment and the same two candidates returned null
    with `MVP` first and `Mnemos Labs` with `Mnemos Labs` first, every time.
    Two calls per unbound EPISODE is still nothing per event, and a name the
    model gives only when it is listed first is not a name it believes.
    """
    options = list(options)
    first = esc.choose(moment, options, ask=ask, max_chars=MOMENT_CHARS)
    if len(options) < 2:
        return first
    second = esc.choose(moment, list(reversed(options)), ask=ask,
                        max_chars=MOMENT_CHARS)
    a = first.chosen.key if first.chosen is not None else None
    b = second.chosen.key if second.chosen is not None else None
    if a is not None and a == b:
        return esc.Escalation(first.chosen, first.index, first.tier,
                              min(first.confidence, second.confidence))
    if a is None and b is None:
        return first
    return esc.Escalation(None, None, first.tier, 0.0, (),
                          f"order_disagreement: {a} vs {b}")


def name_unbound(store, episodes, *, ask=None, min_confidence: float = 0.5,
                 top: int = esc.MAX_OPTIONS) -> list[dict]:
    """Offer every unbound episode to the model; name in memory the ones it
    is sure about. Returns one record per unbound episode, decided or not."""
    records: list[dict] = []
    for ep in episodes or []:
        if ep.get("node_type"):
            continue
        rec = {"frame_seg_id": ep.get("frame_seg_id"),
               "started_at": ep["started_at"], "ended_at": ep["ended_at"],
               "n_events": ep.get("n_events"), "candidates": [],
               "choice": None, "confidence": 0.0, "tier": "", "error": "",
               "applied": False}
        text, windows = episode_text(store, ep.get("_event_ids"))
        cands = candidates_for(store, text, top=top)
        rec["candidates"] = [(c.name, n) for c, n in cands]
        if not cands:
            rec["error"] = "no_candidates"
            records.append(rec)
            continue
        got = choose_debiased(moment_for(text, windows, cands),
                              [c for c, _n in cands], ask=ask)
        rec.update(tier=got.tier, confidence=got.confidence, error=got.error)
        if got.chosen is not None:
            rec["choice"] = got.chosen.name
            if got.confidence >= min_confidence:
                ep["node_type"] = got.chosen.node_type
                ep["node_id"] = str(got.chosen.node_id)
                ep["title"] = got.chosen.name
                ep["method"] = "escalated"
                rec["applied"] = True
        records.append(rec)
    return records


__all__ = ["episode_text", "candidates_for", "moment_for", "choose_debiased",
           "name_unbound",
           "MIN_MENTIONS", "MOMENT_CHARS"]
