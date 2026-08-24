"""General-purpose surface heuristics for the browser agent.

Detects chat/messaging SPAs from URL structure, host tips, and router
intent/site — never from contact display names. Used for vision gating,
messaging mode, and recovery (auto-read on click spirals).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .credentials import host_from_url

# Hosts that are primarily web chat / DM UIs (registrable domain, no www).
CHAT_HOSTS = frozenset({
    "snapchat.com",
    "web.whatsapp.com",
    "whatsapp.com",
    "discord.com",
    "telegram.org",
    "web.telegram.org",
    "messenger.com",
    "facebook.com",  # Messenger often under facebook.com/messages
    "instagram.com",
    "slack.com",
    "teams.microsoft.com",
    "chat.google.com",
    "messages.google.com",
})

# URL path cues that any host may use for an open conversation / inbox.
_CHAT_PATH_RE = re.compile(
    r"(?:^|/)(?:web|chat|chats|messages?|msg|dms?|conversations?|"
    r"inbox|channel|channels)(?:/|$)",
    re.I,
)

_CHAT_INTENT_RE = re.compile(
    r"\b(send_chat|read_chat|web_chat|direct_message|\bdm\b|imessage|"
    r"snapchat|whatsapp|discord|telegram|messenger|slack|"
    r"send.?message|read.?message|chat.?message)\b",
    re.I,
)


def path_of(url: str) -> str:
    try:
        return urlsplit(url or "").path or ""
    except Exception:
        return ""


def is_chat_host(url: str | None = None, host: str | None = None) -> bool:
    h = (host or host_from_url(url or "") or "").lower()
    if not h:
        return False
    if h in CHAT_HOSTS:
        return True
    parts = h.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in CHAT_HOSTS:
            return True
    return False


def is_open_conversation_url(url: str | None) -> bool:
    """True when the URL looks like a specific thread (not just the app shell)."""
    from urllib.parse import urlsplit as _urlsplit
    try:
        parts = _urlsplit(url or "")
    except Exception:
        return False
    path = parts.path or ""
    if not path or path in ("/", ""):
        # Query-only deep links (e.g. wa.me / send?phone=) on chat hosts.
        return bool(parts.query) and is_chat_host(url)
    # /web alone is the shell; /web/<id> is a thread.
    if re.match(r"^/web/?$", path, re.I):
        return False
    if _CHAT_PATH_RE.search(path):
        segs = [s for s in path.strip("/").split("/") if s]
        return len(segs) >= 2
    # Chat host with a non-root path (e.g. /send, /channels/123).
    if is_chat_host(url) and len([s for s in path.strip("/").split("/") if s]) >= 1:
        return True
    return False


def is_chat_surface(
    url: str | None = None,
    *,
    intent: str | None = None,
    site: str | None = None,
    has_provider_tip: bool = False,
) -> bool:
    """Should this turn use chat-oriented perception / recovery?"""
    if has_provider_tip and is_chat_host(url):
        return True
    if is_chat_host(url):
        return True
    if is_open_conversation_url(url):
        return True
    hay = f"{intent or ''} {site or ''} {url or ''}"
    if _CHAT_INTENT_RE.search(hay):
        return True
    return False


def looks_like_chat(scan: dict | None) -> bool:
    """Structural chat detection from a live page scan — works on hosts that
    aren't in CHAT_HOSTS (self-hosted Mattermost/Rocket.Chat, new apps).

    Cues: a message-log region, a composer (editable textbox), and a column of
    selectable conversation rows. Require the composer plus at least one other
    signal so search boxes on ordinary sites don't qualify.
    """
    if not scan:
        return False
    sig = scan.get("chat_signals") or {}
    composers = int(sig.get("composers") or 0)
    if not composers:
        return False
    if sig.get("has_log"):
        return True
    if int(sig.get("list_rows") or 0) >= 5 and any(
            e.get("selected") for e in scan.get("elements", [])):
        return True
    return False


def wants_early_vision(url: str | None, *, escalate: bool = False,
                       scan: dict | None = None) -> bool:
    """Attach a screenshot early on chat SPAs (message bodies often aren't AX)."""
    if escalate:
        return True
    if is_chat_host(url) or is_open_conversation_url(url):
        return True
    if looks_like_chat(scan):
        return True
    return False


# --- opaque (canvas/graphics) surfaces --------------------------------------
# A page can be perfectly visible and still be unactionable through the
# accessibility tree: a <canvas> game, a map, a drawing/CAD editor, a video
# player, a PDF/plugin embed. Everything drawn inside is pixels, so no
# element_id can ever exist for it. When such a surface dominates the view the
# agent falls back to coordinates — confined to that surface's rectangle, which
# is exactly the part of the page the DOM cannot describe.

# Fraction of the viewport an opaque element must cover before pixels beat DOM.
PIXEL_SURFACE_RATIO = 0.25
# Interactive descendants mean the DOM does describe it — keep using element_ids.
_MAX_INNER = 0


def _viewport(scan: dict | None) -> tuple[int, int]:
    vp = ((scan or {}).get("viewport") or {})
    try:
        w, h = int(vp.get("w") or 0), int(vp.get("h") or 0)
    except (TypeError, ValueError):
        w = h = 0
    return (w or 1280), (h or 800)


def pixel_surface(scan: dict | None, *, ratio: float | None = None) -> dict | None:
    """The one graphics surface worth acting on with coordinates, or None.

    Largest visible opaque region that covers at least `ratio` of the viewport
    and exposes no interactive descendants. Returned rect is CSS pixels within
    the viewport: {kind, x, y, w, h, label}.
    """
    if not scan:
        return None
    surfaces = scan.get("surfaces") or []
    if not surfaces:
        return None
    vw, vh = _viewport(scan)
    floor = (PIXEL_SURFACE_RATIO if ratio is None else ratio) * vw * vh
    best = None
    for s in surfaces:
        try:
            w, h = int(s.get("w") or 0), int(s.get("h") or 0)
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0 or int(s.get("inner") or 0) > _MAX_INNER:
            continue
        if w * h < floor:
            continue
        if best is None or w * h > best["w"] * best["h"]:
            best = {"kind": str(s.get("kind") or "canvas"), "x": int(s.get("x") or 0),
                    "y": int(s.get("y") or 0), "w": w, "h": h,
                    "label": str(s.get("label") or "")[:60]}
    return best


def wants_pixel_ui(scan: dict | None) -> bool:
    """True when this page needs the coordinate fallback to be actionable."""
    return pixel_surface(scan) is not None


def inside_surface(surface: dict | None, x: float, y: float,
                   *, pad: int = 0) -> bool:
    """Is (x, y) — CSS pixels — inside the graphics surface? Coordinates outside
    it belong to real DOM elements, which are clicked by element_id instead."""
    if not surface:
        return False
    return (surface["x"] - pad <= x <= surface["x"] + surface["w"] + pad
            and surface["y"] - pad <= y <= surface["y"] + surface["h"] + pad)
