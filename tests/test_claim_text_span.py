"""Claim text/span separation (plan task 0.2).

A claim's text lives in facts.text; source_span is strictly the verbatim
provenance quote — empty when there is none, never a paraphrase substitute.
Covers the write path (no more `source_span or text`), the read path
(_FACT_SELECT surfaces facts.text for claims), the one-time migration that
rescues claim text from pre-fix DBs where it lived in the span column, and
the gate's distinct empty-span drop.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import fact_gate
from app.services.fact_gate import gate_fact
from app.storage import Store

NOW = 1_000_000_000.0


def _cfg(**over):
    base = dict(min_conf=0.35, span_gate=True, dedup=True,
                auto_dup_sim=0.97, adjudicate_sim=0.72,
                recency_weight=0.08, recency_half_life_days=14.0)
    base.update(over)
    return SimpleNamespace(facts=SimpleNamespace(**base))


class ClaimWritePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def _raw(self, fid: int) -> dict:
        row = self.store._conn.execute(
            "SELECT text, source_span FROM facts WHERE id = ?", (fid,)).fetchone()
        return dict(row)

    def test_spanless_claim_keeps_span_empty(self):
        # User-typed claims have no quote — the old code silently wrote the
        # paraphrase INTO source_span; now the span stays honest (empty).
        fid = self.store.add_claim("Alice prefers morning meetings",
                                   extracted_at=NOW)
        raw = self._raw(fid)
        self.assertEqual(raw["text"], "Alice prefers morning meetings")
        self.assertEqual(raw["source_span"], "")

    def test_claim_with_span_keeps_both_verbatim(self):
        fid = self.store.add_claim(
            "the price is $49/seat",
            source_span="David said it's forty-nine a seat",
            extracted_at=NOW)
        raw = self._raw(fid)
        self.assertEqual(raw["text"], "the price is $49/seat")
        self.assertEqual(raw["source_span"], "David said it's forty-nine a seat")

    def test_get_fact_surfaces_claim_text(self):
        fid = self.store.add_claim("demo went well", extracted_at=NOW)
        self.assertEqual(self.store.get_fact(fid)["text"], "demo went well")

    def test_list_facts_surfaces_claim_text_not_span(self):
        self.store.add_claim("budget is approved",
                             source_span="she said the budget is approved",
                             extracted_at=NOW)
        rows = self.store.list_facts(kind="claim")
        self.assertEqual(rows[0]["text"], "budget is approved")
        self.assertEqual(rows[0]["source_span"],
                         "she said the budget is approved")


class ClaimMigrationTests(unittest.TestCase):
    def test_pre_fix_db_backfills_claim_text_from_span(self):
        # Pre-fix schema: no facts.text; a claim's only surviving text is
        # whatever add_claim wrote into source_span. Opening the DB with Store
        # must add the column and move claim text over exactly once.
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE facts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                kind            TEXT    NOT NULL,
                source_event_id INTEGER,
                source_span     TEXT,
                confidence      REAL,
                extracted_at    REAL    NOT NULL
            )
            """)
        conn.execute(
            "INSERT INTO facts (kind, source_span, confidence, extracted_at) "
            "VALUES ('claim', 'Mnemos demo went well', 0.8, ?)", (NOW,))
        conn.execute(
            "INSERT INTO facts (kind, source_span, confidence, extracted_at) "
            "VALUES ('task', 'send the deck', 0.9, ?)", (NOW,))
        conn.commit()
        conn.close()

        store = Store(db_path=db, audio_dir=tmp / "audio")
        rows = {r["kind"]: dict(r) for r in store._conn.execute(
            "SELECT kind, text, source_span FROM facts").fetchall()}
        self.assertEqual(rows["claim"]["text"], "Mnemos demo went well")
        # Span is left as-is: for old rows we can't know whether it was a real
        # quote or the substituted paraphrase, and text is now authoritative.
        self.assertEqual(rows["claim"]["source_span"], "Mnemos demo went well")
        # Non-claim kinds keep their text in the typed tables — no backfill.
        self.assertIsNone(rows["task"]["text"])


class EmptySpanGateTests(unittest.TestCase):
    def test_extracted_claim_with_empty_span_drops(self):
        with patch.object(fact_gate, "settings", _cfg()), \
             patch.object(fact_gate, "_similar_active", return_value=[]):
            v = gate_fact("claim", "the price is $49/seat", 0.9,
                          "", "David said it's forty-nine a seat")
        self.assertEqual(v.action, "drop")
        self.assertEqual(v.reason, "empty source_span")

    def test_unfaithful_span_reason_unchanged(self):
        with patch.object(fact_gate, "settings", _cfg()), \
             patch.object(fact_gate, "_similar_active", return_value=[]):
            v = gate_fact("claim", "the price is $49/seat", 0.9,
                          "the price is definitely $55", "it's forty-nine a seat")
        self.assertEqual(v.action, "drop")
        self.assertIn("verbatim", v.reason)

    def test_no_source_text_still_skips_span_gate(self):
        # OCR/vision paths have no speech to quote — unchanged behavior.
        with patch.object(fact_gate, "settings", _cfg()), \
             patch.object(fact_gate, "_similar_active", return_value=[]):
            v = gate_fact("claim", "finish slides", 0.9, "", "")
        self.assertEqual(v.action, "insert")


if __name__ == "__main__":
    unittest.main()
