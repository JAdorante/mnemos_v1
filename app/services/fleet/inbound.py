"""Inbound signals from the firm relay (Phase 5). Most of the safety lives here.

A signal from a peer fleet becomes observed-tier context and nothing more:

  * only the relay's own inbound credential may deliver one; a paired peer
    cannot push a signal over /peer/ask directly
  * the relay's HMAC and the full envelope are verified again on arrival —
    the relay is trusted for routing, not for content
  * an origin_id this Sparrow's fleet minted is dropped (our own view coming
    back must never look like confirmation)
  * a replayed origin_id is deduped; hops > max_hops and expired signals are
    refused
  * it is emitted as Event(source="peer.signal") — source class peer_signal
    (claims only, never commitments or people), never_authorizes, and
    source_can_authorize() is False for it
  * it is never routed, so it is never re-forwarded. Re-sharing needs a new
    signal with a new origin_id and derived_from set.
  * nothing is queued as an ask, so no reply traffic is generated

Import boundary: this module must never import agent_planner, browser_agent,
or desktop_agent, directly or transitively (tests/test_fleet_inbound.py walks
the import graph to prove it).
"""
from __future__ import annotations

from app.config import settings
from app.services.fleet import dedup, feed, kinds, state
from app.services.fleet import envelope as env


def handle(authorization: str | None, body) -> tuple[int, dict]:
    """(http_status, response) for one POST /peer/ask with kind="signal"."""
    if not settings.fleet.enabled:
        return 404, {"ok": False, "error": "fleet_off"}
    if not state.inbound_token_matches(authorization):
        return 401, {"ok": False, "error": "signals arrive only from the "
                                           "registered relay"}
    if not isinstance(body, dict) or not isinstance(body.get("signal"), dict):
        return 422, {"ok": False, "error": "body.signal must be an object"}
    signal = body["signal"]
    if not env.verify(signal, state.inbound_key()):
        return 403, {"ok": False, "error": "bad_signature"}
    try:
        sig = env.validate(signal, kinds=kinds.registry(),
                           max_hops=settings.fleet.max_hops,
                           outbound=True)
    except env.SignalError as exc:
        return 422, exc.as_dict()
    if dedup.is_own(sig.origin_id):
        return 200, {"ok": True, "status": "dropped", "reason": "own_origin"}
    if not dedup.check_and_mark_seen(sig.origin_id, sig.expires_at):
        return 200, {"ok": True, "status": "duplicate"}
    peer_node = str(body.get("sender_node") or "")[:64]
    item = feed.emit(signal, provenance="peer", peer_node=peer_node)
    return 200, {"ok": True, "status": "accepted", "seq": item["seq"]}
