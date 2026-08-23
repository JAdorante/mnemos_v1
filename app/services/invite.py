"""Tier-1 invite-code onboarding (WS-D) — remove the API-key funnel stop.

Today a tester must create an Anthropic account, add a payment method, mint a
key and paste it in before Mnemos runs at all. That is fine for an engineer and
a hard stop for the dealmaker persona this pilot is for.

Tier 1 keeps the local-first story intact and changes almost nothing: the
operator pre-creates one revocable workspace key per tester, the installer
POSTs an invite code to a small hosted service, and the key it gets back is
written into the tester's own `.credentials.env` exactly as a pasted key would
be. **The key still lives on the tester's machine and calls go straight to the
provider** — no proxy, no runtime dependency on the operator's service after
install, and one tester's key can be revoked without touching anyone else.

Deliberately *not* Tier 2 (a hosted Anthropic-compatible proxy). Tier 2 is
designed in `docs/key-vending.md` and is one config change away, but it makes
the operator's uptime a dependency for every tester's cloud tier — proxy down
means cloud down for the whole cohort — so it stays unbuilt until the hosted
path is actually confirmed.

The BYO-key path is untouched: no invite code, no network call, paste as before.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

# Codes are operator-issued and human-typed over a phone call, so keep the
# alphabet unambiguous and the shape strict enough to reject a typo locally
# instead of spending a round trip on it.
CODE_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")

DEFAULT_TIMEOUT_S = 20.0


class InviteError(RuntimeError):
    """Redemption refused. The message is shown to the tester verbatim."""


def normalize_code(code: str) -> str:
    """Uppercase, strip spaces, and re-hyphenate a 12-character code."""
    raw = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    if len(raw) == 12:
        return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"
    return (code or "").strip().upper()


def code_looks_valid(code: str) -> bool:
    return bool(CODE_RE.match(normalize_code(code)))


def vending_url() -> str:
    return (os.environ.get("QUILL_INVITE_URL") or "").strip()


def redeem(code: str, *, url: str | None = None, transport=None,
           timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Exchange an invite code for a provider key.

    Returns ``{"provider": ..., "key": ...}``. Raises :class:`InviteError` with
    a message meant for a non-engineer on every refusal — an invalid code, an
    expired or already-redeemed one, or a service that is not reachable.

    `transport(url, payload, timeout) -> dict` is injectable for tests; nothing
    here retries, because a tester watching an installer would rather see the
    failure than a silent 30-second stall.
    """
    code = normalize_code(code)
    if not code_looks_valid(code):
        raise InviteError(
            "That invite code does not look right. It should be 12 characters "
            "in three groups, like ABCD-EFGH-JKLM.")
    endpoint = (url or vending_url()).strip()
    if not endpoint:
        raise InviteError(
            "No invite service is configured for this build. Use the "
            "'paste your own API key' option instead.")
    try:
        data = (transport or _post)(endpoint, {"code": code}, timeout_s)
    except InviteError:
        raise
    except Exception as exc:
        raise InviteError(
            f"Could not reach the invite service ({exc}). Check your internet "
            "connection, or use the 'paste your own API key' option.") from exc

    if not isinstance(data, dict):
        raise InviteError("The invite service returned something unexpected.")
    key = str(data.get("key") or "").strip()
    provider = str(data.get("provider") or "anthropic").strip().lower()
    if not key:
        raise InviteError(data.get("detail")
                          or "The invite service did not return a key.")
    from app.services.parent_model import PROVIDERS
    if provider not in PROVIDERS:
        raise InviteError(f"The invite service named an unknown provider "
                          f"{provider!r}.")
    return {"provider": provider, "key": key,
            "label": data.get("label") or "", "expires_at": data.get("expires_at")}


def _post(url: str, payload: dict, timeout_s: float) -> dict:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, method="POST",
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read(64_000).decode("utf-8"))
    except HTTPError as exc:
        # The service explains refusals in the body; surface that, not "HTTP 410".
        detail = ""
        try:
            detail = (json.loads(exc.read().decode("utf-8")) or {}).get("detail", "")
        except Exception:
            pass
        raise InviteError(detail or _http_reason(exc.code)) from exc


def _http_reason(code: int) -> str:
    return {
        400: "That invite code was not accepted.",
        404: "That invite code was not found. Check it with whoever sent it.",
        409: "That invite code has already been used.",
        410: "That invite code has expired. Ask for a new one.",
        429: "Too many attempts. Wait a minute and try again.",
    }.get(int(code), f"The invite service refused the code (HTTP {code}).")


def redeem_and_save(code: str, *, url: str | None = None,
                    transport=None) -> dict[str, Any]:
    """Redeem, then persist exactly as a pasted key would be.

    The vended key lands in the tester's own `.credentials.env` — the same file,
    the same format, the same runtime path. Nothing about the app's behavior
    afterwards distinguishes a vended key from a BYO one, which is what makes
    this tier reversible.
    """
    out = redeem(code, url=url, transport=transport)
    from app.services import parent_model
    path = parent_model.save(out["provider"], out["key"])
    return {"ok": True, "provider": out["provider"], "path": path,
            "label": out.get("label") or "", "expires_at": out.get("expires_at")}
