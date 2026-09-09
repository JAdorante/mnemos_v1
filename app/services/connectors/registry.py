"""Connector registry — google (ready) + planned stubs + custom MCP."""
from __future__ import annotations

from typing import Any

from app.services.connectors import catalog
from app.services.connectors.base import Connector
from app.services.connectors.google import google
from app.services.connectors.planned import PLANNED


def _builtins() -> dict[str, Connector]:
    reg: dict[str, Connector] = {google.id: google}
    for stub in PLANNED:
        reg[stub.id] = stub
    return reg


def _customs() -> dict[str, Connector]:
    try:
        from app.services.connectors import custom as custom_mod
        return {c.id: c for c in custom_mod.load_all()}
    except Exception:
        return {}


def _registry() -> dict[str, Connector]:
    # Customs overlay builtins only when ids don't collide (add() already
    # refuses directory ids).
    reg = _builtins()
    for cid, c in _customs().items():
        if cid not in reg:
            reg[cid] = c
    return reg


def all() -> list[Connector]:
    # Stable order: ready directory, ready custom, then planned alphabetical.
    items = list(_registry().values())
    ready_dir = [c for c in items
                 if getattr(c, "availability", "") == "ready"
                 and getattr(c, "kind", "directory") != "custom"]
    ready_custom = sorted(
        [c for c in items
         if getattr(c, "availability", "") == "ready"
         and getattr(c, "kind", "") == "custom"],
        key=lambda c: c.id,
    )
    planned = sorted(
        [c for c in items if getattr(c, "availability", "") != "ready"],
        key=lambda c: c.id,
    )
    # Keep google first among ready directory connectors.
    ready_dir.sort(key=lambda c: (0 if c.id == "google" else 1, c.id))
    return ready_dir + ready_custom + planned


def get(connector_id: str) -> Connector | None:
    return _registry().get((connector_id or "").strip().lower())


def for_tool(name: str) -> Connector | None:
    needle = (name or "").strip().lower()
    if not needle:
        return None
    for c in all():
        for t in c.tool_names:
            if t.lower() == needle:
                return c
    return None


def list_status() -> list[dict[str, Any]]:
    from app.services.connectors import prefs
    rows = []
    for c in all():
        st = catalog.enrich(c.status())
        st["team_allowed"] = prefs.is_team_allowed(c.id)
        rows.append(st)
    return rows


def tool_status_map() -> dict[str, dict[str, Any]]:
    """Map onboarding tool display name → connector status snippet."""
    out: dict[str, dict[str, Any]] = {}
    for c in all():
        st = catalog.enrich(c.status())
        snippet = {
            "connector_id": c.id,
            "availability": st.get("availability"),
            "configured": st.get("configured"),
            "connected": st.get("connected"),
            "label": c.label,
            "category": st.get("category"),
            "kind": st.get("kind"),
        }
        for name in c.tool_names:
            out[name] = snippet
    return out


def directory() -> dict[str, Any]:
    """Browse payload: categories + enriched connector cards."""
    from app.services.connectors import prefs, session
    return {
        "categories": catalog.categories(),
        "connectors": list_status(),
        "tool_access": prefs.tool_access(),
        "team": prefs.team_policy(),
        "session": session.status(),
        "public_notes": {
            "custom": (
                "Custom MCP servers are reached from this Sparrow install "
                "(localhost and private networks are fine). Unlike Claude "
                "custom connectors, the server does not need a public URL."
            ),
        },
    }
