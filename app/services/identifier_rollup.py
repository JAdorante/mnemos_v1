"""Identifier rollup (WS2c) — stamped screen identifiers become graph
evidence.

`app/perception/identifiers.py` stamps verbatim identifiers onto
desktop.screen events at capture time. This module turns them into derived
`entity —observed_on_screen→ event` edges: each identifier norm is resolved
against EXISTING entities via the alias-aware resolver (bind-only — an
identifier can never mint an entity, and rollup passes record=False so
re-scanning old events cannot inflate alias recurrence evidence), and the
edge weight carries the per-event evidence count.

Runs inside graph.rebuild() as an additional derivation source (counter key
`observed_on_screen`): edges are origin='derived', so the wipe/re-derive
cycle keeps them idempotent, and erase_event's relation cascade removes an
erased frame's evidence automatically.
"""
from __future__ import annotations

import time as _time

from app.storage import Store, get_store


def aggregate_identifiers(events: list[dict], *,
                          t0: float | None = None,
                          t1: float | None = None) -> dict[str, int]:
    """{identifier norm: evidence count} over stamped events, optionally
    restricted to a time window (an activity block). Pure — the testable
    core of the per-block aggregation."""
    out: dict[str, int] = {}
    for ev in events or []:
        t = float(ev.get("time") or 0)
        if t0 is not None and t < t0:
            continue
        if t1 is not None and t > t1:
            continue
        for ident in ev.get("identifiers") or []:
            norm = str((ident or {}).get("norm") or "").strip()
            if norm:
                out[norm] = out.get(norm, 0) + 1
    return out


def derive_edges(store: Store | None = None, *,
                 neighborhood: set | None = None,
                 now: float | None = None,
                 limit: int = 5000) -> int:
    """Write entity—observed_on_screen→event edges from stamped identifiers.

    `neighborhood` (graph.rebuild dirty scope) restricts writes to edges
    incident to it — None means everything (full rebuild). Returns the
    number of edges written. Best-effort: resolution failures skip the
    identifier, never the pass.
    """
    store = store or get_store()
    now = now if now is not None else _time.time()
    try:
        rows = store.events_with_identifiers(limit=limit)
    except Exception as exc:
        print(f"[identifier_rollup] scan skipped ({exc}).")
        return 0

    def _in_nb(nt: str, nid: int) -> bool:
        return neighborhood is None or (nt, int(nid)) in neighborhood

    from app.services import entity_alias
    # Per-pass resolution cache: the same slug appears on many frames.
    cache: dict[str, int | None] = {}
    n_edges = 0
    for ev in rows:
        eid_event = int(ev["id"])
        hits: dict[int, int] = {}
        for ident in ev.get("identifiers") or []:
            norm = str((ident or {}).get("norm") or "").strip()
            kind = str((ident or {}).get("kind") or "")
            if not norm or kind not in ("repo", "title_segment", "path"):
                continue
            key = norm.lower()
            if key not in cache:
                try:
                    cache[key] = entity_alias.resolve(
                        norm, store=store, ts=now, record=False)
                except Exception:
                    cache[key] = None
            ent = cache[key]
            if ent:
                hits[int(ent)] = hits.get(int(ent), 0) + 1
        for ent_id, count in hits.items():
            if not (_in_nb("entity", ent_id) or _in_nb("event", eid_event)):
                continue
            try:
                store.add_relation(
                    "entity", ent_id, "observed_on_screen", "event",
                    eid_event, weight=float(count), origin="derived",
                    source_event_id=eid_event, ts=now)
                n_edges += 1
            except Exception as exc:
                print(f"[identifier_rollup] edge skipped ({exc}).")
    return n_edges
