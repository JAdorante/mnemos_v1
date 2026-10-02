"""Outbound router (Phase 3): Sparrow, not the agent, decides what leaves.

Rules live in data/fleet_routes.json and fail closed:

    {"rules": [
        {"topic": "macro.rates", "producer": "*", "action": "share"},
        {"topic": "equities.*", "producer": "agent:quant", "action": "offer",
         "class": "trading"}
    ]}

  share  signed and sent to the relay immediately (nothing, until opted in)
  offer  queued as an approval packet on the Team page (a rule's default)
  local  never leaves this Sparrow (anything unmatched)

The most specific matching rule wins (exact topic over glob, named producer
over "*"); on a tie the most restrictive action wins. A missing or malformed
routes file means no rules, so everything stays local.

Like `personal` on the peer channel, a rule whose topic names positions,
orders, or P&L can never be `share`: refused when written, and downgraded to
`offer` when enforced, in case the file was edited by hand.

Outbound signals never pass through compose_peer_claims or any LLM: they get
the envelope's egress validation (internal_ok licences, hop cap) and the
compliance restricted list, and are forwarded byte-for-byte.
"""
from __future__ import annotations

import fnmatch
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.services.fleet import _files
from app.services.fleet import envelope as env

ACTIONS = ("share", "offer", "local")
CLASSES = ("trading", "work", "other")
_RESTRICTIVENESS = {"local": 2, "offer": 1, "share": 0}
_TOPIC_GLOB_RE = re.compile(r"^[a-z0-9*][a-z0-9._*-]{0,63}$")
_PRODUCER_RE = re.compile(r"^(\*|agent:[a-z0-9][a-z0-9_-]{0,47})$")
# Topics about holdings or execution. Signals cannot carry those fields, but a
# topic named for them is a sign someone is trying to, so it never auto-shares.
_POSITIONS_RE = re.compile(
    r"(^|[._-])(positions?|orders?|fills?|pnl|p-and-l|p_and_l|exposures?|"
    r"holdings?|book|blotter|trades?|executions?)($|[._-])")


class RouteError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    action: str              # share | offer | local | refused
    reason: str
    rule: dict | None = None

    def as_dict(self) -> dict:
        return {"action": self.action, "reason": self.reason,
                "rule": self.rule}


def names_positions(topic: str) -> bool:
    return bool(_POSITIONS_RE.search((topic or "").lower()))


# --- rules ----------------------------------------------------------------------
def _clean_rule(raw) -> dict:
    if not isinstance(raw, dict):
        raise RouteError("each rule must be an object")
    extra = set(raw) - {"topic", "producer", "action", "class", "note"}
    if extra:
        raise RouteError(f"unknown rule field(s): {', '.join(sorted(extra))}")
    topic = str(raw.get("topic") or "").strip().lower()
    if not _TOPIC_GLOB_RE.match(topic):
        raise RouteError(f"bad topic {topic!r}")
    producer = str(raw.get("producer") or "*").strip().lower()
    if not _PRODUCER_RE.match(producer):
        raise RouteError(f"bad producer {producer!r} (use * or agent:<name>)")
    action = str(raw.get("action") or "offer").strip().lower()
    if action not in ACTIONS:
        raise RouteError(f"action must be one of {ACTIONS}")
    cls = str(raw.get("class") or "other").strip().lower()
    if cls not in CLASSES:
        raise RouteError(f"class must be one of {CLASSES}")
    if action == "share" and names_positions(topic):
        raise RouteError(f"topic {topic!r} names positions or orders; it can "
                         "never be share (use offer or local)")
    out = {"topic": topic, "producer": producer, "action": action,
           "class": cls}
    if raw.get("note"):
        out["note"] = str(raw["note"])[:200]
    return out


def load_rules() -> list[dict]:
    raw = _files.load(settings.fleet.routes_path, None)
    if raw is None:
        return []
    rules = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(rules, list):
        print("[fleet] fleet_routes.json has no 'rules' list; everything "
              "stays local.")
        return []
    out = []
    for r in rules:
        try:
            out.append(_clean_rule(r))
        except RouteError as exc:
            # A share rule edited in by hand on a positions topic lands here
            # too; enforcement below downgrades it if it ever slips through.
            if isinstance(r, dict) and names_positions(str(r.get("topic"))) \
                    and str(r.get("action")) == "share":
                fixed = dict(r, action="offer")
                try:
                    out.append(_clean_rule(fixed))
                    continue
                except RouteError:
                    pass
            print(f"[fleet] ignoring route rule {r!r} ({exc}).")
    return out


def save_rules(rules) -> list[dict]:
    """Validate every rule first; write nothing if any is bad."""
    if not isinstance(rules, list):
        raise RouteError("rules must be a list")
    clean = [_clean_rule(r) for r in rules]
    with _files.lock:
        _files.save(settings.fleet.routes_path,
                    {"version": 1, "updated_at": time.time(), "rules": clean})
    return clean


def _specificity(rule: dict) -> tuple[int, int]:
    return (0 if "*" in rule["topic"] else 1,
            0 if rule["producer"] == "*" else 1)


def match(topic: str, producer: str, rules: list[dict] | None = None
          ) -> dict | None:
    rules = load_rules() if rules is None else rules
    hits = [r for r in rules
            if fnmatch.fnmatchcase(topic, r["topic"])
            and r["producer"] in ("*", producer)]
    if not hits:
        return None
    hits.sort(key=lambda r: (_specificity(r), _RESTRICTIVENESS[r["action"]]),
              reverse=True)
    return hits[0]


# --- restricted list --------------------------------------------------------------
def restricted() -> frozenset[str]:
    """Compliance's list. Raises RouteError when missing or malformed so the
    caller fails closed (nothing leaves) instead of treating it as empty."""
    p = Path(settings.fleet.restricted_list_path)
    if not p.is_file():
        raise RouteError("restricted list missing")
    try:
        return env.load_restricted(json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:
        raise RouteError(f"restricted list unreadable ({exc})") from None


# --- the decision -------------------------------------------------------------------
def outbound_copy(signal: dict) -> dict:
    out = dict(signal)
    out["hops"] = int(out.get("hops") or 0) + 1
    out["sig"] = ""
    return out


def egress_check(signal: dict) -> str | None:
    """None when `signal` may leave; otherwise the refusal reason."""
    try:
        env.validate(outbound_copy(signal), max_hops=settings.fleet.max_hops,
                     outbound=True)
    except env.SignalError as exc:
        return exc.code
    try:
        if env.is_restricted(signal.get("instrument", ""), restricted()):
            return "restricted_instrument"
    except RouteError as exc:
        return f"restricted_list_unavailable: {exc}"
    return None


def route(signal: dict) -> Decision:
    rule = match(signal.get("topic", ""), signal.get("producer", ""))
    if rule is None:
        return Decision("local", "no matching rule")
    action = rule["action"]
    if action == "local":
        return Decision("local", "rule says local", rule)
    if action == "share" and names_positions(rule["topic"]):
        action = "offer"
    refusal = egress_check(signal)
    if refusal:
        return Decision("refused", refusal, rule)
    from app.services.fleet import state
    if not state.relay().get("token") or not state.relay_url():
        return Decision("local", "no relay registered", rule)
    return Decision(action, f"rule says {action}", rule)


def apply(signal: dict) -> Decision:
    """Route and act: share sends now, offer queues a packet."""
    d = route(signal)
    if d.action == "share":
        from app.services.fleet import relay_client
        relay_client.send_async(signal)
    elif d.action == "offer":
        create_offer(signal, rule=d.rule)
    return d


# --- offers (approval packets) -------------------------------------------------------
def _offers() -> dict:
    data = _files.load(settings.fleet.offers_path, {})
    return data if isinstance(data, dict) else {}


def _save_offers(data: dict) -> None:
    _files.save(settings.fleet.offers_path, data)


def _recorder():
    """agent_log Recorder on the live store only; never opens the DB."""
    try:
        st = sys.modules.get("app.storage")
        if st is None or getattr(st, "_store", None) is None:
            return None
        from app.services.agent_log import Recorder
        return Recorder(store=st._store)
    except Exception:
        return None


def create_offer(signal: dict, *, rule: dict | None = None) -> dict:
    h = env.digest(signal)
    offer_id = env.new_id("of")
    packet_id = None
    rec = _recorder()
    if rec is not None:
        packet_id = rec.record_packet(
            summary=f"Share {signal.get('producer')}'s view on "
                    f"{signal.get('instrument')} ({signal.get('topic')})",
            fields={"action": "fleet_share", "signal_sha256": h,
                    "topic": signal.get("topic"),
                    "origin_id": signal.get("origin_id")},
            goal="fleet:offer", approval_required=True, risk_level="medium",
            execution_surface="fleet_relay")
    row = {"offer_id": offer_id, "status": "pending",
           "created_at": time.time(), "signal": signal, "sha256": h,
           "rule": rule, "packet_id": packet_id}
    with _files.lock:
        data = _offers()
        data[offer_id] = row
        _save_offers(data)
    return row


def list_offers(status: str | None = None) -> list[dict]:
    rows = sorted(_offers().values(), key=lambda r: r.get("created_at", 0),
                  reverse=True)
    return [r for r in rows if not status or r.get("status") == status]


def get_offer(offer_id: str) -> dict | None:
    return _offers().get(offer_id)


EDITABLE = ("thesis", "confidence", "direction", "horizon", "expires_at")


def edit_offer(offer_id: str, changes: dict) -> dict:
    """Edit a pending offer. The hash covers the canonical bytes, so any edit
    yields a new sha256 and the earlier approval no longer binds."""
    bad = set(changes or {}) - set(EDITABLE)
    if bad:
        raise RouteError(f"cannot edit {', '.join(sorted(bad))}")
    with _files.lock:
        data = _offers()
        row = data.get(offer_id)
        if not row or row.get("status") != "pending":
            raise RouteError("no pending offer with that id")
        sig = dict(row["signal"], **changes)
        env.validate(sig, max_hops=settings.fleet.max_hops)
        row["signal"] = sig
        row["sha256"] = env.digest(sig)
        row["edited_at"] = time.time()
        data[offer_id] = row
        _save_offers(data)
    return row


def decide_offer(offer_id: str, approve: bool, *, sha256: str = "") -> dict:
    """Approve (must quote the sha256 that was shown) or decline."""
    with _files.lock:
        data = _offers()
        row = data.get(offer_id)
        if not row or row.get("status") != "pending":
            raise RouteError("no pending offer with that id")
        if approve and sha256 != row.get("sha256"):
            raise RouteError("signal changed since it was shown; review the "
                             "current version and approve that")
        row["decided_at"] = time.time()
        row["status"] = "approved" if approve else "declined"
        data[offer_id] = row
        _save_offers(data)
    rec = _recorder()
    if rec is not None and row.get("packet_id"):
        rec.record_decision(row["packet_id"],
                            "approve" if approve else "cancel",
                            approved_via="button")
    if not approve:
        return row
    # Re-run every egress gate at send time: the restricted list may have
    # changed, or the signal may have expired while it waited.
    refusal = egress_check(row["signal"])
    if refusal:
        return _set_offer(offer_id, status="refused", reason=refusal)
    from app.services.fleet import relay_client
    res = relay_client.send(row["signal"])
    return _set_offer(offer_id, status="sent" if res.get("ok") else
                      ("queued" if res.get("queued") else "failed"),
                      reason=res.get("error") or "")


def _set_offer(offer_id: str, **fields) -> dict:
    with _files.lock:
        data = _offers()
        row = data.get(offer_id) or {}
        row.update(fields)
        data[offer_id] = row
        _save_offers(data)
        return row
