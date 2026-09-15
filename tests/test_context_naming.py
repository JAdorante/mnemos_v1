"""CAL — the model reader over unbound episodes, dry (injected `ask`)."""
from __future__ import annotations

import datetime as dt
import tempfile
import time
import unittest
from pathlib import Path

from app.events import Event, Modality
from app.services.context import evaluate as ev
from app.services.context import naming
from app.services.context import replay as rp
from app.storage import Store

PITCH = ("Ravenry is a marketplace for research. The Ravenry team ships weekly. "
         "We opened Firefox to read the Ravenry roadmap. Firefox again. "
         "The company. Our company. Which company?")


class NamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_name_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "a")
        now = time.time()
        self.rav = self.store.resolve_entity("Ravenry", "project", ts=now)
        self.store.resolve_entity("Firefox", "tool", ts=now)
        self.store.resolve_entity("Zed", "project", ts=now)      # mentioned once
        self.store.resolve_entity("company", "idea", ts=now)     # an idea
        # Seen twice: a real node, unlike a name mined from one document.
        self.store.resolve_entity("Ravenry", "project", ts=now + 86400)
        self.day = "2026-08-26"
        self.t0 = dt.datetime.strptime(self.day, "%Y-%m-%d").timestamp() + 3600
        # A mail stretch: the title is useless, the body says what it is about.
        for i in range(12):
            self.store.insert(Event(
                time=self.t0 + i * 30, modality=Modality.INPUT,
                raw=PITCH + " Zed." if i == 0 else "click left at (1,1) on Mail",
                summary="click", source="desktop.click",
                meta={"window": "Mail - Someone Else - Outlook — Mozilla Firefox"}))
        self.calls = []

    def _replay(self):
        return rp.replay(self.store, t0=self.t0 - 1, t1=self.t0 + 10_000)

    def _ask(self, reply):
        def ask(system, messages, tier):
            self.calls.append((system, messages, tier))
            return reply
        return ask

    def test_unbound_episode_is_the_input(self) -> None:
        eps = self._replay()["episodes"]
        self.assertEqual(len(eps), 1)
        self.assertIsNone(eps[0]["node_type"], "titles gave the cheap path nothing")

    def test_candidates_are_mentioned_nameable_entities_only(self) -> None:
        text, windows = naming.episode_text(
            self.store, self._replay()["episodes"][0]["_event_ids"])
        cands = naming.candidates_for(self.store, text)
        self.assertEqual([c.name for c, _n in cands], ["Ravenry"])
        self.assertEqual(cands[0][1], 3)
        self.assertIn("Mail - Someone Else - Outlook", next(iter(windows)))
        # Firefox is mentioned twice but is a tool; Zed once, below the bar;
        # "company" three times but an idea.

    def test_single_sighting_entities_are_not_offered(self) -> None:
        self.store.resolve_entity("Zed", "project", ts=time.time())
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE entities SET first_seen=last_seen WHERE canonical_name='Zed'")
            self.store._conn.execute("UPDATE events SET raw=? WHERE raw LIKE 'Ravenry%'",
                                     (PITCH + " Zed and Zed again.",))
            self.store._conn.commit()
        text, _w = naming.episode_text(
            self.store, self._replay()["episodes"][0]["_event_ids"])
        self.assertEqual([c.name for c, _n in naming.candidates_for(self.store, text)],
                         ["Ravenry"], "Zed is mentioned enough but was seen once")

    def test_position_bias_is_not_a_name(self) -> None:
        C = lambda n: naming.Candidate("entity", n, n)
        # A model that only ever picks index 0 agrees with itself on nothing.
        first_only = lambda sy, msgs, tier: {"choice": 0, "confidence": 1.0}
        got = naming.choose_debiased("m", [C("A"), C("B")], ask=first_only)
        self.assertIsNone(got.chosen)
        self.assertIn("order_disagreement", got.error)
        # A model that finds B wherever it sits names B, at the lower confidence.
        def finds_b(sy, msgs, tier):
            text = msgs[0]["content"]
            idx = 0 if "0. B" in text else 1
            return {"choice": idx, "confidence": 0.9 if idx == 0 else 0.7}
        got = naming.choose_debiased("m", [C("A"), C("B")], ask=finds_b)
        self.assertEqual(got.chosen.name, "B")
        self.assertEqual(got.confidence, 0.7)
        # Null both ways is null, and one option is asked once.
        calls = []
        null = lambda sy, msgs, tier: (calls.append(1), {"choice": None})[1]
        self.assertIsNone(naming.choose_debiased("m", [C("A"), C("B")], ask=null).chosen)
        self.assertEqual(len(calls), 2)
        calls.clear()
        naming.choose_debiased("m", [C("A")], ask=null)
        self.assertEqual(len(calls), 1)

    def test_moment_is_windows_plus_evidence_lines(self) -> None:
        text, windows = naming.episode_text(
            self.store, self._replay()["episodes"][0]["_event_ids"])
        cands = naming.candidates_for(self.store, text)
        m = naming.moment_for(text, windows, cands)
        self.assertTrue(m.startswith("Windows: Mail - Someone Else"))
        self.assertIn("marketplace for research", m)
        self.assertLessEqual(len(m), naming.MOMENT_CHARS)

    def test_model_choice_names_the_episode_in_memory(self) -> None:
        before = self.store.binding_stats()
        eps = self._replay()["episodes"]
        recs = naming.name_unbound(self.store, eps,
                                   ask=self._ask({"choice": 0, "confidence": 0.9}))
        self.assertEqual(eps[0]["title"], "Ravenry")
        self.assertEqual(eps[0]["node_id"], str(self.rav))
        self.assertEqual(eps[0]["method"], "escalated")
        self.assertTrue(recs[0]["applied"])
        self.assertEqual(len(self.calls), 1, "one candidate: asked once")
        self.assertIn("0. Ravenry", self.calls[0][1][0]["content"])
        self.assertEqual(self.store.binding_stats(), before,
                         "nothing minted, nothing written")

    def test_null_and_low_confidence_stay_blank(self) -> None:
        for reply in ({"choice": None, "confidence": 0.9},
                      {"choice": 0, "confidence": 0.3},
                      {"choice": 7, "confidence": 0.9}):
            eps = self._replay()["episodes"]
            recs = naming.name_unbound(self.store, eps, ask=self._ask(reply))
            self.assertIsNone(eps[0]["node_type"], reply)
            self.assertFalse(recs[0]["applied"])

    def test_no_mentions_means_no_call(self) -> None:
        with self.store._lock:
            self.store._conn.execute("UPDATE events SET raw='click'")
            self.store._conn.commit()
        eps = self._replay()["episodes"]
        recs = naming.name_unbound(self.store, eps, ask=self._ask({"choice": 0}))
        self.assertEqual(self.calls, [])
        self.assertEqual(recs[0]["error"], "no_candidates")

    def test_score_with_escalation_grades_the_reader(self) -> None:
        rows = ev.sheet_for_day(self.store, self.day)
        for r in rows:
            r["label_project"] = "ravenry"
        rows[0]["label_boundary"] = "1"
        plain = ev.score_labels(self.store, rows)
        self.assertEqual(plain["attribution"]["recall"], 0.0)
        got = ev.score_labels(self.store, rows, escalate=True,
                              ask=self._ask({"choice": 0, "confidence": 0.8}))
        self.assertEqual(got["attribution"]["recall"], 1.0)
        self.assertEqual(got["model_calls"], 1)
        self.assertTrue(got["escalations"][0]["correct"])
        text = ev.report(got)
        self.assertIn("model reader: 1 calls", text)
        self.assertIn("RIGHT", text)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
