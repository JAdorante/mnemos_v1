"""Firm relay (Phase 4): topics, barriers, chained log, forwarding.

A signal published by node A reaches node B, node C outside the topic never
sees it, tampering with one log line makes verify_chain fail, and the retry
queue drains.
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.services.fleet import envelope as env
from tests.fleet_support import FleetEnvMixin, full_signal

ADMIN = "admin-token-0123456789abcdef"
COMPLIANCE = "compliance-token-0123456789abcdef"


class RelayBase(FleetEnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["QUILL_ORG_COORD_DATA"] = str(self.tmp / "coord")
        os.environ["QUILL_RELAY_ADMIN_TOKEN"] = ADMIN
        os.environ["QUILL_RELAY_COMPLIANCE_TOKEN"] = COMPLIANCE
        os.environ.pop("QUILL_RELAY_LOG", None)
        os.environ.pop("QUILL_RELAY_RESTRICTED", None)
        from org_coordinator import main, relay, relay_log
        self.relay = relay
        self.relay_log = relay_log
        self.client = TestClient(main.app)
        self.admin = {"Authorization": f"Bearer {ADMIN}"}
        self.tokens = {}
        for nid in ("node-a", "node-b", "node-c", "node-d"):
            r = self.client.post("/register", json={"node_id": nid})
            self.assertEqual(r.status_code, 200, r.text)
            self.tokens[nid] = r.json()["token"]
            self.assertEqual(self.client.post(
                "/relay/enroll", headers=self.h(nid),
                json={"fleet_url": f"http://{nid}.test",
                      "inbound_token": f"inbound-{nid}-0123456789abcdef"}
            ).status_code, 200)
        for nid, group in (("node-a", "research"), ("node-b", "research"),
                           ("node-c", "research"), ("node-d", "sales")):
            self.put(f"/relay/admin/nodes/{nid}/group", {"group": group})
        # node-c is outside the topic; node-d is a member behind a barrier.
        self.put("/relay/admin/topics/macro.rates",
                 {"members": ["node-a", "node-b", "node-d"],
                  "groups": ["research"]})
        self.put("/relay/admin/restricted", {"instruments": ["XYZ"]})
        self.delivered = []

        def fake_post(recipient, signal, sender):
            self.delivered.append((recipient, signal, sender))
            return 200

        p = mock.patch.object(relay, "_post_signal", side_effect=fake_post)
        p.start()
        self.addCleanup(p.stop)

    def h(self, nid) -> dict:
        return {"Authorization": f"Bearer {self.tokens[nid]}"}

    def put(self, path, body):
        r = self.client.put(path, json=body, headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def signed(self, nid="node-a", **over) -> dict:
        now = time.time()
        base = {"ts": now, "expires_at": now + 600, "hops": 1,
                "origin_id": f"s{nid}:{time.monotonic_ns()}"}
        sig = full_signal(**{**base, **over})
        return env.sign(sig, env.link_key(self.tokens[nid]))

    def publish(self, nid="node-a", signal=None):
        return self.client.post("/relay/publish", headers=self.h(nid),
                                json={"signal": signal or self.signed(nid)})


class ForwardTests(RelayBase):
    def test_a_reaches_b_and_c_never_sees_it(self) -> None:
        r = self.publish()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["recipients"], ["node-b"])
        self.assertEqual([d[0] for d in self.delivered], ["node-b"])
        self.assertEqual(self.delivered[0][2], "node-a")

    def test_barrier_blocks_a_member_whose_group_is_not_admitted(self) -> None:
        self.publish()
        self.assertNotIn("node-d", [d[0] for d in self.delivered])
        r = self.publish("node-d")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "barrier")

    def test_non_member_cannot_publish(self) -> None:
        r = self.publish("node-c")
        self.assertEqual((r.status_code, r.json()["error"]), (403, "barrier"))
        self.assertEqual(self.delivered, [])

    def test_a_node_with_no_group_sees_nothing(self) -> None:
        self.put("/relay/admin/nodes/node-b/group", {"group": None})
        self.assertEqual(self.publish().json()["recipients"], [])

    def test_signature_is_verified_and_tampering_refused(self) -> None:
        sig = self.signed()
        bad = dict(sig, thesis="something else entirely")
        self.assertEqual(self.publish(signal=bad).json()["error"],
                         "bad_signature")
        forged = env.sign(dict(sig, sig=""), env.link_key(self.tokens["node-b"]))
        self.assertEqual(self.publish(signal=forged).status_code, 403)
        self.assertEqual(self.client.post(
            "/relay/publish", json={"signal": sig}).status_code, 401)

    def test_envelope_is_revalidated(self) -> None:
        r = self.publish(signal=self.signed(hops=3))
        self.assertEqual((r.status_code, r.json()["error"]),
                         (422, "too_many_hops"))
        vendor = self.signed(sources=[{"name": "v", "license": "vendor"}])
        self.assertEqual(self.publish(signal=vendor).json()["error"],
                         "license_not_shareable")
        now = time.time()
        stale = env.sign(full_signal(ts=now - 100, expires_at=now - 1, hops=1),
                         env.link_key(self.tokens["node-a"]))
        self.assertEqual(self.publish(signal=stale).json()["error"], "expired")

    def test_restricted_list_is_enforced_and_fails_closed(self) -> None:
        r = self.publish(signal=self.signed(instrument="NYSE:XYZ"))
        self.assertEqual((r.status_code, r.json()["error"]),
                         (403, "restricted_instrument"))
        self.relay.restricted_path().unlink()
        r = self.publish()
        self.assertEqual((r.status_code, r.json()["error"]),
                         (503, "restricted_list_unavailable"))
        self.assertEqual(self.delivered, [])

    def test_replay_is_deduped_at_the_relay(self) -> None:
        sig = self.signed()
        self.publish(signal=sig)
        r = self.publish(signal=sig)
        self.assertTrue(r.json()["duplicate"])
        self.assertEqual(len(self.delivered), 1)

    def test_forward_is_resigned_per_link_and_otherwise_unchanged(self) -> None:
        from org_coordinator import relay as relay_mod
        sig = self.signed()
        captured = {}

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["auth"] = req.headers.get("Authorization")
            captured["body"] = json.loads(req.data)
            return Resp()

        mock.patch.stopall()
        with mock.patch.object(relay_mod.request, "urlopen",
                               side_effect=fake_urlopen):
            self.assertEqual(relay_mod._post_signal("node-b", sig, "node-a"),
                             200)
        inbound = "inbound-node-b-0123456789abcdef"
        self.assertEqual(captured["url"], "http://node-b.test/peer/ask")
        self.assertEqual(captured["auth"], f"Bearer {inbound}")
        body = captured["body"]
        self.assertEqual(body["kind"], "signal")
        self.assertEqual(body["sender_node"], "node-a")
        wire = body["signal"]
        self.assertTrue(env.verify(wire, env.link_key(inbound)))
        self.assertEqual({k: v for k, v in wire.items() if k != "sig"},
                         {k: v for k, v in sig.items() if k != "sig"})


class AuthTests(RelayBase):
    def test_admin_routes_need_the_admin_token(self) -> None:
        for headers in ({}, self.h("node-a"),
                        {"Authorization": f"Bearer {COMPLIANCE}"}):
            r = self.client.put("/relay/admin/topics/x",
                                json={"members": [], "groups": []},
                                headers=headers)
            self.assertEqual(r.status_code, 401)

    def test_unset_admin_token_refuses_everyone(self) -> None:
        os.environ["QUILL_RELAY_ADMIN_TOKEN"] = ""
        self.assertEqual(self.client.get("/relay/admin/topics",
                                         headers=self.admin).status_code, 401)

    def test_compliance_feed_is_read_only_and_role_scoped(self) -> None:
        self.publish()
        r = self.client.get("/relay/compliance/feed", headers=self.admin)
        self.assertEqual(r.status_code, 401)
        r = self.client.get("/relay/compliance/feed",
                            headers={"Authorization": f"Bearer {COMPLIANCE}"})
        self.assertEqual(r.status_code, 200)
        kinds = [row["kind"] for row in r.json()["rows"]]
        self.assertIn("forward", kinds)
        self.assertIn("topic_updated", kinds)

    def test_directory_never_exposes_forwarding_tokens(self) -> None:
        raw = self.client.get("/directory", headers=self.h("node-a")).text
        self.assertNotIn("inbound-node", raw)
        self.assertNotIn("token_sha256", raw)

    def test_reregistering_a_node_needs_its_current_token(self) -> None:
        r = self.client.post("/register", json={"node_id": "node-b"})
        self.assertEqual(r.status_code, 409)
        r = self.client.post("/register", json={"node_id": "node-b"},
                             headers=self.h("node-b"))
        self.assertEqual(r.status_code, 200)

    def test_topic_members_must_exist(self) -> None:
        r = self.client.put("/relay/admin/topics/t", headers=self.admin,
                            json={"members": ["ghost"], "groups": ["x"]})
        self.assertEqual(r.status_code, 422)


class LogTests(RelayBase):
    def test_chain_verifies_and_records_refusals(self) -> None:
        self.publish()
        self.publish("node-c")
        out = self.relay_log.verify_chain()
        self.assertTrue(out["ok"], out)
        rows = list(self.relay_log.iter_rows())
        kinds = [r["kind"] for r in rows]
        self.assertIn("forward", kinds)
        self.assertIn("refused", kinds)
        fwd = next(r for r in rows if r["kind"] == "forward")
        self.assertEqual(fwd["sender"], "node-a")
        self.assertEqual(fwd["recipients"], ["node-b"])
        self.assertIn("signal", fwd)

    def test_tampering_with_one_line_breaks_the_chain(self) -> None:
        self.publish()
        self.publish()
        p = self.relay_log.log_path()
        lines = p.read_text(encoding="utf-8").splitlines()
        idx = next(i for i, ln in enumerate(lines) if '"forward"' in ln)
        row = json.loads(lines[idx])
        row["recipients"] = ["node-b", "node-c"]
        lines[idx] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = self.relay_log.verify_chain()
        self.assertFalse(out["ok"])
        self.assertEqual(out["bad_line"], idx + 1)

    def test_deleting_a_line_breaks_the_chain(self) -> None:
        self.publish()
        p = self.relay_log.log_path()
        lines = p.read_text(encoding="utf-8").splitlines()
        p.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")
        self.assertFalse(self.relay_log.verify_chain()["ok"])

    def test_verify_chain_cli_exit_codes(self) -> None:
        import contextlib
        import io

        from org_coordinator import verify_chain
        self.publish()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify_chain.main([]), 0)
        p = self.relay_log.log_path()
        p.write_text(p.read_text(encoding="utf-8").replace("node-b", "node-x"),
                     encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(verify_chain.main(["--path", str(p)]), 1)


class RetryQueueTests(RelayBase):
    def test_failed_delivery_queues_and_drains(self) -> None:
        self.relay._post_signal.side_effect = lambda *a: 0
        self.publish()
        self.assertEqual(len(self.relay.queue()), 1)
        self.relay._post_signal.side_effect = lambda *a: 200
        out = self.client.post("/relay/admin/queue/drain",
                               headers=self.admin).json()
        self.assertEqual((out["delivered"], out["pending"]), (1, 0))
        self.assertEqual(self.relay.queue(), [])

    def test_backoff_holds_until_due(self) -> None:
        self.relay._post_signal.side_effect = lambda *a: 503
        self.publish()
        self.assertEqual(self.relay.drain()["pending"], 1)
        self.assertEqual(self.relay._post_signal.call_count, 1)

    def test_refusal_by_the_recipient_is_logged_not_retried(self) -> None:
        self.relay._post_signal.side_effect = lambda *a: 401
        self.publish()
        self.assertEqual(self.relay.queue(), [])
        kinds = [r["kind"] for r in self.relay_log.iter_rows()]
        self.assertIn("delivery_refused", kinds)

    def test_expired_undelivered_is_logged_and_dropped(self) -> None:
        now = time.time()
        self.relay._enqueue("node-b", full_signal(ts=now - 30,
                                                  expires_at=now - 1),
                            "node-a", "HTTP 0")
        self.assertEqual(self.relay.drain(force=True)["expired"], 1)
        kinds = [r["kind"] for r in self.relay_log.iter_rows()]
        self.assertIn("delivery_failed", kinds)
        self.assertTrue(self.relay_log.verify_chain()["ok"])


if __name__ == "__main__":
    unittest.main()
