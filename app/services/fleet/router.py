"""Outbound router (Phase 3): Sparrow, not the agent, decides what leaves.

Rules live in data/fleet_routes.json and fail closed:

    {"rules": [
        {"topic": "research.*", "producer": "*", "action": "share"},
        {"topic": "sales.leads", "producer": "agent:scout", "action": "offer"}
     ],
     "never_share": {"topics": ["hr.*", "legal.*"], "kinds": ["incident"]}}

  share  signed and sent to the relay immediately (nothing, until opted in)
  offer  queued as an approval packet on the Team page (a rule's default)
  local  never leaves this Sparrow (anything unmatched)

The most specific matching rule wins (exact topic over glob, named producer
over "*"); on a tie the most restrictive action wins. A missing or malformed
routes file means no rules, so everything stays local.

`never_share` names topics and kinds that always need a human: a `share` rule
on such a topic is refused when written, and any match (topic or kind) is
downgraded to `offer` when enforced, in case the file was edited by hand.

Outbound signals never pass through compose_peer_claims or any LLM: they get
the envelope's egress validation (registered kind, internal_ok licences, hop
cap) and the blocked-subjects list, and are forwarded byte-for-byte.
"""
from __future__ import annotations

import fnmatch
import json
import re
import sys
import time
from dataclasses import dataclass

from app.config import settings
from app.services.fleet import _files, kinds
from app.services.fleet import envelope as env

ACTIONS = ("share", "offer", "local")
_RESTRICTIVENESS = {"local": 2, "offer": 1, "share": 0}
_TOPIC_GLOB_RE = re.compile(r"^[a-z0-9*][a-z0-9._*-]{0,63}$")
_PRODUCER_RE = re.compile(r"^(\*|agent:[a-z0-9][a-z0-9_-]{0,47})$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


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


# --- never_share ------------------------------------------------------------------
def _clean_never_share(raw) -> dict:
    if raw is None:
        return {"topics": [], "kinds": []}
    if not isinstance(raw, dict) or set(raw) - {"topics", "kinds"}:
        raise RouteError('never_share must be {"topics": [...], "kinds": [...]}')
    topics, kinds_ = [], []
    for t in raw.get("topics") or []:
        t = str(t).strip().lower()
        if not _TOPIC_GLOB_RE.match(t):
            raise RouteError(f"bad never_share topic {t!r}")
        topics.append(t)
    for k in raw.get("kinds") or []:
        k = str(k).strip().lower()
        if not _KIND_RE.match(k):
            raise RouteError(f"bad never_share kind {k!r}")
        kinds_.append(k)
    return {"topics": sorted(set(topics)), "kinds": sorted(set(kinds_))}


def _globs_overlap(a: str, b: str) -> bool:
    return fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a)


def topic_never_shares(topic: str, never: dict) -> bool:
    return any(_globs_overlap(topic, p) for p in never["topics"])


# --- rules ----------------------------------------------------------------------
def _clean_rule(raw, never: dict) -> dict:
    if not isinstance(raw, dict):
        raise RouteError("each rule must be an object")
    extra = set(raw) - {"topic", "producer", "action", "note"}
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
    if action == "share" and topic_never_shares(topic, never):
        raise RouteError(f"topic {topic!r} is on the never_share list; it can "
                         "never be share (use offer or local)")
    out = {"topic": topic, "producer": producer, "action": action}
    if raw.get("note"):
        out["note"] = str(raw["note"])[:200]
    return out


def load_policy() -> tuple[list[dict], dict]:
    """(rules, never_share). Malformed pieces fail toward local / human."""
    raw = _files.load(settings.fleet.routes_path, None)
    if raw is None:
        return [], _clean_never_share(None)
    if not isinstance(raw, dict) or not isinstance(raw.get("rules"), list):
        print("[fleet] fleet_routes.json has no 'rules' list; everything "
              "stays local.")
        return [], _clean_never_share(None)
    try:
        never = _clean_never_share(raw.get("never_share"))
    except RouteError as exc:
        # An unreadable never_share list must not loosen anything: with no
        # way to know what is sensitive, nothing auto-shares.
        print(f"[fleet] never_share unreadable ({exc}); share becomes offer.")
        never = {"topics": ["*"], "kinds": []}
    out = []
    for r in raw["rules"]:
        try:
            out.append(_clean_rule(r, never))
        except RouteError as exc:
            # A hand-edited share rule on a never_share topic is kept as an
            # offer; enforcement downgrades it again if it slips through.
            if isinstance(r, dict) and str(r.get("action")) == "share":
                try:
                    out.append(_clean_rule(dict(r, action="offer"), never))
                    continue
                except RouteError:
                    pass
            print(f"[fleet] ignoring route rule {r!r} ({exc}).")
    return out, never


def load_rules() -> list[dict]:
    return load_policy()[0]


def save_rules(rules, never_share=None) -> dict:
    """Validate everything first; write nothing if any piece is bad.
    `never_share=None` keeps the current list."""
    if not isinstance(rules, list):
        raise RouteError("rules must be a list")
    never = (load_policy()[1] if never_share is None
             else _clean_never_share(never_share))
    clean = [_clean_rule(r, never) for r in rules]
    with _files.lock:
        _files.save(settings.fleet.routes_path,
                    {"version": 2, "updated_at": time.time(), "rules": clean,
                     "never_share": never})
    return {"rules": clean, "never_share": never}


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


# --- the decision -------------------------------------------------------------------
def outbound_copy(signal: dict) -> dict:
    out = dict(signal)
    out["hops"] = int(out.get("hops") or 0) + 1
    out["sig"] = ""
    return out


def egress_check(signal: dict) -> str | None:
    """None when `signal` may leave; otherwise the refusal reason."""
    try:
        env.validate(outbound_copy(signal), kinds=kinds.registry(),
                     max_hops=settings.fleet.max_hops, outbound=True)
    except env.SignalError as exc:
        return exc.code
    try:
        if kinds.blocklist().blocks(signal.get("subject")):
            return "blocked_subject"
    except kinds.BlockedListUnavailable as exc:
        return f"blocked_list_unavailable: {exc}"
    return content_refusal(signal)


def content_refusal(signal: dict) -> str | None:
    """The same privacy rule every other egress surface applies: the words an
    agent wrote (subject, summary, body) are classified, and sensitive or
    never-send content stays home. Ingress stamps the persisted copy, but the
    relay is sent the signal dict, so the check has to run on the dict."""
    from app.services import privacy_class as pc
    try:
        body = json.dumps(signal.get("body") or {}, ensure_ascii=False,
                          sort_keys=True)
    except (TypeError, ValueError):
        body = str(signal.get("body") or "")
    blob = " ".join(str(x) for x in (signal.get("subject"), signal.get("summary"),
                                     body) if x)
    reason = pc.egress_refusal(blob, source="fleet.signal")
    return f"content_{reason}" if reason else None


def route(signal: dict) -> Decision:
    rules, never = load_policy()
    rule = match(signal.get("topic", ""), signal.get("producer", ""), rules)
    if rule is None:
        return Decision("local", "no matching rule")
    action = rule["action"]
    if action == "local":
        return Decision("local", "rule says local", rule)
    if action == "share" and (
            topic_never_shares(signal.get("topic", ""), never)
            or topic_never_shares(rule["topic"], never)
            or signal.get("kind") in never["kinds"]):
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
            summary=f"Share {signal.get('producer')}'s {signal.get('kind')} "
                    f"signal on {signal.get('topic')}"
                    + (f" about {signal['subject']}" if signal.get("subject")
                       else ""),
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


# The owner may revise the content, not the identity or routing of a signal.
EDITABLE = ("summary", "confidence", "body", "expires_at")


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
        env.validate(sig, kinds=kinds.registry(),
                     max_hops=settings.fleet.max_hops)
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
    # Re-run every egress gate at send time: the blocked list may have
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
