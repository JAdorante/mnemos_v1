"""Shared setup for the fleet federation tests (not collected: no test_ prefix).

Every fleet path points into a temp dir, QUILL_FLEET=1, and the previous env
values are RESTORED in tearDown (never popped blindly — see the suite notes on
cross-test env leakage).
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

FLEET_ENV = {
    "QUILL_FLEET_AGENTS": "fleet_agents.json",
    "QUILL_FLEET_ROUTES": "fleet_routes.json",
    "QUILL_FLEET_RESTRICTED": "restricted_list.json",
    "QUILL_FLEET_STATE": "fleet_state.json",
    "QUILL_FLEET_OFFERS": "fleet_offers.json",
    "QUILL_FLEET_OUTBOX": "fleet_outbox.json",
    "QUILL_FLEET_ORIGINS": "fleet_origins.json",
}
FLAG_ENV = ("QUILL_FLEET", "QUILL_FLEET_SEND_SYNC", "QUILL_FLEET_RATE",
            "QUILL_FLEET_MAX_HOPS", "QUILL_FLEET_OWNER_AUTH",
            "QUILL_ORG_COORD_DATA", "QUILL_RELAY_ADMIN_TOKEN",
            "QUILL_RELAY_COMPLIANCE_TOKEN", "QUILL_RELAY_FORWARD_SYNC",
            "QUILL_RELAY_LOG", "QUILL_RELAY_RESTRICTED")


def agent_body(**over) -> dict:
    body = {"topic": "macro.rates", "instrument": "TLT",
            "direction": "bearish", "horizon": "weeks", "confidence": 0.7,
            "thesis": "Term premium is rebuilding after the auction tail.",
            "sources": [{"name": "internal desk notes",
                         "license": "internal_ok"}]}
    body.update(over)
    return body


def full_signal(**over) -> dict:
    now = time.time()
    sig = {"signal_id": "sid-1", "origin_id": "sabc:origin-1",
           "producer": "agent:rates", "ts": now, "expires_at": now + 600,
           "topic": "macro.rates", "instrument": "TLT",
           "direction": "bearish", "horizon": "weeks", "confidence": 0.7,
           "thesis": "Term premium is rebuilding.",
           "sources": [{"name": "desk notes", "license": "internal_ok"}],
           "hops": 0, "thread_id": None, "derived_from": None, "sig": ""}
    sig.update(over)
    return sig


class FleetEnvMixin:
    """Mixin for unittest.TestCase: temp fleet paths + flags, restored after."""

    fleet_enabled = True

    def setUp(self) -> None:  # noqa: D401
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet_"))
        self._saved_env = {k: os.environ.get(k)
                           for k in list(FLEET_ENV) + list(FLAG_ENV)}
        for key, name in FLEET_ENV.items():
            os.environ[key] = str(self.tmp / name)
        os.environ["QUILL_FLEET"] = "1" if self.fleet_enabled else "0"
        os.environ["QUILL_FLEET_SEND_SYNC"] = "1"
        os.environ["QUILL_RELAY_FORWARD_SYNC"] = "1"
        for k in ("QUILL_FLEET_RATE", "QUILL_FLEET_MAX_HOPS",
                  "QUILL_FLEET_OWNER_AUTH"):
            os.environ.pop(k, None)
        (self.tmp / "restricted_list.json").write_text(
            '{"instruments": ["XYZ"]}', encoding="utf-8")
        from app.events import EventBus
        from app.services.fleet import feed, ingress
        # A private bus: a memory subscriber left attached by another module
        # must never persist fleet test events into a real (or closed) store.
        self._saved_bus = (feed.bus, feed._attached)
        self.bus = EventBus()
        feed.bus = self.bus
        feed._attached = False
        feed.reset()
        ingress.reset()

    def tearDown(self) -> None:
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from app.services.fleet import feed, ingress
        feed.reset()
        feed.bus, feed._attached = self._saved_bus
        ingress.reset()
        super().tearDown()


def fleet_app():
    """A FastAPI app with only the fleet router — hermetic, no app startup."""
    from fastapi import FastAPI

    from app.api.fleet_routes import router
    app = FastAPI()
    app.include_router(router)
    return app


class FleetTestCase(FleetEnvMixin, unittest.TestCase):
    pass
