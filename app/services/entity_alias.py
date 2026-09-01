"""Alias-aware entity resolution (ambient-context WS1c) — shared substrate.

Window titles and OCR identifiers name entities in their own dialects:
"nexus_v1" for Nexus, "capital-connect" for Capital Connect. The extractor's
resolver (resolution.Resolver) is a MINTING path; context signals must never
mint, so this module is the bind-only twin: map a raw surface string onto an
EXISTING entity or record a reviewable alias proposal, never a new node.

Resolution order (most→least confident):

  1. exact       — canonical name, case-insensitive (store.find_entity_exact)
  2. alias       — a CONFIRMED entity_aliases row (normalized key)
  3. normalized  — case/dash/underscore-collapsed match against canonical
                   names + on-row aliases ("nexus_v1" == "Nexus V1"); binds,
                   and records itself as a confirmed alias so step 2 catches
                   it next time
  4. embedding   — cosine >= settings.context_anchor.alias_cos_min against
                   entity-NAME embeddings. Never binds directly: it records a
                   *proposal* (confirmed=0) routed to Console review. The
                   people-layer recurrence philosophy applies: a proposal
                   auto-confirms only after N independent resolutions across
                   distinct days (alias_autoconfirm_n, default 3).

Confirmed aliases feed graph._entity_patterns automatically because
store.all_entities() merges them into each entity's aliases list — the
nightly rebuild benefits the moment an alias is confirmed.

Every step is best-effort: embeddings unavailable → step 4 is skipped;
nothing here may raise into a capture/extraction path.
"""
from __future__ import annotations

import re
import time as _time

from app.storage import Store, get_store

# Collapse the separators window titles / slugs use interchangeably.
_SEP_RUN = re.compile(r"[\s\-_./\\]+")


def normalize(name: str) -> str:
    """Case/dash/underscore-insensitive match key ("nexus_v1" -> "nexus v1")."""
    return _SEP_RUN.sub(" ", (name or "").strip().lower()).strip()


def _cfg():
    from app.config import settings
    cfg = getattr(settings, "context_anchor", None)
    return (float(getattr(cfg, "alias_cos_min", 0.86)),
            int(getattr(cfg, "alias_autoconfirm_n", 3)))


def _day(ts: float | None) -> str:
    return _time.strftime("%Y-%m-%d", _time.localtime(
        ts if ts is not None else _time.time()))


# Name-embedding cache — vectors depend only on the text, so a module-level
# map is safe across stores. Bounded by the entity vocabulary size.
_vec_cache: dict[str, object] = {}


def _embed(text: str):
    """L2-normalized name embedding, or None when the embedder is
    unavailable (tests / minimal installs) — step 4 then simply skips."""
    key = (text or "").strip().lower()
    if not key:
        return None
    if key in _vec_cache:
        return _vec_cache[key]
    try:
        from app.services.embeddings import embedder
        vec = embedder.encode(key)
    except Exception:
        return None
    if len(_vec_cache) > 4096:
        _vec_cache.clear()
    _vec_cache[key] = vec
    return vec


def _cos(a, b) -> float:
    if a is None or b is None:
        return -1.0
    import numpy as np
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / denom)


def _plausible(name: str) -> bool:
    try:
        from app.services.name_quality import is_plausible_entity
        return is_plausible_entity(name)
    except Exception:
        return len((name or "").strip()) >= 2


def resolve(name: str, *, store: Store | None = None, ts: float | None = None,
            source: str = "context", record: bool = True) -> int | None:
    """Bind-only resolution of a raw surface string to an existing entity id.

    Returns the entity id, or None when nothing binds (unknowns stay
    candidates — a context string can NEVER mint an entity). `record=True`
    lets fuzzy hits leave their alias/proposal trail; pass False for purely
    speculative probes that should not accumulate recurrence evidence.
    """
    key = (name or "").strip()
    if not key or not _plausible(key):
        return None
    store = store if store is not None else get_store()
    now = ts if ts is not None else _time.time()

    # 1) exact canonical name.
    try:
        eid = store.find_entity_exact(key)
        if eid:
            return int(eid)
    except Exception:
        return None

    norm = normalize(key)
    if not norm:
        return None

    # 2) confirmed alias.
    try:
        eid = store.find_entity_by_alias_norm(norm)
        if eid:
            if record:
                _record(store, int(eid), key, norm, source=source,
                        confirmed=True, ts=now)
            return int(eid)
    except Exception:
        pass

    # 3) normalized match against canonical names + on-row aliases.
    try:
        entities = store.all_entities()
    except Exception:
        entities = []
    for e in entities:
        names = [e.get("name") or ""] + list(e.get("aliases") or [])
        if any(normalize(n) == norm for n in names if n):
            if record:
                _record(store, int(e["id"]), key, norm, source="normalized",
                        confirmed=True, ts=now)
            return int(e["id"])

    # 4) embedding cosine — proposal only, recurrence-gated auto-confirm.
    cos_min, autoconfirm_n = _cfg()
    qv = _embed(norm)
    if qv is None or not entities:
        return None
    best, best_sim = None, -1.0
    for e in entities:
        sim = _cos(qv, _embed(normalize(e.get("name") or "")))
        if sim > best_sim:
            best, best_sim = e, sim
    if best is None or best_sim < cos_min:
        return None
    if not record:
        return None  # a probe must not bind on an unconfirmed proposal
    row = _record(store, int(best["id"]), key, norm, source="embedding",
                  confirmed=False, ts=now)
    if row is None:
        return None
    if row["confirmed"]:
        return int(best["id"])  # confirmed earlier (human or recurrence)
    if len(row.get("seen_days") or []) >= autoconfirm_n:
        try:
            store.confirm_entity_alias(int(row["id"]), True)
        except Exception:
            return None
        return int(best["id"])
    return None  # proposal recorded; Console review (or recurrence) decides


def _record(store: Store, entity_id: int, alias: str, norm: str, *,
            source: str, confirmed: bool, ts: float) -> dict | None:
    try:
        return store.upsert_entity_alias(
            entity_id, alias, norm, source=source, confirmed=confirmed,
            ts=ts, day=_day(ts))
    except Exception:
        return None


def proposals(store: Store | None = None, limit: int = 200) -> list[dict]:
    """Unconfirmed alias proposals for the Console review queue, with the
    target entity's name attached."""
    store = store if store is not None else get_store()
    rows = store.list_entity_aliases(confirmed=False, limit=limit)
    emap = {e["id"]: e for e in store.all_entities(include_hidden=True)}
    for r in rows:
        ent = emap.get(r["entity_id"])
        r["entity_name"] = ent["name"] if ent else None
        r["entity_kind"] = ent["kind"] if ent else None
    return rows
