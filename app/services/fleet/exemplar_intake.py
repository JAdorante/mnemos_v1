"""File a teammate's shared exemplar into this Sparrow's exemplar store.

The receiving half of exemplar_share.py. inbound.py has already verified the
relay's credential and HMAC, the envelope, the hop cap, expiry, and dedupe,
and emitted the signal as a peer.signal Event. This module subscribes to the
bus and, with QUILL_FLEET_EXEMPLARS_ACCEPT=1, embeds an `exemplar` signal
into the exemplar store as tier `peer_shared`.

Why a bus subscriber and not a call from inbound.py: the exemplar store
lazily reaches app.storage, and from there the agent layer, which inbound's
import boundary forbids (tests/test_fleet_inbound.py). Keeping the hop here
leaves that boundary untouched.

What a peer exemplar can and cannot do:
  * it rides only the LOCAL model's prompt as a few-shot demonstration, like
    every exemplar; it never reaches the parent model, authorizes nothing,
    and creates no learning pair (so it never feeds LoRA curation)
  * it ranks below every local tier at equal similarity
  * the `_peer` gate silences all of them at retrieval, and purge_peer()
    deletes them
  * the receiver re-runs the same `internal` ceiling on the text: a peer's
    personal or sensitive example is not kept even if the sender let it go

Embedding is slow, so the work runs on a short-lived thread rather than on
the event loop that dispatches the bus.
"""
from __future__ import annotations

import os
import threading

from app.config import settings
from app.events import Event

KIND = "exemplar"

_lock = threading.Lock()
_attached: set[int] = set()


def accept_enabled() -> bool:
    return bool(settings.fleet.enabled) and os.environ.get(
        "QUILL_FLEET_EXEMPLARS_ACCEPT", "0") in ("1", "true", "True")


def attach(bus=None) -> None:
    """Subscribe once per bus. Flags are read per event, so attaching while
    off is harmless."""
    if bus is None:
        from app.events import bus as default_bus
        bus = default_bus
    with _lock:
        if id(bus) in _attached:
            return
        bus.subscribe(on_event)
        _attached.add(id(bus))


def on_event(event: Event, *, sync: bool = False) -> None:
    if event.source != "peer.signal":
        return
    meta = event.meta or {}
    sig = meta.get("signal")
    if meta.get("provenance") != "peer" or not isinstance(sig, dict):
        return
    if sig.get("kind") != KIND or not accept_enabled():
        return
    if sync:
        ingest(sig)
    else:
        threading.Thread(target=ingest, args=(sig,), daemon=True,
                         name="fleet-exemplar-intake").start()


def ingest(signal: dict) -> str | None:
    """Embed one peer exemplar. Returns the exemplar id, or None when it is
    refused or the store is off. Never raises."""
    try:
        from app.services.fleet.exemplar_share import (SHAREABLE_TYPES,
                                                       privacy_refusal)
        body = signal.get("body") or {}
        task_type = str(body.get("task_type") or "")
        text = str(body.get("input_text") or "")
        target = str(body.get("target_text") or "")
        if task_type not in SHAREABLE_TYPES or not text or not target:
            return None
        refusal = privacy_refusal(f"{text}\n{target}", source="peer.signal")
        if refusal:
            print(f"[fleet] peer exemplar not kept ({refusal}).")
            return None
        from app.services import exemplar_store
        return exemplar_store.exemplar_store.ingest_peer(
            origin_id=str(signal.get("origin_id") or ""),
            task_type=task_type, input_text=text, target_text=target,
            created_at=signal.get("ts"))
    except Exception as exc:
        print(f"[fleet] peer exemplar intake skipped ({exc}).")
        return None
