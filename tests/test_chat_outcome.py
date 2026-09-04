"""Chat UI verdict endpoint: POST /chat/outcome → set_user_outcome(row_id=...).

Escalated chat answers carry distill_id on the poll event; one tap labels the
row the same way scripts/distill_label.py does.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from fastapi import HTTPException

from app.api.routes import ChatOutcomeIn, chat_outcome
from app.services.escalate_log import escalate_log


class ChatOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_chat_outcome_"))
        self.trail = self.tmp / "escalate_distill.jsonl"
        self._orig = (escalate_log._path, escalate_log._counts, escalate_log._total)
        escalate_log._path = self.trail
        escalate_log._counts = Counter()
        escalate_log._total = 0
        row = escalate_log.record(
            task="chat", reason="low_confidence",
            local={"text": "meh", "confidence": 0.1},
            parent={"text": "parent answer"},
            source="model_router", modality="text",
        )
        assert row is not None
        self.row_id = row["id"]

    def tearDown(self) -> None:
        escalate_log._path, escalate_log._counts, escalate_log._total = self._orig

    def _row(self) -> dict:
        return json.loads(self.trail.read_text(encoding="utf-8").strip())

    def test_accepted(self) -> None:
        out = chat_outcome(ChatOutcomeIn(distill_id=self.row_id, outcome="accepted"))
        self.assertEqual(out["outcome"], "accepted")
        self.assertEqual(self._row()["user_outcome"], "accepted")

    def test_rejected(self) -> None:
        chat_outcome(ChatOutcomeIn(distill_id=self.row_id, outcome="rejected"))
        self.assertEqual(self._row()["user_outcome"], "rejected")

    def test_edited_stores_text(self) -> None:
        chat_outcome(ChatOutcomeIn(
            distill_id=self.row_id, outcome="edited",
            edited_text="the corrected answer"))
        row = self._row()
        self.assertEqual(row["user_outcome"], "edited")
        self.assertEqual(row["edited"], "the corrected answer")

    def test_edited_requires_text(self) -> None:
        with self.assertRaises(HTTPException) as cm:
            chat_outcome(ChatOutcomeIn(distill_id=self.row_id, outcome="edited"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_bad_outcome(self) -> None:
        with self.assertRaises(HTTPException) as cm:
            chat_outcome(ChatOutcomeIn(distill_id=self.row_id, outcome="maybe"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_unknown_id_404(self) -> None:
        with self.assertRaises(HTTPException) as cm:
            chat_outcome(ChatOutcomeIn(distill_id="deadbeef" * 4, outcome="accepted"))
        self.assertEqual(cm.exception.status_code, 404)

    def test_emit_carries_distill_id(self) -> None:
        from app.services.agent_bridge import AgentWorker
        w = AgentWorker()
        w._emit("result", "hello", distill_id=self.row_id)
        w._emit("result", "local only")
        self.assertEqual(w.events[0]["distill_id"], self.row_id)
        self.assertNotIn("distill_id", w.events[1])

    def test_emit_carries_sources_and_pop_clears(self) -> None:
        # "Show sources": the provider's sink feeds the result event once —
        # a later result must not display a previous goal's sources.
        from app.services.agent_bridge import AgentWorker
        w = AgentWorker()
        w.grounding_sink = {"sources": [{"label": "timeline memories", "n": 2}]}
        self.assertEqual(w._pop_sources(),
                         [{"label": "timeline memories", "n": 2}])
        self.assertIsNone(w._pop_sources())            # popped, not reusable
        w._emit("result", "hi",
                sources=[{"label": "open tasks & commitments", "n": 1}])
        w._emit("result", "bare")
        self.assertEqual(w.events[0]["sources"][0]["label"],
                         "open tasks & commitments")
        self.assertNotIn("sources", w.events[1])

    def test_live_browser_result_not_downgraded_against_memory(self) -> None:
        # Live hands answers cite people/places from the page the agent just
        # read. Those tokens are not expected to appear in Sparrow memory.
        # Passing the retrieval block into answer_check would rewrite any
        # such summary into Confirmed/Missing + unrelated memory evidence.
        # Emit path must omit context so the done-result text stays intact.
        from app.services.agent_bridge import AgentWorker
        from app.services.answer_check import check_answer

        live = (
            "Recent posts on the page:\n"
            "@alpha_news: Vendor shipped Model 9, near parity on benchmarks.\n"
            "@market_wire: Regulator preparing continuous trading hours."
        )
        memory = (
            "You are Sparrow, the user's personal AI memory assistant.\n"
            "The user works on portfolio company tooling.\n"
            "Open commitment: follow up with the vendor about the invoice."
        )
        destroyed = check_answer(live, memory, question="what's on that page")
        self.assertEqual(destroyed.status, "downgraded")
        self.assertTrue(
            destroyed.text.startswith("Here's what I found, with the evidence:")
        )

        w = AgentWorker()
        w._emit("result", live,
                sources=[{"label": "timeline memories", "n": 3}],
                context=None, question="what's on that page")
        ev = w.events[0]
        self.assertEqual(ev["text"], live)
        self.assertNotIn("Here's what I found, with the evidence:", ev["text"])
        sections = ((ev.get("compiled") or {}).get("sections") or [])
        self.assertFalse(any(
            s.get("type") in ("confirmed", "missing", "likely", "conflicting")
            for s in sections))

    def test_pop_sources_reads_per_agent_sink(self) -> None:
        # Browser + fast lane each own a sink; overwriting worker.grounding_sink
        # must not steal the agent's sources (the live "Sources:" UI bug).
        from types import SimpleNamespace

        from app.services.agent_bridge import AgentWorker
        w = AgentWorker()
        w.grounding_sink = {"sources": [{"label": "stale worker", "n": 1}]}
        agent = SimpleNamespace(
            grounding_sink={"sources": [{"label": "open tasks & commitments",
                                         "n": 8}]})
        got = w._pop_sources(agent)
        self.assertEqual(got[0]["label"], "open tasks & commitments")
        self.assertIsNone(w._pop_sources(agent))
        # worker sink untouched
        self.assertEqual(w.grounding_sink["sources"][0]["label"], "stale worker")

    def test_memory_provider_fills_sink(self) -> None:
        from unittest import mock

        from app.services import agent_bridge as ab
        sink: dict = {}
        provider = ab._make_memory_provider(sink=sink)
        with mock.patch(
                "app.services.grounding.compose",
                return_value={"block": "- x", "hits": [],
                              "sources": [{"label": "timeline memories",
                                           "n": 1}]}):
            out = provider("what did I say about stocks?")
        self.assertIn("- x", out)
        self.assertEqual(sink["sources"][0]["label"], "timeline memories")


if __name__ == "__main__":
    unittest.main()


def setUpModule() -> None:
    # Telemetry sandbox: model_log resolves its trail path once at import, so
    # without this every faked model call in this module appends a bogus row
    # (fake models, 0s latency) to the REAL data/model_calls.jsonl trail.
    global _model_log_orig_path
    import tempfile as _tempfile
    from pathlib import Path as _Path
    from app.services.model_log import model_log as _ml
    _model_log_orig_path = _ml._path
    _ml._path = (_Path(_tempfile.mkdtemp(prefix="mnemos-test-telemetry-"))
                 / "model_calls.jsonl")


def tearDownModule() -> None:
    from app.services.model_log import model_log as _ml
    _ml._path = _model_log_orig_path
