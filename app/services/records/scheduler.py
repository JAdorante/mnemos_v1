"""Records-layer background ticks, run on the single worker thread.

  records_tick     every 10 min, only when QUILL_RECORDS=1: build claims from
                   newly accepted candidates, drain the outbox (queued packet
                   submits, expiry notices), heartbeat the org service.
  capture_expiry   due-checked hourly, runs once per UTC day. Mode comes from
                   QUILL_CAPTURE_EXPIRY (default dry_run: receipts only).
"""
from __future__ import annotations

import threading
import time

_TICK_S = 600.0
_EXPIRY_CHECK_S = 3600.0
_lock = threading.Lock()
_timers: dict[str, threading.Timer] = {}


def records_tick() -> dict:
    from app.services.records import claim_builder, org_client
    out: dict = {}
    if not claim_builder.enabled():
        return {"skipped": "QUILL_RECORDS off"}
    out["claims"] = claim_builder.run_once()
    if org_client.joined():
        try:
            out["outbox"] = org_client.drain_outbox()
            org_client.heartbeat()
            out["heartbeat"] = True
        except Exception as exc:
            out["org_error"] = str(exc)
    return out


def expiry_due(store=None, *, now: float | None = None) -> bool:
    """True when no expiry receipt (or dry run) was written this UTC day."""
    from app.services.records import node_store, retention
    if retention.mode() == "off":
        return False
    if store is None:
        from app.storage import get_store
        store = get_store()
    now = float(now if now is not None else time.time())
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    for action in ("expiry.receipt", "expiry.dry_run"):
        last = node_store.audit_entries(store, action=action, limit=1)
        if last and time.strftime("%Y-%m-%d",
                                  time.gmtime(float(last[0]["at"]))) == today:
            return False
    return True


def _every(name: str, delay: float, fn) -> None:
    def _tick() -> None:
        try:
            fn()
        except Exception as exc:
            print(f"[records] {name} tick skipped ({exc}).")
        with _lock:
            _every(name, delay, fn)

    t = threading.Timer(delay, _tick)
    t.daemon = True
    t.start()
    _timers[name] = t


def attach(worker) -> None:
    from app.services.records import retention

    worker.register("records_tick", lambda _p: records_tick())
    worker.register("capture_expiry", lambda _p: retention.sweep())

    def _tick_records() -> None:
        from app.services.records import claim_builder
        if claim_builder.enabled():
            worker.enqueue("records_tick", unique=True)

    def _tick_expiry() -> None:
        if expiry_due():
            worker.enqueue("capture_expiry", unique=True)

    with _lock:
        _every("records_tick", _TICK_S, _tick_records)
        _every("capture_expiry", _EXPIRY_CHECK_S, _tick_expiry)
    print(f"[records] attached (claims every {int(_TICK_S)}s, expiry mode "
          f"{retention.mode()}).")
