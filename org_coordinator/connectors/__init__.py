"""Write-back connectors for the Org Record Service (records layer, Phase 2).

`build(row)` turns a `connectors` row into a live connector, opening its
sealed secret. Kinds: hubspot, gdrive, webhook.
"""
from __future__ import annotations

from org_coordinator.connectors import secrets
from org_coordinator.connectors.base import (ConflictError, ConnectorError,
                                             PermanentError, TransientError,
                                             plan_core)

KINDS = ("hubspot", "gdrive", "webhook")
DEFAULT_OPS = {"hubspot": ("set_property", "create_note", "create_task"),
               "gdrive": ("append_entry",), "webhook": ("post",)}


def build(row: dict):
    secret = secrets.open_(row.get("secret_enc"), org_id=row["org_id"],
                           connector_id=row["id"])
    config = dict(row.get("config_json") or {})
    if row["kind"] == "hubspot":
        from org_coordinator.connectors.hubspot import HubSpotConnector
        return HubSpotConnector(row["id"], config, secret)
    if row["kind"] == "gdrive":
        from org_coordinator.connectors.gdrive import GoogleDriveConnector
        return GoogleDriveConnector(row["id"], config, secret)
    if row["kind"] == "webhook":
        from org_coordinator.connectors.webhook import WebhookConnector
        return WebhookConnector(row["id"], config, secret)
    raise PermanentError(f"unknown connector kind {row['kind']!r}")


__all__ = ["ConflictError", "ConnectorError", "DEFAULT_OPS", "KINDS",
           "PermanentError", "TransientError", "build", "plan_core"]
