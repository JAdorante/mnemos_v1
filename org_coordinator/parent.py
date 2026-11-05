"""The coordinator's one way to reach the parent model.

Every paid call goes through Sparrow's ModelRouter, so org tasks get the same
cost log, privacy gate, provider choice and distillation trail as everything
else. The slim org image (deploy/org) ships without `app.config` or the
`anthropic` package; there the router cannot be imported and each caller gets
its own deterministic fallback, exactly as before.
"""
from __future__ import annotations

import json
import os
from typing import Any

# Reasons a fallback carries, which callers may key on.
NO_ROUTER = "no anthropic"
NO_KEY = "no_api_key"


def _router():
    try:
        from app.services.model_router import router
        import anthropic  # noqa: F401  — the router's default backend
    except Exception:
        return None
    return router


def _configured() -> bool:
    try:
        from app.services import parent_model
        if parent_model.provider() != "anthropic":
            return bool(parent_model.configured())
    except Exception:
        pass
    return bool(os.environ.get("ANTHROPIC_API_KEY", ""))


def unavailable() -> str | None:
    """NO_ROUTER / NO_KEY when a call cannot be made, else None."""
    if _router() is None:
        return NO_ROUTER
    if not _configured():
        return NO_KEY
    return None


def parse_json(text: str) -> Any:
    """Tolerate fenced JSON. Raises ValueError when it does not parse."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def complete_text(task: str, system: str, user: str, *, max_tokens: int,
                  model: str | None = None) -> str:
    """One parent call through the router. Call `unavailable()` first."""
    router = _router()
    if router is None:
        raise RuntimeError(NO_ROUTER)
    res = router.complete_parent(
        task, system=system, messages=[{"role": "user", "content": user}],
        max_tokens=max_tokens, model=model)
    return res["text"]
