"""One egress policy, decided once.

Every outbound path (peer, team, org digests, fleet relay, records) asks
`egress.decide()`, and every outcome lands in the egress trail /privacy/egress
reads. The governed-egress rungs are settings of that one function.
"""
from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import egress

ROOT = Path(__file__).resolve().parent.parent


class _Sandboxed(unittest.TestCase):
    def setUp(self):
        from app.services.model_log import model_log
        self._orig = model_log._path
        model_log._path = (Path(tempfile.mkdtemp(prefix="mnemos-test-egress-"))
                           / "model_calls.jsonl")
        self.trail = model_log._path
        self._env = mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": "connected"})
        self._env.start()

    def tearDown(self):
        from app.services.model_log import model_log
        model_log._path = self._orig
        self._env.stop()

    def rung(self, r):
        return mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": r})

    def rows(self):
        if not self.trail.is_file():
            return []
        return [json.loads(ln) for ln in self.trail.read_text().splitlines()]


class DecideTests(_Sandboxed):
    def test_connected_follows_the_path_policy(self):
        for policy, want in (("auto", "allow"), ("share", "allow"),
                             ("offer", "offer"), ("deny", "deny"),
                             ("local", "deny")):
            v = egress.decide(path="peer", destination="Marc", policy=policy,
                              text="the launch moved to Friday", log=False)
            self.assertEqual(v.action, want, policy)

    def test_privacy_ceiling_denies_whatever_the_policy(self):
        v = egress.decide(path="fleet_relay", destination="relay",
                          policy="share", approved=True,
                          privacy_class="sensitive", log=False)
        self.assertEqual(v.action, "deny")
        self.assertIn("privacy_class=sensitive", v.reason)

    def test_classifier_failure_fails_closed(self):
        with mock.patch("app.services.privacy_class.classify_text",
                        side_effect=RuntimeError("boom")):
            v = egress.decide(path="peer", destination="x", policy="allow",
                              text="anything", log=False)
        self.assertTrue(v.denied)

    def test_personal_class_needs_a_human(self):
        v = egress.decide(path="peer", destination="x", policy="auto",
                          data_class="personal", text="ok", log=False)
        self.assertEqual(v.action, "offer")
        v = egress.decide(path="peer", destination="x", policy="auto",
                          data_class="personal", text="ok", approved=True,
                          log=False)
        self.assertEqual(v.action, "allow")

    def test_approval_never_lifts_a_deny(self):
        v = egress.decide(path="peer", destination="x", policy="deny",
                          approved=True, text="ok", log=False)
        self.assertTrue(v.denied)

    def test_unknown_path_is_denied(self):
        self.assertTrue(egress.decide(path="carrier_pigeon", destination="x",
                                      policy="allow", log=False).denied)


class RungTests(_Sandboxed):
    def test_sealed_puts_a_human_on_every_send(self):
        with self.rung("sealed"):
            v = egress.decide(path="org_digest", destination="org",
                              policy="allow", text="shipped the beta",
                              log=False)
            self.assertEqual(v.action, "offer")
            v = egress.decide(path="org_digest", destination="org",
                              policy="allow", text="shipped the beta",
                              approved=True, log=False)
            self.assertEqual(v.action, "allow")

    def test_sealed_lowers_the_ceiling(self):
        with self.rung("sealed"):
            v = egress.decide(path="peer", destination="x", policy="allow",
                              privacy_class="personal", approved=True,
                              log=False)
        self.assertTrue(v.denied)

    def test_perimeter_lets_only_approved_records_out(self):
        with self.rung("perimeter"):
            for path in ("peer", "team", "org_digest", "fleet_relay"):
                self.assertTrue(egress.decide(
                    path=path, destination="x", policy="allow",
                    approved=True, text="ok", log=False).denied, path)
            self.assertTrue(egress.decide(
                path="records", destination="org", policy="offer",
                text="ok", log=False).denied)
            self.assertTrue(egress.decide(
                path="records", destination="org", policy="offer",
                approved=True, text="ok", log=False).allowed)

    def test_rung_moves_the_shared_ceiling(self):
        # privacy_class.egress_allowed is what the content filters (peer
        # claims, digest lines, MCP tools, meeting mail) call.
        from app.services import privacy_class as pc
        self.assertTrue(pc.egress_allowed("personal"))
        with self.rung("sealed"):
            self.assertFalse(pc.egress_allowed("personal"))
            self.assertTrue(pc.egress_allowed("internal"))

    def test_org_rung_comes_from_the_heartbeat_cache(self):
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": ""}), \
                mock.patch.object(egress, "_membership",
                                  return_value={"rung": "perimeter"}), \
                mock.patch.object(egress, "_org_rung", (0.0, "")):
            self.assertEqual(egress.rung(), "perimeter")


class TrailTests(_Sandboxed):
    def test_every_path_lands_in_the_egress_view(self):
        from app.services.model_log import model_log
        for path in egress.PATHS:
            egress.decide(path=path, destination=f"to-{path}", policy="allow",
                          approved=True, text="status update")
        egress.decide(path="peer", destination="denied-one", policy="deny",
                      text="x")
        inv = model_log.egress_inventory(recent=50)
        rows = inv.get("recent") or inv.get("rows") or inv.get("trail") or []
        tasks = {r.get("task") for r in rows}
        for path in egress.PATHS:
            self.assertIn(f"egress.{path}", tasks, path)
        actions = {(r.get("meta") or {}).get("egress_action")
                   for r in self.rows()}
        self.assertTrue({"allow", "deny"} <= actions)

    def test_record_send_logs_the_delivery_result(self):
        v = egress.decide(path="peer", destination="Marc", policy="offer",
                          approved=True, text="eta friday", log=False)
        self.assertEqual(self.rows(), [])
        egress.record_send(v, path="peer", destination="Marc", ok=False,
                           approved=True)
        row = self.rows()[-1]
        self.assertFalse(row["ok"])
        self.assertFalse(row["meta"]["sent"])
        self.assertEqual(row["meta"]["approving_action"], "human")


class PathsUseTheOneRuleTests(_Sandboxed):
    def test_fleet_share_under_sealed_becomes_an_offer(self):
        from app.services.fleet import router as fr
        sig = {"topic": "eng.status", "kind": "note", "summary": "beta shipped",
               "body": {}, "producer": "a1"}
        with self.rung("sealed"), \
                mock.patch.object(fr, "route",
                                  return_value=fr.Decision("share", "rule")), \
                mock.patch.object(fr, "create_offer") as co, \
                mock.patch("app.services.fleet.relay_client.send_async") as send:
            d = fr.apply(sig)
        self.assertEqual(d.action, "offer")
        co.assert_called_once()
        send.assert_not_called()

    def test_org_digest_is_held_under_perimeter(self):
        from app.services import org_client, org_digest
        with self.rung("perimeter"), \
                mock.patch.object(org_client, "enabled", return_value=True), \
                mock.patch.object(org_client, "post_digest") as post:
            out = org_digest.ship_digest({"summary": "weekly"},
                                         notify_manager_peer=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["egress"], "deny")
        post.assert_not_called()

    def test_no_outbound_path_reimplements_the_ceiling(self):
        # The five paths call egress (or the rung-aware privacy_class
        # helpers); none compares privacy ranks against a constant itself.
        offenders = []
        for rel in ("app/services/peer_channel.py", "app/services/team_layer.py",
                    "app/services/org_digest.py", "app/services/fleet/router.py",
                    "app/services/records/promotion.py",
                    "app/services/records/org_client.py"):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and node.attr in ("EGRESS_CEILING", "SENSITIVE")
                        and isinstance(node.value, ast.Name)
                        and node.value.id in ("pc", "_pc", "privacy_class")):
                    offenders.append(f"{rel}:{node.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
