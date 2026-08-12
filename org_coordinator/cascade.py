"""Turn company goals into per-role priority packets (Anthropic parent)."""
from __future__ import annotations

import json
import os
from typing import Any

from org_coordinator import store


def _claude_json(system: str, user: str) -> dict:
    try:
        import anthropic
    except ImportError:
        return {"guidance": user[:600], "items": [], "fallback": True}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"guidance": "Align work with active company goals.",
                "items": [], "fallback": True}
    model = os.environ.get("QUILL_ORG_CASCADE_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model, max_tokens=1000, system=system,
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
        return {"guidance": text[:800], "items": []}


_SYSTEM = (
    "You translate company goals into short priority guidance for one employee "
    "role. Return ONLY JSON: {guidance: string, items: [{title, why, weight}]}. "
    "Guidance is advisory — never a command to act without human approval."
)


def cascade_for_node(node: dict[str, Any]) -> dict[str, Any]:
    goals = [g for g in store.list_goals() if g.get("active")]
    if not goals:
        return {"ok": True, "priorities": [], "guidance": "", "n_goals": 0}
    role = node.get("role") or "ic"
    blob = json.dumps(goals, ensure_ascii=False)[:8000]
    out = _claude_json(
        _SYSTEM,
        f"Role: {role}\nName: {node.get('display_name') or node.get('node_id')}\n"
        f"Goals:\n{blob}\n\nReturn JSON only.",
    )
    # Deterministic fallback items from goals when model is unavailable
    items = out.get("items") or []
    if not items:
        items = [{"title": g["title"], "why": g.get("detail") or g.get("horizon") or "",
                  "weight": float(g.get("priority") or 0.5)} for g in goals]
    packet = {
        "kind": "org_priority",
        "target_node_id": node["node_id"],
        "target_role": role,
        "guidance": (out.get("guidance") or "").strip()[:2000],
        "items": items[:12],
        "goals": [{"id": g["id"], "title": g["title"],
                   "priority": g.get("priority")} for g in goals],
    }
    return {"ok": True, "priorities": [packet], "n_goals": len(goals),
            "packet": packet}


def cascade_down(from_node_id: str | None = None) -> dict[str, Any]:
    """Build priority packets for all nodes (or reports of from_node_id)."""
    if from_node_id:
        targets = store.reports_of(from_node_id)
        # Also include the node itself when cascading company-wide from ceo
        self_n = store.get_node(from_node_id)
        if self_n and self_n.get("role") in ("ceo", "exec"):
            targets = list(store.list_nodes().values())
    else:
        targets = list(store.list_nodes().values())
    packets = []
    for n in targets:
        res = cascade_for_node(n)
        if res.get("packet"):
            packets.append(res["packet"])
    return {"ok": True, "n": len(packets), "packets": packets}
