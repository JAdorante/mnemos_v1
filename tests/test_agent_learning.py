"""The agent's learning surfaces: scan deltas, ask_human lessons, post-mortems,
and the step-distillation trail.

Three levers make the agent smarter across runs without any site-specific code:
the executor is told exactly what its last action changed (scan_delta), human
answers and failed-run post-mortems persist in procedural memory keyed by
(intent, site), and every (observation -> action, verified) pair is appended to
sessions/agent_distill.jsonl as imitation data for the local rung.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from browser_agent import config as bcfg
from browser_agent import distill as agent_distill
from browser_agent.llm import LLM
from browser_agent.memory import Memory
from browser_agent.orchestrator import _distill, _lessons_text
from browser_agent.perception import scan_delta


def _scan(elements=None, **extra):
    s = {"url": "https://example.com/", "title": "t",
         "count": len(elements or []), "elements": elements or [],
         "viewport": {"w": 1280, "h": 800}, "dpr": 1,
         "scrollY": 0, "scrollMax": 0, "surfaces": []}
    s.update(extra)
    return s


class ScanDeltaTests(unittest.TestCase):
    def test_no_previous_scan_is_silent(self):
        self.assertEqual(scan_delta(None, _scan()), "")
        self.assertEqual(scan_delta(_scan(), None), "")

    def test_identical_scans_are_silent(self):
        a = _scan([{"role": "button", "name": "Go"}])
        self.assertEqual(scan_delta(a, a), "")

    def test_new_and_gone_elements_are_named(self):
        prev = _scan([{"role": "button", "name": "Go"},
                      {"role": "link", "name": "Old"}])
        cur = _scan([{"role": "button", "name": "Go"},
                     {"role": "clickable", "name": "Q♥"}])
        d = scan_delta(prev, cur)
        self.assertIn("NEW elements: clickable: Q♥", d)
        self.assertIn("GONE elements: link: Old", d)
        self.assertNotIn("Go", d.replace("GONE", ""))  # unchanged not listed

    def test_url_and_dialog_changes(self):
        d = scan_delta(_scan(), _scan(url="https://example.com/two",
                                      modal="Confirm?"))
        self.assertIn("URL changed", d)
        self.assertIn('A dialog opened: "Confirm?"', d)

    def test_value_edits_on_known_fields(self):
        prev = _scan([{"role": "textbox", "name": "To", "editable": True,
                       "value": ""}])
        cur = _scan([{"role": "textbox", "name": "To", "editable": True,
                      "value": "sarah@example.com"}])
        self.assertIn('"To" value is now', scan_delta(prev, cur))

    def test_pixel_only_change_is_reported(self):
        prev = _scan(pixel_hash="aaaa")
        cur = _scan(pixel_hash="bbbb")
        self.assertIn("rendered graphics changed", scan_delta(prev, cur))

    def test_selection_move_is_reported(self):
        prev = _scan([{"role": "clickable", "name": "7♠"},
                      {"role": "clickable", "name": "8♥"}])
        cur = _scan([{"role": "clickable", "name": "7♠", "selected": True},
                     {"role": "clickable", "name": "8♥"}])
        self.assertIn("Selection changed: now selected — 7♠",
                      scan_delta(prev, cur))

    def test_churn_is_capped(self):
        prev = _scan()
        cur = _scan([{"role": "link", "name": f"L{i}"} for i in range(30)])
        d = scan_delta(prev, cur, cap=8)
        self.assertIn("(+22 more)", d)


class AskHumanLessonTests(unittest.TestCase):
    def _hist(self):
        return [
            {"step": 1, "action": "navigate",
             "args": {"url": "https://boards.example/play"}, "verified": True,
             "result": "ok"},
            {"step": 2, "action": "ask_human",
             "args": {"question": "The cards aren't clickable — Auto Complete "
                                  "or drag actions?"},
             "verified": True,
             "result": "human: play properly, click a card then its destination"},
        ]

    def test_human_answer_becomes_a_note(self):
        _recipe, notes = _distill(self._hist())
        guidance = [n for n in notes if n.startswith("human guidance")]
        self.assertEqual(len(guidance), 1)
        self.assertIn("Auto Complete", guidance[0])
        self.assertIn("click a card then its destination", guidance[0])

    def test_unanswered_ask_is_not_a_note(self):
        hist = self._hist()
        hist[1]["result"] = ""   # run ended at needs_input; no answer came
        _recipe, notes = _distill(hist)
        self.assertFalse([n for n in notes if n.startswith("human guidance")])

    def test_guidance_roundtrips_through_procedural_memory(self):
        with tempfile.TemporaryDirectory() as td:
            mem = Memory(Path(td) / "episodic.db")
            try:
                _recipe, notes = _distill(self._hist())
                mem.learn_skill("web_task", "boards.example", "success", 5,
                                ["navigate → boards.example/play"], notes)
                skill = mem.recall_skill("web_task", "boards.example")
                self.assertTrue(any("human guidance" in n
                                    for n in skill["failure_notes"]))
                self.assertIn("human guidance", _lessons_text(skill))
            finally:
                mem.conn.close()   # Windows: unlock the db file for cleanup


class PostmortemTests(unittest.TestCase):
    def _llm(self, json_out):
        llm = LLM.__new__(LLM)
        llm._json_call = mock.Mock(return_value=json_out)
        return llm

    def test_lessons_are_trimmed_and_capped(self):
        llm = self._llm({"lessons": ["  Use the search box, not the menu.  ",
                                     "Wait for the grid to render.",
                                     "Third", "Fourth (over the cap)"]})
        out = llm.postmortem("goal", "stopped_no_progress", "- step 1: ...")
        self.assertEqual(out[0], "Use the search box, not the menu.")
        self.assertEqual(len(out), 3)

    def test_any_failure_returns_empty(self):
        llm = LLM.__new__(LLM)
        llm._json_call = mock.Mock(side_effect=RuntimeError("api down"))
        self.assertEqual(llm.postmortem("g", "stopped", "h"), [])
        llm2 = self._llm({})
        self.assertEqual(llm2.postmortem("g", "stopped", "h"), [])


class DoneCheckTests(unittest.TestCase):
    """Drift guard: a done about a DIFFERENT task than the goal is rejected
    once; honest failure reports and any check trouble fail open."""

    def _llm(self, json_out=None, error=None):
        llm = LLM.__new__(LLM)
        if error:
            llm._json_call = mock.Mock(side_effect=error)
        else:
            llm._json_call = mock.Mock(return_value=json_out)
        return llm

    def test_drift_is_flagged(self):
        llm = self._llm({"satisfied": False,
                         "reason": "goal was solitaire; result is an X feed"})
        out = llm.check_done("play solitaire on hard mode",
                             "Here's what's happening on your X feed…")
        self.assertFalse(out["satisfied"])
        self.assertIn("solitaire", out["reason"])

    def test_on_goal_result_passes(self):
        llm = self._llm({"satisfied": True, "reason": "played to completion"})
        self.assertTrue(llm.check_done("play solitaire", "Won the game")
                        ["satisfied"])

    def test_check_fails_open(self):
        llm = self._llm(error=RuntimeError("api down"))
        self.assertTrue(llm.check_done("g", "r")["satisfied"])
        llm2 = self._llm({})
        self.assertTrue(llm2.check_done("g", "r")["satisfied"])


class DistillTrailTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            bcfg, "SESSIONS_ROOT", Path(self._td.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._td.cleanup()

    def _rows(self):
        p = Path(self._td.name) / "agent_distill.jsonl"
        if not p.is_file():
            return []
        return [json.loads(ln) for ln in
                p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def test_step_and_run_rows_are_appended(self):
        agent_distill.log_step(
            session_id="s1", step=3, url="https://example.com/",
            observation="GOAL:\nplay solitaire\nCURRENT PAGE:\n...",
            action="click", args={"element_id": 4}, model="claude-sonnet-5",
            escalated=False, pixel=True, vision=True, verified=True,
            vnote="the view changed", step_status="verified",
            intent="web_task", site="example.com")
        agent_distill.log_run(
            session_id="s1", status="success", steps=7, replans=0,
            intent="web_task", site="example.com", escalations=1)
        rows = self._rows()
        self.assertEqual([r["task"] for r in rows],
                         ["browser.act", "browser.run"])
        step = rows[0]
        self.assertEqual(step["action"], {"name": "click",
                                          "args": {"element_id": 4}})
        self.assertTrue(step["verified"])
        self.assertIn("play solitaire", step["observation"] or "")
        self.assertEqual(rows[1]["escalations"], 1)

    def test_secret_args_never_persist(self):
        agent_distill.log_step(
            session_id="s1", step=1, url="u", observation="obs",
            action="type", args={"element_id": 2, "password": "hunter2"},
            model="m", escalated=False, pixel=False, vision=False,
            verified=True, vnote="", step_status="", intent="i", site="s")
        row = self._rows()[0]
        self.assertEqual(row["action"]["args"]["password"], "***")
        self.assertNotIn("hunter2", json.dumps(row))

    def test_observation_is_capped(self):
        with mock.patch.object(bcfg, "DISTILL_OBS_CAP", 50):
            agent_distill.log_step(
                session_id="s1", step=1, url="u", observation="x" * 500,
                action="scroll", args={}, model="m", escalated=False,
                pixel=False, vision=False, verified=True, vnote="",
                step_status="", intent="i", site="s")
        obs = self._rows()[0]["observation"]
        self.assertLessEqual(len(obs or ""), 50)

    def test_disabled_by_config(self):
        with mock.patch.object(bcfg, "DISTILL", False):
            agent_distill.log_step(
                session_id="s1", step=1, url="u", observation="obs",
                action="click", args={}, model="m", escalated=False,
                pixel=False, vision=False, verified=True, vnote="",
                step_status="", intent="i", site="s")
            agent_distill.log_run(
                session_id="s1", status="success", steps=1, replans=0,
                intent="i", site="s", escalations=0)
        self.assertEqual(self._rows(), [])


if __name__ == "__main__":
    unittest.main()
