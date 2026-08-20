"""Parent model — the cloud tier is a user choice, not a hardcoded vendor.

Mnemos runs local-first (Ollama) and escalates hard questions to a "parent"
cloud model. Until now that parent was Anthropic by construction. This module
makes the parent a configured account: the user connects THEIR provider —
Anthropic, OpenAI, Google (Gemini), or xAI (Grok) — during setup, key
validated live before persisting (the icloud_account pattern), and every text
escalation routes through it.

One seam: ModelRouter._complete_claude calls `complete()` here instead of
holding its own Anthropic client. Anthropic uses its native SDK (structured
output via output_config); the other three speak the OpenAI-compatible
chat-completions dialect over httpx — no extra SDK dependencies.

Model names stay Claude ids inside the router (they're task labels there);
for a non-Anthropic parent they map onto a two-tier ladder: haiku-class ids →
the provider's light model, everything else → its flagship. Override with
QUILL_PARENT_MODEL (one model for everything) or QUILL_PARENT_MODEL_FLAGSHIP /
QUILL_PARENT_MODEL_LIGHT.

Deliberately NOT covered (Anthropic-only for now, degrade to local without an
Anthropic key): vision escalation (vlm.py), the browser agent's internal
ladder, and shadow-eval grading. The privacy gate + redaction in the router
run BEFORE this module, so every parent gets the same egress hygiene.

Config: QUILL_PARENT_PROVIDER (anthropic|openai|google|xai, default
anthropic) + the provider's key env var — both persisted to .credentials.env
by `save()`.
"""
from __future__ import annotations

import os
from typing import Any

# Providers the setup UI offers. `key_env` is where the key lives (existing
# conventions for each vendor); `key_prefix` is a cheap paste-check, not a
# guarantee; `base` is the OpenAI-compatible endpoint root (None = native
# Anthropic SDK); `token_param` names the max-token field that provider's
# current flagship accepts.
PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "key_env": "ANTHROPIC_API_KEY",
        "key_prefix": "sk-",
        "placeholder": "sk-ant-…",
        "base": None,
        "flagship": "claude-opus-4-8",
        "light": "claude-haiku-4-5",
        "token_param": "max_tokens",
    },
    "openai": {
        "label": "OpenAI (ChatGPT)",
        "key_env": "OPENAI_API_KEY",
        "key_prefix": "sk-",
        "placeholder": "sk-…",
        "base": "https://api.openai.com/v1",
        "flagship": "gpt-5",
        "light": "gpt-5-mini",
        "token_param": "max_completion_tokens",
    },
    "google": {
        "label": "Google (Gemini)",
        "key_env": "GEMINI_API_KEY",
        "key_prefix": "AIza",
        "placeholder": "AIza…",
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "flagship": "gemini-2.5-pro",
        "light": "gemini-2.5-flash",
        "token_param": "max_tokens",
    },
    "xai": {
        "label": "xAI (Grok)",
        "key_env": "XAI_API_KEY",
        "key_prefix": "xai-",
        "placeholder": "xai-…",
        "base": "https://api.x.ai/v1",
        "flagship": "grok-4",
        "light": "grok-3-mini",
        "token_param": "max_tokens",
    },
}

_PROVIDER_ENV = "QUILL_PARENT_PROVIDER"


def provider() -> str:
    """The configured parent provider id (always a valid registry key)."""
    p = (os.environ.get(_PROVIDER_ENV) or "anthropic").strip().lower()
    return p if p in PROVIDERS else "anthropic"


def spec(pid: str | None = None) -> dict[str, Any]:
    return PROVIDERS[pid or provider()]


def configured(pid: str | None = None) -> bool:
    """A key is present for the (given or active) provider. Anthropic also
    honors ANTHROPIC_AUTH_TOKEN / an `ant auth login` profile via the SDK."""
    pid = pid or provider()
    if os.environ.get(spec(pid)["key_env"]):
        return True
    if pid == "anthropic" and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return False


def status() -> dict:
    """For the setup/console UI: every provider + which one is active."""
    return {
        "active": provider(),
        "providers": [
            {"id": pid, "label": s["label"], "placeholder": s["placeholder"],
             "connected": configured(pid)}
            for pid, s in PROVIDERS.items()
        ],
    }


def resolve_model(claude_model: str) -> str:
    """Map the router's Claude-id task label onto the active provider.

    Anthropic passes through untouched. Elsewhere: an explicit
    QUILL_PARENT_MODEL wins; otherwise haiku-class → light, rest → flagship
    (overridable per tier)."""
    if provider() == "anthropic":
        return claude_model
    forced = (os.environ.get("QUILL_PARENT_MODEL") or "").strip()
    if forced:
        return forced
    s = spec()
    if "haiku" in (claude_model or "").lower():
        return (os.environ.get("QUILL_PARENT_MODEL_LIGHT") or "").strip() \
            or s["light"]
    return (os.environ.get("QUILL_PARENT_MODEL_FLAGSHIP") or "").strip() \
        or s["flagship"]


def _flatten_content(content) -> str:
    """Anthropic message content (str or block list) → plain text for the
    OpenAI-compatible dialect. Same tolerance as ollama_text._flatten."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(str(b.get("text") or ""))
            else:
                parts.append(str(getattr(b, "text", "") or ""))
        return "\n".join(x for x in parts if x)
    return str(content or "")


def _openai_compat_complete(pid: str, *, model: str, system: str,
                            messages: list, max_tokens: int,
                            schema: dict | None,
                            timeout: float = 120.0) -> tuple[str, int, int]:
    """One chat completion against an OpenAI-compatible endpoint.
    Returns (text, input_tokens, output_tokens); raises on any failure."""
    import httpx

    s = PROVIDERS[pid]
    key = os.environ.get(s["key_env"]) or ""
    if not key:
        raise RuntimeError(f"no API key configured for {s['label']} "
                           f"(set {s['key_env']} or reconnect in Setup)")
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in messages or []:
        msgs.append({"role": m.get("role", "user"),
                     "content": _flatten_content(m.get("content"))})
    payload: dict[str, Any] = {"model": model, "messages": msgs,
                               s["token_param"]: max_tokens}
    if schema is not None:
        # strict:false — provider schema dialects differ in supported keywords
        # and a rejected schema would fail the whole escalation; the router
        # already json-parses and retries/keeps-local on bad output.
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": schema,
                            "strict": False},
        }
    r = httpx.post(
        s["base"].rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json=payload, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"{s['label']} HTTP {r.status_code}: "
                           f"{r.text[:300]}")
    data = r.json()
    text = (((data.get("choices") or [{}])[0].get("message") or {})
            .get("content") or "")
    usage = data.get("usage") or {}
    return (str(text),
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0))


_anthropic_client = None


def _anthropic_complete(*, model: str, system: str, messages: list,
                        max_tokens: int,
                        schema: dict | None) -> tuple[str, int, int]:
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic()
    kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                              "system": system, "messages": messages}
    if schema is not None:
        kwargs["output_config"] = {"format": {"type": "json_schema",
                                              "schema": schema}}
    resp = _anthropic_client.messages.create(**kwargs)
    u = getattr(resp, "usage", None)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return (text,
            int(getattr(u, "input_tokens", 0) or 0),
            int(getattr(u, "output_tokens", 0) or 0))


def complete(*, model: str, system: str, messages: list, max_tokens: int,
             schema: dict | None = None) -> dict:
    """One parent-model text completion on the ACTIVE provider.

    `model` is the router's Claude id; it is resolved per provider. Returns
    {text, input_tokens, output_tokens, provider, model}. Raises on API
    error — callers (the router) own logging and local-keep fallback.
    Callers also own redaction/privacy gating: nothing here inspects content.
    """
    pid = provider()
    resolved = resolve_model(model)
    if pid == "anthropic":
        text, tin, tout = _anthropic_complete(
            model=resolved, system=system, messages=messages,
            max_tokens=max_tokens, schema=schema)
    else:
        text, tin, tout = _openai_compat_complete(
            pid, model=resolved, system=system, messages=messages,
            max_tokens=max_tokens, schema=schema)
    return {"text": text, "input_tokens": tin, "output_tokens": tout,
            "provider": pid, "model": resolved}


def validate_key(pid: str, key: str) -> str | None:
    """Live-validate a pasted key with the cheapest possible call.
    Returns None when the key works, else a human-readable reason."""
    if pid not in PROVIDERS:
        return f"unknown provider {pid!r}"
    s = PROVIDERS[pid]
    key = (key or "").strip()
    if not key.startswith(s["key_prefix"]):
        return (f"that does not look like a {s['label']} key "
                f"(expected it to start with {s['key_prefix']!r})")
    try:
        if pid == "anthropic":
            import anthropic
            anthropic.Anthropic(api_key=key).messages.create(
                model="claude-haiku-4-5", max_tokens=1,
                messages=[{"role": "user", "content": "ping"}])
        else:
            import httpx
            r = httpx.post(
                s["base"].rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": s["light"],
                      "messages": [{"role": "user", "content": "ping"}],
                      s["token_param"]: 1},
                timeout=30.0)
            if r.status_code >= 400:
                return f"key rejected: HTTP {r.status_code}: {r.text[:200]}"
    except Exception as exc:
        return f"key rejected: {exc}"
    return None


def save(pid: str, key: str) -> str:
    """Persist provider + key to .credentials.env (validate first — this
    function trusts its input) and make them live in this process."""
    from app.services.icloud_account import _cred_path
    s = PROVIDERS[pid]
    path = _cred_path()
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    drop = (f"{s['key_env']}=", f"{_PROVIDER_ENV}=")
    lines = [ln for ln in existing.splitlines()
             if not ln.startswith(drop)]
    lines.append(f"{_PROVIDER_ENV}={pid}")
    lines.append(f"{s['key_env']}={key}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[_PROVIDER_ENV] = pid
    os.environ[s["key_env"]] = key
    return str(path)
