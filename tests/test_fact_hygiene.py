"""Memory hygiene v1 — facts lifecycle (state/supersede/touch), write-time
gates (confidence floor, span faithfulness, dedup, update adjudication), and
lifecycle-aware retrieval helpers.

The gate's LLM adjudicator and vector probe are patched throughout: these tests
prove the DECISION logic and the storage lifecycle, not the models.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import fact_gate
from app.services.fact_gate import Verdict, gate_fact
from app.services.memory import fact_is_retrievable, recency_adjusted
from app.storage import Store


def _cfg(**over):
    base = dict(min_conf=0.35, span_gate=True, dedup=True,
                auto_dup_sim=0.97, adjudicate_sim=0.72,
                recency_weight=0.08, recency_half_life_days=14.0)
    base.update(over)
    return SimpleNamespace(facts=SimpleNamespace(**base))


class StoreLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def _task(self, text: str, ts: float = 100.0, conf: float = 0.8) -> int:
        return self.store.add_task(text, extracted_at=ts, confidence=conf)

    def test_migration_adds_lifecycle_columns(self):
        cols = {r["name"] for r in self.store._conn.execute(
            "PRAGMA table_info(facts)").fetchall()}
        self.assertLessEqual({"state", "superseded_by", "updated_at"}, cols)

    def test_new_fact_is_active_with_updated_at(self):
        fid = self._task("send deck to the investor", ts=123.0)
        f = self.store.get_fact(fid)
        self.assertEqual(f["state"], "active")
        self.assertEqual(f["updated_at"], 123.0)
        self.assertIsNone(f["superseded_by"])

    def test_touch_fact_refreshes_recency_and_keeps_best_confidence(self):
        fid = self._task("book flights", ts=100.0, conf=0.5)
        self.assertTrue(self.store.touch_fact(fid, 200.0, 0.9))
        f = self.store.get_fact(fid)
        self.assertEqual(f["updated_at"], 200.0)
        self.assertEqual(f["confidence"], 0.9)
        # a weaker re-assertion still bumps recency but never lowers confidence
        self.assertTrue(self.store.touch_fact(fid, 300.0, 0.4))
        f = self.store.get_fact(fid)
        self.assertEqual(f["updated_at"], 300.0)
        self.assertEqual(f["confidence"], 0.9)

    def test_supersede_marks_old_and_cancels_typed_row(self):
        old = self._task("meeting with the team at 2pm")
        new = self._task("meeting with the team moved to 3pm", ts=200.0)
        self.assertTrue(self.store.supersede_fact(old, new, 250.0))
        f = self.store.get_fact(old)
        self.assertEqual(f["state"], "superseded")
        self.assertEqual(f["superseded_by"], new)
        self.assertEqual(f["status"], "cancelled")      # typed task row
        self.assertEqual(self.store.get_fact(new)["state"], "active")
        # not re-supersedable, and never self-referential
        self.assertFalse(self.store.supersede_fact(old, new, 260.0))
        self.assertFalse(self.store.supersede_fact(new, new, 260.0))

    def test_facts_since_excludes_superseded_by_default(self):
        old = self._task("old plan")
        new = self._task("new plan", ts=200.0)
        self.store.supersede_fact(old, new, 250.0)
        ids = {f["fact_id"] for f in self.store.facts_since(0.0)}
        self.assertNotIn(old, ids)
        self.assertIn(new, ids)
        ids_all = {f["fact_id"] for f in
                   self.store.facts_since(0.0, exclude_superseded=False)}
        self.assertIn(old, ids_all)

    def test_archive_fact_retires_without_replacement(self):
        fid = self._task("random noise the extractor imagined")
        self.assertTrue(self.store.archive_fact(fid, 400.0))
        f = self.store.get_fact(fid)
        self.assertEqual(f["state"], "archived")
        self.assertIsNone(f["superseded_by"])
        self.assertEqual(f["status"], "cancelled")
        self.assertFalse(self.store.archive_fact(fid, 401.0))  # already gone
        # excluded from reflection material and the LIKE fallback
        self.assertNotIn(fid, {x["fact_id"] for x in self.store.facts_since(0.0)})
        self.assertEqual(self.store.search_facts_like("noise"), [])

    def test_search_facts_like_finds_active_only(self):
        keep = self._task("send the deck to Radnor Capital")
        gone = self._task("send the deck draft to Radnor Capital")
        self.store.supersede_fact(gone, keep, 300.0)
        dismissed = self._task("deck review with nobody")
        self.store.review_fact(dismissed, "dismissed")
        hits = {f["fact_id"] for f in self.store.search_facts_like("deck")}
        self.assertEqual(hits, {keep})
        self.assertEqual(self.store.search_facts_like(""), [])


class GateTests(unittest.TestCase):
    """gate_fact decision logic with the probe/adjudicator/telemetry patched."""

    def setUp(self):
        patches = [
            patch.object(fact_gate, "settings", _cfg()),
            patch.object(fact_gate, "_telemetry", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_confidence_floor_drops(self):
        with patch.object(fact_gate, "_similar_active", return_value=[]):
            v = gate_fact("claim", "the sky is falling", 0.2,
                          "the sky is falling", "he said the sky is falling")
        self.assertEqual(v.action, "drop")
        self.assertIn("floor", v.reason)

    def test_missing_confidence_passes_floor(self):
        with patch.object(fact_gate, "_similar_active", return_value=[]):
            v = gate_fact("claim", "budget approved", None,
                          "budget approved", "the budget approved yesterday")
        self.assertEqual(v.action, "insert")

    def test_unfaithful_span_drops(self):
        with patch.object(fact_gate, "_similar_active", return_value=[]):
            v = gate_fact("task", "call the bank", 0.9,
                          "call the bank", "remind me to email Sam tomorrow")
        self.assertEqual(v.action, "drop")
        self.assertIn("verbatim", v.reason)

    def test_span_faithful_modulo_case_and_punctuation(self):
        with patch.object(fact_gate, "_similar_active", return_value=[]):
            v = gate_fact("task", "buy milk", 0.9,
                          "Buy milk!", "please buy milk today")
        self.assertEqual(v.action, "insert")

    def test_span_gate_skipped_without_source_text(self):
        # OCR/vision paths have no speech to quote — the span gate must not fire.
        with patch.object(fact_gate, "_similar_active", return_value=[]):
            v = gate_fact("task", "finish slides", 0.9, "", "")
        self.assertEqual(v.action, "insert")

    def test_empty_text_drops(self):
        self.assertEqual(gate_fact("task", "  ", 0.9, "x", "x").action, "drop")

    def test_dedup_disabled_never_probes(self):
        with patch.object(fact_gate, "settings", _cfg(dedup=False)), \
             patch.object(fact_gate, "_similar_active") as probe:
            v = gate_fact("task", "buy milk", 0.9, "buy milk", "buy milk")
        self.assertEqual(v.action, "insert")
        probe.assert_not_called()

    def test_near_duplicate_collapses_without_model(self):
        with patch.object(fact_gate, "_similar_active",
                          return_value=[(7, 0.99, "buy milk")]), \
             patch.object(fact_gate, "_adjudicate") as adj:
            v = gate_fact("task", "buy milk", 0.9, "buy milk", "buy milk")
        self.assertEqual((v.action, v.dup_fact_id), ("dedup", 7))
        adj.assert_not_called()

    def test_adjudicated_duplicate_dedups(self):
        with patch.object(fact_gate, "_similar_active",
                          return_value=[(7, 0.80, "meeting at 2pm")]), \
             patch.object(fact_gate, "_adjudicate", return_value="duplicate"):
            v = gate_fact("claim", "the meeting is at 2pm", 0.9,
                          "the meeting is at 2pm", "the meeting is at 2pm")
        self.assertEqual((v.action, v.dup_fact_id), ("dedup", 7))

    def test_adjudicated_update_supersedes(self):
        with patch.object(fact_gate, "_similar_active",
                          return_value=[(7, 0.80, "meeting at 2pm")]), \
             patch.object(fact_gate, "_adjudicate", return_value="update"):
            v = gate_fact("claim", "meeting moved to 3pm", 0.9,
                          "meeting moved to 3pm", "the meeting moved to 3pm")
        self.assertEqual((v.action, v.supersede_ids), ("supersede", (7,)))

    def test_adjudicated_unrelated_inserts(self):
        with patch.object(fact_gate, "_similar_active",
                          return_value=[(7, 0.80, "meeting with Sam at 2pm")]), \
             patch.object(fact_gate, "_adjudicate", return_value="unrelated"):
            v = gate_fact("claim", "meeting with Lee at 2pm", 0.9,
                          "meeting with Lee at 2pm", "meeting with Lee at 2pm")
        self.assertEqual(v.action, "insert")

    def test_below_band_similarity_skips_adjudicator(self):
        with patch.object(fact_gate, "_similar_active",
                          return_value=[(7, 0.50, "something vaguely near")]), \
             patch.object(fact_gate, "_adjudicate") as adj:
            v = gate_fact("claim", "a new thing entirely", 0.9,
                          "a new thing entirely", "a new thing entirely")
        self.assertEqual(v.action, "insert")
        adj.assert_not_called()

    def test_probe_unavailable_degrades_to_insert(self):
        with patch.object(fact_gate, "_similar_active", return_value=[]):
            v = gate_fact("task", "ship the release", 0.9,
                          "ship the release", "we should ship the release")
        self.assertEqual(v.action, "insert")


class RetrievalHelperTests(unittest.TestCase):
    def test_fact_is_retrievable(self):
        self.assertFalse(fact_is_retrievable(None))
        self.assertTrue(fact_is_retrievable({}))                    # defaults
        self.assertTrue(fact_is_retrievable({"state": "active"}))
        self.assertTrue(fact_is_retrievable({"state": None,
                                             "review": "approved"}))
        self.assertFalse(fact_is_retrievable({"state": "superseded"}))
        self.assertFalse(fact_is_retrievable({"review": "dismissed"}))

    def test_recency_adjusted_bonus_decays(self):
        fresh = recency_adjusted(0.5, 0.0, weight=0.08, half_life_days=14)
        mid = recency_adjusted(0.5, 14.0, weight=0.08, half_life_days=14)
        old = recency_adjusted(0.5, 1000.0, weight=0.08, half_life_days=14)
        self.assertAlmostEqual(fresh, 0.58)
        self.assertAlmostEqual(mid, 0.54)
        self.assertAlmostEqual(old, 0.5, places=3)
        self.assertGreater(fresh, mid)
        self.assertGreater(mid, old)

    def test_recency_disabled_is_pure_cosine(self):
        self.assertEqual(recency_adjusted(0.42, 0.0, weight=0.0,
                                          half_life_days=14), 0.42)

    def test_fresh_correction_outranks_stale_twin(self):
        # the "meeting moved to 3pm" scenario: near-equal cosine, 30 days apart
        stale = recency_adjusted(0.80, 30.0, weight=0.08, half_life_days=14)
        fresh = recency_adjusted(0.79, 0.0, weight=0.08, half_life_days=14)
        self.assertGreater(fresh, stale)


class VerdictShapeTests(unittest.TestCase):
    def test_defaults(self):
        v = Verdict("insert")
        self.assertEqual((v.reason, v.dup_fact_id, v.supersede_ids),
                         ("", None, ()))


if __name__ == "__main__":
    unittest.main()


class HygieneTelemetryTests(unittest.TestCase):
    """fact_hygiene is a PASS rate: telemetry used to fire only on the
    drop/dedup/review paths, so the numerator could never increment and the
    console pinned the metric at 0% forever. Inserts must record a hit."""

    def _capture(self):
        calls: list[tuple[str, bool, dict]] = []
        from app.services import cog_telemetry as ct

        def rec(metric, hit, **meta):
            calls.append((metric, hit, meta))
        return calls, patch.object(ct.cog_telemetry, "record", rec)

    def test_insert_records_a_hit_when_dedup_is_off(self):
        calls, cap = self._capture()
        with patch.object(fact_gate, "settings",
                          _cfg(min_conf=0.0, span_gate=False, dedup=False)), cap:
            v = gate_fact("task", "send the deck", 0.9, "", "")
        self.assertEqual(v.action, "insert")
        self.assertEqual(calls, [("fact_hygiene", True,
                                  {"action": "insert",
                                   "reason": "dedup disabled",
                                   "kind": "task",
                                   "text": "send the deck"})])

    def test_insert_records_a_hit_when_all_gates_pass(self):
        calls, cap = self._capture()
        with patch.object(fact_gate, "settings",
                          _cfg(min_conf=0.0, span_gate=False)), \
                patch.object(fact_gate, "_similar_active", return_value=[]), cap:
            v = gate_fact("task", "send the deck", 0.9, "", "")
        self.assertEqual(v.action, "insert")
        self.assertEqual([(m, h) for m, h, _ in calls],
                         [("fact_hygiene", True)])
        self.assertEqual(calls[0][2]["reason"], "passed all gates")

    def test_drop_still_records_a_miss(self):
        calls, cap = self._capture()
        with patch.object(fact_gate, "settings",
                          _cfg(min_conf=0.5, span_gate=False, dedup=False)), cap:
            v = gate_fact("task", "send the deck", 0.1, "", "")
        self.assertEqual(v.action, "drop")
        self.assertEqual([(m, h) for m, h, _ in calls],
                         [("fact_hygiene", False)])

    def test_empty_text_drop_is_no_longer_silent(self):
        calls, cap = self._capture()
        with patch.object(fact_gate, "settings",
                          _cfg(min_conf=0.0, span_gate=False, dedup=False)), cap:
            v = gate_fact("task", "   ", 0.9, "", "")
        self.assertEqual(v.action, "drop")
        self.assertEqual([(m, h) for m, h, _ in calls],
                         [("fact_hygiene", False)])
