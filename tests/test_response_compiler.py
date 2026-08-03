"""Response compiler — semantic AST for chat presentation."""
from __future__ import annotations

import threading
import unittest
from unittest import mock


class ResponseCompilerTests(unittest.TestCase):
    def test_short_reply_stays_uncompiled(self):
        from app.services.response_compiler import compile_response

        self.assertIsNone(compile_response("Got it."))
        self.assertIsNone(compile_response("Yes."))

    def test_takeaway_and_paragraph_split(self):
        from app.services.response_compiler import compile_response

        text = (
            "To find velocity, integrate acceleration with respect to time.\n\n"
            "Acceleration is the rate of change of velocity. "
            "Therefore velocity is the integral of acceleration. "
            "However you still need the constant of integration from initial conditions."
        )
        doc = compile_response(text)
        self.assertIsNotNone(doc)
        types = [s["type"] for s in doc["sections"]]
        self.assertIn("takeaway", types)
        self.assertEqual(doc["sections"][0]["type"], "takeaway")
        # Transition words should create extra explanation blocks
        explanations = [s for s in doc["sections"] if s["type"] == "explanation"]
        self.assertGreaterEqual(len(explanations), 1)

    def test_title_then_takeaway_hierarchy(self):
        from app.services.response_compiler import compile_response

        text = (
            "Acceleration → Velocity\n\n"
            "To find velocity, integrate acceleration with respect to time.\n\n"
            "Acceleration is the rate of change of velocity.\n\n"
            r"\[ v(t) = \int a(t)\, dt + C \]"
        )
        doc = compile_response(text)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["sections"][0]["type"], "title")
        self.assertEqual(doc["sections"][0]["text"], "Acceleration → Velocity")
        self.assertEqual(doc["sections"][1]["type"], "takeaway")
        self.assertIn("integrate", doc["sections"][1]["text"].lower())

    def test_formula_display_and_no_raw_delimiters_in_tex(self):
        from app.services.response_compiler import compile_response

        text = (
            "Acceleration relates to velocity.\n\n"
            r"\[ v(t) = \int a(t)\, dt + C \]"
            "\n\n"
            "The constant of integration captures initial velocity."
        )
        doc = compile_response(text)
        self.assertIsNotNone(doc)
        formulas = [s for s in doc["sections"] if s["type"] == "formula"]
        self.assertEqual(len(formulas), 1)
        self.assertIn("v(t)", formulas[0]["tex"])
        self.assertNotIn(r"\[", formulas[0]["tex"])
        self.assertTrue(doc["educational"])
        self.assertTrue(doc["actions"])

    def test_semantic_fences(self):
        from app.services.response_compiler import compile_response

        text = (
            ":::takeaway\n"
            "Integrate acceleration to get velocity.\n"
            ":::\n\n"
            ":::formula\n"
            r"v = \int a\,dt"
            "\n:::\n\n"
            ":::example\n"
            "If a is constant, v = a t + v0.\n"
            ":::"
        )
        doc = compile_response(text)
        self.assertIsNotNone(doc)
        types = [s["type"] for s in doc["sections"]]
        self.assertEqual(types[0], "takeaway")
        self.assertIn("formula", types)
        self.assertIn("example", types)

    def test_callout_labels(self):
        from app.services.response_compiler import compile_response

        text = (
            "Key idea: Acceleration is the derivative of velocity.\n\n"
            "Definition: Velocity is the rate of change of position.\n\n"
            "Warning: Do not drop the constant of integration.\n\n"
            "Example: a = 2 m/s^2 gives v = 2t + C."
        )
        doc = compile_response(text)
        self.assertIsNotNone(doc)
        types = {s["type"] for s in doc["sections"]}
        self.assertTrue({"key_idea", "definition", "warning", "example"} <= types)

    def test_grounding_collapsed_summary(self):
        from app.services.response_compiler import compile_response

        text = (
            "Your contacts include Justin and Marc.\n\n"
            "Justin is tagged for a pricing follow-up based on recent audio. "
            "Marc appears in working set notes from yesterday's meeting."
        )
        sources = [
            {"label": "PERSON GRAPH", "n": 7, "items": ["Justin", "Marc"]},
            {"label": "TIMELINE", "n": 5, "items": ["follow-up"]},
        ]
        doc = compile_response(text, sources=sources)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["grounding"]["total"], 12)
        self.assertEqual(len(doc["grounding"]["groups"]), 2)

    def test_approval_ask_not_compiled(self):
        from app.services.response_compiler import compile_response

        text = "APPROVAL NEEDED — email Justin\n\nAction: send email"
        self.assertIsNone(compile_response(text, kind="ask"))


class EmitCompiledTests(unittest.TestCase):
    def test_emit_attaches_compiled_on_result(self):
        from app.services.agent_bridge import AgentWorker

        w = AgentWorker.__new__(AgentWorker)
        w.lock = threading.Lock()
        w.events = []
        w.next_id = 1
        text = (
            "To find velocity, integrate acceleration with respect to time.\n\n"
            "Acceleration measures how quickly velocity changes. "
            "Therefore integrating recovers velocity up to a constant."
        )
        with mock.patch("app.services.voice.maybe_speak_reply"):
            AgentWorker._emit(w, "result", text)
        self.assertEqual(len(w.events), 1)
        self.assertIn("compiled", w.events[0])
        self.assertEqual(w.events[0]["compiled"]["version"], 1)
        self.assertTrue(w.events[0]["compiled"]["sections"])

    def test_emit_skips_compiled_for_short(self):
        from app.services.agent_bridge import AgentWorker

        w = AgentWorker.__new__(AgentWorker)
        w.lock = threading.Lock()
        w.events = []
        w.next_id = 1
        with mock.patch("app.services.voice.maybe_speak_reply"):
            AgentWorker._emit(w, "result", "Done.")
        self.assertNotIn("compiled", w.events[0])


if __name__ == "__main__":
    unittest.main()
