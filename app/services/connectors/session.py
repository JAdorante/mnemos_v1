"""Per-conversation connector enablement (Claude-style chat toggles).

Connecting an account in Manage/Privacy is install-wide. Enabling it for
*this* chat is separate — a connector can be authenticated but still off
for the current conversation.

``tool_access`` (prefs) changes the default:
  auto      — connected connectors are on unless the user toggles them off
  on_demand — only connectors the user toggles on are active

Resets when the user starts a new chat (``POST /chat/new``).
"""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.RLock()

# Explicit overrides for the live conversation.
_enabled: set[str] = set()
_disabled: set[str] = set()


def reset() -> None:
    """Clear conversation overrides (call from /chat/new)."""
    with _lock:
        _enabled.clear()
        _disabled.clear()


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "enabled": sorted(_enabled),
            "disabled": sorted(_disabled),
        }


def set_enabled(connector_id: str, on: bool) -> dict[str, Any]:
    cid = (connector_id or "").strip().lower()
    if not cid:
        return {"ok": False, "error": "connector id required"}
    with _lock:
        if on:
            _enabled.add(cid)
            _disabled.discard(cid)
        else:
            _disabled.add(cid)
            _enabled.discard(cid)
    return {"ok": True, **status()}


def status() -> dict[str, Any]:
    """Full session view: tool_access + active ids + per-connector flags."""
    from app.services.connectors import prefs
    from app.services.connectors.registry import all as all_connectors
    from app.services.connectors.registry import get as get_connector

    access = prefs.tool_access()
    connected = []
    for c in all_connectors():
        try:
            if c.connected():
                connected.append(c.id)
        except Exception:
            continue
    connected_set = set(connected)
    active = active_ids(connected=connected_set, tool_access=access)
    with _lock:
        overrides = {
            "enabled": sorted(_enabled),
            "disabled": sorted(_disabled),
        }
    rows = []
    for cid in sorted(connected_set):
        c = get_connector(cid)
        rows.append({
            "id": cid,
            "label": (c.label if c else cid),
            "active": cid in active,
            "override": (
                "on" if cid in _enabled
                else ("off" if cid in _disabled else None)
            ),
        })
    return {
        "tool_access": access,
        "active": sorted(active),
        "connected": sorted(connected_set),
        "overrides": overrides,
        "connectors": rows,
    }


def active_ids(*, connected: set[str] | None = None,
               tool_access: str | None = None) -> set[str]:
    """Connector ids that may contribute tools/data to the live chat."""
    from app.services.connectors import prefs
    from app.services.connectors.registry import all as all_connectors

    access = tool_access or prefs.tool_access()
    if connected is None:
        connected = set()
        for c in all_connectors():
            try:
                if c.connected():
                    connected.add(c.id)
            except Exception:
                continue
    # Team policy can still hide a connected row from chat if revoked later.
    connected = {cid for cid in connected if prefs.is_team_allowed(cid)}
    with _lock:
        enabled = set(_enabled)
        disabled = set(_disabled)
    if access == "on_demand":
        return connected & enabled
    # auto
    return (connected - disabled) | (connected & enabled)


# Source prefixes each connector contributes to the memory timeline.
_SOURCE_PREFIXES: dict[str, tuple[str, ...]] = {
    "google": ("exhaust.gmail", "exhaust.calendar"),
}


def blocked_source_prefixes() -> tuple[str, ...]:
    """Event/fact source prefixes to drop while grounding this chat turn."""
    active = active_ids()
    blocked: list[str] = []
    for cid, prefixes in _SOURCE_PREFIXES.items():
        if cid not in active:
            blocked.extend(prefixes)
    return tuple(blocked)


def is_active(connector_id: str) -> bool:
    return (connector_id or "").strip().lower() in active_ids()


def guidance_line() -> str:
    """Short policy line prepended to agent context when useful."""
    from app.services.connectors import prefs
    from app.services.connectors.registry import get as get_connector

    st = status()
    access = st["tool_access"]
    active = st["active"]
    connected = st["connected"]
    if not connected:
        return ""
    if access == "on_demand" and not active:
        return (
            "CONNECTOR POLICY: tool access is On demand and no connectors "
            "are enabled for this conversation — do not use calendar, mail, "
            "or other connector-backed data; answer from capture/memory only."
        )
    labels = []
    for cid in active:
        c = get_connector(cid)
        labels.append(c.label if c else cid)
    off = [cid for cid in connected if cid not in active]
    parts = [
        "CONNECTOR POLICY: for this conversation, enabled connectors are: "
        + (", ".join(labels) if labels else "(none)")
        + "."
    ]
    if off:
        parts.append(
            "Disabled for this chat (connected but toggled off): "
            + ", ".join(off) + "."
        )
    if prefs.tool_access() == "on_demand":
        parts.append("Tool access mode: on demand.")
    return " ".join(parts)
