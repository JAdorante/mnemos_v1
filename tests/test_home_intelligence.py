"""Important-people ranking — evidence quality over raw edge counts.

Regression tests for the home surface bug where the first 24 people rows (by
insertion order) were the only candidates, raw edge sums let ASR-noise
"people" outrank real contacts, and the user topped their own list."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import self_profile
from app.services.home_intelligence import person_score, rank_people
from app.storage import Store

NOW = 1_000_000_000.0
DAY = 86400.0


def _mention(fid, weight=1.0):
    return {"obj_type": "fact", "obj_id": fid, "predicate": "mentioned_in",
            "weight": weight}


def _typed(fid, pred="committed"):
    return {"obj_type": "fact", "obj_id": fid, "predicate": pred, "weight": 1.0}


class PersonScoreTests(unittest.TestCase):
    def test_typed_relationship_beats_mentions(self):
        typed = person_score([_typed(1)], NOW, NOW)
        mentions = person_score([_mention(1), _mention(2)], NOW, NOW)
        self.assertGreater(typed, mentions)

    def test_recency_decays_but_never_zeroes(self):
        edges = [_typed(1), _typed(2)]
        fresh = person_score(edges, NOW, NOW)
        old = person_score(edges, NOW - 120 * DAY, NOW)
        self.assertGreater(fresh, old)
        self.assertGreater(old, 0.0)
        self.assertGreater(old, fresh * 0.3)   # the 35% floor holds

    def test_single_stale_mention_is_below_floor(self):
        s = person_score([_mention(1)], NOW - 90 * DAY, NOW)
        self.assertLess(s, 1.0)

    def test_duplicate_edges_to_same_fact_count_once(self):
        one = person_score([_mention(1)], NOW, NOW)
        dup = person_score([_mention(1), _mention(1)], NOW, NOW)
        self.assertEqual(one, dup)

    def test_co_occurrence_is_capped(self):
        light = person_score([{"obj_type": "person", "obj_id": 9,
                               "predicate": "co_occurs", "weight": 10}], NOW, NOW)
        heavy = person_score([{"obj_type": "person", "obj_id": 9,
                               "predicate": "co_occurs", "weight": 500}], NOW, NOW)
        self.assertEqual(light, heavy)


class RankPeopleTests(unittest.TestCase):
    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def _person(self, name, ts=NOW):
        return self.store.resolve_person(name, ts=ts)

    def test_ranks_by_evidence_excludes_self_and_noise(self):
        with patch("app.services.identity.user_identity",
                   return_value={"name": "Test Person", "source": "profile"}):
            me = self_profile.self_person_id(self.store)
            ally = self._person("Alice Chen")
            noise = self._person("Dell", ts=NOW - 90 * DAY)
            f1 = self.store.add_task("send Alice the term sheet",
                                     extracted_at=NOW)
            f2 = self.store.add_claim("Alice runs platform at Foundry",
                                      extracted_at=NOW)
            self.store.add_relation("person", ally, "committed", "fact", f1,
                                    ts=NOW)
            self.store.add_relation("person", ally, "mentioned_in", "fact", f2,
                                    ts=NOW)
            self.store.add_relation("person", noise, "mentioned_in", "fact", f2,
                                    ts=NOW - 90 * DAY)
            self.store.add_relation("person", me, "committed", "fact", f1,
                                    ts=NOW)
            ranked = rank_people(self.store, now=NOW)
        names = [p["name"] for p in ranked]
        self.assertIn("Alice Chen", names)
        self.assertNotIn("Test Person", names)   # self never ranks itself
        self.assertNotIn("Dell", names)          # one stale mention: floored
        self.assertEqual(names[0], "Alice Chen")

    def test_no_insertion_order_cap(self):
        # 30 filler people first (old, weak), then a late-added strong contact:
        # under the old [:24] slice they could never appear.
        with patch("app.services.identity.user_identity", return_value={}):
            for i in range(30):
                pid = self._person(f"Filler Number{i}", ts=NOW - 200 * DAY)
                f = self.store.add_claim(f"filler {i}", extracted_at=NOW)
                self.store.add_relation("person", pid, "mentioned_in", "fact",
                                        f, ts=NOW - 200 * DAY)
            late = self._person("Hugh Salva")
            f = self.store.add_task("demo Mnemos with Hugh", extracted_at=NOW)
            self.store.add_relation("person", late, "committed", "fact", f,
                                    ts=NOW)
            ranked = rank_people(self.store, now=NOW)
        self.assertEqual(ranked[0]["name"], "Hugh Salva")


if __name__ == "__main__":
    unittest.main()
