"""Connector contract — connect / status / sync / disconnect."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Connector(Protocol):
    id: str
    label: str
    tool_names: tuple[str, ...]
    # "ready" | "planned" — planned stubs refuse connect/sync.
    availability: str

    def configured(self) -> bool:
        """Env credentials present (OAuth client, API key, …)."""
        ...

    def connected(self) -> bool:
        ...

    def status(self) -> dict[str, Any]:
        """configured / connected / availability / last_sync / error extras."""
        ...

    def begin_connect(self, *, public_base: str | None = None,
                      return_path: str | None = None) -> dict[str, Any]:
        """Start OAuth or equivalent.

        Hosted HTTPS: ``{ok, mode: "redirect", auth_url}``.
        Desktop: ``{ok, mode: "loopback"}`` after completing loopback.
        ``return_path`` is the same-site page to land on when the flow ends.
        """
        ...

    def complete_connect(self, code: str, state: str,
                         *, redirect_uri: str) -> dict[str, Any]:
        """Finish a web-redirect OAuth callback."""
        ...

    def sync(self) -> dict[str, Any]:
        ...

    def disconnect(self) -> dict[str, Any]:
        ...


def public_base_url() -> str | None:
    """HTTPS public origin for OAuth redirects, or None for desktop loopback.

    ``QUILL_PUBLIC_BASE_URL`` wins; else ``QUILL_PEER_BASE_URL`` when it is https.
    """
    import os
    for key in ("QUILL_PUBLIC_BASE_URL", "QUILL_PEER_BASE_URL"):
        raw = (os.environ.get(key) or "").strip().rstrip("/")
        if raw.lower().startswith("https://"):
            return raw
    return None


def _normalize_https_origin(raw: str | None) -> str | None:
    u = (raw or "").strip().rstrip("/")
    if not u.lower().startswith("https://"):
        return None
    # Drop path/query if a full URL was passed.
    try:
        from urllib.parse import urlparse
        p = urlparse(u)
        if not p.netloc:
            return None
        return f"https://{p.netloc}"
    except Exception:
        return None


def request_public_base(request) -> str | None:
    """Prefer the live site the browser is on (Origin / Forwarded Host).

    Quick tunnels and reverse proxies change hostnames; env
    ``QUILL_PEER_BASE_URL`` can lag. OAuth redirect_uri must match the URL
    the user is actually visiting so the callback returns to this instance.
    Falls back to :func:`public_base_url` when the request has no HTTPS hint.
    """
    if request is not None:
        headers = getattr(request, "headers", None) or {}
        origin = _normalize_https_origin(headers.get("origin"))
        if origin:
            return origin

        proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        host = (
            headers.get("x-forwarded-host")
            or headers.get("host")
            or ""
        ).split(",")[0].strip()
        # Strip brackets / port noise; reject obvious internal compose hosts
        # so we don't mint redirect_uris Google will never accept.
        if proto == "https" and host and "://" not in host:
            host_l = host.lower()
            if not host_l.startswith(("sparrow-user", "localhost", "127.")):
                return f"https://{host}"

        # Starlette base_url when the app itself is served on https.
        try:
            base = str(getattr(request, "base_url", "") or "").rstrip("/")
            got = _normalize_https_origin(base)
            if got:
                return got
        except Exception:
            pass

    return public_base_url()


def oauth_redirect_base() -> str | None:
    """The one HTTPS origin whose ``/oauth/google/callback`` is registered.

    ``QUILL_OAUTH_REDIRECT_BASE`` points at a stable relay (a Tailscale
    Funnel hostname on the GB10) so Google needs a single redirect URI no
    matter how often the per-user quick-tunnel hostnames rotate. Unset →
    the redirect is minted from the live origin, as before.
    """
    import os
    return _normalize_https_origin(os.environ.get("QUILL_OAUTH_REDIRECT_BASE"))


DEFAULT_RETURN_PATH = "/onboarding?step=2"


def safe_return_path(raw: str | None,
                     default: str = DEFAULT_RETURN_PATH) -> str:
    """Sanitize a caller-supplied post-OAuth landing path.

    Only a same-site path is ever accepted: an absolute URL, a protocol-
    relative ``//host`` or anything with a control character would turn the
    OAuth callback into an open redirect, so those fall back to ``default``.
    """
    p = (raw or "").strip()
    if not p or len(p) > 512:
        return default
    if not p.startswith("/") or p.startswith("//") or p.startswith("/\\"):
        return default
    if any(ch in p for ch in ("\\", "\r", "\n", "\t")) or "://" in p:
        return default
    return p
