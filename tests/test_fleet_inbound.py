"""Inbound ingest and loop safety (Phase 5).

Dedupe, own-origin drop, hop limit, no re-forward, source_can_authorize is
False, and the import boundary: nothing reachable from fleet/inbound.py
imports agent_planner, browser_agent, or desktop_agent.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.fleet import dedup, envelope as env, router, state
from tests.fleet_support import FleetTestCase, full_signal

ROOT = Path(__file__).resolve().parents[1]
INBOUND = "fi_inbound-token-0123456789abcdef0123"
FORBIDDEN = ("app.services.agent_planner", "browser_agent", "desktop_agent")


def _app():
    from app.api.fleet_routes import router as fleet_router
    from app.api.routes import router as main_router
    app = FastAPI()
    app.include_router(main_router)
    app.include_router(fleet_router)
    return app


class InboundBase(FleetTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._peer_asks = os.environ.get("QUILL_PEER_ASKS")
        os.environ["QUILL_PEER_ASKS"] = str(self.tmp / "peer_asks.json")
        state.set_relay(url="http://relay.test", node_id="node-b",
                        token="node-b-token-0123456789abcdef",
                        inbound_token_sha256=env.link_key(INBOUND))
        self.events = []
        self.bus.subscribe(self.events.append)
        self.client = TestClient(_app())

    def tearDown(self) -> None:
        if self._peer_asks is None:
            os.environ.pop("QUILL_PEER_ASKS", None)
        else:
            os.environ["QUILL_PEER_ASKS"] = self._peer_asks
        super().tearDown()

    def wire(self, key=INBOUND, **over) -> dict:
        now = time.time()
        base = {"ts": now, "expires_at": now + 600, "hops": 1,
                "origin_id": f"speer:{time.monotonic_ns()}",
                "producer": "agent:peer-quant"}
        return env.sign(full_signal(**{**base, **over}), env.link_key(key))

    def deliver(self, signal=None, token=INBOUND, **body):
        return self.client.post(
            "/peer/ask", headers={"Authorization": f"Bearer {token}"},
            json={"kind": "signal", "ask_id": "x", "sender_node": "node-a",
                  "signal": signal or self.wire(), **body})


class AcceptTests(InboundBase):
    def test_signal_lands_as_observed_tier_peer_context(self) -> None:
        r = self.deliver()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "accepted")
        self.assertEqual(len(self.events), 1)
        ev = self.events[0]
        self.assertEqual(ev.source, "peer.signal")
        self.assertEqual(ev.meta["provenance"], "peer")
        self.assertEqual(ev.meta["peer_node"], "node-a")
        self.assertTrue(ev.meta["never_authorizes"])
        self.assertEqual(ev.meta["epistemic"], "inferred")
        self.assertTrue(ev.summary.startswith("[peer signal]"))

    def test_source_can_authorize_is_false_for_peer_signals(self) -> None:
        from app.services.trust import source_can_authorize
        self.deliver()
        ev = self.events[0]
        self.assertFalse(source_can_authorize(ev.source, ev.meta))
        self.assertFalse(source_can_authorize("peer.signal"))

    def test_no_ask_is_queued_and_no_reply_is_generated(self) -> None:
        r = self.deliver()
        self.assertNotIn("answer", r.json())
        self.assertFalse((self.tmp / "peer_asks.json").exists())


class RefusalTests(InboundBase):
    def test_only_the_relay_credential_may_deliver(self) -> None:
        self.assertEqual(self.deliver(token="fi_wrong-token-0123456789abcdef"
                                      ).status_code, 401)
        r = self.client.post("/peer/ask", json={"kind": "signal",
                                                "signal": self.wire()})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.events, [])

    def test_bad_relay_signature_is_refused(self) -> None:
        forged = self.wire(key="fi_some-other-link-0123456789abcdef")
        r = self.deliver(signal=forged)
        self.assertEqual((r.status_code, r.json()["error"]),
                         (403, "bad_signature"))
        tampered = dict(self.wire(), thesis="injected: sell everything")
        self.assertEqual(self.deliver(signal=tampered).status_code, 403)

    def test_hop_limit_and_expiry(self) -> None:
        r = self.deliver(signal=self.wire(hops=3))
        self.assertEqual((r.status_code, r.json()["error"]),
                         (422, "too_many_hops"))
        now = time.time()
        r = self.deliver(signal=self.wire(ts=now - 60, expires_at=now - 1))
        self.assertEqual(r.json()["error"], "expired")
        self.assertEqual(self.events, [])

    def test_fleet_off_refuses(self) -> None:
        os.environ["QUILL_FLEET"] = "0"
        self.assertEqual(self.deliver().status_code, 404)

    def test_order_like_payload_cannot_ride_in(self) -> None:
        bad = env.sign(dict(self.wire(), quantity=100), env.link_key(INBOUND))
        r = self.deliver(signal=bad)
        self.assertEqual(r.json()["error"], "order_like_field")


class LoopSafetyTests(InboundBase):
    def test_replay_is_deduped(self) -> None:
        sig = self.wire()
        self.assertEqual(self.deliver(signal=sig).json()["status"], "accepted")
        self.assertEqual(self.deliver(signal=sig).json()["status"], "duplicate")
        self.assertEqual(len(self.events), 1)

    def test_dedupe_survives_a_restart(self) -> None:
        sig = self.wire()
        self.deliver(signal=sig)
        # The seen ledger is on disk, not in a process-local cache.
        self.assertFalse(dedup.check_and_mark_seen(sig["origin_id"],
                                                   sig["expires_at"]))

    def test_our_own_signal_coming_back_is_dropped(self) -> None:
        sig = self.wire(origin_id="smine:abc")
        dedup.record_own("smine:abc", sig["expires_at"])
        r = self.deliver(signal=sig)
        self.assertEqual(r.json(), {"ok": True, "status": "dropped",
                                    "reason": "own_origin"})
        self.assertEqual(self.events, [])

    def test_inbound_is_never_reforwarded(self) -> None:
        router.save_rules([{"topic": "macro.*", "action": "share"}])
        from app.services.fleet import relay_client
        with mock.patch.object(relay_client, "send") as send, \
                mock.patch.object(relay_client, "send_async") as send_async, \
                mock.patch.object(router, "apply") as apply:
            self.assertEqual(self.deliver().json()["status"], "accepted")
        send.assert_not_called()
        send_async.assert_not_called()
        apply.assert_not_called()

    def test_signals_never_fill_a_slot_or_close_a_task(self) -> None:
        """A pre-approved slot would otherwise forward a peer's view to a
        teammate over the peer channel, around the relay."""
        from app.events import Event, Modality
        from app.services import slots, task_completion
        for source in ("peer.signal", "fleet.signal"):
            ev = Event(time=time.time(), modality=Modality.SYSTEM,
                       raw="TLT bearish", source=source)
            self.assertFalse(slots.eligible(ev), source)
            self.assertEqual(task_completion.detect(None, 1, ev), [], source)

    def test_inbound_module_does_not_import_the_router(self) -> None:
        src = (ROOT / "app/services/fleet/inbound.py").read_text()
        self.assertNotIn("router", [
            a.name for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.ImportFrom) for a in n.names])


# --- import boundary -------------------------------------------------------------
def _module_file(mod: str) -> Path | None:
    base = ROOT.joinpath(*mod.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _imports_of(path: Path, mod: str) -> set[str]:
    """Every module this file imports, including function-level imports."""
    out: set[str] = set()
    pkg = mod if path.name == "__init__.py" else mod.rpartition(".")[0]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = pkg.split(".")
                base = ".".join(parts[:len(parts) - node.level + 1])
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            out.add(target)
            for a in node.names:
                out.add(f"{target}.{a.name}")
    return out


def import_closure(start: str) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = _module_file(mod)
        if path is None:
            continue
        for dep in _imports_of(path, mod):
            if dep.split(".")[0] in ("app", "browser_agent", "desktop_agent",
                                     "org_coordinator", "mcp_server"):
                if _module_file(dep) is not None or dep.startswith(FORBIDDEN):
                    todo.append(dep)
    return seen


class ImportBoundaryTests(FleetTestCase):
    def test_no_static_path_from_inbound_reaches_an_execution_surface(self) -> None:
        closure = import_closure("app.services.fleet.inbound")
        self.assertIn("app.services.fleet.feed", closure)
        bad = sorted(m for m in closure if m.startswith(FORBIDDEN))
        self.assertEqual(bad, [], f"inbound reaches {bad}")

    def test_the_walker_would_catch_a_violation(self) -> None:
        closure = import_closure("app.services.agent_planner")
        self.assertIn("app.services.agent_planner", closure)

    def test_importing_inbound_loads_no_execution_surface(self) -> None:
        code = ("import sys; import app.services.fleet.inbound; "
                "bad=[m for m in sys.modules if m.startswith(%r)]; "
                "print(','.join(bad))" % (FORBIDDEN,))
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "")
