"""CAL Stage 1 — cached binding lookup, `associate()`, and the decision log.

The properties here are the ones that make the economics work: the common case
touches no model and no scorer, a stale cache can cost money but never
correctness, capture never waits on inference, and every attribution stores the
reasoning that produced it.
"""
from __future__ import annotations

import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from app.events import Event, Modality
from app.services.context import bindings as b
from app.services.context import keys as k
from app.services.context import pipeline as pl
from app.services.context import resolver as rs
from app.storage import Store


def _ev(raw="", window="", **meta):
    m = {"window": window} if window else {}
    m.update(meta)
    return Event(time=1_700_000_000.0, modality=Modality.VISION, raw=raw,
                 summary=raw[:40], source="desktop.screen", meta=m)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_pipe_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "a")
        self.eid = self.store.resolve_entity("Ravenry", "project", ts=time.time())
        self.cache = b.BindingCache(self.store)

    def bind_repo(self):
        self.cache.mint("entity", self.eid, "repo", "github.com/x/rav",
                        strength=0.95, key_class="identity")


class BindingCacheTests(_Case):
    def test_a_mint_is_visible_immediately(self) -> None:
        """The ratchet must not leave a stale miss that re-escalates the thing
        it just learned."""
        sk = k.remote("git@github.com:x/rav.git")
        self.assertEqual(self.cache.anchors([sk]), [])
        self.cache.lookup("repo", "github.com/x/rav")      # warm a miss
        self.bind_repo()
        got = self.cache.anchors([sk])
        self.assertEqual([(a.node_type, a.node_id) for a in got],
                         [("entity", self.eid)])

    def test_invalidation_is_scoped(self) -> None:
        self.bind_repo()
        self.cache.lookup("repo", "github.com/x/rav")
        self.cache.lookup("path", "/tmp/x")
        self.assertEqual(self.cache.invalidate("path", "/tmp/x"), 1)
        self.assertEqual(self.cache.stats["size"], 1)
        self.cache.invalidate()
        self.assertEqual(self.cache.stats["size"], 0)

    def test_entries_expire(self) -> None:
        """A cache with no expiry is a second source of truth nobody
        reconciles."""
        c = b.BindingCache(self.store, ttl_s=10.0)
        c.lookup("repo", "github.com/x/rav", now=100.0)
        self.bind_repo()
        self.assertEqual(c.lookup("repo", "github.com/x/rav", now=105.0), [],
                         "still inside the TTL — a stale miss, which is safe")
        self.assertTrue(c.lookup("repo", "github.com/x/rav", now=200.0))

    def test_unbindable_keys_do_not_resolve(self) -> None:
        """A path seen once is an observation, not yet a meaning."""
        self.cache.mint("entity", self.eid, "path", "/home/x/dev/rav",
                        strength=0.6, key_class="convention")
        sk = k.path("/home/x/dev/rav", resolve=False)
        self.assertEqual(self.cache.anchors([sk]), [])
        self.assertTrue(self.cache.anchors([sk], bindable_only=False))

    def test_lru_evicts_and_reports(self) -> None:
        c = b.BindingCache(self.store, maxsize=2)
        for i in range(5):
            c.lookup("path", f"/p{i}")
        self.assertLessEqual(c.stats["size"], 2)
        self.assertGreaterEqual(c.stats["evictions"], 3)


class AssociateTests(_Case):
    def test_a_bound_identifier_exits_without_a_scorer(self) -> None:
        self.bind_repo()
        ev = _ev(raw="see https://github.com/x/rav/pull/3", window="Cursor")
        row = self.store.insert(ev)
        res = pl.associate(ev, self.store, event_id=row, cache=self.cache)
        self.assertEqual(res.band, "deterministic")
        self.assertIsNone(res.decision, "the scorer never ran")
        self.assertEqual(res.reason, "single_strong_key")

    def test_the_attribution_is_persisted(self) -> None:
        self.bind_repo()
        ev = _ev(raw="https://github.com/x/rav/issues/1")
        row = self.store.insert(ev)
        pl.associate(ev, self.store, event_id=row, cache=self.cache)
        got = self.store.context_for_event(row)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["node_id"], str(self.eid))
        self.assertEqual(got[0]["method"], "key")
        self.assertEqual(got[0]["shadow"], 1, "shadow unless asked otherwise")

    def test_two_bound_repos_do_not_take_the_fast_exit(self) -> None:
        self.bind_repo()
        other = self.store.resolve_entity("Tea Leaf", "project", ts=time.time())
        self.cache.mint("entity", other, "repo", "github.com/x/tea",
                        strength=0.95, key_class="identity")
        ev = _ev(raw="https://github.com/x/rav/1 and https://github.com/x/tea/2")
        row = self.store.insert(ev)
        res = pl.associate(ev, self.store, event_id=row, cache=self.cache)
        self.assertNotEqual(res.band, "deterministic")
        self.assertIsNotNone(res.decision)

    def test_nothing_bound_is_honestly_unbound(self) -> None:
        ev = _ev(raw="just some prose with no identifiers in it at all")
        row = self.store.insert(ev)
        res = pl.associate(ev, self.store, event_id=row, cache=self.cache)
        self.assertEqual(res.band, "unbound")
        self.assertEqual(self.store.context_for_event(row), [])

    def test_associate_never_calls_a_model(self) -> None:
        """Capture must not wait on inference. An ambiguous event is written
        `pending` and handed to a caller running elsewhere."""
        self.bind_repo()
        other = self.store.resolve_entity("Tea Leaf", "project", ts=time.time())
        self.cache.mint("entity", other, "repo", "github.com/x/rav",
                        strength=0.93, key_class="identity")
        ev = _ev(raw="https://github.com/x/rav/pull/9")
        row = self.store.insert(ev)
        import app.services.model_router as mr
        with unittest.mock.patch.object(mr, "router") as router:
            res = pl.associate(ev, self.store, event_id=row, cache=self.cache)
            router.complete_json.assert_not_called()
        self.assertIn(res.band, ("pending", "scored", "scored_multi",
                                 "provisional"))

    def test_an_excluded_event_is_not_attributed_at_all(self) -> None:
        """Not 'attributed but hidden' — an event that never becomes a binding
        cannot leak through one later."""
        ev = _ev(raw="https://github.com/x/rav/1")
        row = self.store.insert(ev)
        with unittest.mock.patch.object(pl, "_privacy_class",
                                        return_value="never_send"):
            res = pl.associate(ev, self.store, event_id=row, cache=self.cache)
        self.assertEqual(res.band, "excluded")
        self.assertEqual(self.store.context_for_event(row), [])
        self.assertIsNone(self.store.decision_for_event(row))

    def test_the_deterministic_path_is_inside_its_latency_budget(self) -> None:
        self.bind_repo()
        ev = _ev(raw="https://github.com/x/rav/pull/3")
        row = self.store.insert(ev)
        pl.associate(ev, self.store, event_id=row, cache=self.cache)  # warm
        t0 = time.perf_counter()
        for _ in range(50):
            pl.associate(ev, self.store, event_id=row, cache=self.cache,
                         persist=False)
        per_ms = (time.perf_counter() - t0) / 50 * 1000
        self.assertLess(per_ms, 25.0, f"{per_ms:.1f} ms exceeds the budget")


class CandidateTests(unittest.TestCase):
    def test_supporting_evidence_cannot_propose(self) -> None:
        """Frame weight attaches to candidates that exist; it never makes one."""
        got = pl.candidates([], (), frame_weights={("entity", 7): 0.9})
        self.assertEqual(got, [])

    def test_frame_weight_attaches_to_a_proposed_candidate(self) -> None:
        from app.services.context.frames import Anchor
        got = pl.candidates([Anchor("entity", 7, "R", 0.9, "strong")], (),
                            frame_weights={("entity", 7): 0.5})
        self.assertEqual(got[0].features["f_frame"], 0.5)
        self.assertIn("f_key", got[0].features)

    def test_corroboration_counts_distinct_keys(self) -> None:
        from app.services.context.frames import Anchor
        one = pl.candidates([Anchor("entity", 7, "a", 0.9, "strong")])
        two = pl.candidates([Anchor("entity", 7, "a", 0.9, "strong"),
                             Anchor("entity", 7, "b", 0.9, "strong")])
        self.assertGreater(two[0].features["f_key_n"], one[0].features["f_key_n"])


class DecisionLogTests(_Case):
    def test_the_reasoning_is_stored_not_just_the_outcome(self) -> None:
        """explain_predicate renders a belief's history; nothing rendered an
        attribution's."""
        d = rs.decide(rs.score([
            rs.Candidate("entity", 1, "A", {"f_key": 0.8}, frozenset({"z"})),
            rs.Candidate("entity", 2, "B", {"f_key": 0.78}, frozenset({"z"}))]))
        did = self.store.record_context_decision(41882, d, latency_ms=3.1,
                                                 weights_version=1)
        got = self.store.decision_for_event(41882)
        self.assertEqual(got["id"], did)
        self.assertEqual(got["band"], "pending")
        self.assertEqual(len(got["candidates"]), 2)
        self.assertIn("f_key", got["candidates"][0]["features"])
        self.assertEqual(got["weights_version"], 1)

    def test_band_stats_flag_overconfidence(self) -> None:
        for band_events, band in ((3, "deterministic"), (1, "unbound")):
            for i in range(band_events):
                d = rs.Decision(band, (), 0.0, 0.0, ())
                self.store.record_context_decision(1000 + i + hash(band) % 100,
                                                   d)
        st = self.store.context_band_stats()
        self.assertEqual(st["total"], 4)
        self.assertGreater(st["unbound_rate"], 0.0)

    def test_events_for_node_walks_back(self) -> None:
        self.bind_repo()
        for i in range(3):
            ev = _ev(raw="https://github.com/x/rav/pull/%d" % i)
            pl.associate(ev, self.store, event_id=self.store.insert(ev),
                         cache=self.cache)
        self.assertEqual(len(self.store.events_for_node("entity", self.eid)), 3)


class EscalationHandoffTests(_Case):
    def test_a_deferred_escalation_upgrades_the_event(self) -> None:
        self.bind_repo()
        other = self.store.resolve_entity("Tea Leaf", "project", ts=time.time())
        self.cache.mint("entity", other, "repo", "github.com/x/rav",
                        strength=0.93, key_class="identity")
        ev = _ev(raw="https://github.com/x/rav/pull/9")
        row = self.store.insert(ev)
        res = pl.associate(ev, self.store, event_id=row, cache=self.cache)
        if not res.needs_escalation:
            self.skipTest("scorer separated them; nothing to escalate")
        got = pl.escalate_pending(self.store, res, summary="reviewing a PR",
                                  ask=lambda s, m, t: {"choice": 0,
                                                       "confidence": 0.9})
        self.assertIsNotNone(got.chosen)
        methods = {r["method"] for r in self.store.context_for_event(row)}
        self.assertIn("escalated", methods)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
