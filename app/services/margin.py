"""Margin note helpers — typed payloads for the ambient column (WS4)."""
from __future__ import annotations

from typing import Any


def note(
    text: str,
    *,
    kind: str = "observation",
    attention: bool = False,
    refs: list[str] | None = None,
    action: dict[str, Any] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Build one margin statement. Frontend never hand-assembles these strings."""
    out: dict[str, Any] = {
        "text": text,
        "kind": kind,  # stat | observation | nudge
        "attention": bool(attention),
        "refs": list(refs or []),
    }
    if action:
        out["action"] = action
    if source:
        out["source"] = source
    return out


def action_route(label: str, route: str) -> dict[str, str]:
    return {"label": label, "route": route}


def action_command(label: str, command: str) -> dict[str, str]:
    return {"label": label, "command": command}
