"""Claude-assisted team rollup from IC digests (Anthropic parent)."""
from __future__ import annotations

import json
import os
from typing import Any

from org_coordinator import parent


def _claude_json(system: str, user: str, *, model: str | None = None) -> dict:
    """Parent call through the router; a fixed fallback in the slim image."""
    why = parent.unavailable()
    if why == parent.NO_ROUTER:
        return {"summary": user[:800], "highlights": [], "blockers": [],
                "confidence": 0.3, "fallback": True}
    if why:
        return {"summary": user[:800], "highlights": [], "blockers": [],
                "confidence": 0.2, "fallback": True, "error": "no_api_key"}
    model = model or os.environ.get("QUILL_ORG_ROLLUP_MODEL", "claude-sonnet-4-6")
    text = parent.complete_text("org_rollup", system, user, max_tokens=1200,
                                model=model)
    try:
        return parent.parse_json(text)
    except Exception:
        return {"summary": text.strip()[:1200], "highlights": [], "blockers": [],
                "confidence": 0.4}


_SYSTEM = (
    "You aggregate employee status digests for a manager. Return ONLY JSON with "
    "keys: summary (string), highlights (string[]), blockers (string[]), "
    "asks (string[]), deps (string[]), confidence (0-1). No raw personal data — "
    "keep it role-scoped and actionable. Prefer fewer sharp bullets."
)


def rollup_digests(digests: list[dict[str, Any]], *,
                   team_label: str = "team") -> dict[str, Any]:
    if not digests:
        return {"summary": f"No recent digests for {team_label}.",
                "highlights": [], "blockers": [], "asks": [], "deps": [],
                "confidence": 1.0, "n": 0}
    payload = json.dumps([
        {
            "from": d.get("display_name") or d.get("node_id"),
            "role": d.get("role"),
            "progress": d.get("progress") or [],
            "blockers": d.get("blockers") or [],
            "asks": d.get("asks") or [],
            "deps": d.get("deps") or [],
            "period": d.get("period"),
        }
        for d in digests
    ], ensure_ascii=False)[:12000]
    out = _claude_json(
        _SYSTEM,
        f"Team: {team_label}\nDigests:\n{payload}\n\nReturn JSON only.",
    )
    out["n"] = len(digests)
    out.setdefault("highlights", [])
    out.setdefault("blockers", [])
    out.setdefault("asks", [])
    out.setdefault("deps", [])
    return out
