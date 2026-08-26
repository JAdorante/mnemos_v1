"""Agent-distill harvest: sessions/agent_distill.jsonl -> learning_pairs.

Covers the selection policy (verified steps from successful runs only), the
row mapping (task_type agent.act, machine-trust labeling), idempotence via
content-hash dedupe + watermark, and the isolation guards that keep agent
trajectories out of the TEXT champion's training and prompts.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.services import agent_harvest, learning_store
from app.storage import Store
from scripts.distill_curate import pair_to_distill_row


def mk_store() -> Store:
    tmp = Path(tempfile.mkdtemp())
    return Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")


def _step(session="s1", step=1, verified=True, obs="GOAL:\nplay solitaire\n"
          "CURRENT PAGE:\n[0] button: New Game", action=None, t=None):
    return {
        "id": f"{session}-{step}", "time": t or time.time(),
        "task": "browser.act", "session_id": session, "step": step,
        "url": "https://example.com/", "intent": "web_task",
        "site": "example.com", "model": "claude-sonnet-5",
        "escalated": False, "pixel": False, "vision": True,
        "observation": obs,
        "action": action or {"name": "click", "args": {"element_id": 3}},
        "verified": verified, "vnote": "the view changed",
        "step_status": "verified",
    }


def _run(session="s1", status="success", t=None):
    return {"id": f"{session}-run", "time": t or time.time(),
            "task": "browser.run", "session_id": session, "status": status,
            "steps": 3, "replans": 0, "escalations": 0,
            "intent": "web_task", "site": "example.com"}


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.store = mk_store()
        self.tmp = Path(tempfile.mkdtemp())
        self.trail = self.tmp / "agent_distill.jsonl"

    def _write(self, rows):
        with self.trail.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def _pairs(self):
        return [p for p in self.store.list_learning_pairs(limit=100)
                if p["task_type"] == "agent.act"]

    def test_verified_steps_of_successful_runs_become_pairs(self):
        self._write([_step(step=1), _step(step=2, verified=False),
                     _run(status="success")])
        counts = agent_harvest.harvest(self.trail, store=self.store)
        self.assertEqual(counts["harvested"], 1)
        self.assertEqual(counts["skipped_unverified"], 1)
        pair = self._pairs()[0]
        self.assertEqual(pair["verdict"], "accepted")
        self.assertEqual(pair["verdict_source"], "shadow.agent_verified")
        self.assertFalse(pair["human_confirmed"])
        self.assertEqual(pair["model_tag"], "claude-sonnet-5")
        self.assertEqual(json.loads(pair["final_target"])["name"], "click")
        self.assertEqual(pair["source_refs"]["site"], "example.com")
        self.assertIn("play solitaire", pair["input_text"])

    def test_failed_run_steps_are_skipped(self):
        self._write([_step(), _run(status="stopped_no_progress")])
        counts = agent_harvest.harvest(self.trail, store=self.store)
        self.assertEqual(counts["harvested"], 0)
        self.assertEqual(counts["skipped_run_not_success"], 1)
        self.assertEqual(self._pairs(), [])

    def test_run_without_outcome_row_is_skipped(self):
        self._write([_step()])   # crashed session: no browser.run row yet
        counts = agent_harvest.harvest(self.trail, store=self.store)
        self.assertEqual(counts["harvested"], 0)
        self.assertEqual(counts["skipped_run_not_success"], 1)

    def test_observationless_rows_are_skipped(self):
        self._write([_step(obs=None), _run()])
        counts = agent_harvest.harvest(self.trail, store=self.store)
        self.assertEqual(counts["skipped_no_observation"], 1)
        self.assertEqual(self._pairs(), [])

    def test_reharvest_is_idempotent(self):
        self._write([_step(), _run()])
        first = agent_harvest.harvest(self.trail, store=self.store)
        self.assertEqual(first["harvested"], 1)
        second = agent_harvest.harvest(self.trail, store=self.store)
        self.assertEqual(second["harvested"], 0)
        self.assertEqual(len(self._pairs()), 1)

    def test_watermark_skips_old_rows_but_overlap_covers_late_run_rows(self):
        # Steps land before their run row: a step just under the watermark
        # must still be harvestable once its outcome arrives.
        t0 = time.time()
        self._write([_step(step=1, t=t0)])
        agent_harvest.harvest(self.trail, store=self.store)  # no outcome yet
        self.assertEqual(self._pairs(), [])
        self._write([_run(t=t0 + 1)])
        counts = agent_harvest.harvest(self.trail, store=self.store)
        self.assertEqual(counts["harvested"], 1)

    def test_missing_trail_is_quiet(self):
        counts = agent_harvest.harvest(self.tmp / "nope.jsonl",
                                       store=self.store)
        self.assertEqual(counts["scanned"], 0)
        self.assertNotIn("error", counts)


class IsolationTests(unittest.TestCase):
    """Agent pairs must never reach the TEXT champion's training or prompts."""

    def setUp(self):
        self.store = mk_store()
        self.tmp = Path(tempfile.mkdtemp())
        self.trail = self.tmp / "agent_distill.jsonl"
        with self.trail.open("w", encoding="utf-8") as f:
            for r in [_step(), _run()]:
                f.write(json.dumps(r) + "\n")
        agent_harvest.harvest(self.trail, store=self.store)
        self.pair = [p for p in self.store.list_learning_pairs(limit=10)
                     if p["task_type"] == "agent.act"][0]

    def test_text_lora_curation_excludes_agent_pairs(self):
        self.assertIsNone(pair_to_distill_row(self.pair))
        # Even a future human confirmation must not change that.
        confirmed = dict(self.pair, human_confirmed=True)
        self.assertIsNone(pair_to_distill_row(confirmed))

    def test_exemplar_router_types_never_map_to_agent(self):
        from app.services.exemplar_store import ROUTER_TASK_TYPES
        for task, types in ROUTER_TASK_TYPES.items():
            self.assertFalse(
                any(t.startswith("agent.") for t in types),
                f"router task {task} maps to an agent type")

    def test_task_type_is_registered(self):
        self.assertIn("agent.act", learning_store.TASK_TYPES)


if __name__ == "__main__":
    unittest.main()
