"""Connector directory metadata — categories, blurbs, capabilities.

Mirrors Claude's browse-by-category directory. Built-in connectors and
planned stubs share this catalog; custom MCP rows carry their own fields.
"""
from __future__ import annotations

from typing import Any

# Stable category ids used by the Manage modal and API.
CATEGORIES: tuple[dict[str, str], ...] = (
    {"id": "productivity", "label": "Productivity"},
    {"id": "calendar", "label": "Calendar & email"},
    {"id": "crm", "label": "CRM & sales"},
    {"id": "dev", "label": "Engineering"},
    {"id": "custom", "label": "Custom"},
)

# id → directory card. Keep in sync with registry ids.
DIRECTORY: dict[str, dict[str, Any]] = {
    "google": {
        "category": "calendar",
        "description": (
            "Read-only Gmail headers and Google Calendar events. "
            "Sparrow never sees message bodies or your password — "
            "you sign in on Google's own screen."
        ),
        "capabilities": (
            "Import contacts from recent mail headers",
            "Import upcoming and recent calendar events",
            "Seed People and your next-meeting view",
        ),
        "auth": "oauth",
        "scopes_blurb": "gmail.metadata + calendar.events.readonly",
    },
    "slack": {
        "category": "productivity",
        "description": "Workspace channels and DMs (planned).",
        "capabilities": ("Channel metadata", "Member roster"),
        "auth": "oauth",
    },
    "outlook": {
        "category": "calendar",
        "description": "Outlook mail and Microsoft Calendar (planned).",
        "capabilities": ("Mail headers", "Calendar events", "Teams presence"),
        "auth": "oauth",
    },
    "hubspot": {
        "category": "crm",
        "description": "CRM contacts and deal stages (planned).",
        "capabilities": ("Contacts", "Deals"),
        "auth": "oauth",
    },
    "linear": {
        "category": "dev",
        "description": "Issues and project status (planned).",
        "capabilities": ("Issues", "Projects"),
        "auth": "oauth",
    },
    "notion": {
        "category": "productivity",
        "description": "Pages and databases (planned).",
        "capabilities": ("Pages", "Databases"),
        "auth": "oauth",
    },
    "github": {
        "category": "dev",
        "description": "Repos, PRs, and issues (planned).",
        "capabilities": ("Repos", "Pull requests", "Issues"),
        "auth": "oauth",
    },
}


def enrich(status: dict[str, Any]) -> dict[str, Any]:
    """Merge catalog fields onto a connector status dict."""
    cid = (status.get("id") or "").strip().lower()
    meta = DIRECTORY.get(cid) or {}
    out = dict(status)
    out.setdefault("kind", status.get("kind") or "directory")
    out.setdefault("category", meta.get("category") or "productivity")
    out.setdefault("description", meta.get("description") or "")
    caps = status.get("capabilities")
    if caps is None:
        caps = list(meta.get("capabilities") or ())
    out["capabilities"] = list(caps)
    if "auth" not in out and meta.get("auth"):
        out["auth"] = meta["auth"]
    if meta.get("scopes_blurb") and "scopes_blurb" not in out:
        out["scopes_blurb"] = meta["scopes_blurb"]
    return out


def categories() -> list[dict[str, str]]:
    return [dict(c) for c in CATEGORIES]
