"""Ingest-level salience — "is there anything here?" before a model call
(pipeline fix list F26).

A 40-page PDF used to cost one extraction call per 2,500-char chunk no
matter what was on the page; a calendar title cost a call for two words.
Speech already had two pre-filters (the deterministic intent router and
the learned extract_triage head) — this module runs the same two over
document chunks and connector items, adds the one signal a document has
that speech does not (does the chunk name something we already know?),
and caps spend per source so a bulk import cannot burn a day's budget.

    plan = ingest_salience.plan(text, source="google.mail", store=store)
    for chunk in plan.chunks:          # the ones worth a model call
        ...extract(chunk.text)...
        ingest_salience.charge("google.mail", 1)

Everything not selected stays searchable: memory indexes every document's
chunk vectors (F02), so a skipped chunk is still findable by meaning — it
just is not mined for facts. Deterministic and offline; no model here.

Knobs (env, read at call time):
  QUILL_INGEST_SALIENCE=0              bypass: every chunk is extracted
  QUILL_EXTRACT_CHUNKS_PER_ITEM=12     chunks per document / item
  QUILL_EXTRACT_BUDGET_DEFAULT=400     model calls per source family per day
  QUILL_EXTRACT_BUDGET_<FAMILY>=N      per family (GOOGLE, PHONE, CHAT ...)
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field


@dataclass
class ChunkPlan:
    index: int
    text: str
    reason: str


@dataclass
class Plan:
    chunks: list[ChunkPlan] = field(default_factory=list)
    skipped: int = 0
    budget_exhausted: bool = False
    family: str = ""
    reasons: dict[str, int] = field(default_factory=dict)


# --- knobs -------------------------------------------------------------------
def enabled() -> bool:
    return os.environ.get("QUILL_INGEST_SALIENCE", "1") not in ("0", "false", "False")


def per_item_cap() -> int:
    try:
        return max(1, int(os.environ.get("QUILL_EXTRACT_CHUNKS_PER_ITEM", "12")))
    except ValueError:
        return 12


def source_family(source: str) -> str:
    """'google.mail' -> 'google', 'chat.attach' -> 'chat', 'phone.note' -> 'phone'."""
    s = (source or "").strip().lower()
    return (s.split(".", 1)[0] or "unknown") if s else "unknown"


def daily_budget(source: str) -> int:
    fam = source_family(source).upper().replace("-", "_")
    raw = os.environ.get(f"QUILL_EXTRACT_BUDGET_{fam}")
    if raw is None:
        raw = os.environ.get("QUILL_EXTRACT_BUDGET_DEFAULT", "400")
    try:
        return max(0, int(raw))
    except ValueError:
        return 400


# --- the per-day spend counter (in-process; a restart starts a new day) -------
_lock = threading.Lock()
_spent: dict[tuple[str, str], int] = {}


def _day(now: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def spent(source: str, *, now: float | None = None) -> int:
    with _lock:
        return int(_spent.get((source_family(source), _day(now)), 0))


def charge(source: str, n: int = 1, *, now: float | None = None) -> int:
    """Record `n` model calls against this source family's daily budget."""
    key = (source_family(source), _day(now))
    with _lock:
        _spent[key] = int(_spent.get(key, 0)) + int(n)
        # Keep the table small: only today's rows matter.
        for k in [k for k in _spent if k[1] != key[1]]:
            _spent.pop(k, None)
        return _spent[key]


def reset(source: str | None = None) -> None:
    """Tests / operators: forget spend (one family, or all)."""
    with _lock:
        if source is None:
            _spent.clear()
        else:
            fam = source_family(source)
            for k in [k for k in _spent if k[0] == fam]:
                _spent.pop(k, None)


# --- known names (what the memory already cares about) -----------------------
_names_cache: dict = {"at": 0.0, "store": None, "names": ()}
_NAMES_TTL_S = 60.0
_MIN_NAME_CHARS = 4


def known_names(store, *, now: float | None = None) -> tuple[str, ...]:
    """Lower-cased names of the entities and people this store knows (cached
    briefly). A chunk that mentions one is worth extracting regardless of
    what the heads think — it may update a field or add a relation."""
    now = float(now if now is not None else time.time())
    with _lock:
        if (_names_cache["store"] is store
                and now - float(_names_cache["at"]) < _NAMES_TTL_S):
            return _names_cache["names"]
    names: set[str] = set()
    try:
        for e in store.all_entities():
            nm = str(e.get("name") or "").strip().lower()
            if len(nm) >= _MIN_NAME_CHARS:
                names.add(nm)
    except Exception:
        pass
    try:
        for p in store.all_people():
            nm = str(p.get("name") or "").strip().lower()
            if len(nm) >= _MIN_NAME_CHARS and " " in nm:
                names.add(nm)
    except Exception:
        pass
    out = tuple(sorted(names))
    with _lock:
        _names_cache.update({"at": now, "store": store, "names": out})
    return out


def _anchored(text_low: str, names: tuple[str, ...]) -> bool:
    return any(n in text_low for n in names)


# --- the decision -----------------------------------------------------------
def plan(text: str, *, source: str, store=None, now: float | None = None,
         chunk_size: int | None = None) -> Plan:
    """Decide which chunks of `text` deserve a model call.

    Order of precedence per chunk: the learned head's active skip wins
    (it is only active after its shadow week cleared it); then a chunk
    that names a known entity or person is kept; then the intent router's
    "no actionable signal" skips; everything else is kept. Kept chunks are
    ranked entity-anchored first, cut to the per-item cap and the family's
    remaining daily budget, then restored to document order."""
    from app.services.documents import chunk_text
    now = float(now if now is not None else time.time())
    fam = source_family(source)
    chunks = chunk_text(text or "", size=chunk_size)
    out = Plan(family=fam)
    if not chunks:
        return out
    if not enabled():
        out.chunks = [ChunkPlan(i, c, "salience off") for i, c in enumerate(chunks)]
        return out
    budget_left = daily_budget(source) - spent(source, now=now)
    if budget_left <= 0:
        out.skipped = len(chunks)
        out.budget_exhausted = True
        out.reasons["budget"] = len(chunks)
        return out

    names = known_names(store, now=now) if store is not None else ()
    try:
        from app.services import intent as _intent
        intent_on = _intent.enabled()
    except Exception:
        _intent = None  # type: ignore[assignment]
        intent_on = False
    try:
        from app.services import fast_heads as _heads
    except Exception:
        _heads = None  # type: ignore[assignment]

    kept: list[tuple[int, int, str]] = []   # (priority, index, reason)
    for i, ch in enumerate(chunks):
        reason = "text"
        head = None
        if _heads is not None:
            try:
                head = _heads.consult("extract_triage", ch)
            except Exception:
                head = None
        if head and head.get("skip"):
            out.skipped += 1
            out.reasons["head"] = out.reasons.get("head", 0) + 1
            continue
        if names and _anchored(ch.lower(), names):
            kept.append((0, i, "known entity"))
            continue
        if intent_on:
            try:
                res = _intent.classify(ch)
                if not res.should_extract:
                    out.skipped += 1
                    out.reasons["intent"] = out.reasons.get("intent", 0) + 1
                    continue
                reason = res.reason or reason
            except Exception:
                pass
        if head and head.get("band") == "run":
            reason = f"head p={head.get('p')}"
        kept.append((1, i, reason))

    kept.sort()
    cap = min(per_item_cap(), budget_left)
    chosen, overflow = kept[:cap], kept[cap:]
    if overflow:
        out.skipped += len(overflow)
        out.reasons["cap"] = len(overflow)
    chosen.sort(key=lambda t: t[1])
    out.chunks = [ChunkPlan(i, chunks[i], r) for _, i, r in chosen]
    for _, _, r in chosen:
        key = "known entity" if r == "known entity" else "kept"
        out.reasons[key] = out.reasons.get(key, 0) + 1
    return out
