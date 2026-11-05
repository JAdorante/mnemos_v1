"""Phase 5 — deferred chat ingest, queries, OTR, answer-check rewrite flag."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import chat_log
from app.services.agent_bridge import AgentWorker


class DeferredIngestTests(unittest.TestCase):
    def test_emit_run_result_ingests_after_answer(self) -> None:
        w = AgentWorker(persist=False)
        seen = {}

        def _capture(message, *, answer=None, off_the_record=False):
            seen.update(message=message, answer=answer,
                        off_the_record=off_the_record)
            return 1

        cmd = {
            "type": "goal",
            "text": "Hugh and I decided to demo Sparrow Friday",
            "display": "Hugh and I decided to demo Sparrow Friday",
            "off_the_record": False,
        }

        class _Agent:
            last_distill_id = None
            last_route = "memory"

        with patch("app.services.chat_ingest.ingest", side_effect=_capture):
            w._emit_run_result(cmd, _Agent(), "Noted — Friday works.", "success")
        self.assertEqual(seen.get("message"),
                         "Hugh and I decided to demo Sparrow Friday")
        self.assertEqual(seen.get("answer"), "Noted — Friday works.")
        self.assertFalse(seen.get("off_the_record"))
        self.assertTrue(cmd.get("_chat_ingested"))

    def test_off_the_record_reaches_ingest(self) -> None:
        w = AgentWorker(persist=False)
        seen = {}

        def _capture(message, *, answer=None, off_the_record=False):
            seen["off_the_record"] = off_the_record
            return None

        cmd = {
            "display": "secret plan for the board next week",
            "text": "secret plan for the board next week",
            "off_the_record": True,
        }

        class _Agent:
            last_distill_id = None

        with patch("app.services.chat_ingest.ingest", side_effect=_capture):
            w._maybe_chat_ingest(cmd, "I won't remember that.", "success")
        self.assertTrue(seen.get("off_the_record"))

    def test_cancelled_status_skips_ingest(self) -> None:
        w = AgentWorker(persist=False)
        with patch("app.services.chat_ingest.ingest") as ingest:
            w._maybe_chat_ingest(
                {"display": "Hugh and I decided to demo Sparrow Friday"},
                "", "cancelled")
            ingest.assert_not_called()


class EmitRewriteFlagTests(unittest.TestCase):
    def test_context_alone_does_not_rewrite(self) -> None:
        w = AgentWorker(persist=False)
        live = "Sarah said the page lists Model 9 near parity."
        memory = "David said the subscription costs $49 per month."
        w._emit("result", live, context=memory, question="what's on that page",
                rewrite=False)
        self.assertEqual(w.events[0]["text"], live)
        self.assertNotIn("answer_check", (w.events[0].get("compiled") or {}))

    def test_rewrite_true_applies_check_and_badge_meta(self) -> None:
        w = AgentWorker(persist=False)
        answer = "Sarah said it costs $55 per month."
        memory = "David said the subscription costs $49 per month."
        w._emit("result", answer, context=memory,
                question="what did David say?", rewrite=True)
        ev = w.events[0]
        self.assertTrue(
            ev["text"].startswith("Here's what I found, with the evidence:")
        )
        ac = (ev.get("compiled") or {}).get("answer_check") or {}
        self.assertEqual(ac.get("status"), "downgraded")
        self.assertTrue(ac.get("corrected"))
        self.assertEqual(ac.get("original"), answer)


class AgentOffDeferredIngestTests(unittest.TestCase):
    def test_agent_off_ingests_after_result(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.services.agent_bridge import worker

        tmp = Path(tempfile.mkdtemp(prefix="quill_chat_phase5_"))
        chat_log.reset_log(tmp / "chat_log.db")
        with worker.lock:
            saved_events = list(worker.events)
            saved_next = worker.next_id
        seen = {}

        def _capture(message, *, answer=None, off_the_record=False):
            seen.update(message=message, answer=answer,
                        off_the_record=off_the_record)
            return 9

        try:
            with patch.dict(os.environ, {"QUILL_AGENT": "0"}):
                with patch("app.api.routes.llm.answer",
                           return_value={"answer": "Noted.",
                                         "sources": []}):
                    with patch("app.services.chat_ingest.ingest",
                               side_effect=_capture):
                        with TestClient(app) as client:
                            r = client.post(
                                "/chat",
                                json={
                                    "message": "Hugh and I decided to demo Sparrow Friday",
                                    "off_the_record": False,
                                },
                                headers={"Origin": "http://testserver"},
                            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(
                seen.get("message"),
                "Hugh and I decided to demo Sparrow Friday")
            self.assertEqual(seen.get("answer"), "Noted.")
            self.assertFalse(seen.get("off_the_record"))
        finally:
            with worker.lock:
                worker.events[:] = saved_events
                worker.next_id = saved_next

    def test_agent_off_otr_skips_remembering(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.services.agent_bridge import worker

        tmp = Path(tempfile.mkdtemp(prefix="quill_chat_phase5_otr_"))
        chat_log.reset_log(tmp / "chat_log.db")
        with worker.lock:
            saved_events = list(worker.events)
            saved_next = worker.next_id
        seen = {}

        def _capture(message, *, answer=None, off_the_record=False):
            seen["off_the_record"] = off_the_record
            return None

        try:
            with patch.dict(os.environ, {"QUILL_AGENT": "0"}):
                with patch("app.api.routes.llm.answer",
                           return_value={"answer": "Got it.",
                                         "sources": []}):
                    with patch("app.services.chat_ingest.ingest",
                               side_effect=_capture):
                        with TestClient(app) as client:
                            r = client.post(
                                "/chat",
                                json={
                                    "message": "secret plan for the board next week",
                                    "off_the_record": True,
                                },
                                headers={"Origin": "http://testserver"},
                            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(seen.get("off_the_record"))
        finally:
            with worker.lock:
                worker.events[:] = saved_events
                worker.next_id = saved_next


if __name__ == "__main__":
    unittest.main()
