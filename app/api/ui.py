"""Sparrow app shell — Jinja layout, component macros, per-request rail.

Frontend spec (2026-11-06), "App shell" and migration step 1. One layout
wraps every destination: a left rail, a top bar and a content column. New
screens render from templates in app/templates; legacy Python-string pages
are wrapped by mnemos_theme.apply() and get the same rail.

The server is the state: the rail's active item and inbox count come from
the template context, never from client code.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from app.api import mnemos_theme as theme

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(("html", "xml")),
    trim_blocks=True,
    lstrip_blocks=True,
)
env.globals.update(
    brand=theme.BRAND,
    company=theme.COMPANY,
    copyright=theme.COPYRIGHT,
    brand_mark=Markup(theme.BRAND_MARK),
)

# key, label, href — the five destinations in rail order (spec, IA table).
DESTINATIONS: tuple[tuple[str, str, str], ...] = (
    ("today", "Today", "/today"),
    ("chat", "Chat", "/chat"),
    ("memory", "Memory", "/memory"),
    ("people", "People & Projects", "/profile?tab=people"),
    ("inbox", "Inbox", "/inbox"),
)
SETTINGS = ("settings", "Settings", "/settings")
INSPECT = ("inspect", "Inspect", "/inspect")

_RAIL_RE = re.compile(r'<aside class="app-rail" id="sparrowRail".*?</aside>', re.S)


def active_key(path: str, query: str = "") -> str | None:
    """Which rail item a route belongs to (spec, IA "Replaces today")."""
    p = (path or "/").split("?", 1)[0].rstrip("/") or "/"
    if p in ("/", "/today", "/shell"):
        return "today"
    if p == "/chat" or p.startswith("/chat/"):
        return "chat"
    if p in ("/memory/changes", "/records", "/triggers", "/selfreport") \
            or p.startswith(("/inspect", "/console/trace")):
        return "inspect"
    if p in ("/memory", "/console", "/meetings") \
            or p.startswith(("/memory/", "/meetings/", "/meeting/")):
        return "memory"
    if p == "/profile" or p.startswith(("/people", "/projects", "/org/", "/entities/")):
        return "people"
    if p == "/inbox" or p.startswith("/inbox/"):
        return "inbox"
    if p.startswith(("/settings", "/capture", "/peer", "/onboarding", "/phone",
                     "/desktop-access", "/assistants", "/org-network", "/teams",
                     "/desktop")):
        return "settings"
    return None


def _local_line() -> str:
    hosted = os.environ.get("QUILL_HEADLESS", "").strip().lower() in ("1", "true", "yes")
    return "Hosted seat · memory stays on your seat" if hosted \
        else "Local · all memory on this device"


def _scope_line() -> str:
    try:
        from app.services import peer_channel
        n = len(peer_channel.peers() or [])
    except Exception:
        n = 0
    if n == 0:
        return "Nothing shared yet"
    return f"Shared with {n} teammate{'s' if n != 1 else ''}"


def _inbox_count() -> int:
    """Needs-you count for the rail pill. Reads the store the app already
    attached at startup; it never opens one (no `_ensure_store()` here — a
    page render must not bind the memory singleton to whatever store happens
    to be current, which is how a test's temp store leaked into background
    threads and crashed a full suite run)."""
    try:
        from app.services.memory import memory
        from app.services import inbox
        store = getattr(memory, "_store", None)
        if store is None:
            return 0
        return inbox.needs_you_count(store, worker=_worker())
    except Exception:
        return 0


def _worker():
    try:
        from app.api.routes import _agent_worker
        return _agent_worker()
    except Exception:
        return None


def rail_context(path: str, query: str = "", *, inbox_count: int | None = None,
                 live: bool = True) -> dict[str, Any]:
    return {
        "items": DESTINATIONS,
        "settings": SETTINGS,
        "inspect": INSPECT,
        "active": active_key(path, query),
        "inbox_count": (_inbox_count() if inbox_count is None and live
                        else int(inbox_count or 0)),
        "local_line": _local_line() if live else "Local · all memory on this device",
        "scope_line": _scope_line() if live else "",
    }


def rail_html(path: str, query: str = "", *, live: bool = False,
              inbox_count: int | None = None) -> str:
    """The rail alone. `live=False` is the static default baked into legacy
    pages at import time; routes swap in a live one via finish()."""
    ctx = rail_context(path, query, inbox_count=inbox_count, live=live)
    return env.get_template("rail.html").render(rail=ctx)


def finish(html: str, *, path: str, query: str = "") -> str:
    """Per-request pass over a page: the rail with its active item and inbox
    count. Idempotent; pages without a rail are returned unchanged."""
    live = rail_html(path, query, live=True)
    out, n = _RAIL_RE.subn(lambda _m: live, html, count=1)
    return out if n else html


def _head_links() -> Markup:
    v = theme.asset_version()
    return Markup(theme.font_links(v) + "\n" + theme.theme_style_links(v))


def render(template: str, *, path: str, query: str = "", status: int = 200,
           agent_worker=None, next_url: str | None = None, katex: bool = False,
           **ctx: Any) -> HTMLResponse:
    """Render a destination from app/templates inside the layout."""
    html = render_html(template, path=path, query=query, katex=katex, **ctx)
    return page_response(html, path=path, query=query, status=status,
                         agent_worker=agent_worker, next_url=next_url)


def render_html(template: str, *, path: str, query: str = "",
                katex: bool = False, **ctx: Any) -> str:
    v = theme.asset_version()
    tpl = env.get_template(template)
    return tpl.render(
        rail=rail_context(path, query),
        topbar=Markup(theme.nav_markup()),
        head_links=_head_links(),
        katex_links=Markup(theme.KATEX_LINKS) if katex else Markup(""),
        script_links=Markup(theme.theme_script_links(v)),
        theme_boot=Markup(theme.THEME_BOOT),
        path=path,
        **ctx,
    )


def render_partial(template: str, **ctx: Any) -> HTMLResponse:
    """One card's body, for /…/partials/{card} refetches."""
    html = env.get_template(template).render(**ctx)
    return HTMLResponse(html, headers=_NO_STORE)


_NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


def page_response(html: str, *, path: str, query: str = "", status: int = 200,
                  agent_worker=None, next_url: str | None = None) -> HTMLResponse:
    """Finish a page (live rail, approval banner) and send it uncached.

    Every HTML route goes through here so the shell is the same everywhere.
    The approval banner is the existing cross-page affordance; it is kept
    until the Inbox replaces every per-surface prompt (migration step 2).
    """
    html = finish(html, path=path, query=query)
    from app.api.approval_partial import inject_page
    html = inject_page(html, next_url=next_url or path, agent_worker=agent_worker)
    return HTMLResponse(html, status_code=status, headers=_NO_STORE)
