"""Plan 4.3 — open-loop engine: waiting-on-them, snooze, horizon, dismiss rate."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

NOW = 1_700_000_000.0


def _mk(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db")


class OpenLoopDetectTests(unittest.TestCase):
    def test_waiting_on_them_with_evidence(self):
        from app.services import open_loops

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                marc = store.resolve_person("Marc", ts=NOW)
                me = store.resolve_person("me", ts=NOW)
                fid = store.add_commitment(
                    "Marc will send the term sheet",
                    from_person_id=marc, to_person_id=me,
                    due="2023-01-01",
                    source_span="Marc will send the term sheet by Friday",
                    extracted_at=NOW - 10 * 86400,
                )
                store.transition_commitment(
                    fid, "active", reason="test",
                    evidence={"source": "test"})
                loops = open_loops.detect_waiting_on_them(store, now=NOW)
                self.assertTrue(loops)
                hit = next(x for x in loops if x["fact_id"] == fid)
                self.assertEqual(hit["kind"], "waiting_on_them")
                self.assertTrue(any("waiting on" in r.lower()
                                    for r in hit["reason"]))
                self.assertIn("term sheet", (hit["evidence"].get("source_span")
                                             or hit["evidence"].get("text")
                                             or "").lower())
                row = store.get_fact(fid)
                self.assertTrue(row.get("counterparty_expects"))
            finally:
                store.close()

    def test_snooze_respected(self):
        from app.services import open_loops

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                marc = store.resolve_person("Marc", ts=NOW)
                me = store.resolve_person("me", ts=NOW)
                fid = store.add_commitment(
                    "Marc owes the deck",
                    from_person_id=marc, to_person_id=me,
                    due="2023-06-01",
                    source_span="Marc owes the deck",
                    extracted_at=NOW - 20 * 86400,
                )
                store.transition_commitment(
                    fid, "active", reason="test",
                    evidence={"source": "test"})
                before = open_loops.detect_waiting_on_them(store, now=NOW)
                self.assertTrue(any(x["fact_id"] == fid for x in before))
                self.assertTrue(open_loops.snooze(store, fid, now=NOW,
                                                  kind="waiting_on_them"))
                after = open_loops.detect_waiting_on_them(store, now=NOW)
                self.assertFalse(any(x["fact_id"] == fid for x in after))
                # After snooze window, resurfaces
                later = open_loops.detect_waiting_on_them(
                    store, now=NOW + open_loops.SNOOZE_S + 10)
                self.assertTrue(any(x["fact_id"] == fid for x in later))
            finally:
                store.close()

    def test_horizon_surfaces_loops_without_calendar(self):
        from app.services import horizon, open_loops

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                marc = store.resolve_person("Marc", ts=NOW)
                me = store.resolve_person("me", ts=NOW)
                fid = store.add_commitment(
                    "Waiting on Marc's reply",
                    from_person_id=marc, to_person_id=me,
                    due="2023-01-15",
                    source_span="Marc said he'd reply by Monday",
                    extracted_at=NOW - 30 * 86400,
                )
                store.transition_commitment(
                    fid, "active", reason="test",
                    evidence={"source": "test"})
                with mock.patch.object(horizon, "_cfg") as cfg:
                    cfg.return_value = type("C", (), {
                        "horizon": True,
                        "horizon_min_p": 0.5,
                        "horizon_horizon_s": 90 * 60,
                    })()
                    items = horizon.predict(store, now=NOW, limit=3)
                self.assertTrue(items)
                self.assertTrue(any(
                    it.get("source") == "open_loop"
                    and it.get("fact_id") == fid for it in items))
                # Direct helper shape
                chips = open_loops.horizon_items(store, now=NOW, limit=3)
                self.assertTrue(any(c.get("loop_kind") == "waiting_on_them"
                                    for c in chips))
            finally:
                store.close()

    def test_dismiss_rate_metric(self):
        from app.services import open_loops
        from app.services.cog_telemetry import (
            CogTelemetry, OPEN_LOOP, OPEN_LOOP_DISMISS,
        )

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "trail.jsonl"
            tel = CogTelemetry()
            tel._path = path
            for _ in range(3):
                tel.record(OPEN_LOOP, True, kind="waiting_on_them")
            for _ in range(2):
                tel.record(OPEN_LOOP_DISMISS, True, kind="waiting_on_them")
            with mock.patch(
                "app.services.cog_telemetry.cog_telemetry", tel
            ):
                stats = open_loops.dismiss_rate()
            self.assertEqual(stats["surfaces"], 3)
            self.assertEqual(stats["dismisses"], 2)
            self.assertAlmostEqual(stats["dismiss_rate"], 2 / 3, places=3)

    def test_horizon_dismiss_snoozes(self):
        from app.services import horizon, open_loops

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                marc = store.resolve_person("Marc", ts=NOW)
                me = store.resolve_person("me", ts=NOW)
                fid = store.add_commitment(
                    "Marc will call",
                    from_person_id=marc, to_person_id=me,
                    due="2022-12-01",
                    source_span="Marc will call",
                    extracted_at=NOW - 40 * 86400,
                )
                store.transition_commitment(
                    fid, "active", reason="test",
                    evidence={"source": "test"})
                self.assertTrue(
                    any(x["fact_id"] == fid
                        for x in open_loops.detect_waiting_on_them(
                            store, now=NOW)))
                ok = horizon.dismiss(store, f"fact:{fid}")
                self.assertTrue(ok)
                self.assertFalse(
                    any(x["fact_id"] == fid
                        for x in open_loops.detect_waiting_on_them(
                            store, now=NOW)))
            finally:
                store.close()


class ExtractorQuestionsSchemaTests(unittest.TestCase):
    def test_schema_requires_questions(self):
        from app.services.extractor import _SCHEMA

        self.assertIn("questions", _SCHEMA["properties"])
        self.assertIn("questions", _SCHEMA["required"])

    def test_persist_question(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_question(
                    "What's the valuation?",
                    source_span="What's the valuation?",
                    confidence=0.9,
                    extracted_at=NOW - 2 * 86400,
                )
                from app.services import open_loops
                qs = open_loops.detect_unanswered_questions(store, now=NOW)
                self.assertTrue(any(x["fact_id"] == fid for x in qs))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
