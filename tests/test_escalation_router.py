"""Workstream D — trained escalation router.

Acceptance criteria covered:
  * synthetic separable fixture (>=100 rows) → calibrated model trains
  * three-band routing behaves per thresholds
  * shadow mode provably never changes routing (differential test)
  * activation requires the explicit flag flip
  * the router module is never imported by the decide/approval layer
  * retrain-on-N-new-labels triggers under the idle scheduler
  * D.2b retrieval features: measured at call time, threaded to predict,
    recorded on the rows training reads, and version-guarded on load
"""
from __future__ import annotations

import ast
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.services import escalation_router as er
from app.services import router_train as rt
from app.storage import Store


def fake_vec(text: str) -> np.ndarray:
    v = np.zeros(32, dtype=np.float32)
    for w in str(text).lower().split():
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 32] += 1.0
    n = float(np.linalg.norm(v)) or 1.0
    return v / n


def fake_embed(texts):
    return [fake_vec(t) for t in texts]


_ORIG_FEATURIZE = rt.featurize


def patch_featurize():
    """Route rt.featurize through the fake embedder (no MiniLM load)."""
    return patch.object(
        rt, "featurize",
        lambda rows, embed=None: _ORIG_FEATURIZE(rows, embed=fake_embed))


def synthetic_rows(n: int = 120) -> list[dict]:
    """Separable by construction: 'thorny' questions fail, 'simple' succeed."""
    rows = []
    for i in range(n // 2):
        rows.append({"task": "chat",
                     "text": f"simple lookup question number {i} about lunch",
                     "confidence": 0.9, "ts": 1000.0 + i, "label": 1})
        rows.append({"task": "chat",
                     "text": f"thorny multi hop reasoning puzzle {i} "
                             "requiring deep analysis synthesis",
                     "confidence": 0.4, "ts": 2000.0 + i, "label": 0})
    return rows


class _Env(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        p = patch.dict(os.environ, {
            "QUILL_ROUTER_DIR": str(self.tmp / "router"),
            "QUILL_ROUTER": "shadow",
        }, clear=False)
        p.start()
        self.addCleanup(p.stop)
        self.router = er.EscalationRouter()

    def _fit_and_save(self):
        model, metrics = rt.train(synthetic_rows(), embed=fake_embed)
        rt.save(model, metrics, n_labels=120)
        return model, metrics


class TrainingTests(_Env):
    def test_separable_fixture_trains_calibrated(self) -> None:
        model, metrics = rt.train(synthetic_rows(), embed=fake_embed)
        self.assertGreaterEqual(metrics["n"], 10)
        self.assertIsNotNone(metrics["auc"])
        self.assertGreater(metrics["auc"], 0.9)      # known separability
        # Calibrated probabilities live in [0,1] and separate the classes.
        X_fail = rt.featurize(
            [{"task": "chat", "text": "thorny multi hop reasoning puzzle "
              "requiring deep analysis synthesis", "confidence": 0.4}],
            embed=fake_embed)
        X_ok = rt.featurize(
            [{"task": "chat", "text": "simple lookup question about lunch",
              "confidence": 0.9}], embed=fake_embed)
        p_fail = float(model.predict_proba(X_fail)[:, 1][0])
        p_ok = float(model.predict_proba(X_ok)[:, 1][0])
        self.assertGreater(p_fail, p_ok)

    def test_versioned_persistence(self) -> None:
        self._fit_and_save()
        self.assertEqual(rt.latest_version(), 1)
        self._fit_and_save()
        self.assertEqual(rt.latest_version(), 2)
        model, meta = rt.load_latest()
        self.assertIsNotNone(model)
        self.assertEqual(meta["version"], 2)
        self.assertIn("holdout", meta)

    def test_scalar_features_fast_and_bounded(self) -> None:
        import time as _t
        t0 = _t.time()
        for _ in range(1000):
            rt.scalar_features("chat", "some question " * 40, 0.7)
        self.assertLess((_t.time() - t0) / 1000, 0.01)   # <10ms per call

    def test_retrieval_features_are_optional_and_fast(self) -> None:
        """Adding retrieval must not slow the <10ms budget or change width."""
        stats = {"n_chunks": 4, "max_sim": 0.8, "mean_sim": 0.5,
                 "entity_coverage": 0.75}
        base = rt.scalar_features("chat", "q about Bob", 0.7)
        withr = rt.scalar_features("chat", "q about Bob", 0.7, None, stats)
        self.assertEqual(len(base), len(withr))
        self.assertNotEqual(base, withr)
        import time as _t
        t0 = _t.time()
        for _ in range(1000):
            rt.scalar_features("chat", "some question " * 40, 0.7, None, stats)
        self.assertLess((_t.time() - t0) / 1000, 0.01)

    def test_feature_version_guard_refuses_stale_model(self) -> None:
        """A model fitted on another layout must not be loaded — otherwise the
        router silently reverts to the heuristic with no explanation."""
        import json
        self._fit_and_save()
        self.assertIsNotNone(rt.load_latest()[0])
        meta_p = rt.model_dir() / f"router_v{rt.latest_version()}.json"
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        self.assertEqual(meta["feature_version"], rt.FEATURE_VERSION)
        meta["feature_version"] = rt.FEATURE_VERSION + 1
        meta_p.write_text(json.dumps(meta), encoding="utf-8")
        self.assertEqual(rt.load_latest(), (None, {}))


class RetrievalFeatureTests(unittest.TestCase):
    """D.2b: 'did retrieval find the answer?' as a routing feature."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_stats_from_grounding_output(self) -> None:
        st = rt.retrieval_stats(
            "Did Bob ship 3 widgets?",
            hits=[{"score": 0.9}, {"score": 0.5}, {"score": None}],
            block="Bob shipped 3 widgets on Tuesday")
        self.assertEqual(st["n_chunks"], 3)          # counts every hit
        self.assertAlmostEqual(st["max_sim"], 0.9)   # ...scores only the scored
        self.assertAlmostEqual(st["mean_sim"], 0.7)
        # "Did" is a _ENTITYISH match but not an entity — filtered, so the
        # two real ones (Bob, 3) both being present scores a clean 1.0.
        self.assertAlmostEqual(st["entity_coverage"], 1.0)

    def test_empty_retrieval_is_not_missing_retrieval(self) -> None:
        """Retrieved nothing != nobody measured. The flags must separate them,
        or 'grounding failed' looks identical to 'ungrounded task'."""
        empty = rt.retrieval_stats("Where is Bob?", hits=[], block="")
        self.assertEqual(empty["n_chunks"], 0)
        self.assertIsNone(empty["max_sim"])
        self.assertNotEqual(rt._retrieval_features(empty),
                            rt._retrieval_features(None))
        self.assertEqual(rt._retrieval_features(None), [0.0] * 8)

    def test_coverage_none_when_question_has_no_entities(self) -> None:
        """Every token here is a question word, so there is nothing to cover —
        that is None (unmeasurable), never 0.0 (measured and missing)."""
        st = rt.retrieval_stats("What is it?", hits=[{"score": 0.3}], block="x")
        self.assertIsNone(st["entity_coverage"])

    def test_values_are_bounded(self) -> None:
        """Scores are cosines but nothing upstream guarantees the range."""
        feats = rt._retrieval_features(
            {"n_chunks": 9999, "max_sim": 4.0, "mean_sim": -2.0,
             "entity_coverage": 0.5})
        self.assertTrue(all(0.0 <= f <= 1.0 for f in feats), feats)

    def test_build_dataset_reads_captured_stats(self) -> None:
        """The stats must survive the trip through the pair's source_refs —
        that join is the only path from call time to training time."""
        stats = {"n_chunks": 2, "max_sim": 0.7, "mean_sim": 0.6,
                 "entity_coverage": 0.5}

        class _Store:
            def list_learning_pairs(self, limit=0):
                return [{"input_text": "q", "verdict": "accepted",
                         "created_at": 1.0,
                         "source_refs": {"task": "chat", "retrieval": stats}}]

        # Isolate from this machine's real shadow grades.
        from types import SimpleNamespace
        fake = SimpleNamespace(shadow=SimpleNamespace(
            grades_path=str(Path(self.tmp.name) / "no_grades.jsonl")))
        with patch.object(rt, "settings", fake):
            rows = rt.build_dataset(store=_Store())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["retrieval"], stats)


class BandingTests(_Env):
    def test_three_bands(self) -> None:
        self.assertEqual(self.router.band(0.1), "local")
        self.assertEqual(self.router.band(0.25), "shadow_priority")
        self.assertEqual(self.router.band(0.59), "shadow_priority")
        self.assertEqual(self.router.band(0.6), "escalate")
        self.assertEqual(self.router.band(None), "no_model")


class ShadowModeTests(_Env):
    def test_shadow_never_changes_routing(self) -> None:
        """Differential: with a trained model that WOULD escalate, shadow
        mode returns exactly the heuristic's decision, both ways."""
        self._fit_and_save()
        with patch_featurize():
            msgs = [{"role": "user",
                     "content": "thorny multi hop reasoning puzzle "
                                "requiring deep analysis synthesis"}]
            for heuristic in (True, False):
                d = self.router.decide("chat", msgs, 0.4,
                                       heuristic_escalates=heuristic,
                                       heuristic_reason="low_confidence")
                self.assertEqual(d["escalate"], heuristic)
        # And the decision was logged for the report.
        self.assertTrue((self.tmp / "router" / "shadow_log.jsonl").is_file())
        rep = self.router.report()
        self.assertEqual(rep["week"]["decisions"], 2)

    def test_off_mode_is_inert(self) -> None:
        with patch.dict(os.environ, {"QUILL_ROUTER": "off"}, clear=False):
            d = self.router.decide("chat", [{"role": "user", "content": "x"}],
                                   0.5, heuristic_escalates=True)
            self.assertEqual(d, {"mode": "off", "p_fail": None,
                                 "band": "no_model", "escalate": True,
                                 "shadow_priority": False})


class ActiveModeTests(_Env):
    def _decide(self, text, conf, heuristic, reason):
        with patch.dict(os.environ, {"QUILL_ROUTER": "active"}, clear=False):
            with patch_featurize():
                return self.router.decide(
                    "chat", [{"role": "user", "content": text}], conf,
                    heuristic_escalates=heuristic, heuristic_reason=reason)

    def test_activation_requires_flag(self) -> None:
        """Same trained model: shadow keeps the heuristic, active overrides."""
        self._fit_and_save()
        hard_q = ("thorny multi hop reasoning puzzle requiring deep "
                  "analysis synthesis")
        with patch_featurize():
            shadow = self.router.decide(
                "chat", [{"role": "user", "content": hard_q}], 0.4,
                heuristic_escalates=False, heuristic_reason=None)
        self.assertFalse(shadow["escalate"])         # shadow: heuristic wins
        active = self._decide(hard_q, 0.4, False, None)
        self.assertTrue(active["escalate"])          # active: router adds it

    def test_active_keeps_confident_local_answer(self) -> None:
        """The spend-reduction half: low p(fail) overrides a low_confidence
        heuristic escalation (never a hard gate — that's the caller's rule)."""
        self._fit_and_save()
        d = self._decide("simple lookup question about lunch", 0.9,
                         True, "low_confidence")
        self.assertFalse(d["escalate"])

    def test_middle_band_flags_shadow_priority(self) -> None:
        self._fit_and_save()
        with patch.object(self.router, "predict", lambda *a, **k: 0.4):
            d = self._decide("whatever question", 0.7, False, None)
        self.assertFalse(d["escalate"])
        self.assertTrue(d["shadow_priority"])


class RetrainTests(_Env):
    def test_retrain_triggers_on_new_labels(self) -> None:
        store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        with patch.object(rt, "build_dataset",
                          lambda store=None: synthetic_rows(120)), \
             patch_featurize():
            out = self.router.maybe_retrain(store=store)
            self.assertTrue(out["trained"])
            self.assertTrue(out["promoted"])         # first model always lands
            self.assertEqual(rt.latest_version(), 1)
            # No new labels since → gated.
            out2 = self.router.maybe_retrain(store=store)
            self.assertIn("skipped", out2)
        # Below min_labels → gated.
        with patch.object(rt, "build_dataset", lambda store=None: []):
            out3 = self.router.maybe_retrain(store=store)
            self.assertIn("skipped", out3)

    def test_idle_scheduler_invokes_retrain(self) -> None:
        from app.services.idle_trainer import IdleTrainer
        t = IdleTrainer()
        probes = {"enabled": False, "now": 0.0, "pairs": 0, "idle_s": 99999.0,
                  "on_ac": True, "free_gb": 100.0, "min_new_pairs": 150,
                  "min_idle_s": 1200.0, "min_free_gb": 25.0, "min_days": 7.0,
                  "max_fails": 3}
        with patch.object(t, "_probes", lambda: probes), \
             patch.object(er.escalation_router, "maybe_retrain") as mr, \
             patch("app.services.shadow_eval.maybe_run_idle"):
            t.tick()
            mr.assert_called_once()
            probes["idle_s"] = 10.0                  # user active → no retrain
            t.tick()
            mr.assert_called_once()


class InvariantTests(unittest.TestCase):
    def test_router_never_imported_by_approval_layer(self) -> None:
        """Invariant 3 / D.4 hard rule: the router has no code path into
        approval/risk classification — risk stays a lookup table."""
        banned = {"app.services.escalation_router", "app.services.router_train"}
        for target in ("app/services/trust.py", "app/services/readiness.py",
                       "app/services/agent_planner.py"):
            p = Path(target)
            if not p.is_file():
                continue
            found = set()
            for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module)
                if isinstance(node, ast.Import):
                    for n in node.names:
                        found.add(n.name)
            self.assertFalse(found & banned,
                             f"{target} imports the escalation router")

    def test_router_module_only_touches_routing(self) -> None:
        src = Path("app/services/escalation_router.py").read_text(
            encoding="utf-8")
        found = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            if isinstance(node, ast.Import):
                for n in node.names:
                    found.add(n.name)
        self.assertFalse(found & {"app.services.trust",
                                  "app.services.readiness"})


if __name__ == "__main__":
    unittest.main()
