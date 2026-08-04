"""Plan 5.2 — planner graduation: multi-step ≥2 packets pre-persisted + defaults."""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


NOW_GOAL = (
    "Send Marc the pricing follow-up and prep me for my meeting with Marc"
)


def _two_tasks():
    from app.services.multitask import AtomicTask
    return [
        AtomicTask(id="t1", text="Send Marc the pricing follow-up"),
        AtomicTask(id="t2", text="Prep me for my meeting with Marc"),
    ]


class PlannerDefaultsTests(unittest.TestCase):
    def test_planner_code_default_on(self):
        from app.services import agent_bridge, agent_planner

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUILL_PLANNER", None)
            self.assertTrue(agent_planner._enabled())
            self.assertTrue(agent_bridge._planner_enabled())

    def test_approval_binding_unset_is_enforce(self):
        from app.services.agent_planner import approval_binding_is_enforce

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QUILL_APPROVAL_BIND", None)
            self.assertTrue(approval_binding_is_enforce())


class MultiStepCompileTests(unittest.TestCase):
    def test_compile_multistep_yields_two_packets(self):
        from app.services import agent_planner as ap

        ap._LLM = False
        try:
            with mock.patch("app.services.multitask.decompose",
                            return_value=_two_tasks()):
                layer = ap.PersonalAgentLayer(store=mock.Mock())
                layer.select_context = lambda goal, person=None: ap.SelectedContext(
                    memory_block="- Marc pricing",
                    source_fact_ids=[7],
                    open_commitments=[{"text": "Send Marc pricing",
                                       "fact_id": 7}],
                )
                plan = layer.compile(NOW_GOAL)
        finally:
            ap._LLM = None

        self.assertGreaterEqual(len(plan.steps), 2)
        for step in plan.steps:
            self.assertIsNotNone(step.packet)
            self.assertTrue((step.packet.goal or step.goal or "").strip())

    def test_run_goal_prepersists_each_compiled_packet(self):
        """AC: multi-step goal → ≥2 packets each recorded before execute."""
        from app.services.agent_log import Recorder
        from app.services import agent_planner as ap
        from app.storage import Store
        from browser_agent.orchestrator import Agent

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            rec = Recorder(store=store)
            try:
                ap._LLM = False
                layer = ap.PersonalAgentLayer(store=store)
                layer.select_context = lambda goal, person=None: ap.SelectedContext(
                    memory_block="- Marc",
                    source_fact_ids=[1],
                )
                with mock.patch("app.services.multitask.decompose",
                                return_value=_two_tasks()):
                    plan = layer.compile(NOW_GOAL)
                self.assertGreaterEqual(len(plan.steps), 2)

                agent = Agent(recorder=rec, on_log=lambda _s: None,
                              on_ask=lambda _q: "approve")
                with mock.patch.object(
                    agent, "_run_goal_inner",
                    return_value=("ok", "success"),
                ):
                    for step in plan.steps:
                        if step.surface == "none":
                            rec.start_run(step.goal, surface="none",
                                          agent_type=step.agent_type)
                            self.assertIsNotNone(
                                rec.record_from_packet(step.packet))
                            rec.finish_run(status="success")
                        else:
                            agent.run_goal(
                                step.to_goal_text(),
                                dry_run="approval",
                                surface=step.surface or "browser",
                                packet=step.packet,
                            )

                with store._lock:
                    n = store._conn.execute(
                        "SELECT COUNT(*) FROM action_packets").fetchone()[0]
                self.assertGreaterEqual(int(n), 2)
            finally:
                ap._LLM = None
                store.close()

    def test_bridge_run_planned_records_two(self):
        from app.services.agent_bridge import AgentWorker
        from app.services.agent_log import Recorder
        from app.services import agent_planner as ap
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            rec = Recorder(store=store)
            try:
                ap._LLM = False
                real = ap.PersonalAgentLayer(store=store)
                real.select_context = lambda goal, person=None: ap.SelectedContext()
                with mock.patch("app.services.multitask.decompose",
                                return_value=_two_tasks()):
                    compiled = real.compile(NOW_GOAL)
                self.assertGreaterEqual(len(compiled.steps), 2)

                worker = AgentWorker.__new__(AgentWorker)
                worker.lock = threading.Lock()
                worker.agent = mock.Mock()
                worker.agent._recorder = rec
                worker.agent.last_distill_id = None
                recorded: list[int] = []

                def fake_run_goal(goal, dry_run=None, surface=None, packet=None,
                                  source_fact_id=None, source_fact_ids=None,
                                  study_mode=None):
                    if packet is not None:
                        rec.start_run(goal, surface=surface or "browser")
                        pid = rec.record_from_packet(packet)
                        if pid is not None:
                            recorded.append(int(pid))
                        rec.finish_run(status="success")
                    return ("ok", "success")

                worker.agent.run_goal = fake_run_goal
                worker._emit = lambda *a, **k: None
                worker._pop_grounding = lambda _a: ([], None, None)

                def fake_brief(step):
                    rec.start_run(step.goal, surface="none")
                    pid = rec.record_from_packet(step.packet)
                    if pid is not None:
                        recorded.append(int(pid))
                    rec.finish_run(status="success")
                    return "brief"

                worker._deliver_briefing = fake_brief

                with mock.patch("app.services.agent_planner.planner") as pl:
                    pl.compile.return_value = compiled
                    out = AgentWorker._run_planned(
                        worker, {"text": NOW_GOAL, "dry_run": "approval",
                                 "surface": None, "fact_id": None,
                                 "study_mode": None})
                self.assertEqual(out[1], "success")
                self.assertGreaterEqual(len(recorded), 2)
            finally:
                ap._LLM = None
                store.close()


class CoreOnlyGateStillWorks(unittest.TestCase):
    def test_planner_off_uses_core_allowlist(self):
        from app.services.agent_bridge import _should_plan

        with mock.patch.dict(os.environ, {
            "QUILL_PLANNER": "0",
            "QUILL_PLANNER_CORE": "1",
        }):
            self.assertTrue(_should_plan(
                "draft a follow-up email to Justin", None, None))
            self.assertFalse(_should_plan(
                "what's on my calendar today", None, None))


if __name__ == "__main__":
    unittest.main()
