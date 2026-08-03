"""Entity details (Track B) — the person_attrs pattern generalized.

Contracts: mining is deterministic and receipt-carrying; user assertions win
and are re-minable claims; every merged field answers the Track B question —
what is believed, at what confidence, based on what, and is it stale.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.services import entity_details


def _fact(text, *, fid=1, review=None, conf=0.8, updated=None):
    return {"fact_id": fid, "text": text, "review": review,
            "confidence": conf, "updated_at": updated or time.time()}


class MineTests(unittest.TestCase):
    def test_mines_url_status_owner_location(self):
        facts = [
            _fact("Atlas is on hold until the fundraise closes", fid=1),
            _fact("Marc Chen leads the Atlas project", fid=2),
            _fact("Atlas docs live at https://atlas.dev/docs", fid=3),
            _fact("Atlas is based in Austin", fid=4),
        ]
        found = entity_details.mine("Atlas", [], facts)
        self.assertEqual(found["status"]["value"], "on hold")
        self.assertEqual(found["owner"]["value"], "Marc Chen")
        self.assertIn("https://atlas.dev/docs", found["url"]["value"])
        self.assertEqual(found["location"]["value"], "Austin")
        # Receipts: every mined value points at its backing fact.
        self.assertEqual(found["status"]["fact_id"], 1)
        self.assertEqual(found["owner"]["fact_id"], 2)

    def test_owner_requires_entity_anchor(self):
        # "Marc Chen leads the sales team" must not claim ownership of Atlas.
        found = entity_details.mine(
            "Atlas", [], [_fact("Marc Chen leads the sales team")])
        self.assertNotIn("owner", found)

    def test_reviewed_facts_beat_unreviewed(self):
        old = time.time() - 86400
        facts = [
            _fact("Atlas is blocked", fid=1, review=None, updated=time.time()),
            _fact("Atlas is shipped", fid=2, review="approved", updated=old),
        ]
        found = entity_details.mine("Atlas", [], facts)
        self.assertEqual(found["status"]["value"], "shipped")

    def test_claim_text_is_reminable(self):
        # The claim written on user edit must be recoverable by mine() — the
        # override table could be lost and mining rebuilds the value.
        for key, value in (("status", "on hold"), ("owner", "Marc Chen"),
                           ("url", "https://atlas.dev"), ("location", "Austin")):
            text = entity_details.claim_text(key, "Atlas", value)
            found = entity_details.mine("Atlas", [], [_fact(text)])
            self.assertIn(key, found, f"claim for {key!r} not re-minable: {text}")
            self.assertEqual(found[key]["value"], value)


class MergeTests(unittest.TestCase):
    def test_user_override_wins_with_full_epistemics(self):
        now = time.time()
        mined = {"status": {"value": "blocked", "fact_id": 9, "quote": "x",
                            "confidence": 0.6, "ts": now}}
        attrs = {"status": {"value": "on track", "fact_id": 12,
                            "updated_at": now}}
        out = entity_details.merge(mined, attrs, now=now)
        row = out["status"]
        self.assertEqual(row["value"], "on track")
        self.assertEqual(row["source"], "you")
        self.assertEqual(row["confidence"], 1.0)
        self.assertEqual(row["fact_id"], 12)
        self.assertFalse(row["stale"])

    def test_staleness_follows_decay_class(self):
        now = time.time()
        month_old = now - 30 * 86400
        mined = {
            "status": {"value": "live", "fact_id": 1, "quote": "q",
                       "confidence": 0.7, "ts": month_old},
            "url": {"value": "https://a.dev", "fact_id": 2, "quote": "q",
                    "confidence": 0.7, "ts": month_old},
        }
        out = entity_details.merge(mined, {}, now=now)
        self.assertTrue(out["status"]["stale"])   # 30d > 14d horizon
        self.assertFalse(out["url"]["stale"])     # 30d < 365d horizon
        self.assertEqual(out["status"]["freshness_days"], 14.0)


class StoreAttrTests(unittest.TestCase):
    def test_attr_roundtrip_supersede_and_forget_cleanup(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                eid = int(store.resolve_entity("Atlas", kind="project"))
                now = time.time()
                # First assertion: no previous claim to supersede.
                prev = store.set_entity_attr(eid, "status", "on hold", 101, now)
                self.assertIsNone(prev)
                # Re-edit returns the prior claim for supersession.
                prev = store.set_entity_attr(eid, "status", "shipped", 102, now)
                self.assertEqual(prev, 101)
                attrs = store.entity_attrs(eid)
                self.assertEqual(attrs["status"]["value"], "shipped")
                self.assertEqual(attrs["status"]["fact_id"], 102)
                # Clear returns the backing claim; the row is gone.
                fid = store.clear_entity_attr(eid, "status")
                self.assertEqual(fid, 102)
                self.assertEqual(store.entity_attrs(eid), {})
                # delete_entity sweeps any remaining attrs.
                store.set_entity_attr(eid, "url", "https://a.dev", None, now)
                store.delete_entity(eid)
                self.assertEqual(store.entity_attrs(eid), {})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
