"""People v3 WS-B — connection score v2 + shadow harness.

Design principle under test: mentions corroborate a relationship, they
cannot BE one. Damping, the dialogue-partner term, and provenance weights
are pure functions over edge dicts (config injected); the loader fails
closed; the shadow job records comparison rows and gates cutover on 7
consecutive clean nightlies.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import people_noise_metrics as nm
from app.services import score_v2
from app.services.home_intelligence import person_score, person_score_terms
from app.storage import Store

NOW = 1_700_000_000.0
DAY = 86400.0

CFG = {
    "version": "test",
    "weights": {"typed": 3.0, "mention": 1.0, "cooccur": 0.5,
                "asserted": 2.0, "dialogue": 2.5},
    "provenance": {"asserted": 1.0, "document": 0.7, "asr": 0.5,
                   "unknown": 0.6},
    "mention_cap_ratio": 0.4,
    "recency_half_life_days": 30.0,
    "recency_floor": 0.35,
    "score_floor": 1.0,
    "top_n": 12,
}


def _mentions(n: int, source: str = "audio.whisper") -> list[dict]:
    return [{"obj_type": "fact", "obj_id": 10_000 + i,
             "predicate": "mentioned_in", "source": source}
            for i in range(n)]


def _typed(n: int, source: str = "audio.whisper") -> list[dict]:
    return [{"obj_type": "fact", "obj_id": i, "predicate": "owns",
             "source": source}
            for i in range(n)]


def _v2(edges, *, last_seen=NOW, dialogue=0.0):
    return score_v2.person_score_v2_terms(
        edges, last_seen, NOW, dialogue_turns=dialogue, cfg=CFG)


class DampingTests(unittest.TestCase):
    def test_damping_damps_mentions(self):
        t10 = _v2(_typed(3) + _mentions(10))
        t100 = _v2(_typed(3) + _mentions(100))
        # 10x the mention volume buys far less than 10x the mention evidence.
        self.assertGreater(t100["mention_raw"], t10["mention_raw"])
        self.assertLess(t100["mention_raw"], 3 * t10["mention_raw"])

    def test_damping_damps_typed_volume(self):
        one = _v2(_typed(1, source="onboarding"))
        sixteen = _v2(_typed(16, source="onboarding"))
        self.assertGreater(sixteen["terms"]["typed"], one["terms"]["typed"])
        self.assertLess(sixteen["terms"]["typed"], 5 * one["terms"]["typed"])

    def test_one_unit_scores_like_v1(self):
        # damp() is normalized: ONE user-asserted typed fact = v1's 3.0.
        t = _v2(_typed(1, source="onboarding"))
        self.assertAlmostEqual(t["terms"]["typed"], 3.0, places=9)


class DialogueTests(unittest.TestCase):
    def test_partner_beats_mention_only_loudmouth(self):
        partner = _typed(0) + _mentions(3)
        loudmouth = _mentions(30)
        # v1: volume wins.
        self.assertGreater(person_score(loudmouth, NOW, NOW),
                           person_score(partner, NOW, NOW))
        # v2: the person the user actually talks WITH wins.
        p = _v2(partner, dialogue=10)
        l = _v2(loudmouth, dialogue=0)
        self.assertGreater(p["score"], l["score"])

    def test_mention_only_scores_zero(self):
        t = _v2(_mentions(50))
        self.assertEqual(t["base"], 0.0)
        self.assertEqual(t["score"], 0.0)

    def test_dialogue_is_positive_evidence(self):
        quiet = _v2(_typed(2))
        talks = _v2(_typed(2), dialogue=8)
        self.assertGreater(talks["score"], quiet["score"])
        self.assertGreater(talks["terms"]["dialogue"], 0.0)


class ProvenanceTests(unittest.TestCase):
    def test_provenance_orders_asserted_document_asr(self):
        asserted = _v2(_typed(5, source="onboarding"))["score"]
        doc = _v2(_typed(5, source="documents.scan"))["score"]
        asr = _v2(_typed(5, source="audio.whisper"))["score"]
        self.assertGreater(asserted, doc)
        self.assertGreater(doc, asr)

    def test_asserted_origin_wins_over_source(self):
        e = {"obj_type": "fact", "obj_id": 1, "predicate": "owns",
             "origin": "asserted", "source": "audio.whisper"}
        self.assertEqual(score_v2.provenance_class(e), "asserted")
        self.assertEqual(
            score_v2.provenance_class({"source": "desktop.screen"}),
            "document")
        self.assertEqual(score_v2.provenance_class({}), "unknown")

    def test_mention_cap_bounds_share_under_gate(self):
        edges = [{"obj_type": "person", "obj_id": 1, "predicate": "co_occurs",
                  "weight": 4.0}] + _mentions(25)
        t = _v2(edges)
        self.assertLessEqual(t["mention_share"], nm.MENTION_SHARE_MAX)
        # The raw (uncapped) mention evidence really was dominant.
        self.assertGreater(t["mention_raw"], t["terms"]["mentions"])

    def test_recency_prefers_fresh(self):
        fresh = _v2(_typed(3), last_seen=NOW)
        stale = _v2(_typed(3), last_seen=NOW - 60 * DAY)
        self.assertGreater(fresh["score"], stale["score"])


class V1TermsApiUnbroken(unittest.TestCase):
    """The noise evals depend on person_score_terms — exact v1 numbers."""

    def test_v1_decomposition_unchanged(self):
        edges = ([{"obj_type": "fact", "obj_id": i, "predicate": "owns"}
                  for i in range(2)]
                 + [{"obj_type": "fact", "obj_id": 100 + i,
                     "predicate": "mentioned_in"} for i in range(3)])
        t = person_score_terms(edges, NOW, NOW)
        self.assertEqual(t["terms"]["typed"], 6.0)
        self.assertEqual(t["terms"]["mentions"], 3.0)
        self.assertEqual(t["base"], 9.0)
        self.assertAlmostEqual(t["mention_share"], 3.0 / 9.0)
        self.assertAlmostEqual(person_score(edges, NOW, NOW), t["score"])


class _ConfigPathCase(unittest.TestCase):
    """Point the loader at a controlled path; always restore + clear cache."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = score_v2._CONFIG_PATH
        score_v2._raw_config.cache_clear()

    def tearDown(self):
        score_v2._CONFIG_PATH = self._orig
        score_v2._raw_config.cache_clear()

    def _point_at(self, path: Path):
        score_v2._CONFIG_PATH = path
        score_v2._raw_config.cache_clear()


class FailClosedLoaderTests(_ConfigPathCase):
    def test_missing_file_fails_closed(self):
        self._point_at(self.tmp / "nope.json")
        self.assertFalse(score_v2.config_loaded())
        self.assertEqual(score_v2.config_version(), "not-loaded")
        self.assertFalse(score_v2.health()["config_loaded"])
        with self.assertRaises(score_v2.ScoreV2NotReady):
            score_v2.person_score_v2_terms(_mentions(1), NOW, NOW)

    def test_corrupt_json_fails_closed(self):
        p = self.tmp / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        self._point_at(p)
        self.assertFalse(score_v2.config_loaded())

    def test_missing_required_keys_fail_closed(self):
        p = self.tmp / "partial.json"
        cfg = json.loads(json.dumps(CFG))
        del cfg["weights"]["dialogue"]
        p.write_text(json.dumps(cfg), encoding="utf-8")
        self._point_at(p)
        self.assertFalse(score_v2.config_loaded())

    def test_shipped_config_loads(self):
        self.assertTrue(score_v2.config_loaded())
        self.assertNotEqual(score_v2.config_version(), "not-loaded")
        self.assertTrue(score_v2.health()["config_loaded"])


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")
        # This machine may have a real onboarding identity; the self-node
        # lookup must never mint the user into the throwaway store.
        self._sp = patch("app.services.self_profile.self_person_id",
                         lambda store=None: None)
        self._sp.start()

    def tearDown(self):
        self._sp.stop()
        try:
            self.store._conn.close()
        except Exception:
            pass

    # -- seed helpers ------------------------------------------------------
    def _person(self, name: str) -> int:
        return self.store.insert_person(name, ts=NOW)

    def _turn(self, speaker: str, n: int = 1):
        with self.store._lock:
            for i in range(n):
                self.store._conn.execute(
                    "INSERT INTO turns (start, end, speaker, text) "
                    "VALUES (?, ?, ?, ?)",
                    (NOW + i, NOW + i + 5, speaker, "hello"))
            self.store._conn.commit()

    def _event(self, source: str) -> int:
        with self.store._lock:
            cur = self.store._conn.execute(
                "INSERT INTO events (time, modality, raw, source) "
                "VALUES (?, 'audio', 'x', ?)", (NOW, source))
            self.store._conn.commit()
            return int(cur.lastrowid)

    def _report_count(self) -> int:
        with self.store._lock:
            return int(self.store._conn.execute(
                "SELECT COUNT(*) FROM score_shadow_reports").fetchone()[0])

    def _insert_report(self, clean: bool, ts: float):
        with self.store._lock:
            self.store._conn.execute(
                "INSERT INTO score_shadow_reports (ts, clean, report_json) "
                "VALUES (?, ?, ?)", (ts, int(clean), "{}"))
            self.store._conn.commit()


class EvidenceCollectionTests(_StoreCase):
    def test_dialogue_turn_counts_match_speaker_labels(self):
        sid = self._person("Sarah Chen")
        self.store.touch_person(sid, NOW, alias="Sarah")
        pid = self._person("Podcast Pete")
        self._turn("Sarah Chen", 3)
        self._turn("sarah", 2)     # alias, case-insensitive
        self._turn("user", 9)      # bookkeeping label, never a partner
        counts = score_v2.dialogue_turn_counts(self.store)
        self.assertEqual(counts[sid], 5)
        self.assertEqual(counts[pid], 0)

    def test_annotate_edge_sources(self):
        ev = self._event("documents.scan")
        edges = [{"obj_type": "fact", "obj_id": 1,
                  "predicate": "mentioned_in", "source_event_id": ev}]
        out = score_v2.annotate_edge_sources(self.store, edges)
        self.assertEqual(out[0]["source"], "documents.scan")
        self.assertEqual(score_v2.provenance_class(out[0]), "document")


class ShadowJobTests(_StoreCase):
    def _seed_board(self):
        sid = self._person("Sarah Chen")
        pid = self._person("Podcast Pete")
        for i in range(4):
            self.store.add_relation("person", sid, "owed", "fact", 100 + i,
                                    ts=NOW)
        self._turn("Sarah Chen", 6)
        for i in range(25):
            self.store.add_relation("person", pid, "mentioned_in", "fact",
                                    10_000 + i, ts=NOW)
        return sid, pid

    def test_shadow_writes_comparison_row(self):
        sid, pid = self._seed_board()
        report = score_v2.run_shadow(self.store, now=NOW)
        self.assertEqual(self._report_count(), 1)
        self.assertNotIn("skipped", report)
        self.assertEqual(report["people_scored"], 2)
        # v1 board carries the mention-only loudmouth; v2's does not.
        self.assertIn(pid, report["v1_board"])
        self.assertNotIn(pid, report["v2_board"])
        self.assertIn(sid, report["v2_board"])
        self.assertGreater(report["worst_mention_share_v1"],
                           nm.MENTION_SHARE_MAX)
        self.assertLessEqual(report["worst_mention_share_v2"],
                             nm.MENTION_SHARE_MAX)
        self.assertTrue(report["clean"])
        self.assertIn("top_overlap", report)
        self.assertIn("max_displacement", report)
        self.assertTrue(report["deltas"])
        stored = score_v2.latest_reports(self.store, limit=1)[0]
        self.assertEqual(int(stored["clean"]), 1)
        self.assertEqual(stored["report"]["people_scored"], 2)

    def test_cutover_ready_needs_seven_consecutive_clean(self):
        self.assertFalse(score_v2.cutover_ready(self.store)["ready"])
        for i in range(6):
            self._insert_report(True, NOW + i)
        self.assertFalse(score_v2.cutover_ready(self.store)["ready"])
        self._insert_report(True, NOW + 6)
        self.assertTrue(score_v2.cutover_ready(self.store)["ready"])

    def test_dirty_nightly_resets_streak(self):
        for i in range(7):
            self._insert_report(True, NOW + i)
        self.assertTrue(score_v2.cutover_ready(self.store)["ready"])
        self._insert_report(False, NOW + 7)
        self.assertFalse(score_v2.cutover_ready(self.store)["ready"])
        for i in range(6):
            self._insert_report(True, NOW + 8 + i)
        self.assertFalse(score_v2.cutover_ready(self.store)["ready"])
        self._insert_report(True, NOW + 14)
        self.assertTrue(score_v2.cutover_ready(self.store)["ready"])


class ShadowFailClosedTests(_StoreCase):
    def test_shadow_records_nothing_without_config(self):
        orig = score_v2._CONFIG_PATH
        try:
            score_v2._CONFIG_PATH = Path(self.tmp) / "missing.json"
            score_v2._raw_config.cache_clear()
            report = score_v2.run_shadow(self.store, now=NOW)
            self.assertEqual(report.get("skipped"), "config_not_loaded")
            self.assertEqual(self._report_count(), 0)
        finally:
            score_v2._CONFIG_PATH = orig
            score_v2._raw_config.cache_clear()


class LiveSwitchTests(_StoreCase):
    def _settings(self, live: bool):
        return SimpleNamespace(score=SimpleNamespace(shadow=False,
                                                     live_v2=live))

    def test_flag_off_means_v1_even_when_ready(self):
        for i in range(7):
            self._insert_report(True, NOW + i)
        with patch.object(score_v2, "settings", self._settings(False)):
            self.assertFalse(score_v2.live_v2_enabled(self.store))
            self.assertIsNone(score_v2.live_scores(self.store, NOW))

    def test_flag_on_still_gated_on_cutover(self):
        with patch.object(score_v2, "settings", self._settings(True)):
            self.assertFalse(score_v2.live_v2_enabled(self.store))
        for i in range(7):
            self._insert_report(True, NOW + i)
        with patch.object(score_v2, "settings", self._settings(True)):
            self.assertTrue(score_v2.live_v2_enabled(self.store))
            scores = score_v2.live_scores(self.store, NOW)
            self.assertIsInstance(scores, dict)

    def test_settings_patched_without_score_block(self):
        # Older suites patch settings with bare SimpleNamespace objects —
        # flag reads must not explode, they must read as OFF.
        with patch.object(score_v2, "settings", SimpleNamespace()):
            self.assertFalse(score_v2.shadow_enabled())
            self.assertFalse(score_v2.live_flag())
            self.assertFalse(score_v2.live_v2_enabled(self.store))

    def test_run_job_respects_flag(self):
        with patch.object(score_v2, "_store", lambda: self.store):
            with patch.object(score_v2, "settings", SimpleNamespace(
                    score=SimpleNamespace(shadow=False, live_v2=False))):
                score_v2.run_job()
                self.assertEqual(self._report_count(), 0)
            with patch.object(score_v2, "settings", SimpleNamespace(
                    score=SimpleNamespace(shadow=True, live_v2=False))):
                score_v2.run_job()
                self.assertEqual(self._report_count(), 1)


class GoldenFixtureV2(unittest.TestCase):
    """The spec target on the golden fixture: v1 worst mention share FAILS
    the 30% gate, v2 (report-only) PASSES it."""

    FIXTURE = (Path(__file__).resolve().parent
               / "fixtures" / "goldens" / "people_noise.jsonl")

    def _profiles(self):
        rows = [json.loads(x) for x in
                self.FIXTURE.read_text(encoding="utf-8").splitlines()
                if x.strip()]
        return [r for r in rows if r.get("type") == "score_profile"]

    def test_v2_fixes_mention_share_on_golden(self):
        now = time.time()
        v1_people, v2_people = [], []
        for r in self._profiles():
            src = r.get("mention_source") or "audio.whisper"
            edges = ([{"obj_type": "fact", "obj_id": i, "predicate": "owns",
                       "source": src} for i in range(int(r.get("typed") or 0))]
                     + [{"obj_type": "fact", "obj_id": 10_000 + i,
                         "predicate": "mentioned_in", "source": src}
                        for i in range(int(r.get("mentions") or 0))])
            if r.get("cooccur"):
                edges.append({"obj_type": "person", "obj_id": 1,
                              "predicate": "co_occurs",
                              "weight": float(r["cooccur"])})
            for i in range(int(r.get("asserted") or 0)):
                edges.append({"obj_type": "entity", "obj_id": 20_000 + i,
                              "origin": "asserted"})
            last_seen = now - float(r.get("last_seen_days") or 0) * DAY
            v1_people.append((r["name"], edges, last_seen))
            v2_people.append((r["name"], edges, last_seen,
                              float(r.get("dialogue_turns") or 0)))

        v1 = nm.top10_mention_shares(v1_people, now)
        v2 = nm.topn_mention_shares_v2(v2_people, now)
        worst_v1 = max(s for _, _, s in v1)
        worst_v2 = max((s for _, _, s in v2), default=0.0)
        self.assertGreater(worst_v1, nm.MENTION_SHARE_MAX)   # baseline FAIL
        self.assertLessEqual(worst_v2, nm.MENTION_SHARE_MAX)  # v2 PASS
        v2_names = {n for n, _, _ in v2}
        self.assertNotIn("Doc Marc", v2_names)  # mention-only: off the board
        self.assertIn("Sarah Chen", v2_names)


if __name__ == "__main__":
    unittest.main()
