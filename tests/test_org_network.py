"""Hermetic tests for Org AI Network (coordinator + node services).

No network / no Anthropic required — uses fallbacks and TempDir stores.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))


class OrgCoordinatorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="org_coord_")
        os.environ["QUILL_ORG_COORD_DATA"] = self._tmp
        from org_coordinator import store
        self.store = store

    def tearDown(self) -> None:
        os.environ.pop("QUILL_ORG_COORD_DATA", None)

    def test_register_chain_and_skip_level(self) -> None:
        s = self.store
        s.upsert_node({
            "node_id": "ceo1", "role": "ceo", "display_name": "CEO",
            "reports_to": "", "token_sha256": "a",
        })
        s.upsert_node({
            "node_id": "exec1", "role": "exec", "display_name": "Exec",
            "reports_to": "ceo1", "token_sha256": "b",
        })
        s.upsert_node({
            "node_id": "mgr1", "role": "manager", "display_name": "Mgr",
            "reports_to": "exec1", "token_sha256": "c",
        })
        s.upsert_node({
            "node_id": "ic1", "role": "ic", "display_name": "IC",
            "reports_to": "mgr1", "token_sha256": "d",
        })
        chain = s.manager_chain("ic1")
        self.assertEqual([n["node_id"] for n in chain],
                         ["ic1", "mgr1", "exec1", "ceo1"])
        target = s.skip_level_target("ic1", min_role="exec")
        self.assertEqual(target["node_id"], "exec1")
        reports = s.reports_of("mgr1")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["node_id"], "ic1")

    def test_goals_and_digests(self) -> None:
        s = self.store
        g = s.add_goal("Ship launch", horizon="Q3", priority=0.9)
        self.assertTrue(g["id"])
        self.assertEqual(len(s.list_goals()), 1)
        s.upsert_node({
            "node_id": "ic1", "role": "ic", "display_name": "IC",
            "reports_to": "mgr1", "token_sha256": "d",
        })
        s.upsert_node({
            "node_id": "mgr1", "role": "manager", "display_name": "Mgr",
            "reports_to": "", "token_sha256": "c",
        })
        s.append_digest({
            "node_id": "ic1", "progress": ["built X"],
            "blockers": ["waiting on fab"], "asks": [], "deps": [],
            "summary": "fab delay", "confidence": 0.8, "role": "ic",
        })
        team = s.digests_from_reports("mgr1")
        self.assertEqual(len(team), 1)


class OrgEscalateKeywordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="org_esc_")
        os.environ["QUILL_ORG_COORD_DATA"] = self._tmp
        from org_coordinator import store
        store.upsert_node({
            "node_id": "ic1", "role": "ic", "reports_to": "mgr1",
            "token_sha256": "x", "display_name": "IC",
        })
        store.upsert_node({
            "node_id": "mgr1", "role": "manager", "reports_to": "exec1",
            "token_sha256": "y", "display_name": "Mgr",
        })
        store.upsert_node({
            "node_id": "exec1", "role": "exec", "reports_to": "",
            "token_sha256": "z", "display_name": "Exec",
        })

    def tearDown(self) -> None:
        os.environ.pop("QUILL_ORG_COORD_DATA", None)

    def test_force_strategic_routes_to_exec(self) -> None:
        from org_coordinator import escalate as esc
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            res = esc.route("ic1", {
                "summary": "manufacturing delay threatens product launch",
                "blockers": ["fab slip 3 weeks"],
                "force_strategic": True,
            })
        self.assertTrue(res["ok"])
        self.assertTrue(res["decision"]["escalate"])
        self.assertEqual(res["target"]["node_id"], "exec1")


class OrgDigestFallbackTests(unittest.TestCase):
    def test_fallback_digest_structure(self) -> None:
        from app.services import org_digest
        packet = "OPEN TASKS:\n- finish API\n- blocked on vendor delay\n"
        d = org_digest._fallback_digest(packet)
        self.assertIn("summary", d)
        self.assertIn("progress", d)
        self.assertIn("blockers", d)
        self.assertTrue(d["blockers"])


class OrgPriorityGroundingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="org_pri_")
        os.environ["QUILL_ORG_PRIORITIES"] = str(
            Path(self._tmp) / "priorities.json")
        # Force settings path via env — OrgNodeConfig reads at import time;
        # org_priority._path uses settings.org.priorities_path property which
        # re-reads env each call via _get in the property... actually the
        # property uses _get which reads env live. Good.
        from app.services import org_priority
        self.org_priority = org_priority
        self.org_priority.save([])

    def tearDown(self) -> None:
        os.environ.pop("QUILL_ORG_PRIORITIES", None)

    def test_ingest_and_grounding_lines(self) -> None:
        res = self.org_priority.ingest_packet({
            "guidance": "Prioritize launch readiness over side projects.",
            "items": [{"title": "Unblock manufacturing", "why": "launch",
                       "weight": 0.9}],
            "goals": [{"id": "g1", "title": "Ship launch"}],
        })
        self.assertTrue(res["ok"])
        lines = self.org_priority.grounding_lines()
        self.assertTrue(any("launch" in ln.lower() for ln in lines))


class OnboardingReportingPredicateTests(unittest.TestCase):
    def test_reports_to_aliases(self) -> None:
        from app.services.onboarding import _predicate
        self.assertEqual(_predicate("manager"), "reports_to")
        self.assertEqual(_predicate("reports to"), "reports_to")
        self.assertEqual(_predicate("direct report"), "manages")
        self.assertEqual(_predicate("works with"), "works_with")


class PeerOrgKindTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="peer_org_")
        os.environ["QUILL_PEER_REGISTRY"] = str(Path(self._tmp) / "peers.json")
        os.environ["QUILL_PEER_ASKS"] = str(Path(self._tmp) / "asks.json")
        os.environ["QUILL_PEER_SENT"] = str(Path(self._tmp) / "sent.json")
        os.environ["QUILL_PEER_INGEST"] = "0"
        from app.services import peer_channel as pch
        self.pch = pch
        pch._pairing = None

    def tearDown(self) -> None:
        for k in ("QUILL_PEER_REGISTRY", "QUILL_PEER_ASKS",
                  "QUILL_PEER_SENT", "QUILL_PEER_INGEST"):
            os.environ.pop(k, None)
        self.pch._pairing = None

    def test_ask_rejects_unknown_kind(self) -> None:
        res = self.pch.ask("nope", "hi", kind="bogus")
        self.assertFalse(res.get("ok"))

    def test_handle_ask_org_escalate_always_offers(self) -> None:
        start = self.pch.start_pairing()
        claim = self.pch.claim_pairing(
            start["code"], "Mgr", "http://198.51.100.7:8000",
            "remote-token-0123456789abcdef")
        self.assertTrue(claim["ok"], claim)
        peer_id = claim["peer_id"]
        reg = json.loads(Path(os.environ["QUILL_PEER_REGISTRY"]).read_text(
            encoding="utf-8"))
        peer = reg[peer_id]
        # Present as authenticated peer dict used by handle_ask
        peer_view = {"peer_id": peer_id, "name": "Mgr",
                     "policy": {c: "auto" for c in self.pch.CLASSES}}
        res = self.pch.handle_ask(peer_view, {
            "ask_id": "abc123",
            "question": "[strategic escalation] launch blocked",
            "kind": "org_escalate",
        })
        self.assertEqual(res.get("status"), "pending")
        pending = self.pch.pending_asks()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "org_escalate")


class ModelRouterOrgTasksTests(unittest.TestCase):
    def test_org_tasks_registered(self) -> None:
        from app.services.model_router import MODELS
        for t in ("org_digest", "org_rollup", "org_escalate", "org_cascade"):
            self.assertIn(t, MODELS)


class OrgClientDisabledTests(unittest.TestCase):
    def test_ship_digest_disabled(self) -> None:
        from app.services import org_digest
        with mock.patch("app.services.org_client.enabled", return_value=False):
            res = org_digest.ship_digest({"summary": "x", "progress": [],
                                          "blockers": [], "asks": [],
                                          "deps": [], "confidence": 0.5})
        self.assertFalse(res.get("ok"))


if __name__ == "__main__":
    unittest.main()
