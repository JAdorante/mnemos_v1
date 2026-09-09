"""Google connector — Gmail + Calendar metadata via exhaust_ingest."""
from __future__ import annotations

import threading
from typing import Any

from app.services.connectors.base import oauth_redirect_base, public_base_url


class GoogleConnector:
    id = "google"
    label = "Google (Gmail + Calendar)"
    tool_names = ("Gmail", "Google Calendar")
    availability = "ready"
    kind = "directory"
    category = "calendar"
    description = (
        "Read-only Gmail headers and Google Calendar events. "
        "You sign in on Google's screen — Sparrow never sees your password."
    )
    capabilities = (
        "Import contacts from recent mail headers",
        "Import upcoming and recent calendar events",
        "Seed People and your next-meeting view",
    )

    def configured(self) -> bool:
        from app.services import exhaust_ingest as ex
        return ex.oauth_configured()

    def connected(self) -> bool:
        from app.services import exhaust_ingest as ex
        return ex.connected()

    def status(self) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        st = ex.status()
        return {
            "id": self.id,
            "label": self.label,
            "tool_names": list(self.tool_names),
            "availability": self.availability,
            "kind": self.kind,
            "category": self.category,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "configured": st.get("oauth_configured", False),
            "connected": st.get("connected", False),
            "enabled": st.get("enabled", True),
            "days": st.get("days"),
            "scopes": st.get("scopes"),
            "progress": st.get("progress"),
            "ledger": st.get("ledger"),
            "oauth_mode": "redirect" if public_base_url() else "loopback",
            "public_base": public_base_url(),
            "redirect_base": oauth_redirect_base(),
            "error": None,
        }

    def begin_connect(self, *, public_base: str | None = None,
                      return_path: str | None = None) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        base = (public_base or "").strip().rstrip("/") or public_base_url()
        if base and base.lower().startswith("https://"):
            return ex.start_oauth_redirect(
                base, redirect_base=oauth_redirect_base(),
                return_path=return_path)
        # Desktop / no public HTTPS — existing loopback (blocks until done).
        result = ex.start_oauth_loopback()
        if result.get("ok"):
            result = {**result, "mode": "loopback"}
        return result

    def complete_connect(self, code: str, state: str,
                         *, redirect_uri: str) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        return ex.complete_oauth_redirect(code, state, redirect_uri=redirect_uri)

    def sync(self) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        if not ex.connected():
            return {"ok": False, "error": "not connected"}
        if ex.progress().get("running"):
            return {"ok": True, "running": True, **ex.progress()}

        def _worker():
            ex.run_ingest(fetch=True)

        t = threading.Thread(target=_worker, name="exhaust-ingest", daemon=True)
        t.start()
        return {"ok": True, "started": True}

    def disconnect(self) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        return ex.clear_tokens()

    def purge(self) -> dict[str, Any]:
        from app.services import exhaust_ingest as ex
        return ex.purge()


google = GoogleConnector()
