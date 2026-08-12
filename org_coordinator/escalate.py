"""Score strategic relevance and pick skip-level escalation targets."""
from __future__ import annotations

import json
import os
from typing import Any

from org_coordinator import store


def _claude_json(system: str, user: str) -> dict:
    try:
        import anthropic
    except ImportError:
        return {"escalate": False, "score": 0.0, "reason": "no anthropic",
                "target_role": "manager"}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"escalate": False, "score": 0.0, "reason": "no_api_key",
                "target_role": "manager"}
    model = os.environ.get("QUILL_ORG_ESCALATE_MODEL", "claude-opus-4-8")
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model, max_tokens=600, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(getattr(b, "text", "") for b in msg.content
                   if getattr(b, "type", None) == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except Exception:
        return {"escalate": False, "score": 0.0, "reason": text[:400],
                "target_role": "manager"}


_SYSTEM = (
    "Decide whether a work blocker/signal should escalate past the immediate "
    "manager to an exec/CEO. Return ONLY JSON: "
    "{escalate: bool, score: 0-1, reason: string, target_role: 'manager'|'exec'|'ceo'}. "
    "Escalate only when the issue materially affects company strategy, launch "
    "timelines, revenue, safety, or cross-org commitments."
)


def classify(signal: dict[str, Any]) -> dict[str, Any]:
    blob = json.dumps(signal, ensure_ascii=False)[:6000]
    out = _claude_json(_SYSTEM, f"Signal:\n{blob}\n\nReturn JSON only.")
    # Keyword / offline fallback when model is unavailable
    if out.get("fallback") or out.get("reason") in ("no_api_key", "no anthropic"):
        text = " ".join(str(x) for x in (
            signal.get("summary"), signal.get("blockers"), signal.get("text"),
        )).lower()
        keys = ("launch", "revenue", "outage", "legal", "security", "delay",
                "blocked", "ceo", "board", "customer churn")
        hit = bool(signal.get("force_strategic")) or any(k in text for k in keys)
        out = {
            "escalate": hit,
            "score": 0.7 if hit else 0.2,
            "reason": "keyword_fallback" if hit else "below_threshold",
            "target_role": "exec" if hit else "manager",
        }
    elif signal.get("force_strategic") and not out.get("escalate"):
        out["escalate"] = True
        out["score"] = max(float(out.get("score") or 0), 0.75)
        out["target_role"] = out.get("target_role") or "exec"
        out["reason"] = (out.get("reason") or "") + " [force_strategic]"
    return out


def route(node_id: str, signal: dict[str, Any]) -> dict[str, Any]:
    decision = classify(signal)
    target_role = decision.get("target_role") or "manager"
    target = None
    if decision.get("escalate") and target_role in ("exec", "ceo"):
        target = store.skip_level_target(node_id, min_role=target_role)
    if target is None:
        # Default: immediate manager
        node = store.get_node(node_id)
        mid = (node or {}).get("reports_to") or ""
        target = store.get_node(mid) if mid else None
    rec = store.append_escalation({
        "from_node_id": node_id,
        "decision": decision,
        "target_node_id": (target or {}).get("node_id"),
        "target_role": (target or {}).get("role"),
        "signal_summary": (signal.get("summary") or signal.get("text") or "")[:400],
    })
    return {
        "ok": True,
        "decision": decision,
        "target": target,
        "escalation_id": rec.get("id"),
    }
