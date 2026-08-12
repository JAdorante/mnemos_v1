"""People v3 P0/P1 — WS-F overlap dedup, WS-D alias rules, WS-E queue
hygiene, WS-G noise metrics.

Design principle under test: evidence must earn identity. These prove the
deterministic decision logic and storage lifecycle; models and the vector
index are never involved (dedup=False in gate configs, so only the
structural check runs).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import fact_gate
from app.services import people_pipeline as pp
from app.services import queue_hygiene
from app.services.fact_gate import gate_fact
from app.storage import Store

NOW = 1_700_000_000.0
DAY = 86400.0


def _cfg(**over):
    base = dict(min_conf=0.35, span_gate=True, dedup=False,
                auto_dup_sim=0.97, adjudicate_sim=0.72,
                recency_weight=0.08, recency_half_life_days=14.0,
                dedup_overlap=True, overlap_frac=0.5, overlap_token_sim=0.5,
                ttl_enabled=True, ttl_days=14.0, ttl_max_conf=0.7)
    base.update(over)
    return SimpleNamespace(facts=SimpleNamespace(**base))


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")


# --------------------------------------------------------------------------
# WS-F — span-overlap dedup at the fact gate
# --------------------------------------------------------------------------
class OverlapDedupTests(_StoreCase):
    def _existing_task(self, text="Send Marc the pricing deck",
                       lo=10, hi=20, conf=0.8) -> int:
        fid = self.store.add_task(text, source_event_id=lo,
                                  confidence=conf, extracted_at=NOW)
        if hi is not None:
            self.store.set_fact_event_hi(fid, hi)
        return fid

    def _gate(self, text, event_range, kind="task"):
        return gate_fact(kind, text, 0.8, "", "",
                         event_range=event_range, store=self.store)

    def test_rephrased_fact_from_overlapping_window_dedups(self):
        fid = self._existing_task()
        with patch.object(fact_gate, "settings", _cfg()):
            v = self._gate("I'll send the pricing deck to Marc", (15, 25))
        self.assertEqual(v.action, "dedup")
        self.assertEqual(v.dup_fact_id, fid)
        self.assertIn("event-range overlap", v.reason)

    def test_distinct_fact_from_same_window_survives(self):
        # Two DIFFERENT facts born from one turn share an identical event
        # range — the token-similarity guard must keep them apart.
        self._existing_task()
        with patch.object(fact_gate, "settings", _cfg()):
            v = self._gate("Book the conference room for Thursday", (10, 20))
        self.assertEqual(v.action, "insert")

    def test_small_overlap_is_not_a_duplicate(self):
        self._existing_task(lo=10, hi=20)
        with patch.object(fact_gate, "settings", _cfg()):
            v = self._gate("Send Marc the pricing deck", (20, 40))
        self.assertEqual(v.action, "insert")

    def test_legacy_single_event_row_inside_window_dedups(self):
        fid = self._existing_task(lo=12, hi=None)  # pre-migration provenance
        with patch.object(fact_gate, "settings", _cfg()):
            v = self._gate("Marc needs the pricing deck sent over", (10, 20))
        self.assertEqual(v.action, "dedup")
        self.assertEqual(v.dup_fact_id, fid)

    def test_flag_off_keeps_todays_behavior(self):
        self._existing_task()
        with patch.object(fact_gate, "settings", _cfg(dedup_overlap=False)):
            v = self._gate("I'll send the pricing deck to Marc", (15, 25))
        self.assertEqual(v.action, "insert")

    def test_different_kind_never_collides(self):
        self._existing_task()
        with patch.object(fact_gate, "settings", _cfg()):
            v = self._gate("I'll send the pricing deck to Marc", (15, 25),
                           kind="commitment")
        self.assertEqual(v.action, "insert")

    def test_archived_fact_is_not_a_dedup_target(self):
        fid = self._existing_task()
        self.store.archive_fact(fid, NOW + 1)
        with patch.object(fact_gate, "settings", _cfg()):
            v = self._gate("I'll send the pricing deck to Marc", (15, 25))
        self.assertEqual(v.action, "insert")

    def test_old_test_configs_without_new_fields_still_work(self):
        # Older suites patch settings with a namespace that predates the
        # WS-F fields — the gate must default them off, not crash.
        legacy = SimpleNamespace(facts=SimpleNamespace(
            min_conf=0.35, span_gate=True, dedup=False,
            auto_dup_sim=0.97, adjudicate_sim=0.72))
        self._existing_task()
        with patch.object(fact_gate, "settings", legacy):
            v = self._gate("I'll send the pricing deck to Marc", (15, 25))
        self.assertEqual(v.action, "insert")


# --------------------------------------------------------------------------
# WS-D — alias rules: merges become training data
# --------------------------------------------------------------------------
class AliasRuleTests(_StoreCase):
    def test_add_and_lookup_roundtrip(self):
        pid = self.store.insert_person("Justin Adorante", ts=NOW)
        self.assertTrue(self.store.add_alias_rule(pid, "Justin", "positive",
                                                  created_by="test", ts=NOW))
        # duplicate is a no-op, lookup is case-insensitive
        self.assertFalse(self.store.add_alias_rule(pid, "justin", "positive"))
        rules = self.store.alias_rules_for("JUSTIN")
        self.assertEqual([(r["person_id"], r["kind"]) for r in rules],
                         [(pid, "positive")])

    def test_soft_merge_writes_positive_aliases_for_absorbed_spellings(self):
        survivor = self.store.insert_person("Justin Adorante", ts=NOW)
        absorbed = self.store.insert_person("justin", ts=NOW)
        self.store.soft_merge_people(survivor, absorbed, reason="dup", ts=NOW)
        rules = self.store.alias_rules_for("justin")
        self.assertIn((survivor, "positive"),
                      [(r["person_id"], r["kind"]) for r in rules])

    def test_follow_canonical_resolves_absorbed_chain(self):
        survivor = self.store.insert_person("Justin Adorante", ts=NOW)
        absorbed = self.store.insert_person("justin", ts=NOW)
        self.store.soft_merge_people(survivor, absorbed, ts=NOW)
        self.assertEqual(self.store.follow_canonical_person(absorbed), survivor)
        self.assertEqual(self.store.follow_canonical_person(survivor), survivor)

    def test_merged_alias_rebinds_instead_of_resplitting(self):
        # The historical justin scenario: after ONE manual merge, the same
        # mention auto-binds via the positive alias — never a duplicate node.
        survivor = self.store.insert_person("Justin Adorante", ts=NOW)
        absorbed = self.store.insert_person("justin", ts=NOW)
        self.store.soft_merge_people(survivor, absorbed, ts=NOW)
        res = pp.resolve_person_mention(
            "justin", store=self.store, event_source="audio.whisper",
            text="justin should review the deck", now=NOW,
            relationship_boost=0.85)
        self.assertEqual(res.decision, "auto_resolve")
        self.assertEqual(res.person_id, survivor)
        self.assertGreaterEqual(res.confidence, 0.99)

    def test_negative_rule_bans_the_bind(self):
        pid = self.store.insert_person("Marc Chen", ts=NOW)
        self.store.add_alias_rule(pid, "Marc Chen", "negative",
                                  created_by="test", ts=NOW)
        res = pp.resolve_person_mention(
            "Marc Chen", store=self.store, event_source="audio.whisper",
            text="Marc Chen from the podcast said pricing is broken", now=NOW,
            relationship_boost=0.85)
        # Whatever else happens (mint, leave_open), it must not be pid.
        self.assertNotEqual(res.person_id, pid)


# --------------------------------------------------------------------------
# WS-E — review-queue TTL + SLO
# --------------------------------------------------------------------------
class QueueHygieneTests(_StoreCase):
    def _event(self, source: str, ts: float) -> int:
        from app.events import Event, Modality
        modality = Modality.AUDIO if source.startswith("audio") else Modality.VISION
        return self.store.insert(Event(time=ts, modality=modality,
                                       raw="raw", source=source))

    def _ambient_claim(self, ts: float, conf: float = 0.4,
                       source: str = "desktop.screen") -> int:
        eid = self._event(source, ts)
        return self.store.add_claim("some screen-mined claim",
                                    source_event_id=eid, confidence=conf,
                                    extracted_at=ts)

    def test_stale_unreferenced_ambient_fact_archives(self):
        fid = self._ambient_claim(NOW - 20 * DAY)
        with patch.object(queue_hygiene, "settings", _cfg()):
            report = queue_hygiene.sweep_ttl(self.store, now=NOW)
        self.assertEqual(report["archived"], 1)
        self.assertEqual(self.store.get_fact(fid)["state"], "archived")

    def test_sweep_is_idempotent(self):
        self._ambient_claim(NOW - 20 * DAY)
        with patch.object(queue_hygiene, "settings", _cfg()):
            queue_hygiene.sweep_ttl(self.store, now=NOW)
            second = queue_hygiene.sweep_ttl(self.store, now=NOW)
        self.assertEqual(second["archived"], 0)

    def test_speech_facts_are_exempt(self):
        fid = self._ambient_claim(NOW - 20 * DAY, source="audio.whisper")
        with patch.object(queue_hygiene, "settings", _cfg()):
            queue_hygiene.sweep_ttl(self.store, now=NOW)
        self.assertEqual(self.store.get_fact(fid)["state"], "active")

    def test_reviewed_facts_are_exempt(self):
        fid = self._ambient_claim(NOW - 20 * DAY)
        self.store.review_fact(fid, "approved")
        with patch.object(queue_hygiene, "settings", _cfg()):
            queue_hygiene.sweep_ttl(self.store, now=NOW)
        self.assertEqual(self.store.get_fact(fid)["state"], "active")

    def test_referenced_facts_are_exempt(self):
        # A grounding hit = the fact earned its keep; TTL must skip it.
        fid = self._ambient_claim(NOW - 20 * DAY)
        self.store.add_attention_impressions([
            {"node_type": "fact", "node_id": fid, "surface": "grounding"}])
        with patch.object(queue_hygiene, "settings", _cfg()):
            queue_hygiene.sweep_ttl(self.store, now=NOW)
        self.assertEqual(self.store.get_fact(fid)["state"], "active")

    def test_young_and_confident_facts_are_exempt(self):
        young = self._ambient_claim(NOW - 2 * DAY)
        confident = self._ambient_claim(NOW - 20 * DAY, conf=0.9)
        with patch.object(queue_hygiene, "settings", _cfg()):
            queue_hygiene.sweep_ttl(self.store, now=NOW)
        self.assertEqual(self.store.get_fact(young)["state"], "active")
        self.assertEqual(self.store.get_fact(confident)["state"], "active")

    def test_queue_slo_counts_unreviewed_depth_and_age(self):
        self._ambient_claim(NOW - 1.5 * DAY)
        self._ambient_claim(NOW - 0.5 * DAY)
        reviewed = self._ambient_claim(NOW - 9 * DAY)
        self.store.review_fact(reviewed, "approved")
        slo = queue_hygiene.queue_slo(self.store, now=NOW)
        self.assertEqual(slo["depth"], 2)   # the reviewed fact left the queue
        self.assertAlmostEqual(slo["age_p50_s"], 1.5 * DAY)
        self.assertTrue(slo["ok"])          # p50 36h < 48h target

    def test_queue_slo_flags_a_stale_queue(self):
        for d in (3, 4, 5):
            self._ambient_claim(NOW - d * DAY)
        slo = queue_hygiene.queue_slo(self.store, now=NOW)
        self.assertFalse(slo["ok"])         # p50 4d > 48h target

    def test_archived_ttl_fact_leaves_grounding_but_stays_searchable(self):
        fid = self._ambient_claim(NOW - 20 * DAY)
        with patch.object(queue_hygiene, "settings", _cfg()):
            queue_hygiene.sweep_ttl(self.store, now=NOW)
        from app.services.memory import fact_is_retrievable
        self.assertFalse(fact_is_retrievable(self.store.get_fact(fid)))


# --------------------------------------------------------------------------
# WS-G — noise metrics
# --------------------------------------------------------------------------
class NoiseMetricTests(unittest.TestCase):
    def test_junk_mint_rate(self):
        from app.services import people_noise_metrics as nm
        self.assertEqual(nm.junk_mint_rate(2, 2.0), 1.0)
        self.assertEqual(nm.junk_mint_rate(0, 0.0), 0.0)

    def test_wrong_owner_counts_wrong_not_missed(self):
        from app.services import people_noise_metrics as nm
        assignments = [
            ("Sarah Chen", "Sarah Chen"),   # right
            ("Marcus Webb", None),          # missed — recoverable, not wrong
            ("Justin Adorante", "justin"),  # wrong person
            (None, "Anyone"),               # golden has no owner — skipped
        ]
        self.assertAlmostEqual(nm.wrong_owner_rate(assignments), 1 / 3)

    def test_person_score_terms_matches_person_score(self):
        from app.services.home_intelligence import (person_score,
                                                    person_score_terms)
        edges = ([{"obj_type": "fact", "obj_id": i, "predicate": "owns"}
                  for i in range(3)]
                 + [{"obj_type": "fact", "obj_id": 100 + i,
                     "predicate": "mentioned_in"} for i in range(5)]
                 + [{"obj_type": "person", "obj_id": 1,
                     "predicate": "co_occurs", "weight": 4.0},
                    {"obj_type": "entity", "obj_id": 7, "origin": "asserted"}])
        t = person_score_terms(edges, NOW - 3 * DAY, NOW)
        self.assertAlmostEqual(t["score"], person_score(edges, NOW - 3 * DAY, NOW))
        # base = 3*3 typed + 5 mentions + 0.5*4 co + 2 asserted = 18
        self.assertAlmostEqual(t["base"], 18.0)
        self.assertAlmostEqual(t["mention_share"], 5 / 18)

    def test_mention_volume_dominates_v1_top10(self):
        # The corpus-level assertion behind WS-B: under the v1 formula a
        # never-typed, heavily-mentioned person outranks a real contact.
        from app.services import people_noise_metrics as nm
        pete = [{"obj_type": "fact", "obj_id": i, "predicate": "mentioned_in"}
                for i in range(25)]
        sarah = ([{"obj_type": "fact", "obj_id": 500 + i, "predicate": "owns"}
                  for i in range(4)]
                 + [{"obj_type": "fact", "obj_id": 600, "predicate": "mentioned_in"}])
        shares = nm.top10_mention_shares(
            [("Podcast Pete", pete, NOW - DAY), ("Sarah Chen", sarah, NOW - DAY)],
            NOW)
        self.assertEqual(shares[0][0], "Podcast Pete")
        self.assertGreater(shares[0][2], nm.MENTION_SHARE_MAX)

    def test_gate_report(self):
        from app.services import people_noise_metrics as nm
        good = nm.gate_report(junk_rate=0.0, doc_mints=0, wrong_rate=0.0,
                              shares=[("a", 10.0, 0.1)])
        self.assertTrue(good["ok"])
        bad = nm.gate_report(junk_rate=1.0, doc_mints=1, wrong_rate=0.1,
                             shares=[("a", 10.0, 0.9)])
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["junk_mint_ok"])
        self.assertFalse(bad["doc_mint_ok"])
        self.assertFalse(bad["wrong_owner_ok"])
        self.assertFalse(bad["mention_share_ok"])


if __name__ == "__main__":
    unittest.main()
