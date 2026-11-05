"""Phase 4: intercepts become chips; they do not consume the turn."""
from __future__ import annotations

import queue
import unittest
from unittest import mock

from app.services import chat_suggestions as cs
from app.services.agent_bridge import AgentWorker


class CollectTests(unittest.TestCase):
    def test_task_line_becomes_a_chip(self) -> None:
        chips = cs.collect("Have a call with Marc")
        kinds = [c["kind"] for c in chips]
        self.assertIn("task_create", kinds)
        task = next(c for c in chips if c["kind"] == "task_create")
        self.assertEqual(task["action"], "task.create")
        self.assertEqual(task["label"], "Make this a task")

    def test_slot_line_becomes_a_chip(self) -> None:
        chips = cs.collect("get me the Boston quote")
        self.assertTrue(any(c["kind"] == "slot_create" for c in chips))

    def test_calendar_gate_without_llm(self) -> None:
        with mock.patch("app.services.calendar_intent.parse") as parse:
            chips = cs.collect("put coffee with Sam on my calendar tomorrow at 3")
            parse.assert_not_called()
        self.assertTrue(any(c["kind"] == "calendar_add" for c in chips))

    def test_plain_question_has_no_chips(self) -> None:
        self.assertEqual(cs.collect("what did Marc say yesterday?"), [])


class ApplyTests(unittest.TestCase):
    def test_calendar_parse_runs_off_thread(self) -> None:
        w = AgentWorker()
        w.propose_calendar = mock.Mock(return_value=True)
        with mock.patch("app.services.calendar_intent.parse",
                        return_value={"summary": "Coffee", "start": "2026-11-06T15:00:00",
                                      "all_day": False, "when_text": "Fri"}) as parse, \
             mock.patch("app.services.icloud_account.status",
                        return_value={"connected": True}), \
             mock.patch("threading.Thread") as thr:
            started = {}

            def _start():
                started["ran"] = True
                # Run the target synchronously so the test can assert.
                args, kwargs = thr.call_args
                target = kwargs.get("target") or (args[0] if args else None)
                target()

            thr.return_value.start.side_effect = _start
            out = cs.apply("calendar.propose",
                           {"text": "coffee tomorrow at 3"}, worker=w)
        self.assertTrue(out["ok"])
        self.assertEqual(out["routed"], "calendar_propose_async")
        self.assertTrue(started.get("ran"))
        parse.assert_called_once()
        w.propose_calendar.assert_called_once()

    def test_task_create_mints(self) -> None:
        w = AgentWorker()
        with mock.patch("app.services.task_completion.create_user_task",
                        return_value=42) as create, \
             mock.patch("app.services.memory.memory._ensure_store",
                        return_value=mock.Mock()):
            out = cs.apply(
                "task.create",
                {"text": "Have a call with Marc", "counterparty": "Marc",
                 "kind": "call"},
                worker=w)
        create.assert_called_once()
        self.assertTrue(out["ok"])
        self.assertEqual(out["task_id"], 42)
        self.assertTrue(any(e.get("kind") == "result" for e in w.events))


class SendCarriesSuggestionsTests(unittest.TestCase):
    def test_send_keeps_suggestions_on_the_chat_lane(self) -> None:
        w = AgentWorker()
        w.start = mock.Mock(side_effect=lambda: setattr(w, "_started", True))
        chips = cs.collect("Have a call with Marc")
        with mock.patch("app.services.agent_bridge._should_plan", return_value=False):
            w.send("Have a call with Marc", suggestions=chips)
        cmd = w.chat_q.get_nowait()
        self.assertEqual(cmd["lane"], "chat")
        self.assertTrue(cmd["suggestions"])
        with self.assertRaises(queue.Empty):
            w.cmd_q.get_nowait()

    def test_result_stream_carries_suggestion_actions(self) -> None:
        w = AgentWorker()
        chips = cs.collect("Have a call with Marc")
        cmd = {"suggestions": chips, "display": "Have a call with Marc",
               "text": "Have a call with Marc"}

        class _Agent:
            last_distill_id = None
            grounding_sink = {}

        w._emit_run_result(cmd, _Agent(), "Sure — want me to track that?",
                           "answered_no_browser")
        results = [e for e in w.events if e.get("kind") == "result"]
        self.assertTrue(results)
        stream = results[-1].get("stream") or {}
        self.assertEqual(stream.get("type"), "chat.suggestions")
        self.assertTrue(any(a.get("action") == "task.create"
                            for a in stream.get("actions") or []))


class ChatPageSuggestionUiTests(unittest.TestCase):
    def test_page_wires_apply(self) -> None:
        from app.api.chat_page import CHAT_PAGE
        self.assertIn("applySuggestion", CHAT_PAGE)
        self.assertIn("/chat/suggestions/apply", CHAT_PAGE)
        self.assertIn("a.action", CHAT_PAGE)


if __name__ == "__main__":
    unittest.main()
