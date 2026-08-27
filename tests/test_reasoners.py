"""Track D reasoners — commitment / relationship / scheduling."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class CommitmentReasonerTests(unittest.TestCase):
    def test_at_risk_commitment_becomes_proposal(self):
        from app.services.reasoners import commitment
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                due = time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(now - 2 * 86400))
                fid = store.add_commitment(
                    "Send Scott the term sheet",
                    confidence=0.9, extracted_at=now - 10 * 86400,
                    due=due)
                props = commitment.propose(store, now=now)
                self.assertTrue(any(p.fact_id == fid for p in props))
                self.assertTrue(all(p.deliverable_only for p in props
                                    if p.fact_id == fid))
                self.assertTrue(any("Follow through" in p.goal for p in props
                                    if p.fact_id == fid))
            finally:
                store.close()

    def test_ancient_overdue_skipped_for_chat(self):
        """1125d-overdue junk must not reopen Chat on every launch."""
        from app.services.reasoners import commitment
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                due = time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(now - 1125 * 86400))
                fid = store.add_commitment(
                    "Join reference calls with the speaker commitment",
                    confidence=0.9, extracted_at=now - 1200 * 86400,
                    due=due)
                props = commitment.propose(store, now=now)
                self.assertFalse(any(p.fact_id == fid for p in props))
            finally:
                store.close()

    def test_dismiss_survives_reload(self):
        from app.services.reasoners import base as rb
        from app.services.reasoners.base import Proposal, clear_cooldown_for_tests

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cd.json"
            with mock.patch.dict(os.environ, {
                    "QUILL_REASONER_COOLDOWN_PATH": str(path)}):
                clear_cooldown_for_tests()
                # Force reload from the patched path next time.
                rb._loaded = False
                prop = Proposal(
                    reasoner="commitment",
                    goal="Follow through on this open commitment with Justin: x",
                    summary="Follow through: x",
                    fact_id=99,
                    person="Justin Adorante",
                )
                rb.mark_dismissed(prop)
                self.assertTrue(path.is_file())
                rb._recent.clear()
                rb._loaded = False
                self.assertTrue(rb.on_cooldown(prop))


class RelationshipReasonerTests(unittest.TestCase):
    def test_quiet_person_surfaces(self):
        from app.services.reasoners import relationship
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                pid = store.resolve_person("Quiet Friend", ts=now - 60 * 86400)
                with store._lock:
                    store._conn.execute(
                        "UPDATE people SET last_seen=? WHERE id=?",
                        (now - 45 * 86400, pid))
                    store._conn.commit()
                props = relationship.propose(store, now=now)
                self.assertTrue(any(
                    (p.person or "").lower() == "quiet friend" for p in props))
                self.assertTrue(all(p.deliverable_only for p in props))
            finally:
                store.close()


class SchedulingReasonerTests(unittest.TestCase):
    def test_due_soon_task_proposes_schedule(self):
        from app.services.reasoners import scheduling
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                due = time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(now + 1.5 * 86400))
                fid = store.add_task(
                    "Finish investor memo",
                    confidence=0.85, extracted_at=now - 86400, due=due)
                props = scheduling.propose(store, now=now)
                self.assertTrue(any(p.fact_id == fid for p in props))
                self.assertTrue(any("schedule" in p.goal.lower() for p in props
                                    if p.fact_id == fid))
            finally:
                store.close()

    def test_meeting_note_not_schedule_work(self):
        """Meeting announcements must not reopen Chat as 'Schedule work'."""
        from app.services.reasoners import scheduling
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                due = time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(now + 0.5 * 86400))
                fid = store.add_task(
                    "I have a meeting with Andy Karos today at 8:30 pm about Mnemos",
                    confidence=0.9, extracted_at=now - 3600, due=due)
                props = scheduling.propose(store, now=now)
                self.assertFalse(any(p.fact_id == fid for p in props))
            finally:
                store.close()


class ReasonerGateAndCompilerTests(unittest.TestCase):
    def test_run_once_respects_readiness_hold(self):
        from app.services import reasoners
        from app.services.reasoners.base import Proposal, clear_cooldown_for_tests
        from app.storage import Store

        clear_cooldown_for_tests()
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                prop = Proposal(
                    reasoner="commitment",
                    goal="Follow through on: ignore me",
                    summary="test",
                    confidence=0.1,
                    fact_id=1,
                    deliverable_only=True,
                    why=["test"],
                )
                with mock.patch.object(reasoners, "enabled", return_value=True), \
                     mock.patch.object(reasoners, "scan", return_value=[prop]), \
                     mock.patch("app.services.readiness.for_task") as ft:
                    class V:
                        should_offer = False
                        band = "hold"
                        score = 0.1
                        risk = "low"
                    ft.return_value = V()
                    out = reasoners.run_once(store, surface=False)
                self.assertTrue(out["ok"])
                self.assertFalse(out["offered"])
                self.assertIn("readiness", out.get("reason") or "")
            finally:
                store.close()

    def test_daily_budget_blocks(self):
        from app.services import reasoners
        from app.services.reasoners import base as rb
        from app.services.reasoners.base import Proposal, clear_cooldown_for_tests
        from app.storage import Store

        clear_cooldown_for_tests()
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                prop = Proposal(
                    reasoner="commitment",
                    goal="Follow through on: budget test",
                    summary="budget",
                    confidence=0.9,
                    fact_id=2,
                    deliverable_only=True,
                    why=["test"],
                )
                with mock.patch.object(rb, "_DAILY_MAX", 0), \
                     mock.patch.object(reasoners, "enabled", return_value=True), \
                     mock.patch.object(reasoners, "scan", return_value=[prop]):
                    out = reasoners.run_once(store, surface=True)
                self.assertEqual(out.get("reason"), "daily_budget")
                self.assertFalse(out.get("offered"))
            finally:
                store.close()

    def test_commitment_compiler_routes(self):
        from app.services import agent_planner
        from app.services.agent_planner import (
            PersonalAgentLayer, core_workflow_for, _action_kind_of,
            render_deliverable,
        )
        goal = "Follow through on this open commitment: Send Scott the term sheet"
        self.assertEqual(_action_kind_of(goal), "follow_through")
        self.assertEqual(core_workflow_for(goal), "commitment_follow_through")
        with tempfile.TemporaryDirectory() as td:
            from app.storage import Store
            store = Store(Path(td) / "t.db")
            try:
                layer = PersonalAgentLayer(store=store)
                # Keep the test LLM-free so the plain brief path is deterministic.
                with mock.patch.object(agent_planner, "_llm", return_value=None):
                    plan = layer.compile(goal)
                self.assertTrue(plan.steps)
                self.assertEqual(plan.steps[0].agent_type, "commitment_agent")
                self.assertEqual(plan.steps[0].surface, "none")
                fields = plan.steps[0].packet.fields or {}
                self.assertIn("briefing", fields)
                self.assertIn("follow-through",
                              (fields.get("summary") or "").lower()
                              + (fields.get("briefing") or "").lower())
                text = render_deliverable(plan.steps[0].packet)
                self.assertTrue(len(text) > 20)
            finally:
                store.close()

    def test_risk_table_send_untouched(self):
        """I-4: reasoners must not soften send/buy/pay."""
        from app.services.agent_planner import classify_risk, RISK_TABLE
        self.assertEqual(RISK_TABLE["send"], "high")
        self.assertEqual(RISK_TABLE["buy"], "high")
        self.assertEqual(RISK_TABLE["pay"], "high")
        self.assertEqual(classify_risk("send")[0], "high")
        self.assertEqual(classify_risk("follow_through")[0], "low")


class FulfillmentBaselineTests(unittest.TestCase):
    def test_stamp_and_delta(self):
        from app.services import fulfillment
        with tempfile.TemporaryDirectory() as td:
            base_path = Path(td) / "fulfillment_baseline.json"
            with mock.patch.object(fulfillment, "_baseline_path",
                                   return_value=base_path):
                s1 = {"fulfillment_rate": 0.5, "on_time_rate": 0.4,
                      "overdue_open": 2, "counts": {"done": 5, "cancelled": 5,
                                                    "open": 3}}
                stamped = fulfillment.stamp_baseline(s1, note="test")
                self.assertEqual(stamped["fulfillment_rate"], 0.5)
                s2 = dict(s1)
                s2["fulfillment_rate"] = 0.7
                out = fulfillment.with_baseline(s2)
                self.assertAlmostEqual(out["fulfillment_delta"], 0.2)


class PersonFreshnessTests(unittest.TestCase):
    def test_merge_marks_stale_role(self):
        from app.services import person_details
        now = time.time()
        mined = {"role": {"value": "CEO", "fact_id": 1, "quote": "x",
                          "confidence": 0.8, "ts": now - 200 * 86400}}
        out = person_details.merge(mined, {}, now=now)
        self.assertTrue(out["role"]["stale"])
        self.assertEqual(out["role"]["source"], "memory")
