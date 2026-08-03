"""P0 attention golden corpus + offer-surface ledger contracts."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class AttentionCorpusTests(unittest.TestCase):
    def test_frozen_corpus_meets_p0_floors(self):
        from app.services import attention_corpus as corpus

        report = corpus.validate()
        self.assertTrue(report["ok"], report.get("errors"))
        self.assertGreaterEqual(report["n"], corpus.MIN_CASES)
        self.assertGreaterEqual(report["misses"], corpus.MIN_MISSES)
        self.assertGreaterEqual(report["anticipation"], corpus.MIN_ANTICIPATION)
        self.assertTrue(report["frozen"], "MANIFEST.json missing — run freeze script")
        self.assertTrue(corpus.CORPUS_PATH.is_file())

    def test_hit_miss_consistency_rules(self):
        from app.services.attention_corpus import validate_case

        hit = {
            "id": "t-hit", "kind": "recall", "query": "Marc?",
            "needed": {"type": "person", "name": "Marc"},
            "field_at_ask": ["person:Marc"], "expect": "hit",
        }
        miss = {
            "id": "t-miss", "kind": "miss", "query": "Priya?",
            "needed": {"type": "person", "name": "Priya"},
            "field_at_ask": ["person:Scott"], "expect": "miss",
        }
        bad_hit = dict(hit, field_at_ask=["person:Scott"])
        bad_miss = dict(miss, field_at_ask=["person:Priya"])
        self.assertEqual(validate_case(hit), [])
        self.assertEqual(validate_case(miss), [])
        self.assertTrue(any("expect=hit" in e for e in validate_case(bad_hit)))
        self.assertTrue(any("expect=miss" in e for e in validate_case(bad_miss)))

    def test_layers_on_gravity_golden_module(self):
        # Corpus is additive — GravityGoldenTests must still import cleanly.
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parent / "test_vinceo_interface.py"
        spec = importlib.util.spec_from_file_location("tvi", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(issubclass(mod.GravityGoldenTests, unittest.TestCase))


class OfferLedgerTests(unittest.TestCase):
    def test_record_and_close_offer_with_fact(self):
        from app.services.attention_ledger import AttentionLedger
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = AttentionLedger()
                self.assertEqual(
                    led.record_offer(fact_id=42, text="email Justin",
                                     kind="task", score=0.8, store=store), 1)
                stats = store.attention_stats(days=1)
                self.assertEqual(stats["offers"], 1)
                self.assertTrue(
                    led.close_offer(fact_id=42, text="email Justin",
                                    accepted=True, kind="task", store=store))
                stats = store.attention_stats(days=1)
                self.assertEqual(stats["offer_accepted"], 1)
                self.assertEqual(stats["offer_accept_rate"], 1.0)
            finally:
                store.close()

    def test_close_offer_without_fact_matches_text(self):
        from app.services.attention_ledger import AttentionLedger
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = AttentionLedger()
                led.record_offer(text="text Abby I'll be late", kind="phone",
                                 store=store)
                self.assertTrue(
                    led.close_offer(text="text Abby I'll be late",
                                    accepted=False, kind="phone", store=store))
                stats = store.attention_stats(days=1)
                self.assertEqual(stats["offer_dismissed"], 1)
                self.assertEqual(stats["offer_accept_rate"], 0.0)
            finally:
                store.close()

    def test_add_offer_writes_ledger_row(self):
        from app.services.agent_bridge import AgentWorker
        from app.storage import Store
        import app.storage as storage_mod
        import queue
        import threading

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                w = AgentWorker.__new__(AgentWorker)
                w.lock = threading.Lock()
                w.pending_todo = None
                w.offer_queue = []
                w.busy = w.busy_fast = False
                w.awaiting = w.awaiting_fast = False
                w.cmd_q = queue.Queue()
                w.fast_q = queue.Queue()
                w._emit = lambda *a, **k: None
                with mock.patch.object(w, "expire_stale_offers", return_value=0), \
                     mock.patch.object(storage_mod, "get_store",
                                       return_value=store):
                    shown = w._add_offer({
                        "items": ["email Justin the deck"],
                        "title": "",
                        "message": "offer?",
                        "kind": "task",
                        "fact_id": 7,
                    })
                self.assertTrue(shown)
                self.assertEqual(store.attention_stats(days=1).get("offers"), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
