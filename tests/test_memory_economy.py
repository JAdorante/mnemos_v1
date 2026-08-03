"""Memory economy (Track C) — retention scoring, lifecycle sweep, span-
preserving compaction + restore, growth snapshots, and the console routes.

Safety contract under test: compaction never runs unless explicitly enabled
(flag or manual endpoint), never touches events backing open work, always
archives the original first, and restore() brings the raw back verbatim (I-1).
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.events import Event, Modality
from app.storage import Store

NOW = time.time()
DAY = 86400.0


def _mk_store(tmp) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio")


def _add_event(store, *, age_days: float, raw: str, source: str = "audio.mic",
               summary: str | None = None, confidence: float = 0.8,
               extracted: bool = True) -> int:
    eid = store.insert(Event(
        time=NOW - age_days * DAY, modality=Modality.AUDIO, raw=raw,
        summary=summary or "", source=source, confidence=confidence, meta={},
    ))
    if extracted:
        store.mark_extracted([eid], NOW - age_days * DAY + 60)
    return int(eid)


class RetentionScoreTests(unittest.TestCase):
    def test_age_decays(self):
        from app.services.memory_economy import retention_score
        kw = dict(confidence=0.8, source="audio.mic", absorbed=True)
        young = retention_score(age_days=1, **kw)
        old = retention_score(age_days=300, **kw)
        self.assertGreater(young, old)

    def test_unabsorbed_held_high(self):
        from app.services.memory_economy import retention_score
        only_copy = retention_score(age_days=60, confidence=0.4,
                                    source="desktop.screen", absorbed=False)
        absorbed = retention_score(age_days=60, confidence=0.4,
                                   source="desktop.screen", absorbed=True)
        self.assertGreater(only_copy, absorbed)

    def test_footprint_and_recall_slow_decay(self):
        from app.services.memory_economy import retention_score
        kw = dict(age_days=180, confidence=0.8, source="audio.mic",
                  absorbed=True)
        bare = retention_score(**kw)
        with_facts = retention_score(n_facts=3, **kw)
        with_recall = retention_score(recall_n=4, **kw)
        with_value = retention_score(v_max=0.9, **kw)
        self.assertGreater(with_facts, bare)
        self.assertGreater(with_recall, bare)
        self.assertGreater(with_value, bare)

    def test_open_work_floor(self):
        from app.services import memory_economy as me
        s = me.retention_score(age_days=400, confidence=0.3,
                               source="desktop.click", absorbed=True,
                               has_open=True)
        self.assertGreaterEqual(s, me.OPEN_WORK_FLOOR)

    def test_source_class_ordering(self):
        from app.services.memory_economy import retention_score
        kw = dict(age_days=30, confidence=0.8, absorbed=True)
        chat = retention_score(source="chat", **kw)
        screen = retention_score(source="desktop.screen.frame", **kw)
        self.assertGreater(chat, screen)

    def test_bounds(self):
        from app.services import memory_economy as me
        lo = me.retention_score(age_days=10000, confidence=0.05,
                                source="desktop.click", absorbed=True,
                                contradiction=1.0)
        hi = me.retention_score(age_days=0, confidence=1.0, source="chat",
                                absorbed=True, n_facts=4, recall_n=6,
                                v_max=1.0)
        self.assertGreaterEqual(lo, me.RETENTION_MIN)
        self.assertLessEqual(hi, me.RETENTION_MAX)


class SweepTests(unittest.TestCase):
    def test_lifecycle_transitions_and_scores(self):
        from app.services import memory_economy as me
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                old_absorbed = _add_event(store, age_days=30,
                                          raw="old absorbed utterance")
                fresh_young = _add_event(store, age_days=1,
                                         raw="young utterance")
                unextracted = _add_event(store, age_days=30,
                                         raw="never extracted", extracted=False)
                res = me.sweep(store, now=NOW)
                self.assertEqual(res["scored"], 3)
                self.assertEqual(res["absorbed"], 1)
                by_id = {e["id"]: e for e in store.events_for_economy()}
                self.assertEqual(by_id[old_absorbed]["lifecycle"], "absorbed")
                self.assertEqual(by_id[fresh_young]["lifecycle"], "fresh")
                self.assertEqual(by_id[unextracted]["lifecycle"], "fresh")
                for e in by_id.values():
                    self.assertIsNotNone(e["retention"])
                # audit row persisted; a fresh sweep is no longer due
                self.assertIsNotNone(store.last_economy_run())
                self.assertFalse(me.due_for(store))
            finally:
                store.close()

    def test_candidates_listed_but_not_compacted_by_default(self):
        from app.services import memory_economy as me
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                raw = "screen noise from three months ago"
                eid = _add_event(store, age_days=100, raw=raw,
                                 source="desktop.click", confidence=0.3)
                me.sweep(store, now=NOW)             # fresh -> absorbed
                res = me.sweep(store, now=NOW)       # now a candidate
                self.assertGreaterEqual(res["candidates"], 1)
                self.assertEqual(res["compacted"], 0)  # QUILL_COMPACTION off
                ev = store.get_event(eid)
                self.assertEqual(ev["raw"], raw)       # untouched
            finally:
                store.close()

    def test_disabled_is_a_noop(self):
        from app.services import memory_economy as me
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                _add_event(store, age_days=30, raw="x")
                with patch.object(me, "_cfg") as cfg:
                    cfg.return_value.enabled = False
                    res = me.sweep(store, now=NOW)
                self.assertEqual(res["reason"], "disabled")
                self.assertEqual(res["scored"], 0)
            finally:
                store.close()


class CompactionTests(unittest.TestCase):
    def test_compact_preserves_spans_and_restores_verbatim(self):
        from app.services import memory_economy as me
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                raw = ("Long rambling transcript. Somewhere in here: "
                       "I promised Dana the revised deck by Friday. "
                       "Plus twenty minutes of chit-chat.")
                span = "I promised Dana the revised deck by Friday"
                eid = _add_event(store, age_days=90, raw=raw,
                                 summary="Call with Dana")
                fid = store.add_claim("promise to Dana", source_event_id=eid,
                                      source_span=span, confidence=0.8,
                                      extracted_at=NOW - 90 * DAY)
                r = me.compact_one(store, eid, now=NOW)
                self.assertTrue(r["ok"])
                ev = store.get_event(eid)
                self.assertEqual(ev["lifecycle"], "compacted")
                self.assertIn(span, ev["raw"])          # I-1: verbatim span kept
                self.assertIn("Call with Dana", ev["raw"])
                self.assertNotIn("chit-chat", ev["raw"])
                # fact's own span copy untouched
                self.assertEqual(store.get_fact(fid)["source_span"], span)
                # forgotten-this-month review list sees it
                seen = store.compacted_events(since=NOW - DAY)
                self.assertIn(eid, [x["event_id"] for x in seen])
                # restore brings the original back verbatim
                self.assertTrue(me.restore(store, eid))
                ev = store.get_event(eid)
                self.assertEqual(ev["raw"], raw)
                self.assertEqual(ev["lifecycle"], "absorbed")
                self.assertEqual(store.compacted_events(), [])
            finally:
                store.close()

    def test_open_work_refuses_compaction(self):
        from app.services import memory_economy as me
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                eid = _add_event(store, age_days=90, raw="owes deck")
                store.add_task("send the deck", source_event_id=eid,
                               source_span="send the deck", confidence=0.9,
                               extracted_at=NOW - 90 * DAY)
                r = me.compact_one(store, eid, now=NOW)
                self.assertFalse(r["ok"])
                self.assertEqual(r["reason"], "open_facts")
                self.assertEqual(store.get_event(eid)["raw"], "owes deck")
            finally:
                store.close()

    def test_double_compact_refused_and_missing_handled(self):
        from app.services import memory_economy as me
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                eid = _add_event(store, age_days=90, raw="something old")
                self.assertTrue(me.compact_one(store, eid, now=NOW)["ok"])
                again = me.compact_one(store, eid, now=NOW)
                self.assertFalse(again["ok"])
                self.assertEqual(again["reason"], "already_compacted")
                self.assertFalse(me.compact_one(store, 99999, now=NOW)["ok"])
                self.assertFalse(me.restore(store, 99999))
            finally:
                store.close()

    def test_sweep_compacts_when_flag_on_with_churn_cap(self):
        from app.services import memory_economy as me
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                for i in range(5):
                    _add_event(store, age_days=100 + i,
                               raw=f"stale screen event {i}",
                               source="desktop.click", confidence=0.3)
                me.sweep(store, now=NOW)  # absorb pass
                real = me._cfg()

                class _Cfg:
                    enabled = True
                    compaction = True
                    absorb_after_days = real.absorb_after_days
                    compact_after_days = real.compact_after_days
                    retention_threshold = real.retention_threshold
                    compact_max_per_run = 2          # cap under test
                    growth_every_s = real.growth_every_s
                    due_after_s = real.due_after_s

                with patch.object(me, "_cfg", return_value=_Cfg()):
                    res = me.sweep(store, now=NOW)
                self.assertEqual(res["compacted"], 2)
                stats = store.events_lifecycle_stats()
                self.assertEqual(stats["counts"].get("compacted"), 2)
            finally:
                store.close()


class GrowthSnapshotTests(unittest.TestCase):
    def test_snapshot_and_throttle(self):
        from app.services import memory_economy as me
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                _add_event(store, age_days=1, raw="x")
                snap = me.growth_snapshot(store, now=NOW, force=True)
                self.assertGreater(snap["db_bytes"], 0)
                self.assertEqual(snap["n_events"], 1)
                # throttled second call
                self.assertIsNone(me.growth_snapshot(store, now=NOW + 60))
                self.assertEqual(len(store.list_storage_growth()), 1)
            finally:
                store.close()


class LedgerSignalTests(unittest.TestCase):
    def test_open_work_blocks_candidate_and_boosts_score(self):
        from app.services import memory_economy as me
        with tempfile.TemporaryDirectory() as td:
            store = _mk_store(td)
            try:
                eid = _add_event(store, age_days=60, raw="promise about Scott")
                fid = store.add_commitment(
                    "promise about Scott",
                    confidence=0.9,
                    extracted_at=NOW - 50 * DAY,
                    source_event_id=eid)
                store.add_attention_impressions([{
                    "node_type": "fact", "node_id": fid, "surface": "field",
                    "score": 0.5, "ts": NOW - 86400,
                }])
                sig = store.economy_signals_for_events([eid])
                self.assertTrue(sig[eid]["has_open"])
                self.assertGreaterEqual(sig[eid]["recall_n"], 1)
                with patch.object(me, "_cfg") as cfg:
                    cfg.return_value.enabled = True
                    cfg.return_value.compaction = False
                    cfg.return_value.absorb_after_days = 1
                    cfg.return_value.compact_after_days = 1
                    cfg.return_value.retention_threshold = 0.99
                    cfg.return_value.compact_max_per_run = 10
                    cfg.return_value.growth_every_s = 0
                    cfg.return_value.due_after_s = 0
                    out = me.sweep(store, now=NOW)
                # Open work must not appear as a compaction candidate
                cand_ids = {c["id"] for c in (out.get("candidate_preview") or [])}
                self.assertNotIn(eid, cand_ids)
            finally:
                store.close()


class EconomyEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = _mk_store(self.tmp)
        self.addCleanup(self.store.close)
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_console_status_and_sweep(self):
        _add_event(self.store, age_days=30, raw="old one")
        r = self.client.post("/console/economy/sweep")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scored"], 1)
        st = self.client.get("/console/economy").json()
        self.assertTrue(st["enabled"])
        self.assertFalse(st["compaction"])
        self.assertEqual(st["lifecycle"]["counts"].get("absorbed"), 1)
        self.assertIsNotNone(st["last_sweep"])

    def test_manual_compact_and_restore_roundtrip(self):
        eid = _add_event(self.store, age_days=90, raw="the original raw text")
        r = self.client.post(f"/console/economy/compact?event_id={eid}")
        self.assertTrue(r.json()["ok"])
        self.assertIn("compacted",
                      self.store.get_event(eid)["raw"])
        r = self.client.post(f"/console/economy/restore?event_id={eid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.get_event(eid)["raw"],
                         "the original raw text")
        # restoring twice -> 404 (no archived original anymore)
        r = self.client.post(f"/console/economy/restore?event_id={eid}")
        self.assertEqual(r.status_code, 404)

    def test_lance_optimize_endpoint(self):
        with patch("app.vectorstore.get_vectorstore") as gv:
            vs = MagicMock()
            vs.force_optimize.return_value = {"ok": True, "skipped": True}
            gv.return_value = vs
            r = self.client.post("/console/economy/lance/optimize")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("ok"))

    def test_shell_restore_endpoint(self):
        eid = _add_event(self.store, age_days=90, raw="shell restore me")
        self.assertTrue(
            self.client.post(f"/console/economy/compact?event_id={eid}")
            .json()["ok"])
        r = self.client.post("/today/restore", json={"event_id": eid})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.get_event(eid)["raw"], "shell restore me")


if __name__ == "__main__":
    unittest.main()
