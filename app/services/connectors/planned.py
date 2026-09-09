"""Planned connectors — registry stubs with no network I/O."""
from __future__ import annotations

from typing import Any

from app.services.connectors import catalog


class PlannedConnector:
    """Named but not implemented yet — UI can show Planned vs Available."""

    availability = "planned"
    kind = "directory"

    def __init__(self, id: str, label: str, tool_names: tuple[str, ...]):
        self.id = id
        self.label = label
        self.tool_names = tool_names
        meta = catalog.DIRECTORY.get(id) or {}
        self.category = meta.get("category") or "productivity"
        self.description = meta.get("description") or f"{label} (planned)."
        self.capabilities = tuple(meta.get("capabilities") or ())

    def configured(self) -> bool:
        return False

    def connected(self) -> bool:
        return False

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "tool_names": list(self.tool_names),
            "availability": self.availability,
            "kind": self.kind,
            "category": self.category,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "configured": False,
            "connected": False,
            "error": None,
        }

    def begin_connect(self, *, public_base: str | None = None,
                      return_path: str | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "error": f"{self.label} connector is not implemented yet",
            "planned": True,
        }

    def complete_connect(self, code: str, state: str,
                         *, redirect_uri: str) -> dict[str, Any]:
        return self.begin_connect()

    def sync(self) -> dict[str, Any]:
        return self.begin_connect()

    def disconnect(self) -> dict[str, Any]:
        return {"ok": True, "noop": True}


PLANNED: tuple[PlannedConnector, ...] = (
    PlannedConnector("slack", "Slack", ("Slack",)),
    PlannedConnector("outlook", "Outlook",
                     ("Outlook", "Microsoft Calendar", "Microsoft Teams")),
    PlannedConnector("hubspot", "HubSpot", ("HubSpot",)),
    PlannedConnector("linear", "Linear", ("Linear",)),
    PlannedConnector("notion", "Notion", ("Notion",)),
    PlannedConnector("github", "GitHub", ("GitHub",)),
)
