"""Build and ship redacted upward org digests from local memory.

Uses open tasks/commitments, recent reflection, and meta-memory risk signals —
never raw WAV/JPEG. Ships to the Org Coordinator; optionally notifies the
manager peer via peer_channel kind=org_digest.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.config import settings
from app.services import privacy_class as _pc
from app.storage import Store, get_store

_DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "progress": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "asks": {"type": "array", "items": {"type": "string"}},
        "deps": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "strategic": {"type": "boolean"},
    },
    "required": ["summary", "progress", "blockers", "asks", "deps",
                 "confidence", "strategic"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You write a concise status digest for a manager from an employee's local "
    "work memory. Return ONLY JSON matching the schema. No secrets, passwords, "
    "or verbatim private conversation — summarize work progress, blockers, "
    "asks, and cross-team dependencies. Mark strategic=true only if a blocker "
    "threatens launch, revenue, legal/safety, or company-level commitments."
)


def _gather_packet(store: Store, *, period_hours: float = 24.0
                   ) -> tuple[str, float, float]:
    now = time.time()
    since = now - period_hours * 3600.0
    lines: list[str] = [f"Period: last {period_hours:.0f}h."]

    try:
        open_tasks = store.list_facts(kind="task", status="open", limit=40) or []
    except TypeError:
        open_tasks = [f for f in (store.list_facts(kind="task", limit=80) or [])
                      if (f.get("status") or "open") == "open"][:40]
    except Exception:
        open_tasks = []
    try:
        open_comms = store.list_facts(kind="commitment", status="open",
                                      limit=40) or []
    except TypeError:
        open_comms = [f for f in (store.list_facts(kind="commitment",
                                                   limit=80) or [])
                      if (f.get("status") or "open") == "open"][:40]
    except Exception:
        open_comms = []

    open_tasks = [f for f in open_tasks if _shareable_fact(f)]
    open_comms = [f for f in open_comms if _shareable_fact(f)]
    lines.append("OPEN TASKS:")
    for t in open_tasks[:25]:
        lines.append(f"- { (t.get('text') or '')[:200]}")
    lines.append("OPEN COMMITMENTS:")
    for c in open_comms[:25]:
        lines.append(f"- { (c.get('text') or '')[:200]}")

    try:
        prior = store.latest_reflection("daily")
        summary = (prior or {}).get("summary") or ""
        if summary and not _pc.egress_refusal(summary):
            lines.append(f"PRIOR REFLECTION:\n{summary[:600]}")
    except Exception:
        pass

    try:
        from app.services import meta_memory
        audit = meta_memory.run(store=store, write_reflections=False)
        if isinstance(audit, dict):
            risks = audit.get("at_risk") or audit.get("risks") or []
            if risks:
                lines.append("AT-RISK / META:")
                for r in risks[:12]:
                    if isinstance(r, dict):
                        text = r.get('text') or r.get('summary') or str(r)
                    else:
                        text = str(r)
                    if not _pc.egress_refusal(text):
                        lines.append(f"- {text[:200]}")
    except Exception as exc:
        lines.append(f"(meta_memory skipped: {exc})")

    return "\n".join(lines), since, now


def _shareable_fact(f: dict) -> bool:
    """The digest leaves this node (coordinator + manager peer), so a fact
    from a sensitive or never-send capture never enters the packet."""
    if not _pc.egress_allowed(f.get("source_privacy_class")):
        return False
    return not _pc.egress_refusal(f.get("text") or "")


def _scrub_digest(digest: dict[str, Any]) -> dict[str, Any]:
    """Second check on what the model wrote: drop any line that classifies
    sensitive or above and redact secrets in the rest."""
    from app.services.redact import redact_text
    for key in ("progress", "blockers", "asks", "deps"):
        digest[key] = [redact_text(str(x)) for x in digest.get(key) or []
                       if not _pc.egress_refusal(str(x))]
    summary = str(digest.get("summary") or "")
    reason = _pc.egress_refusal(summary)
    digest["summary"] = (f"(summary withheld: {reason})" if reason
                         else redact_text(summary))
    return digest


def _fallback_digest(packet: str) -> dict[str, Any]:
    """Heuristic digest when the model is unavailable (tests / offline)."""
    blockers = []
    progress = []
    for line in packet.splitlines():
        low = line.lower()
        if any(k in low for k in ("block", "stuck", "risk", "delay", "waiting")):
            blockers.append(line.lstrip("- ")[:200])
        elif line.startswith("- "):
            progress.append(line[2:][:200])
    return {
        "summary": "Local heuristic digest (model unavailable).",
        "progress": progress[:8],
        "blockers": blockers[:8],
        "asks": [],
        "deps": [],
        "confidence": 0.35,
        "strategic": bool(blockers),
    }


def build_digest(*, period_hours: float = 24.0,
                 store: Store | None = None) -> dict[str, Any]:
    store = store or get_store()
    packet, since, now = _gather_packet(store, period_hours=period_hours)
    out: dict[str, Any]
    try:
        from app.services.model_router import router
        out = router.complete_json(
            "org_digest", system=_SYSTEM,
            messages=[{"role": "user", "content": packet}],
            schema=_DIGEST_SCHEMA, max_tokens=1200,
        )
    except Exception as exc:
        print(f"[org_digest] model failed ({exc}); using fallback.")
        out = _fallback_digest(packet)

    digest = {
        "summary": (out.get("summary") or "")[:2000],
        "progress": list(out.get("progress") or [])[:20],
        "blockers": list(out.get("blockers") or [])[:20],
        "asks": list(out.get("asks") or [])[:20],
        "deps": list(out.get("deps") or [])[:20],
        "confidence": float(out.get("confidence") or 0.5),
        "force_strategic": bool(out.get("strategic")),
        "period": {"since": since, "until": now, "hours": period_hours},
    }
    return _scrub_digest(digest)


def _format_peer_message(digest: dict, *, from_name: str) -> str:
    parts = [f"[org digest from {from_name}]",
             digest.get("summary") or ""]
    if digest.get("progress"):
        parts.append("Progress: " + "; ".join(digest["progress"][:5]))
    if digest.get("blockers"):
        parts.append("Blockers: " + "; ".join(digest["blockers"][:5]))
    if digest.get("asks"):
        parts.append("Asks: " + "; ".join(digest["asks"][:5]))
    if digest.get("deps"):
        parts.append("Deps: " + "; ".join(digest["deps"][:5]))
    return "\n".join(p for p in parts if p).strip()


def ship_digest(digest: dict | None = None, *,
                notify_manager_peer: bool = True) -> dict[str, Any]:
    from app.services import org_client

    if not org_client.enabled():
        return {"ok": False, "error": "org network disabled"}
    digest = digest or build_digest()
    res = org_client.post_digest(digest)
    peer_res = None
    if notify_manager_peer:
        peer_id = ""
        try:
            peer_id = org_client.manager_peer_id()
        except Exception:
            peer_id = settings.org.manager_peer_id
        if peer_id:
            try:
                from app.services import peer_channel as pch
                msg = _format_peer_message(
                    digest,
                    from_name=(settings.org.display_name
                               or org_client.node_id() or "report"))
                peer_res = pch.ask(peer_id, msg, kind="org_digest")
            except Exception as exc:
                peer_res = {"ok": False, "error": str(exc)}
    # Local escalate path when coordinator flags or digest is strategic
    esc_res = None
    if digest.get("force_strategic") or (
            isinstance(res.get("escalation"), dict)
            and (res["escalation"].get("decision") or {}).get("escalate")):
        try:
            from app.services import org_escalate
            esc_res = org_escalate.record_and_notify(
                digest, coordinator_result=res.get("escalation"))
        except Exception as exc:
            esc_res = {"ok": False, "error": str(exc)}
    return {"ok": bool(res.get("ok")), "coordinator": res,
            "peer": peer_res, "escalation": esc_res, "digest": digest}


def due_for_digest() -> bool:
    from app.services import org_client
    st = org_client._state()
    last = float(st.get("last_digest_at") or 0)
    interval = settings.org.digest_interval_h * 3600.0
    return (time.time() - last) >= interval


def run_digest_job(_payload: dict | None = None) -> dict[str, Any]:
    from app.services import org_client
    if not org_client.enabled():
        return {"skipped": "org network disabled"}
    if not org_client.node_token():
        return {"skipped": "not registered"}
    result = ship_digest()
    if result.get("ok"):
        st = org_client._state()
        st["last_digest_at"] = time.time()
        org_client._save_state(st)
    return result
