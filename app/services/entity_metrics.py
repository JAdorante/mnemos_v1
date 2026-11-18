"""Entity model, Phase 5: is the Orgs & tools tab staying fixed? One seat.

Every number is this seat's own, read from its own store, so a seat that
mints junk cannot hide behind one that does not (`GET /entities/metrics`
answers for the seat it runs on; `scripts/entity_metrics.py` prints one row
per store copy). Nothing here keeps or returns a name.

- forget rate: the rows the user forgot in the window over the rows the tab
  showed in it (visible now + forgotten then). One forget is one row,
  however many names it tombstoned, and stays counted when the user later
  adds the name back (`entity_candidate_log`, event `forgotten`, written by
  `Store.delete_entity`). A seat whose extraction mints junk shows it here
  first. `repair_drops` counts the rows the repair deleted the same way.
- kind-correction rate: the rows whose kind the user changed in the window
  (`decisions`, surface `entities.kind`, verdict `edited`: the tab's kind
  menu and the map's reclassify) over the visible rows.
- never seen twice: the visible rows with at most one supporting event
  (`entity_repair.support`: facts, ledger sightings, edges and the event
  that minted the row, counted as the repair's rule 10 counts them), over
  the visible rows. A state, not a window: what the tab holds now.
- candidates: names with pending sightings now; candidates the candidate
  gate promoted (not a manual add, a filing or an email-domain org, which
  mint without the gate) and names whose sightings expired in the window
  (`entity_candidate_log`, counts only); candidates the user dismissed in
  the window.

`days` 0 (or None) is all time. `now` moves the window's end (the script
reads a snapshot "as of" its own latest event).
"""
from __future__ import annotations

import time

FORGET_REASON = "forget"
DISMISS_REASON = "candidate_dismiss"


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def compute(store, *, days: float | None = 7.0,
            now: float | None = None) -> dict:
    """This seat's entity metrics over the last `days` (see the module
    docstring). Read-only; a store from before the entity-model tables
    reads as zeros where it has nothing."""
    until = float(now if now is not None else time.time())
    since = until - float(days) * 86400.0 if days else None
    c = store.entity_metric_counts(since=since, until=until)
    log = store.entity_candidate_log_counts(since=since, until=until)

    def logged(event: str) -> int:
        return int((log.get(event) or {}).get("names") or 0)

    forgotten = logged("forgotten")
    dismissed = logged("dismissed")
    repaired = logged("repair_dropped")
    visible = int(c.get("visible") or 0)
    try:
        from app.services import entity_repair
        support = entity_repair.support(store)
    except Exception as exc:
        print(f"[entity_metrics] support counts skipped ({exc}).")
        support = {}
    once = sum(1 for n in support.values() if n <= 1)
    corrected = int(c.get("kind_corrections") or 0)
    return {
        "window": {"days": float(days) if days else None, "since": since,
                   "until": until},
        # `created`: rows first seen in the window that are still here.
        "entities": {"visible": visible, "hidden": int(c.get("hidden") or 0),
                     "created": int(c.get("created") or 0)},
        "forget": {"forgotten": forgotten,
                   "rate": _rate(forgotten, visible + forgotten),
                   "repair_drops": repaired},
        "kind_correction": {"corrected": corrected,
                            "rate": _rate(corrected, visible)},
        "never_seen_twice": {"nodes": once, "of": len(support),
                             "share": _rate(once, len(support))},
        "candidates": {
            "pending": int(c.get("pending_names") or 0),
            "promoted": int((log.get("promoted") or {}).get("names") or 0),
            "expired": int((log.get("expired") or {}).get("names") or 0),
            "expired_sightings": int((log.get("expired") or {})
                                     .get("sightings") or 0),
            "dismissed": dismissed},
    }


def row(label: str, m: dict) -> dict:
    """One flat row of `compute`'s result, for a table per seat."""
    return {"seat": label,
            "visible": m["entities"]["visible"],
            "created": m["entities"]["created"],
            "forgotten": m["forget"]["forgotten"],
            "forget_rate": m["forget"]["rate"],
            "kind_corrected": m["kind_correction"]["corrected"],
            "kind_correction_rate": m["kind_correction"]["rate"],
            "never_seen_twice": m["never_seen_twice"]["nodes"],
            "never_seen_twice_share": m["never_seen_twice"]["share"],
            "pending": m["candidates"]["pending"],
            "promoted": m["candidates"]["promoted"],
            "expired": m["candidates"]["expired"],
            "dismissed": m["candidates"]["dismissed"]}
