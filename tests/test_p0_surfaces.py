"""Phase 0 harness surfaces — fulfillment baseline, weekly self-report,
and supersession surfacing/revert (contradiction visibility)."""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path


def _iso(days_from_now: float) -> str:
    return (datetime.now() + timedelta(days=days_from_now)).strftime("%Y-%m-%d")


class FulfillmentTests(unittest.TestCase):
    def test_rates_and_overdue(self):
        from app.services import fulfillment

        now = time.time()
        day = 86400.0
        facts = [
            # open + overdue (due yesterday)
            {"kind": "task", "status": "open", "due": _iso(-1),
             "extracted_at": now - 10 * day, "updated_at": now - 10 * day},
            # open, no due
            {"kind": "commitment", "status": "open",
             "extracted_at": now - 2 * day, "updated_at": now - 2 * day},
            # done ON TIME: finished 3 days ago, was due tomorrow
            {"kind": "task", "status": "done", "due": _iso(1),
             "extracted_at": now - 8 * day, "updated_at": now - 3 * day},
            # done LATE: finished now, was due 5 days ago
            {"kind": "commitment", "status": "done", "due": _iso(-5),
             "extracted_at": now - 9 * day, "updated_at": now},
            # dropped
            {"kind": "task", "status": "cancelled",
             "extracted_at": now - 20 * day, "updated_at": now - 15 * day},
            # dismissed = judged noise, not abandoned work: excluded entirely
            {"kind": "task", "status": "cancelled", "review": "dismissed",
             "extracted_at": now - 20 * day, "updated_at": now - 15 * day},
            # non-work facts are ignored
            {"kind": "claim", "status": None, "extracted_at": now},
        ]
        s = fulfillment.summarize(facts, now)
        self.assertEqual(s["counts"], {"open": 2, "done": 2, "cancelled": 1})
        self.assertAlmostEqual(s["fulfillment_rate"], 2 / 3, places=3)
        self.assertAlmostEqual(s["on_time_rate"], 0.5, places=3)
        self.assertEqual(s["overdue_open"], 1)
        self.assertIsNotNone(s["median_open_age_days"])
        self.assertEqual(sum(s["weekly"]["resolved"]), 2)
        self.assertEqual(s["by_kind"]["task"]["done"], 1)

    def test_empty_is_calm(self):
        from app.services import fulfillment

        s = fulfillment.summarize([], time.time())
        self.assertIsNone(s["fulfillment_rate"])
        self.assertIsNone(s["on_time_rate"])
        self.assertEqual(s["counts"]["open"], 0)


class SelfReportTests(unittest.TestCase):
    def test_roundtrip_and_due_window(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                self.assertIsNone(store.last_self_report_ts())  # first week: due
                rid = store.add_self_report(load_score=4, trust_score=5,
                                            interrupt_score=3, note="solid week")
                self.assertIsInstance(rid, int)
                last = store.last_self_report_ts()
                self.assertLess(abs(time.time() - last), 5)   # fresh: not due
                rows = store.list_self_reports()
                self.assertEqual(rows[0]["load_score"], 4)
                self.assertEqual(rows[0]["note"], "solid week")
                # An 8-day-old report means a new one is due again.
                store.add_self_report(load_score=2, trust_score=2,
                                      interrupt_score=2,
                                      ts=time.time() - 8 * 86400)
                self.assertLess(abs(time.time() - store.last_self_report_ts()),
                                5)  # MAX(ts) still the fresh one
            finally:
                store.close()


class SupersessionTests(unittest.TestCase):
    def _two_tasks(self, store):
        old = store.add_task("Meet Marc at 2pm", confidence=0.8,
                             extracted_at=time.time() - 3600)
        new = store.add_task("Meet Marc at 3pm", confidence=0.9,
                             extracted_at=time.time())
        store.supersede_fact(old, new, time.time())
        return old, new

    def test_list_shows_live_pairs_with_text(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                old, new = self._two_tasks(store)
                pairs = store.recent_supersessions()
                self.assertEqual(len(pairs), 1)
                p = pairs[0]
                self.assertEqual((p["old_id"], p["new_id"]), (old, new))
                self.assertIn("2pm", p["old_text"])
                self.assertIn("3pm", p["new_text"])
            finally:
                store.close()

    def test_revert_swaps_direction_and_typed_rows(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                old, new = self._two_tasks(store)
                # supersede cancelled the old typed row
                self.assertEqual(store.get_fact(old)["status"], "cancelled")
                self.assertTrue(store.revert_supersession(old))
                fo, fn = store.get_fact(old), store.get_fact(new)
                self.assertEqual(fo["state"], "active")
                self.assertEqual(fo["status"], "open")       # typed row reopened
                self.assertEqual(fn["state"], "superseded")
                self.assertEqual(fn["superseded_by"], old)
                self.assertEqual(fn["status"], "cancelled")
                # The reverted pair is now offered the other way round.
                pairs = store.recent_supersessions()
                self.assertEqual((pairs[0]["old_id"], pairs[0]["new_id"]),
                                 (new, old))
            finally:
                store.close()

    def test_chain_middle_is_not_revertible_or_listed(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                old, mid = self._two_tasks(store)
                newest = store.add_task("Meet Marc at 4pm", confidence=0.9,
                                        extracted_at=time.time())
                store.supersede_fact(mid, newest, time.time())
                # old -> mid is history (mid itself superseded): not offered,
                # not revertible. mid -> newest is the live decision.
                pairs = store.recent_supersessions()
                self.assertEqual([(p["old_id"], p["new_id"]) for p in pairs],
                                 [(mid, newest)])
                self.assertFalse(store.revert_supersession(old))
                self.assertTrue(store.revert_supersession(mid))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
