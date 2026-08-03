"""Learning Memory Engine — student understanding over the shared graph.

Concepts are `entities(kind=idea)`. Mastery lives in `entity_attrs` (`learn_*`).
Strength/forgetting reuse `node_dynamics` access + idea gravity half-life.
No parallel student DB.
"""
from __future__ import annotations

import re
import time
from typing import Any

ATTR_MASTERY = "learn_mastery"
ATTR_STREAK = "learn_streak"
ATTR_LAST_OUTCOME = "learn_last_outcome"
ATTR_LAST_PRACTICED = "learn_last_practiced_at"
ATTR_ENCOUNTERS = "learn_encounters"
ATTR_LAST_SEEN = "learn_last_seen_at"

_LEARN_KEYS = (
    ATTR_MASTERY, ATTR_STREAK, ATTR_LAST_OUTCOME,
    ATTR_LAST_PRACTICED, ATTR_ENCOUNTERS, ATTR_LAST_SEEN,
)

_STUDY_MODES = frozenset({
    "homework", "study_quiz", "lecture_notes", "reading",
    "essay_rubric", "syllabus",
})

# EMA: m' = 0.7*m + 0.3*(1 or 0); first encounter seeds 0.4
_EMA_OLD = 0.7
_EMA_NEW = 0.3
_SEED_MASTERY = 0.4

_STOP = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "into", "onto",
    "when", "where", "what", "which", "while", "about", "after", "before",
    "find", "given", "using", "show", "prove", "part", "problem", "answer",
    "solution", "chapter", "section", "page", "figure", "table", "example",
})

_QUOTED = re.compile(r"[\"'“”]([^\"'“”]{3,60})[\"'“”]")
_TITLE_CASE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,4})\b"
)
_EQUATIONISH = re.compile(
    r"\b([a-zA-Z]\([a-zA-Z0-9,\s]+\)|[a-zA-Z]_\{?[a-zA-Z0-9]+\}?|"
    r"(?:velocity|acceleration|position|momentum|force|energy|entropy|"
    r"derivative|integral|matrix|eigenvalue|kinetics|equilibrium|"
    r"photosynthesis|mitosis|meiosis|transcription|translation))\b",
    re.I,
)
_DONT_UNDERSTAND = re.compile(
    r"(?:i\s+(?:don'?t|do\s+not)\s+understand|confused\s+about|"
    r"help\s+(?:me\s+)?(?:with|understanding))\s+(.+)$",
    re.I,
)
_CORRECT = re.compile(
    r"^\s*(?:i\s+got\s+it|got\s+it|that(?:'s|\s+is)\s+correct|correct|"
    r"right|yes\s+that(?:'s|\s+is)\s+right|makes\s+sense)\s*[.!?]?\s*$",
    re.I,
)
_WRONG = re.compile(
    r"^\s*(?:that\s+was\s+wrong|wrong|incorrect|i\s+(?:got\s+it\s+)?wrong|"
    r"nope|not\s+quite)\s*[.!?]?\s*$",
    re.I,
)


def study_mode_active(mode_id: str | None = None) -> bool:
    if mode_id:
        return mode_id in _STUDY_MODES
    try:
        from app.services import agent_chat_mode as _smode
        return _smode.current().get("id") in _STUDY_MODES
    except Exception:
        return False


def _attr(store, entity_id: int, key: str, default: str = "") -> str:
    try:
        row = store.entity_attrs(entity_id).get(key) or {}
        return str(row.get("value") or default)
    except Exception:
        return default


def _set_attr(store, entity_id: int, key: str, value: str,
              ts: float | None = None) -> None:
    store.set_entity_attr(entity_id, key, value, None,
                          float(ts if ts is not None else time.time()))


def _f(store, entity_id: int, key: str, default: float = 0.0) -> float:
    raw = _attr(store, entity_id, key, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _i(store, entity_id: int, key: str, default: int = 0) -> int:
    return int(_f(store, entity_id, key, float(default)))


def upsert_concept(store, name: str, *, source_event_id: int | None = None,
                   ts: float | None = None) -> int:
    """Resolve/create an idea entity and mark an encounter."""
    label = (name or "").strip()
    if len(label) < 2:
        return 0
    now = float(ts if ts is not None else time.time())
    eid = int(store.resolve_entity(label, "idea", ts=now) or 0)
    if not eid:
        return 0
    try:
        store.touch_entity(eid, ts=now)
    except Exception:
        pass
    record_encounter(store, eid, ts=now)
    # source_event_id reserved for future claim linking
    _ = source_event_id
    return eid


def record_encounter(store, entity_id: int, *, ts: float | None = None) -> None:
    """Seen (lecture/homework) but not necessarily tested."""
    eid = int(entity_id or 0)
    if not eid:
        return
    now = float(ts if ts is not None else time.time())
    n = _i(store, eid, ATTR_ENCOUNTERS, 0) + 1
    _set_attr(store, eid, ATTR_ENCOUNTERS, str(n), ts=now)
    _set_attr(store, eid, ATTR_LAST_SEEN, str(now), ts=now)
    if not _attr(store, eid, ATTR_MASTERY):
        _set_attr(store, eid, ATTR_MASTERY, str(_SEED_MASTERY), ts=now)
    try:
        store.record_node_access("entity", eid, ts=now)
    except Exception:
        pass


def record_practice(store, entity_id: int, correct: bool,
                    *, ts: float | None = None) -> dict[str, Any]:
    """Update mastery EMA + streak + dynamics after a practice outcome."""
    eid = int(entity_id or 0)
    if not eid:
        return {"ok": False, "error": "no entity"}
    now = float(ts if ts is not None else time.time())
    prev = _f(store, eid, ATTR_MASTERY, _SEED_MASTERY)
    target = 1.0 if correct else 0.0
    mastery = _EMA_OLD * prev + _EMA_NEW * target
    streak = _i(store, eid, ATTR_STREAK, 0)
    streak = (streak + 1) if correct else 0
    _set_attr(store, eid, ATTR_MASTERY, f"{mastery:.4f}", ts=now)
    _set_attr(store, eid, ATTR_STREAK, str(streak), ts=now)
    _set_attr(store, eid, ATTR_LAST_OUTCOME,
              "correct" if correct else "incorrect", ts=now)
    _set_attr(store, eid, ATTR_LAST_PRACTICED, str(now), ts=now)
    _set_attr(store, eid, ATTR_LAST_SEEN, str(now), ts=now)
    try:
        store.record_node_access("entity", eid, ts=now)
        store.bump_node_value("entity", eid,
                              "used" if correct else "rejected")
    except Exception:
        pass
    conf = effective_confidence(store, eid, now=now)
    return {
        "ok": True,
        "entity_id": eid,
        "mastery": round(mastery, 4),
        "streak": streak,
        "correct": bool(correct),
        "effective_confidence": conf,
    }


def effective_confidence(store, entity_id: int,
                         *, now: float | None = None) -> float:
    """Mastery × idea decay since last practice (or last seen / encounter)."""
    eid = int(entity_id or 0)
    if not eid:
        return 0.0
    now = float(now if now is not None else time.time())
    mastery = _f(store, eid, ATTR_MASTERY, _SEED_MASTERY)
    practiced = _f(store, eid, ATTR_LAST_PRACTICED, 0.0)
    seen = _f(store, eid, ATTR_LAST_SEEN, 0.0)
    anchor = practiced or seen or now
    age_days = max(0.0, (now - anchor) / 86400.0)
    try:
        from app.services.graph import decay_for_kind
        decay = float(decay_for_kind("idea", age_days))
    except Exception:
        decay = 1.0
    return round(max(0.0, min(1.0, mastery * decay)), 4)


def _concept_rows(store) -> list[dict[str, Any]]:
    """Entities that have any learn_* attrs."""
    with store._lock:
        rows = store._conn.execute(
            "SELECT DISTINCT ea.entity_id AS id, e.canonical_name AS name, "
            "e.kind AS kind "
            "FROM entity_attrs ea "
            "JOIN entities e ON e.id = ea.entity_id "
            "WHERE ea.key IN (?, ?, ?) "
            "ORDER BY ea.updated_at DESC "
            "LIMIT 200",
            (ATTR_MASTERY, ATTR_ENCOUNTERS, ATTR_LAST_PRACTICED),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "entity_id": int(r["id"]),
            "name": r["name"] or "",
            "kind": r["kind"] or "idea",
        })
    return out


def concept_snapshot(store, entity_id: int,
                     *, now: float | None = None) -> dict[str, Any]:
    now = float(now if now is not None else time.time())
    eid = int(entity_id)
    name = ""
    try:
        with store._lock:
            row = store._conn.execute(
                "SELECT canonical_name FROM entities WHERE id = ?",
                (eid,)).fetchone()
        name = (row["canonical_name"] if row else "") or ""
    except Exception:
        pass
    return {
        "entity_id": eid,
        "name": name,
        "mastery": round(_f(store, eid, ATTR_MASTERY, _SEED_MASTERY), 4),
        "streak": _i(store, eid, ATTR_STREAK, 0),
        "encounters": _i(store, eid, ATTR_ENCOUNTERS, 0),
        "last_outcome": _attr(store, eid, ATTR_LAST_OUTCOME, ""),
        "last_practiced_at": _f(store, eid, ATTR_LAST_PRACTICED, 0.0) or None,
        "last_seen_at": _f(store, eid, ATTR_LAST_SEEN, 0.0) or None,
        "effective_confidence": effective_confidence(store, eid, now=now),
    }


def weak_concepts(store, limit: int = 12,
                  *, now: float | None = None) -> list[dict[str, Any]]:
    """Lowest effective-confidence study concepts first."""
    now = float(now if now is not None else time.time())
    snaps = [concept_snapshot(store, r["entity_id"], now=now)
             for r in _concept_rows(store)]
    snaps.sort(key=lambda s: (
        float(s.get("effective_confidence") or 0.0),
        -float(s.get("encounters") or 0),
    ))
    return snaps[: max(1, int(limit))]


def recent_concepts(store, limit: int = 12,
                    *, now: float | None = None) -> list[dict[str, Any]]:
    now = float(now if now is not None else time.time())
    snaps = [concept_snapshot(store, r["entity_id"], now=now)
             for r in _concept_rows(store)]
    snaps.sort(key=lambda s: -(
        float(s.get("last_practiced_at") or s.get("last_seen_at") or 0)
    ))
    return snaps[: max(1, int(limit))]


def link_requires(store, concept_id: int, prereq_id: int,
                  *, confidence: float = 0.8) -> None:
    cid, pid = int(concept_id or 0), int(prereq_id or 0)
    if not cid or not pid or cid == pid:
        return
    store.add_relation(
        "entity", cid, "requires", "entity", pid,
        origin="asserted", confidence=float(confidence),
        ts=time.time(),
    )


def render_lines(store, limit: int = 8) -> list[str]:
    """Grounding section for weak study concepts."""
    weak = weak_concepts(store, limit=limit)
    if not weak:
        return []
    lines = [
        "WEAK CONCEPTS (study memory; prefer these for hints/quiz):",
    ]
    for s in weak:
        pct = int(round(float(s.get("effective_confidence") or 0) * 100))
        name = (s.get("name") or f"#{s.get('entity_id')}").strip()
        outcome = s.get("last_outcome") or "untested"
        lines.append(
            f"- {name} — confidence {pct}% "
            f"(mastery {s.get('mastery')}, last={outcome})"
        )
    return lines


def extract_concept_names(text: str, *, limit: int = 8) -> list[str]:
    """Light heuristic concept tags from OCR / problem text (no LLM)."""
    blob = (text or "").strip()
    if not blob:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        t = re.sub(r"\s+", " ", (raw or "").strip(" .,;:()-"))
        if len(t) < 3 or len(t) > 60:
            return
        low = t.lower()
        if low in seen or low in _STOP:
            return
        words = [w for w in re.findall(r"[A-Za-z']+", t) if w]
        if words and all(w.lower() in _STOP for w in words):
            return
        seen.add(low)
        found.append(t)

    for m in _QUOTED.finditer(blob):
        _add(m.group(1))
    for m in _TITLE_CASE.finditer(blob):
        _add(m.group(1))
    for m in _EQUATIONISH.finditer(blob):
        _add(m.group(1))

    # First informative heading-ish line
    for line in blob.splitlines():
        line = line.strip()
        if re.match(r"^(problem|part|ex\.?|exercise)\b", line, re.I):
            rest = re.sub(r"^(problem|part|ex\.?|exercise)\s*[\d.a-d()]*\s*",
                          "", line, flags=re.I).strip(" :-")
            if rest:
                _add(rest[:60])
            break

    return found[: max(1, int(limit))]


def ingest_text_concepts(store, text: str, *, limit: int = 8,
                         source_event_id: int | None = None) -> list[int]:
    """Extract + upsert concepts from free text; return entity ids."""
    ids: list[int] = []
    for name in extract_concept_names(text, limit=limit):
        eid = upsert_concept(store, name, source_event_id=source_event_id)
        if eid:
            ids.append(eid)
    return ids


def parse_chat_verdict(message: str) -> dict[str, Any] | None:
    """Detect short practice verdicts from chat. Returns action dict or None."""
    text = (message or "").strip()
    if not text or len(text) > 200:
        return None
    if _CORRECT.match(text):
        return {"action": "practice", "correct": True, "concept": None}
    if _WRONG.match(text):
        return {"action": "practice", "correct": False, "concept": None}
    m = _DONT_UNDERSTAND.match(text)
    if m:
        concept = (m.group(1) or "").strip(" .!?")
        if 2 < len(concept) < 80:
            return {"action": "struggle", "correct": False, "concept": concept}
    return None


def apply_chat_verdict(store, message: str) -> dict[str, Any] | None:
    """Best-effort: record practice from a chat line. Uses most recent concept
    when the verdict has no explicit topic."""
    verdict = parse_chat_verdict(message)
    if not verdict:
        return None
    concept = verdict.get("concept")
    if concept:
        eid = upsert_concept(store, concept)
    else:
        recent = recent_concepts(store, limit=1)
        if not recent:
            return None
        eid = int(recent[0]["entity_id"])
    if not eid:
        return None
    if verdict["action"] == "struggle":
        # Struggle = incorrect practice + encounter reinforcement
        record_encounter(store, eid)
        return record_practice(store, eid, False)
    return record_practice(store, eid, bool(verdict.get("correct")))
