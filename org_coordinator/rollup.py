"""Claude-assisted team rollup from IC digests (Anthropic parent)."""
from __future__ import annotations

import json
import os
from typing import Any


def _claude_json(system: str, user: str, *, model: str | None = None) -> dict:
    """Direct Anthropic call — coordinator may run without full Sparrow."""
    try:
        import anthropic
    except ImportError:
        return {"summary": user[:800], "highlights": [], "blockers": [],
                "confidence": 0.3, "fallback": True}
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return {"summary": user[:800], "highlights": [], "blockers": [],
                "confidence": 0.2, "fallback": True, "error": "no_api_key"}
    model = model or os.environ.get("QUILL_ORG_ROLLUP_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model,
        max_tokens=1200,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = ""
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            text += block.text
    text = text.strip()
    # Tolerate fenced JSON
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except Exception:
        return {"summary": text[:1200], "highlights": [], "blockers": [],
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
