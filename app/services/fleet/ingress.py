"""Fleet ingress (Phase 1): one authenticated write path for local agents.

publish(agent, body):
  1. the agent-facing gate — only the fields an agent may set
  2. topic must be one the agent registered for
  3. per-agent rate limit (sliding 60 s window)
  4. stamp producer = agent:<name>, a fresh origin_id, ts, hops = 0
  5. full envelope validation
  6. record the origin as our own (so it is dropped if it ever comes back)
  7. emit Event(source="fleet.signal") — timeline, memory, local fan-out
  8. route: share | offer | local | refused (router.py)

An agent never addresses a peer: the body has no recipient field, and the
router alone decides whether a signal leaves.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app.config import settings
from app.services.fleet import dedup, feed, registry, router, state
from app.services.fleet import envelope as env


class PublishError(Exception):
    def __init__(self, status: int, code: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.code = code
        self.reason = reason

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.code, "reason": self.reason}


_rate_lock = threading.Lock()
_rate: dict[str, deque] = defaultdict(deque)
# (agent, signal_id) -> published item, so an agent's retry is idempotent.
_recent: dict[tuple[str, str], tuple[float, dict]] = {}


def reset() -> None:
    """Tests only."""
    with _rate_lock:
        _rate.clear()
        _recent.clear()


def _take_slot(name: str, now: float) -> bool:
    limit = settings.fleet.max_signals_per_min
    with _rate_lock:
        q = _rate[name]
        while q and q[0] <= now - 60.0:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def _idempotent_hit(name: str, signal_id: str, now: float) -> dict | None:
    with _rate_lock:
        for k in [k for k, (exp, _) in _recent.items() if exp <= now]:
            _recent.pop(k, None)
        hit = _recent.get((name, signal_id))
        return hit[1] if hit else None


def publish(agent: dict, body) -> dict:
    if not settings.fleet.enabled:
        raise PublishError(404, "fleet_off", "fleet federation is off")
    try:
        env.check_agent_input(body)
    except env.SignalError as exc:
        raise PublishError(422, exc.code, exc.reason) from None
    name = agent["name"]
    topic = str(body.get("topic") or "").strip().lower()
    if not registry.can_publish(agent, topic):
        raise PublishError(403, "topic_not_allowed",
                           f"agent {name!r} may not publish to {topic!r}")
    now = time.time()
    signal_id = str(body.get("signal_id") or "").strip()
    if signal_id:
        prior = _idempotent_hit(name, signal_id, now)
        if prior is not None:
            return {**prior, "duplicate": True}
    if not _take_slot(name, now):
        raise PublishError(429, "rate_limited",
                           f"over {settings.fleet.max_signals_per_min} "
                           "signals per minute")

    expires_at = body.get("expires_at")
    if expires_at is None:
        expires_at = now + settings.fleet.default_ttl_s
    stamped = {
        **body,
        "topic": topic,
        "signal_id": signal_id or env.new_id(),
        "origin_id": env.new_id(state.instance_tag()),
        "producer": f"agent:{name}",
        "ts": now,
        "expires_at": expires_at,
        "sources": body.get("sources") or [],
        "hops": 0,
        "sig": "",
    }
    try:
        sig = env.validate(stamped, now=now, max_hops=settings.fleet.max_hops)
    except env.SignalError as exc:
        raise PublishError(422, exc.code, exc.reason) from None
    # Store the normalized form so every later hash and signature is over the
    # same bytes no matter how the agent spelled optional fields.
    signal = sig.to_dict()
    dedup.record_own(signal["origin_id"], signal["expires_at"])
    item = feed.emit(signal, provenance="local")
    try:
        decision = router.apply(signal)
    except Exception as exc:  # a routing fault keeps the signal home
        print(f"[fleet] routing failed, signal stays local ({exc}).")
        decision = router.Decision("local", f"routing error: {exc}")
    out = {"ok": True, "signal": signal, "seq": item["seq"],
           "route": decision.as_dict()}
    with _rate_lock:
        _recent[(name, signal["signal_id"])] = (signal["expires_at"], out)
    return out
