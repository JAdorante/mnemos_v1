"""Fan signals out to local agents by topic (Phase 2).

Every accepted signal — from a sibling agent (source "fleet.signal") or from
a peer through the relay (source "peer.signal") — is emitted as an Event on
the bus, so it lands in memory and the timeline like any other context. This
module also subscribes to the bus, keeps a bounded ring, and wakes the SSE
streams of local agents.

Agents read; they never get a reply channel back into the peer. A delivered
item says where it came from (provenance local | peer, origin, hop count) so
an agent can weight a peer's view differently from its own.

Cursor: `seq` is a strictly increasing integer near the emit time in
microseconds. It is stamped into the persisted event's meta, so catch-up past
the ring (or across a restart) reads the event store and stays duplicate-free.
"""
from __future__ import annotations

import asyncio
import itertools
import sys
import threading
import time
from collections import deque

from app.config import settings
from app.events import Event, Modality, bus
from app.services import confidence as _conf
from app.services.fleet.envelope import MAX_TTL_S

SOURCES = ("fleet.signal", "peer.signal")

_lock = threading.Lock()
_ring: deque = deque(maxlen=1000)
_ring_seqs: set[int] = set()
_last_seq = 0
_attached = False
_subs: dict[int, tuple[asyncio.AbstractEventLoop | None, asyncio.Queue]] = {}
_sub_ids = itertools.count(1)


def attach() -> None:
    """Subscribe to the bus once per process."""
    global _attached
    with _lock:
        if _attached:
            return
        bus.subscribe(_on_event)
        _attached = True


def reset() -> None:
    """Tests only: forget the ring and every subscriber."""
    global _last_seq
    with _lock:
        _ring.clear()
        _ring_seqs.clear()
        _subs.clear()
        _last_seq = 0


def next_seq() -> int:
    global _last_seq
    with _lock:
        _last_seq = max(_last_seq + 1, int(time.time() * 1_000_000))
        return _last_seq


def render(signal: dict) -> str:
    """One line for the timeline: who, what kind, about what, and the summary.
    The structured body stays in meta.signal; it is not flattened into text."""
    head = f"{signal.get('producer', '?')} ({signal.get('kind', 'note')})"
    if signal.get("subject"):
        head += f" on {signal['subject']}"
    conf = signal.get("confidence")
    if conf is not None:
        head += f", confidence {float(conf):.2f}"
    return f"{head}: {signal.get('summary', '')}"


def emit(signal: dict, *, provenance: str, peer_node: str = "") -> dict:
    """Publish one accepted signal. Returns the delivery item."""
    attach()
    seq = next_seq()
    source = "fleet.signal" if provenance == "local" else "peer.signal"
    text = render(signal)
    prefix = "[peer signal]" if provenance == "peer" else "[fleet signal]"
    meta = {
        "signal": signal,
        "fleet_seq": seq,
        "provenance": provenance,
        "origin_id": signal.get("origin_id"),
        "producer": signal.get("producer"),
        "hops": signal.get("hops", 0),
        # Observed-tier context only: never approval, never an instruction.
        "never_authorizes": True,
        "origin": "fleet" if provenance == "local" else "peer",
    }
    if peer_node:
        meta["peer_node"] = peer_node
    ev = Event(time=time.time(), modality=Modality.SYSTEM, raw=text,
               summary=f"{prefix} {text[:200]}", source=source, meta=meta)
    # "inferred": an agent's signal is a view, so it never outranks what the
    # user observed or said themselves.
    conf = signal.get("confidence")
    _conf.attach(ev, _conf.INFERRED,
                 model=None if conf is None else float(conf))
    bus.publish_nowait(ev)
    return _item(meta)


def _item(meta: dict) -> dict:
    sig = meta.get("signal") or {}
    out = {
        "seq": int(meta.get("fleet_seq") or 0),
        "provenance": meta.get("provenance") or "local",
        "origin_id": sig.get("origin_id"),
        "producer": sig.get("producer"),
        "hops": sig.get("hops", 0),
        "topic": sig.get("topic"),
        "signal": sig,
    }
    if meta.get("peer_node"):
        out["peer_node"] = meta["peer_node"]
    return out


def _on_event(event: Event) -> None:
    if event.source not in SOURCES:
        return
    meta = event.meta or {}
    if not isinstance(meta.get("signal"), dict) or not meta.get("fleet_seq"):
        return
    _deliver(_item(meta))


def _deliver(item: dict) -> None:
    with _lock:
        if _ring.maxlen != settings.fleet.ring_size:
            _resize(settings.fleet.ring_size)
        if item["seq"] in _ring_seqs:
            return
        if len(_ring) == _ring.maxlen and _ring:
            _ring_seqs.discard(_ring[0]["seq"])
        _ring.append(item)
        _ring_seqs.add(item["seq"])
        subs = list(_subs.values())
    for loop, q in subs:
        try:
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(q.put_nowait, item)
            else:
                q.put_nowait(item)
        except Exception as exc:  # a closed loop must not stall the others
            print(f"[fleet] stream wake skipped ({exc}).")


def _resize(n: int) -> None:
    global _ring
    items = list(_ring)[-max(1, n):]
    _ring = deque(items, maxlen=max(1, n))
    _ring_seqs.clear()
    _ring_seqs.update(i["seq"] for i in _ring)


def subscribe(loop: asyncio.AbstractEventLoop | None = None
              ) -> tuple[int, asyncio.Queue]:
    attach()
    q: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    with _lock:
        sid = next(_sub_ids)
        _subs[sid] = (loop, q)
    return sid, q


def unsubscribe(sid: int) -> None:
    with _lock:
        _subs.pop(sid, None)


# --- delivery rules -------------------------------------------------------------
def deliverable(item: dict, agent: dict, topics: set[str] | None = None,
                now: float | None = None) -> bool:
    """Registered topics bound what an agent sees, whatever it asked for.
    Expired signals are dropped here, at delivery, not only at ingest."""
    allowed = set(agent.get("topics") or [])
    if topics:
        allowed &= set(topics)
    if item.get("topic") not in allowed:
        return False
    sig = item.get("signal") or {}
    now = time.time() if now is None else now
    try:
        if float(sig.get("expires_at") or 0) <= now:
            return False
    except (TypeError, ValueError):
        return False
    # An agent does not need its own signal echoed back.
    if (item.get("provenance") == "local"
            and sig.get("producer") == f"agent:{agent.get('name')}"):
        return False
    return True


def _store():
    """The live store, if the app already opened one. Never imports or opens
    storage itself: tests and a fleet-only process stay off the real data
    dir, and the inbound import boundary stays clean (app.storage lazily
    reaches the agent layer)."""
    st = sys.modules.get("app.storage")
    return getattr(st, "_store", None) if st is not None else None


def catch_up(agent: dict, topics: set[str] | None, since: int,
             limit: int = 500, store=None) -> list[dict]:
    """Deliverable items with seq > since, oldest first, no duplicates."""
    limit = max(1, min(int(limit or 500), 2000))
    items = _collect(int(since or 0), store)
    now = time.time()
    return [i for i in items if deliverable(i, agent, topics, now)][:limit]


def recent(topics: set[str] | None = None, since: int = 0, limit: int = 50,
           store=None) -> list[dict]:
    """The owner's view (MCP `signals` tool): every unexpired signal, any
    topic, newest last. Same provenance fields an agent sees."""
    now = time.time()
    out = []
    for i in _collect(int(since or 0), store):
        if topics and i.get("topic") not in topics:
            continue
        try:
            if float((i.get("signal") or {}).get("expires_at") or 0) <= now:
                continue
        except (TypeError, ValueError):
            continue
        out.append(i)
    return out[-max(1, min(int(limit or 50), 500)):]


def _collect(since: int, store=None) -> list[dict]:
    with _lock:
        ring = list(_ring)
    by_seq = {i["seq"]: i for i in ring if i["seq"] > since}
    oldest = ring[0]["seq"] if ring else None
    if oldest is None or since < oldest:
        store = store if store is not None else _store()
        if store is not None:
            # Nothing older than the TTL cap can still be deliverable.
            t0 = max(since / 1_000_000 - 5.0, time.time() - MAX_TTL_S)
            for source in SOURCES:
                try:
                    rows = store.events_in_window(t0, time.time() + 1,
                                                  source=source, limit=5000)
                except Exception as exc:
                    print(f"[fleet] store catch-up skipped ({exc}).")
                    rows = []
                for r in rows:
                    meta = r.get("meta") or {}
                    seq = int(meta.get("fleet_seq") or 0)
                    if seq > since and seq not in by_seq and \
                            isinstance(meta.get("signal"), dict):
                        by_seq[seq] = _item(meta)
    return [by_seq[s] for s in sorted(by_seq)]
