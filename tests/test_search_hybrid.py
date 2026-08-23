"""WS-E — hybrid search union: exact identifiers can no longer be buried.

The failure this fixes is silent: `MemoryEngine.search` was vector-first, and
the keyword path only ran when the ANN index *errored*. So a product codename
or an unusual surname whose embedding sits far from the query simply never
surfaced, and there was no error to notice.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import memory as mem
from app.services.memory import FACT_ID_OFFSET, MemoryEngine
from app.storage import Store


class FakeVectors:
    """An index whose neighbours are deliberately wrong for exact queries.

    `hits` is what search() returns regardless of the query — the adversarial
    fixture: the embedding is far from the exact term, so vector-only retrieval
    must miss it and hybrid must not.
    """

    def __init__(self, hits=None, raises=None):
        self.hits = hits or []
        self.raises = raises
        self.queries = 0

    def search(self, _vec, k=10, modality=None):
        self.queries += 1
        if self.raises:
            raise self.raises
        return list(self.hits)[:k]

    def list_ids(self):
        return []


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_hyb_"))
        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": str(self.tmp),
                                           "QUILL_USAGE_LEDGER": "0"},
                              clear=False)
        self.env.start()
        self.store = Store(db_path=self.tmp / "quill.db",
                           audio_dir=self.tmp / "audio")
        self.engine = MemoryEngine(store=self.store)
        self.engine._semantic = False
        self.engine._embed = lambda text: [0.0] * 8   # never load a model

    def tearDown(self) -> None:
        self.env.stop()

    def add_event(self, raw: str, *, ts: float = 1_756_000_000.0) -> int:
        return self.store.insert(Event(time=ts, modality=Modality.AUDIO,
                                       raw=raw, summary=raw, source="test"))

    def add_fact(self, text: str, *, ts: float = 1_756_000_000.0) -> int:
        return self.store.add_claim(text, source_event_id=None,
                                    source_span=text, confidence=0.9,
                                    extracted_at=ts)

    def bind(self, vectors):
        self.engine._vectors = vectors
        self.engine._semantic = True
        return vectors

    def settings(self, **memory_fields):
        """Swap in a modified frozen Settings for the module under test.

        Deliberately not `importlib.reload(app.config)`: reloading builds a new
        `settings` object that every already-imported module keeps a stale
        reference to, which leaks across the whole test session.
        """
        import dataclasses
        from app.config import settings as real
        patched = dataclasses.replace(
            real, memory=dataclasses.replace(real.memory, **memory_fields))
        return patch.object(mem, "settings", patched)


class ExactMatchTests(_Base):
    def test_exact_identifier_surfaces_despite_a_far_embedding(self) -> None:
        """The adversarial case: ANN returns only unrelated neighbours."""
        target = self.add_fact("Renewal owner for capital-connect is Dana Iqbal")
        for i in range(6):
            self.add_fact(f"unrelated note about quarterly planning {i}",
                          ts=1_756_100_000.0 + i)
        # The index returns only the wrong things. Their scores sit in the
        # noise band a far-from-everything query produces — which is exactly
        # what the 0.55 floor is calibrated above.
        noise = [{"id": FACT_ID_OFFSET + fid, "score": 0.42, "time": 1_756_100_000.0}
                 for fid in range(target + 1, target + 6)]
        self.bind(FakeVectors(noise))

        vector_only_ids = {h["id"] - FACT_ID_OFFSET for h in noise}
        self.assertNotIn(target, vector_only_ids)   # the fixture is adversarial

        hits = self.engine.search("capital-connect", limit=5)
        self.assertIn(target, [h.get("fact_id") for h in hits])
        # And it leads: an exact match beats five mediocre neighbours.
        self.assertEqual(hits[0].get("fact_id"), target)

    def test_the_floor_is_a_survival_line_not_a_guarantee_of_rank(self) -> None:
        """Honest bound: enough genuinely strong semantic hits still win.

        With `limit` results all scoring above the floor, an exact hit ranks
        below them and can fall outside the cut. That is the intended trade —
        the floor exists so exact matches beat *weak* neighbours, not so they
        outrank real relevance. Raise QUILL_SEARCH_EXACT_FLOOR to shift it.
        """
        target = self.add_fact("capital-connect renewal owner is Dana")
        strong = [self.add_fact(f"strongly relevant note {i}",
                                ts=1_756_100_000.0 + i) for i in range(5)]
        self.bind(FakeVectors([{"id": FACT_ID_OFFSET + fid, "score": 0.90,
                                "time": 1_756_100_000.0} for fid in strong]))
        ids = [h.get("fact_id") for h in
               self.engine.search("capital-connect", limit=5)]
        self.assertNotIn(target, ids)
        # It is there the moment the caller asks for one more row.
        self.assertIn(target, [h.get("fact_id") for h in
                               self.engine.search("capital-connect", limit=6)])

    def test_unusual_surname_in_an_event_surfaces(self) -> None:
        eid = self.add_event("spoke with Anneliese Brzezinski about the renewal")
        for i in range(5):
            self.add_event(f"talked about renewals generally {i}",
                           ts=1_756_100_000.0 + i)
        self.bind(FakeVectors([
            {"id": eid + 1 + i, "score": 0.9, "time": 1_756_100_000.0}
            for i in range(4)]))
        hits = self.engine.search("Brzezinski", limit=5)
        self.assertTrue(any("Brzezinski" in (h.get("raw") or "") for h in hits))

    def test_exact_hit_enters_at_the_configured_floor(self) -> None:
        self.add_fact("the capital-connect renewal")
        self.bind(FakeVectors([]))
        hit = self.engine.search("capital-connect", limit=5)[0]
        self.assertAlmostEqual(hit["score"], 0.55, places=6)

    def test_floor_is_tunable(self) -> None:
        self.add_fact("the capital-connect renewal")
        self.bind(FakeVectors([]))
        with self.settings(exact_floor=0.31):
            hit = self.engine.search("capital-connect", limit=5)[0]
        self.assertAlmostEqual(hit["score"], 0.31, places=6)

    def test_a_strong_semantic_match_still_outranks_an_exact_one(self) -> None:
        """The floor lets exact hits survive the cut, not dominate."""
        strong = self.add_fact("Dana owns the renewal conversation")
        weak = self.add_fact("capital-connect is mentioned in passing")
        self.bind(FakeVectors([{"id": FACT_ID_OFFSET + strong, "score": 0.95,
                                "time": 1_756_000_000.0}]))
        hits = self.engine.search("capital-connect", limit=5)
        ids = [h.get("fact_id") for h in hits]
        self.assertEqual(ids[0], strong)
        self.assertIn(weak, ids)


class DedupeTests(_Base):
    def test_a_hit_found_both_ways_appears_once_at_the_max_score(self) -> None:
        fid = self.add_fact("capital-connect renewal owner is Dana")
        self.bind(FakeVectors([{"id": FACT_ID_OFFSET + fid, "score": 0.91,
                                "time": 1_756_000_000.0}]))
        hits = self.engine.search("capital-connect", limit=10)
        matching = [h for h in hits if h.get("fact_id") == fid]
        self.assertEqual(len(matching), 1)
        self.assertAlmostEqual(matching[0]["score"], 0.91, places=6)

    def test_the_lower_score_never_wins(self) -> None:
        """A weak ANN score must not drag an exact hit below the floor."""
        fid = self.add_fact("capital-connect renewal")
        self.bind(FakeVectors([{"id": FACT_ID_OFFSET + fid, "score": 0.10,
                                "time": 1_756_000_000.0}]))
        hit = [h for h in self.engine.search("capital-connect", limit=10)
               if h.get("fact_id") == fid][0]
        self.assertAlmostEqual(hit["score"], 0.55, places=6)

    def test_events_and_facts_with_the_same_id_do_not_collide(self) -> None:
        eid = self.add_event("capital-connect kickoff call")
        fid = self.add_fact("capital-connect renewal owner is Dana")
        self.bind(FakeVectors([]))
        hits = self.engine.search("capital-connect", limit=10)
        self.assertEqual(len(hits), 2)
        self.assertEqual({h.get("fact_id") for h in hits}, {fid, None})


class LifecycleTests(_Base):
    def test_dismissed_facts_do_not_leak_in_via_the_keyword_path(self) -> None:
        fid = self.add_fact("capital-connect renewal owner is Dana")
        self.store.review_fact(fid, "dismissed")
        self.bind(FakeVectors([]))
        hits = self.engine.search("capital-connect", limit=10)
        self.assertNotIn(fid, [h.get("fact_id") for h in hits])

    def test_archived_facts_do_not_leak_in_via_the_keyword_path(self) -> None:
        fid = self.add_fact("capital-connect renewal owner is Dana")
        self.store.archive_fact(fid, time.time())
        self.bind(FakeVectors([]))
        self.assertNotIn(fid, [h.get("fact_id") for h in
                               self.engine.search("capital-connect", limit=10)])

    def test_dismissed_facts_still_filtered_on_the_vector_path(self) -> None:
        fid = self.add_fact("capital-connect renewal owner is Dana")
        self.store.review_fact(fid, "dismissed")
        self.bind(FakeVectors([{"id": FACT_ID_OFFSET + fid, "score": 0.99,
                                "time": 1_756_000_000.0}]))
        self.assertEqual(self.engine.search("capital-connect", limit=10), [])


class ContributionCapTests(_Base):
    def test_a_pathological_like_match_cannot_flood_the_pool(self) -> None:
        for i in range(60):
            self.add_fact(f"a note number {i}", ts=1_756_000_000.0 + i)
        strong = self.add_fact("the important semantic result")
        self.bind(FakeVectors([{"id": FACT_ID_OFFSET + strong, "score": 0.99,
                                "time": 1_756_500_000.0}]))
        hits = self.engine.search("a", limit=5)
        self.assertEqual(len(hits), 5)
        # The semantic winner is still in there, not crowded out by 60 LIKEs.
        self.assertEqual(hits[0].get("fact_id"), strong)


class FallbackTests(_Base):
    def test_a_broken_index_still_returns_keyword_results(self) -> None:
        self.add_fact("capital-connect renewal owner is Dana")
        self.bind(FakeVectors(raises=RuntimeError("lance is on fire")))
        hits = self.engine.search("capital-connect", limit=5)
        self.assertEqual(len(hits), 1)

    def test_no_vector_store_at_all_still_searches(self) -> None:
        self.add_fact("capital-connect renewal owner is Dana")
        self.add_event("capital-connect kickoff")
        self.engine._vectors = None
        self.engine._semantic = False
        self.assertEqual(len(self.engine.search("capital-connect", limit=5)), 2)

    def test_hybrid_can_be_switched_off(self) -> None:
        """QUILL_SEARCH_HYBRID=0 restores vector-first-with-fallback."""
        self.add_fact("capital-connect renewal owner is Dana")
        other = self.add_fact("a different note entirely")
        self.bind(FakeVectors([{"id": FACT_ID_OFFSET + other, "score": 0.7,
                                "time": 1_756_000_000.0}]))
        with self.settings(hybrid=False):
            hits = self.engine.search("capital-connect", limit=5)
        self.assertEqual([h.get("fact_id") for h in hits], [other])

    def test_empty_query_still_returns_the_recent_timeline(self) -> None:
        self.engine._events = [Event(time=1.0, modality=Modality.AUDIO,
                                     raw="x", summary="x", source="t")]
        self.assertEqual(len(self.engine.search("   ", limit=5)), 1)


class LatencyTests(_Base):
    """Budget: hybrid <= vector-only + 15 ms on a 10k-event store."""

    def test_keyword_side_costs_under_15ms_on_10k_events(self) -> None:
        rows = [Event(time=1_756_000_000.0 + i, modality=Modality.AUDIO,
                      raw=f"utterance {i} about renewals and planning",
                      summary=f"u{i}", source="test") for i in range(10_000)]
        for ev in rows:
            self.store.insert(ev)
        self.bind(FakeVectors([]))

        # Warm the SQLite page cache so this measures the query, not first read.
        self.engine.search("utterance 9999", limit=20)

        best = None
        for _ in range(5):     # best-of-N: the machine may be busy
            t0 = time.perf_counter()
            self.engine.search("utterance 9999", limit=20)
            elapsed = (time.perf_counter() - t0) * 1000.0
            best = elapsed if best is None else min(best, elapsed)
        self.assertLess(best, 15.0, f"keyword side took {best:.1f} ms")


if __name__ == "__main__":
    unittest.main()
