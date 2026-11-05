"""One egress policy, decided once.

Data leaves this node five ways: the peer channel, the team layer, org
digests, the fleet relay and the records outbox. Each still owns its policy
*configuration* (a peer's per-class disclosure map, a fleet routing rule, a
human-approved records packet); this module owns the *decision*. Every
outbound path calls `decide()` with (path, destination, scope, data class,
content) plus what its own configuration says, and gets back allow, offer or
deny. Every outcome is written to the same egress trail that /privacy/egress
reads, so the view covers all five paths, not just the two that used to log.

The rule, most restrictive wins:

1. Privacy ceiling. The content's privacy class (declared, or classified from
   the text; a classifier error denies) must be at or below the rung's
   ceiling.
2. The path's own policy: allow / offer / deny (aliases: auto, share, local).
3. A `personal` disclosure class never goes out on its own: allow → offer.
4. The rung (below) may demand a human or close the path.
5. A human approval of this exact item (`approved=True`) turns offer into
   allow. It never lifts a deny.

Rungs (governed egress). The org sets one (records heartbeat), or
QUILL_EGRESS_RUNG overrides it on this node:

  connected   today's behavior: ceiling `personal`, paths decide.
  sealed      ceiling `internal`; nothing leaves without a human (allow →
              offer unless approved).
  perimeter   only human-approved records cross; every other path is denied.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

PATHS = ("peer", "team", "org_digest", "fleet_relay", "records")
RUNGS = ("connected", "sealed", "perimeter")
ACTIONS = ("allow", "offer", "deny")

_ORDER = {"allow": 0, "offer": 1, "deny": 2}
_POLICY_ALIASES = {"auto": "allow", "share": "allow", "allow": "allow",
                   "offer": "offer", "deny": "deny", "local": "deny",
                   "refused": "deny"}


@dataclass(frozen=True)
class Verdict:
    action: str                    # allow | offer | deny
    reason: str
    privacy_class: str
    rung: str
    checks: tuple[str, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @property
    def denied(self) -> bool:
        return self.action == "deny"


_ORG_RUNG_TTL_S = 10.0
_org_rung: tuple[float, str] = (0.0, "")


def rung() -> str:
    """This node's egress rung: env override, else the org's (cached by the
    records heartbeat), else connected. Content filters call this per item,
    so the membership file is read at most every few seconds."""
    global _org_rung
    v = os.environ.get("QUILL_EGRESS_RUNG", "").strip().lower()
    if v in RUNGS:
        return v
    now = time.monotonic()
    if now - _org_rung[0] >= _ORG_RUNG_TTL_S:
        org = ""
        try:
            org = str(_membership().get("rung") or "").lower()
        except Exception:
            pass
        _org_rung = (now, org)
    return _org_rung[1] if _org_rung[1] in RUNGS else "connected"


def _membership() -> dict:
    """The records membership file (records/org_client.py writes it on each
    heartbeat). Read directly: privacy_class imports this module, and
    org_client's imports would give every privacy check — including the
    fleet inbound path — a static route to the agent bridge."""
    import json
    from pathlib import Path
    from app.config import settings
    data = json.loads((Path(settings.storage.data_dir)
                       / "org_membership.json").read_text("utf-8"))
    return data if isinstance(data, dict) else {}


def ceiling(r: str | None = None) -> str:
    from app.services import privacy_class as pc
    return pc.PERSONAL if (r or rung()) == "connected" else pc.INTERNAL


def classify(text: str = "", *, privacy_class: str | None = None,
             source: str = "") -> str:
    """The content's privacy class. Raises when the classifier fails, so
    callers fail closed."""
    from app.services import privacy_class as pc
    return pc.max_class(privacy_class, pc.classify_text(text or "",
                                                        source=source)
                        if text else None) or pc.INTERNAL


def content_refusal(text: str = "", *, privacy_class: str | None = None,
                    source: str = "") -> str | None:
    """Just the ceiling check, for paths that filter items before deciding
    on the whole send (peer claims, fleet signals). None when the content may
    leave under this node's rung."""
    from app.services import privacy_class as pc
    try:
        cls = classify(text, privacy_class=privacy_class, source=source)
    except Exception as exc:
        return f"privacy_class unavailable ({type(exc).__name__})"
    if pc.rank(cls) <= pc.rank(ceiling()):
        return None
    return f"privacy_class={cls}"


def decide(*, path: str, destination: str, policy: str = "offer",
           scope: str = "personal", data_class: str = "other",
           text: str = "", privacy_class: str | None = None,
           approved: bool = False, source: str = "", log: bool = True,
           meta: dict[str, Any] | None = None) -> Verdict:
    """allow / offer / deny for one outbound send. See the module docstring
    for the rule. `log=False` is for a pre-check whose send is logged later
    (a fleet route computed now, sent when the human approves)."""
    from app.services import privacy_class as pc
    r = rung()
    checks: list[str] = []
    action, reason = "allow", "allowed"

    def tighten(a: str, why: str) -> None:
        nonlocal action, reason
        checks.append(f"{a}:{why}")
        if _ORDER[a] > _ORDER[action]:
            action, reason = a, why

    try:
        cls = classify(text, privacy_class=privacy_class, source=source)
    except Exception as exc:
        cls = pc.SENSITIVE
        tighten("deny", f"privacy_class unavailable ({type(exc).__name__})")
    if pc.rank(cls) > pc.rank(ceiling(r)):
        tighten("deny", f"privacy_class={cls}")

    p = _POLICY_ALIASES.get(str(policy or "").lower())
    if p is None:
        tighten("deny", f"unknown policy {policy!r}")
    elif p != "allow":
        tighten(p, f"policy {p}")
    if data_class == "personal":
        tighten("offer", "personal needs a human")

    if path not in PATHS:
        tighten("deny", f"unknown path {path!r}")
    elif r == "perimeter" and not (path == "records" and approved):
        tighten("deny", "rung perimeter: only approved records leave")
    elif r == "sealed":
        tighten("offer", "rung sealed: a human approves each send")

    if approved and action == "offer":
        action, reason = "allow", "approved by a human"
        checks.append("allow:approved")

    v = Verdict(action, reason, cls, r, tuple(checks))
    if log:
        _log(v, path=path, destination=destination, scope=scope,
             data_class=data_class, approved=approved, meta=meta)
    return v


def record_send(v: Verdict, *, path: str, destination: str, ok: bool,
                scope: str = "personal", data_class: str = "other",
                approved: bool = False, approving_action: str | None = None,
                meta: dict[str, Any] | None = None) -> None:
    """Log the send itself, once it has happened (or failed), for a verdict
    taken earlier with log=False. `approving_action` names the human act
    more precisely than "human" when the path knows it (user_tap)."""
    _log(v, path=path, destination=destination, scope=scope,
         data_class=data_class, approved=approved, meta=meta, sent=ok,
         approving_action=approving_action)


def _log(v: Verdict, *, path: str, destination: str, scope: str,
         data_class: str, approved: bool, meta: dict | None,
         sent: bool | None = None,
         approving_action: str | None = None) -> None:
    try:
        from app.services.model_log import model_log
        m = {"egress_action": v.action, "reason": v.reason, "rung": v.rung,
             "scope": scope, "data_class": data_class}
        if sent is not None:
            m["sent"] = bool(sent)
        if meta:
            m.update(meta)
        model_log.log_egress(
            kind=path, destination=str(destination or "?"),
            privacy_class=v.privacy_class,
            approving_action=approving_action or (
                "human" if approved else "policy"),
            ok=(v.action != "deny") if sent is None else bool(sent),
            action=v.action, meta=m)
    except Exception as exc:
        print(f"[egress] log skipped ({exc}).")
