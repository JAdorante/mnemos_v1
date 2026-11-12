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
    {"id": "messages", "label": "Messages"},
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
        "description": (
            "Read-only messages from the channels and DMs you are in, as you. "
            "DMs stay sensitive and never leave this machine on their own."
        ),
        "capabilities": (
            "Remember what was said in your channels",
            "Keep DMs as sensitive memory that never leaves this machine",
            "Close tasks when someone answers you in Slack",
        ),
        "auth": "oauth",
        "scopes_blurb": "user scopes: channels/groups/im/mpim :history + :read, users:read",
    },
    "outlook": {
        "category": "calendar",
        "description": (
            "Read-only Outlook mail headers and Microsoft Calendar events. "
            "Sparrow never sees message bodies or your password — "
            "you sign in on Microsoft's own screen."
        ),
        "capabilities": (
            "Import contacts from recent mail headers",
            "Import upcoming and recent calendar events",
            "Seed People and your next-meeting view",
        ),
        "auth": "oauth",
        "scopes_blurb": "Mail.ReadBasic + Calendars.Read",
    },
    "imessage": {
        "category": "messages",
        "description": (
            "Read-only texts from Messages on this Mac. Sparrow can't send them."
        ),
        "capabilities": (
            "Remember iMessage and SMS conversations",
            "Close tasks when you text someone back",
        ),
        "auth": "device",
    },
    "hubspot": {
        "category": "crm",
        "description": (
            "Read-only CRM contacts, deal stages and companies. "
            "You sign in on HubSpot's own screen."
        ),
        "capabilities": (
            "Remember contacts and the companies they belong to",
            "Track deals and their stage changes",
            "Seed People from your CRM",
        ),
        "auth": "oauth",
        "scopes_blurb": "crm.objects.contacts.read + deals.read + companies.read",
    },
    "linear": {
        "category": "dev",
        "description": (
            "Read-only issues and project status from your Linear workspace. "
            "You sign in on Linear's own screen."
        ),
        "capabilities": (
            "Remember issues assigned to you and your team",
            "Track project status and target dates",
            "Close tasks when an issue you promised moves",
        ),
        "auth": "oauth",
        "scopes_blurb": "read",
    },
    "notion": {
        "category": "productivity",
        "description": (
            "Read-only pages and databases you share with Sparrow. "
            "You pick the pages on Notion's own screen."
        ),
        "capabilities": (
            "Remember pages as they change",
            "Track databases and their rows",
            "Find a page by what it says, not just its title",
        ),
        "auth": "oauth",
        "scopes_blurb": "no scopes — read capabilities set on the integration; you choose the pages",
    },
    "github": {
        "category": "dev",
        "description": (
            "Read-only issues, pull requests and repos you take part in. "
            "You sign in on GitHub's own screen."
        ),
        "capabilities": (
            "Remember issues and pull requests you are on",
            "Track repos you push to",
            "Close tasks when an issue or PR you promised moves",
        ),
        "auth": "oauth",
        "scopes_blurb": "repo + read:org + read:user (GitHub has no read-only private-repo scope)",
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
