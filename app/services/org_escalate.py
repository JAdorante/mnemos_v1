"""Smart org escalation — classify strategic blockers and notify the right role.

Distinct from model escalate_log (local→Claude distillation). This routes
people-escalation signals through the Org Coordinator and optionally the
peer channel (kind=org_escalate), always as a human offer at the exec node.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.config import settings

_SCHEMA = {
    "type": "object",
    "properties": {
        "escalate": {"type": "boolean"},
        "score": {"type": "number"},
        "reason": {"type": "string"},
        "target_role": {"type": "string",
                        "enum": ["manager", "exec", "ceo"]},
    },
    "required": ["escalate", "score", "reason", "target_role"],
    "additionalProperties": False,
}

_SYSTEM = (
    "Decide whether a work signal should escalate to exec/CEO. Return JSON only. "
    "Escalate only for material strategy/launch/revenue/safety impacts."
)


def _log_path() -> Path:
    return Path(settings.org.escalations_path)


def append_local(row: dict) -> dict:
    p = _log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {**row, "at": time.time()}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def classify_local(signal: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.services.model_router import router
        blob = json.dumps(signal, ensure_ascii=False)[:6000]
        return router.complete_json(
            "org_escalate", system=_SYSTEM,
            messages=[{"role": "user",
                       "content": f"Signal:\n{blob}\n\nReturn JSON only."}],
            schema=_SCHEMA, max_tokens=400,
        )
    except Exception as exc:
        text = " ".join(str(x) for x in (
            signal.get("summary"), signal.get("blockers"), signal.get("text"),
        )).lower()
        keys = ("launch", "revenue", "outage", "legal", "security", "delay",
                "blocked", "strategic")
        hit = bool(signal.get("force_strategic")) or any(k in text for k in keys)
        return {
            "escalate": hit,
            "score": 0.7 if hit else 0.2,
            "reason": f"fallback ({exc})",
            "target_role": "exec" if hit else "manager",
        }


def record_and_notify(digest_or_signal: dict,
                      *, coordinator_result: dict | None = None) -> dict[str, Any]:
    signal = {
        "summary": digest_or_signal.get("summary") or digest_or_signal.get("text") or "",
        "blockers": digest_or_signal.get("blockers") or [],
        "force_strategic": bool(digest_or_signal.get("force_strategic")
                                or digest_or_signal.get("strategic")),
    }
    local = classify_local(signal)
    coord = coordinator_result
    if coord is None:
        try:
            from app.services import org_client
            if org_client.enabled() and org_client.node_token():
                coord = org_client.escalate(signal)
        except Exception as exc:
            coord = {"ok": False, "error": str(exc)}

    decision = local
    if isinstance(coord, dict) and coord.get("decision"):
        decision = coord["decision"]

    rec = append_local({
        "signal": signal,
        "local_decision": local,
        "coordinator": coord,
        "decision": decision,
    })

    # Observed event for Console / memory
    try:
        from app.events import Event, Modality, bus
        from app.services import confidence as _conf
        from app.storage import get_store
        text = (f"[org escalate → {decision.get('target_role')}] "
                f"{signal.get('summary') or signal.get('blockers')}")[:2000]
        ev = Event(time=time.time(), modality=Modality.SYSTEM, raw=text,
                   summary=text[:200], source="org.escalate",
                   meta={"kind": "org_escalate", "decision": decision})
        _conf.attach(ev, _conf.OBSERVED)
        bus.publish_nowait(ev)
        get_store().insert(ev)
    except Exception as exc:
        print(f"[org_escalate] event skipped ({exc}).")

    # Peer notify manager (exec skip-level is coordinator-driven; surface as
    # org_escalate offer on the configured manager peer when paired).
    peer_res = None
    peer_id = ""
    try:
        from app.services import org_client
        peer_id = org_client.manager_peer_id()
    except Exception:
        peer_id = settings.org.manager_peer_id
    if decision.get("escalate") and peer_id:
        try:
            from app.services import peer_channel as pch
            msg = (f"[strategic escalation] {signal.get('summary') or ''}\n"
                   f"Blockers: {'; '.join(signal.get('blockers') or [])}\n"
                   f"Reason: {decision.get('reason')}")[:2000]
            peer_res = pch.ask(peer_id, msg, kind="org_escalate")
        except Exception as exc:
            peer_res = {"ok": False, "error": str(exc)}

    return {"ok": True, "decision": decision, "log_id": rec.get("at"),
            "coordinator": coord, "peer": peer_res}


def run_escalate_check(digest: dict) -> dict[str, Any]:
    return record_and_notify(digest)
