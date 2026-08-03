"""Horizon strip + ranking learn (Track A4)."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class HorizonTests(unittest.TestCase):
    def test_calendar_event_surfaces_person_and_commitment(self):
        from app.services import horizon
        from app.storage import Store
        from app.events import Event, Modality

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                pid = store.resolve_person("Scott Reeves", ts=now)
                tid = store.add_task(
                    "Send Scott Reeves the term sheet",
                    confidence=0.9, owner_person_id=pid,
                    extracted_at=now - 86400)
                # Also tag owner name on the fact row if supported
                start = time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.localtime(now + 40 * 60))
                store.insert(Event(
                    time=now, modality=Modality.SYSTEM,
                    raw=f"Calendar: Scott Reeves — Series A ({start})",
                    summary="[calendar] Scott Reeves Series A",
                    source="phone.calendar",
                    meta={"start": start, "summary": "Series A with Scott Reeves",
                          "origin": "icloud"},
                ))
                items = horizon.predict(store, now=now, limit=3)
                self.assertGreaterEqual(len(items), 1)
                labels = " ".join(i.get("label") or "" for i in items)
                self.assertIn("Scott", labels)
                # At least one item cites calendar timing
                self.assertTrue(any(
                    any("calendar" in (r or "").lower()
                        for r in (i.get("reason") or []))
                    for i in items))
                # Confidence gate
                self.assertTrue(all(i["p_need"] >= 0.5 for i in items))
                # Fact may appear as related open work
                ids = {i.get("id") for i in items}
                self.assertTrue(
                    f"person:{pid}" in ids or f"fact:{tid}" in ids)
            finally:
                store.close()

    def test_below_min_p_renders_nothing(self):
        from app.services import horizon
        with mock.patch.object(horizon, "_cfg") as cfg:
            cfg.return_value.horizon = True
            cfg.return_value.horizon_min_p = 0.99
            cfg.return_value.horizon_horizon_s = 90 * 60
            with mock.patch.object(horizon, "_next_calendar_events",
                                   return_value=[{
                                       "start_ts": time.time() + 80 * 60,
                                       "when_s": 80 * 60,
                                       "title": "Far meeting",
                                       "text": "Nobody Known",
                                       "event_key": "x",
                                   }]):
                items = horizon.predict(store=None, limit=3)
        self.assertEqual(items, [])


class RankingLearnTests(unittest.TestCase):
    def test_kill_switch_freezes_at_prior(self):
        from app.services import ranking_learn
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                with mock.patch.object(ranking_learn, "_learn_enabled",
                                       return_value=False):
                    out = ranking_learn.update_from_outcome(
                        store,
                        decomp={"pin": 0, "pros": 0.9, "rel": 0.2, "fut": 0.5,
                                "unres": 0.4, "cent": 0.1, "sem": 0.3,
                                "rep": 0.1, "temp": 0.4, "unc": 0.1, "act": 0},
                        outcome="dismiss")
                    self.assertIsNone(out)
                    beta = ranking_learn.current_beta(store)
                    prior = ranking_learn.prior_beta()
                    self.assertEqual(beta, prior)
            finally:
                store.close()

    def test_sgd_moves_weights_when_enabled(self):
        from app.services import ranking_learn
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                ranking_learn.revert_to_prior(store)
                with mock.patch.object(ranking_learn, "_learn_enabled",
                                       return_value=True), \
                     mock.patch.object(ranking_learn, "_max_daily_drift",
                                       return_value=1.0):
                    ranking_learn.refresh_thompson(store)
                    before = dict(ranking_learn.load(store)["beta"])
                    out = ranking_learn.update_from_outcome(
                        store,
                        decomp={"pin": 0, "pros": 0.95, "rel": 0.1, "fut": 0.8,
                                "unres": 0.5, "cent": 0.1, "V": 0.4,
                                "rep": 0.1, "B": 0.5, "unc": 0.05, "act": 0.2},
                        outcome="click")
                    self.assertTrue(out and out.get("ok"))
                    after = ranking_learn.load(store, force=True)["beta"]
                    # At least one weight moved
                    moved = sum(1 for k in before
                                if abs(after[k] - before[k]) > 1e-9)
                    self.assertGreaterEqual(moved, 1)
                    exp = ranking_learn.explain(store)
                    self.assertTrue(exp["learn_enabled"])
                    self.assertGreaterEqual(exp["n_updates"], 1)
            finally:
                store.close()


class MetaMemoryTests(unittest.TestCase):
    def test_at_risk_escalates_urgency(self):
        from app.services import meta_memory
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                # Overdue commitment
                due = time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(now - 2 * 86400))
                fid = store.add_commitment(
                    "Intro promise to Scott",
                    confidence=0.9, extracted_at=now - 10 * 86400,
                    due=due)
                risks = meta_memory.scan_at_risk(store, now=now)
                self.assertTrue(any(r["fact_id"] == fid for r in risks))
                result = meta_memory.apply_urgency(store, risks, now=now)
                self.assertGreaterEqual(result["applied"], 1)
                row = store.node_dynamics_map([("fact", fid)]).get(("fact", fid))
                if row:
                    self.assertGreaterEqual(float(row.get("U") or 0), 0.75)
            finally:
                store.close()

    def test_dropped_thread_and_open_question(self):
        from app.services import meta_memory
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                tid = store.add_task(
                    "Finish term-sheet draft",
                    confidence=0.8, extracted_at=now - 30 * 86400)
                qid = store.add_claim(
                    "Should we raise the Series A now?",
                    confidence=0.6, extracted_at=now - 20 * 86400)
                dropped = meta_memory.scan_dropped_threads(store, now=now)
                self.assertTrue(any(d["fact_id"] == tid for d in dropped))
                qs = meta_memory.scan_open_questions(store, now=now)
                self.assertTrue(any(q["fact_id"] == qid for q in qs))
            finally:
                store.close()

    def test_weakening_relationship(self):
        from app.services import meta_memory
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                now = time.time()
                pid = store.resolve_person("Quiet Contact", ts=now - 60 * 86400)
                # Force last_seen into the past
                with store._lock:
                    store._conn.execute(
                        "UPDATE people SET last_seen=? WHERE id=?",
                        (now - 45 * 86400, pid))
                    store._conn.commit()
                weak = meta_memory.scan_weakening_relationships(store, now=now)
                self.assertTrue(any(w["node_id"] == pid for w in weak))
            finally:
                store.close()


class RankingPromoteTests(unittest.TestCase):
    def _seed_labeled(self, store, n: int = 20, *, favor_cand: bool = False):
        """Closed impressions with decomp + outcome for promote metrics."""
        import json
        from app.services import ranking_learn
        prior = ranking_learn.prior_beta()
        # Candidate that is clearly better on this synthetic set: high pros
        # weight when outcome is click.
        rows = []
        now = time.time()
        for i in range(n):
            pos = i % 2 == 0
            decomp = {
                "pin": 0, "pros": 0.9 if pos else 0.1, "rel": 0.2,
                "fut": 0.3, "unres": 0.2, "cent": 0.1, "sem": 0.2,
                "rep": 0.1, "temp": 0.3, "unc": 0.1, "act": 0.1,
            }
            rows.append({
                "ts": now - i * 60,
                "node_type": "fact",
                "node_id": i + 1,
                "surface": "field",
                "score": 0.5,
                "decomposition": json.dumps(decomp),
                "outcome": "click" if pos else "dismiss",
                "outcome_ts": now - i * 60,
            })
        store.add_attention_impressions(rows)
        if favor_cand:
            # Save a candidate β that leans hard on pros
            beta = dict(prior)
            beta["pros"] = 3.0
            ranking_learn.save(store, {
                "beta": beta,
                "beta_var": ranking_learn.prior_var(),
                "prior": prior,
                "n_updates": n,
                "drift": 0.01,
                "version": "candidate",
            }, note="test candidate")

    def test_insufficient_holds(self):
        from app.services import ranking_promote
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                out = ranking_promote.run(days=14, store=store)
                self.assertEqual(out["status"], "insufficient")
                self.assertFalse(out["promoted"])
            finally:
                store.close()

    def test_learn_disabled_holds(self):
        from app.services import ranking_promote, ranking_learn
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                self._seed_labeled(store, n=20, favor_cand=True)
                with mock.patch.object(ranking_learn, "_learn_enabled",
                                       return_value=False), \
                     mock.patch.object(ranking_promote, "continuity_run",
                                       return_value={"passed": True,
                                                     "status": "pass",
                                                     "mean_tau": 0.9}):
                    out = ranking_promote.run(days=14, store=store)
                self.assertEqual(out["status"], "hold")
                self.assertEqual(out["reason"], "learn_disabled")
                self.assertFalse(out["promoted"])
            finally:
                store.close()

    def test_force_promote_marks_version(self):
        from app.services import ranking_promote, ranking_learn
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                self._seed_labeled(store, n=20, favor_cand=True)
                with mock.patch.object(ranking_learn, "_learn_enabled",
                                       return_value=True), \
                     mock.patch.object(ranking_promote, "continuity_run",
                                       return_value={"passed": True,
                                                     "status": "pass",
                                                     "mean_tau": 0.9}):
                    out = ranking_promote.run(days=14, store=store,
                                              force_promote=True)
                self.assertTrue(out["promoted"])
                self.assertEqual(out["status"], "promote")
                row = store.active_ranking_model()
                self.assertEqual(row.get("version"), "promoted")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
