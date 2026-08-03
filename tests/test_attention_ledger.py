"""Attention ledger (Cognitive OS Phase 0) — instrument-only contracts.

The ledger observes the surfaces; it must never change them. These tests pin
the recording semantics (throttle, decomposition, miss detection, outcome
closing) so learned ranking later trains on rows whose meaning never drifted.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


def _fresh_ledger():
    from app.services.attention_ledger import AttentionLedger
    return AttentionLedger()


class LedgerStoreTests(unittest.TestCase):
    def test_bulk_insert_outcome_and_stats(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                n = store.add_attention_impressions([
                    {"node_type": "person", "node_id": 1, "surface": "field",
                     "layer": "focus", "score": 0.61,
                     "decomposition": json.dumps({"pros": 0.4})},
                    {"node_type": "fact", "node_id": 7, "surface": "grounding"},
                ])
                self.assertEqual(n, 2)
                # Closing hits the newest OPEN row for the node.
                row_id = store.set_attention_outcome("person", 1, "pin")
                self.assertIsInstance(row_id, int)
                stats = store.attention_stats(days=1)
                self.assertEqual(stats["by_surface"].get("field"), 1)
                self.assertEqual(stats["by_surface"].get("grounding"), 1)
                self.assertEqual(stats["outcomes"].get("pin"), 1)
                self.assertEqual(stats["field_impressions"], 1)
                self.assertEqual(stats["field_engaged"], 1)
            finally:
                store.close()

    def test_reaction_without_open_impression_is_kept(self):
        # A pin on a node the ledger never saw surfaced is still signal —
        # recorded as a standalone closed row, never dropped.
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.set_attention_outcome("entity", 3, "hide")
                stats = store.attention_stats(days=1)
                self.assertEqual(stats["outcomes"].get("hide"), 1)
                self.assertEqual(stats["by_surface"].get("reaction"), 1)
            finally:
                store.close()

    def test_last_attention_ts(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                self.assertIsNone(store.last_attention_ts("person", 9, "field"))
                store.add_attention_impressions([
                    {"node_type": "person", "node_id": 9, "surface": "field"}])
                ts = store.last_attention_ts("person", 9, "field")
                self.assertIsNotNone(ts)
                self.assertLess(abs(time.time() - ts), 5)
            finally:
                store.close()


class LedgerServiceTests(unittest.TestCase):
    def _nodes(self, score=0.5, layer="focus"):
        return [{"id": "person:1", "layer": layer, "gravity": score,
                 "_decomp": {"pros": 0.3, "temp": 0.8}}]

    def test_field_recording_is_throttled(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = _fresh_ledger()
                with mock.patch("app.services.activity.describe_recent",
                                return_value=[]):
                    self.assertEqual(led.record_field(self._nodes(), store), 1)
                    # Same node, same score, same layer, seconds later: throttled.
                    self.assertEqual(led.record_field(self._nodes(), store), 0)
                    # Score moved past the delta: recorded again.
                    self.assertEqual(
                        led.record_field(self._nodes(score=0.75), store), 1)
                    # Layer change defeats the throttle too.
                    self.assertEqual(
                        led.record_field(self._nodes(score=0.75,
                                                     layer="periphery"), store), 1)
            finally:
                store.close()

    def test_field_row_carries_decomposition(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = _fresh_ledger()
                with mock.patch("app.services.activity.describe_recent",
                                return_value=["Coding — VS Code"]):
                    led.record_field(self._nodes(), store)
                row = store._conn.execute(
                    "SELECT * FROM attention_impressions").fetchone()
                decomp = json.loads(row["decomposition"])
                self.assertEqual(decomp["pros"], 0.3)
                self.assertEqual(row["surface"], "field")
                self.assertIsNotNone(row["context_id"])
                snap = store.latest_context_snapshot()
                self.assertIn("Coding", snap["app"])
            finally:
                store.close()

    def test_grounding_miss_when_field_never_surfaced(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = _fresh_ledger()
                led.record_grounding([4], [11], store)
                stats = store.attention_stats(days=1)
                # person 4 + fact 11 grounding rows, plus person 4 miss row.
                self.assertEqual(stats["by_surface"].get("grounding"), 3)
                self.assertEqual(stats["misses"], 1)
            finally:
                store.close()

    def test_no_miss_when_field_recently_surfaced(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = _fresh_ledger()
                with mock.patch("app.services.activity.describe_recent",
                                return_value=[]):
                    led.record_field(
                        [{"id": "person:4", "layer": "focus", "gravity": 0.5,
                          "_decomp": {}}], store)
                led.record_grounding([4], [], store)
                self.assertEqual(store.attention_stats(days=1)["misses"], 0)
            finally:
                store.close()

    def test_outcome_parses_node_ids_and_never_raises(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = _fresh_ledger()
                self.assertTrue(led.outcome("person:2", "pin", store=store))
                self.assertFalse(led.outcome("garbage", "pin", store=store))
                self.assertFalse(led.outcome("unknown:5", "pin", store=store))
                stats = store.attention_stats(days=1)
                self.assertEqual(stats["outcomes"].get("pin"), 1)
            finally:
                store.close()


class VerdictJoinTests(unittest.TestCase):
    def test_verdict_closes_grounding_impressions_in_row_window(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = _fresh_ledger()
                led.record_grounding([4], [11], store)
                row_time = time.time() + 5  # model call follows compose
                with mock.patch("app.services.escalate_log.escalate_log.row_by_id",
                                return_value={"id": "r1", "time": row_time}):
                    n = led.close_grounding_for_row("r1", "accepted", store=store)
                # person + fact grounding rows closed as 'used'; the miss row
                # was born closed and stays 'miss'.
                self.assertEqual(n, 2)
                stats = store.attention_stats(days=1)
                self.assertEqual(stats["outcomes"].get("used"), 2)
                self.assertEqual(stats["misses"], 1)
            finally:
                store.close()

    def test_verdict_outside_window_closes_nothing(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = _fresh_ledger()
                led.record_grounding([4], [], store)
                stale_row_time = time.time() - 3600  # an hour-old answer
                with mock.patch("app.services.escalate_log.escalate_log.row_by_id",
                                return_value={"id": "r2", "time": stale_row_time}):
                    n = led.close_grounding_for_row("r2", "rejected", store=store)
                self.assertEqual(n, 0)
            finally:
                store.close()

    def test_unknown_row_or_verdict_is_a_noop(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                led = _fresh_ledger()
                with mock.patch("app.services.escalate_log.escalate_log.row_by_id",
                                return_value=None):
                    self.assertEqual(
                        led.close_grounding_for_row("nope", "accepted",
                                                    store=store), 0)
                self.assertEqual(
                    led.close_grounding_for_row("r1", "weird", store=store), 0)
            finally:
                store.close()


class ComposeGateTests(unittest.TestCase):
    def test_record_attention_false_writes_nothing(self):
        from app.services.grounding import compose
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Justin Adorante")
                compose("what did Justin say?", store=store,
                        record_attention=False)
                self.assertEqual(
                    store.attention_stats(days=1)["by_surface"], {})
            finally:
                store.close()

    def test_compose_records_person_grounding_by_default(self):
        from app.services.attention_ledger import attention_ledger
        from app.services.grounding import compose
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Justin Adorante")
                # compose() uses the singleton ledger — clear its in-process
                # throttle so earlier suites in this run can't suppress it.
                attention_ledger._last.clear()
                compose("what did Justin say?", store=store)
                stats = store.attention_stats(days=1)
                self.assertGreaterEqual(
                    stats["by_surface"].get("grounding", 0), 1)
            finally:
                store.close()


class ConstellationImpressionTests(unittest.TestCase):
    def test_field_render_logs_surfaced_nodes(self):
        from app.services import graph
        from app.services.attention_ledger import attention_ledger
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Justin")
                store.add_task("Follow up with Justin on pricing",
                               confidence=0.9, extracted_at=time.time())
                # The singleton throttle may hold keys from other tests in this
                # process — clear so this render records.
                attention_ledger._last.clear()
                attention_ledger._snapshot_id = None
                with mock.patch("app.services.activity.describe_recent",
                                return_value=[]):
                    data = graph.constellation(store, limit=24,
                                               record_impressions=True)
                stats = store.attention_stats(days=1)
                self.assertGreaterEqual(stats["field_impressions"], 1)
                self.assertEqual(stats["field_impressions"], len(data["nodes"]))
                row = store._conn.execute(
                    "SELECT decomposition FROM attention_impressions "
                    "WHERE surface = 'field' LIMIT 1").fetchone()
                decomp = json.loads(row["decomposition"])
                for key in ("pros", "rel", "cent", "sem", "temp", "unc",
                            "decay", "trust", "raw", "conf"):
                    self.assertIn(key, decomp)
                # Payload stays clean: no internal fields leak to the UI.
                self.assertNotIn("_decomp", data["nodes"][0])
            finally:
                store.close()

    def test_internal_reuse_does_not_record(self):
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Marc")
                graph.constellation(store, limit=24)  # default: no recording
                self.assertEqual(
                    store.attention_stats(days=1)["field_impressions"], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
