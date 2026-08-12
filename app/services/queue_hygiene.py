"""People v3 WS-E — review-queue hygiene: ambient TTL + queue SLO.

The review queue's perceived noise is mostly pileup: screen- and
document-mined facts nobody will ever review sit in the unreviewed queue
forever. The TTL sweep archives (never deletes) ACTIVE, unreviewed,
ambient-sourced facts below the confidence ceiling that nothing referenced
for `ttl_days` — no grounding hit, no field impression, nothing. Archived
rows keep provenance and stay retrievable in timeline search; they just stop
being queue and grounding load. Speech-derived facts are exempt by
construction (source filter) — they are the product.

The queue SLO turns "the queue feels noisy" into two numbers the Console can
show: unreviewed depth (target < 25) and age p50 (target < 48h). If the queue
can't stay under that with real usage, extraction is too chatty — that's a
signal, not a UI problem.

Flag: QUILL_QUEUE_TTL (default off — P1 rollout, flips after the eval pass).
Job kind: `queue_ttl`, registered in app/main.py, enqueued daily by attach()
(same self-rescheduling timer pattern as kg_parity).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from app.config import settings

# Ambient sources the TTL applies to. Everything else — speech above all —
# is exempt. Prefixes match events.source.
TTL_SOURCE_PREFIXES = ("desktop.screen", "documents.")

# Queue SLO targets (WS-E). Constants, not config: they are a product
# definition ("what does 'not noisy' mean"), not a tuning knob.
SLO_DEPTH = 25
SLO_AGE_P50_S = 48 * 3600.0

_attach_lock = threading.Lock()
_timer: threading.Timer | None = None
_INTERVAL_S = float(os.environ.get("QUILL_QUEUE_TTL_INTERVAL_S", "86400")
                    or "86400")


def enabled() -> bool:
    return settings.facts.ttl_enabled


def _store():
    from app.services.memory import memory
    return memory._ensure_store()


def sweep_ttl(store=None, *, now: float | None = None) -> dict[str, Any]:
    """One TTL pass. Archive-only and idempotent: a second run over the same
    state archives nothing. Returns counts for the Console / job log."""
    now = float(now if now is not None else time.time())
    s = store if store is not None else _store()
    cfg = settings.facts
    cutoff = now - cfg.ttl_days * 86400.0
    ids = s.ttl_archive_candidates(cutoff, cfg.ttl_max_conf,
                                   TTL_SOURCE_PREFIXES)
    archived = 0
    for fid in ids:
        try:
            if s.archive_fact(fid, now):
                archived += 1
        except Exception as exc:
            print(f"[queue_ttl] archive of fact {fid} skipped ({exc}).")
    report = {"ts": now, "eligible": len(ids), "archived": archived,
              "ttl_days": cfg.ttl_days, "max_conf": cfg.ttl_max_conf}
    if archived:
        print(f"[queue_ttl] archived {archived} unreferenced ambient fact(s) "
              f"older than {cfg.ttl_days:g}d.")
    return report


def queue_slo(store=None, *, now: float | None = None) -> dict[str, Any]:
    """Unreviewed-queue depth and age vs the WS-E targets."""
    now = float(now if now is not None else time.time())
    s = store if store is not None else _store()
    stats = s.unreviewed_queue_stats(now)
    return {
        **stats,
        "target_depth": SLO_DEPTH,
        "target_age_p50_s": SLO_AGE_P50_S,
        "ok": (stats["depth"] < SLO_DEPTH
               and stats["age_p50_s"] < SLO_AGE_P50_S),
    }


def run_job(_payload=None) -> None:
    """Worker handler for job kind `queue_ttl`."""
    if not enabled():
        return
    sweep_ttl()


def attach() -> None:
    """Daily TTL enqueue while the flag is on (kg_parity pattern)."""
    if not enabled():
        return
    with _attach_lock:
        _schedule_next()
    print(f"[queue_ttl] attached (every {int(max(60.0, _INTERVAL_S))}s).")


def _schedule_next() -> None:
    global _timer
    if not enabled():
        return
    delay = max(60.0, _INTERVAL_S)

    def _tick() -> None:
        try:
            from app.services.worker import worker
            worker.enqueue("queue_ttl", unique=True)
        except Exception as exc:
            print(f"[queue_ttl] schedule tick skipped ({exc}).")
        with _attach_lock:
            _schedule_next()

    t = threading.Timer(delay, _tick)
    t.daemon = True
    t.start()
    _timer = t
