"""Latency program, Phase 2 — classifier heads.

The heads decide whether a model runs at all, so the tests are weighted toward
the ways that can go wrong quietly: shadow mode influencing behavior, the
feature vector changing width between fit and predict, the activation gate
being measured over the wrong population, and a head failing open into "skip".
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.services import fast_heads as fh
from app.storage import Store


def fake_embed(texts):
    """Deterministic stand-in for MiniLM: one dimension keyed on a marker word,
    so a trivially separable problem exists without loading a model."""
    return np.asarray([[1.0 if "ACTION" in t else 0.0, len(t) / 100.0]
                       for t in texts], dtype=float)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_heads_"))
        self.env = patch.dict(os.environ, {
            "QUILL_DATA_DIR": str(self.tmp),
            "QUILL_HEADS_DIR": str(self.tmp / "heads"),
            "QUILL_HEADS": "off",
        }, clear=False)
        self.env.start()
        self.store = Store(db_path=self.tmp / "quill.db",
                           audio_dir=self.tmp / "audio")

    def tearDown(self) -> None:
        self.env.stop()

    def rows(self, n: int = 80) -> list[dict]:
        return [{"text": ("ACTION send the deck" if i % 2 else "filler chatter"),
                 "label": i % 2} for i in range(n)]


class ModeTests(_Base):
    def test_off_is_the_default_and_is_inert(self) -> None:
        d = fh.consult("extract_triage", "ACTION send the deck")
        self.assertEqual(d["mode"], "off")
        self.assertFalse(d["skip"])
        self.assertFalse(d["would_skip"])
        self.assertIsNone(d["p"])

    def test_global_shadow_caps_a_per_head_active(self) -> None:
        """The global flag has to be a real kill switch, or a stray per-head
        env var could activate a head nobody vetted."""
        with patch.dict(os.environ, {"QUILL_HEADS": "shadow",
                                     "QUILL_HEAD_EXTRACT_TRIAGE_MODE": "active"},
                        clear=False):
            self.assertEqual(fh.head_mode("extract_triage"), "shadow")

    def test_a_single_head_can_go_active_alone(self) -> None:
        with patch.dict(os.environ, {"QUILL_HEADS": "active",
                                     "QUILL_HEAD_FRAME_KEEP_MODE": "shadow"},
                        clear=False):
            self.assertEqual(fh.head_mode("extract_triage"), "active")
            self.assertEqual(fh.head_mode("frame_keep"), "shadow")

    def test_off_beats_every_per_head_setting(self) -> None:
        with patch.dict(os.environ, {"QUILL_HEADS": "off",
                                     "QUILL_HEAD_EXTRACT_TRIAGE_MODE": "active"},
                        clear=False):
            self.assertEqual(fh.head_mode("extract_triage"), "off")

    def test_a_garbage_mode_falls_back_rather_than_activating(self) -> None:
        with patch.dict(os.environ, {"QUILL_HEADS": "banana"}, clear=False):
            self.assertEqual(fh.mode(), "off")

    def test_thresholds_are_per_head_overridable(self) -> None:
        with patch.dict(os.environ, {"QUILL_HEAD_FRAME_KEEP_T_LOW": "0.42"},
                        clear=False):
            self.assertEqual(fh.thresholds("frame_keep")[0], 0.42)
            self.assertEqual(fh.thresholds("extract_triage")[0],
                             fh._cfg().t_low)

    def test_a_malformed_threshold_falls_back_to_the_default(self) -> None:
        with patch.dict(os.environ, {"QUILL_HEAD_FRAME_KEEP_T_LOW": "nope"},
                        clear=False):
            self.assertEqual(fh.thresholds("frame_keep")[0], fh._cfg().t_low)


class FeatureContractTests(_Base):
    """Fit and predict must produce identical vector widths, per head."""

    def test_declared_extras_are_emitted_present_or_not(self) -> None:
        with_extra = fh.featurize([{"text": "x", "extra": {"motion": 0.5}}],
                                  name="frame_keep", embed=fake_embed)
        without = fh.featurize([{"text": "x"}], name="frame_keep",
                               embed=fake_embed)
        self.assertEqual(with_extra.shape, without.shape)

    def test_absent_is_distinguishable_from_zero(self) -> None:
        """A has-flag beside the value, so a missing motion reading is not
        silently the same as a perfectly static frame."""
        present = fh.featurize([{"text": "x", "extra": {"motion": 0.0}}],
                               name="frame_keep", embed=fake_embed)[0]
        absent = fh.featurize([{"text": "x"}], name="frame_keep",
                              embed=fake_embed)[0]
        self.assertFalse(np.array_equal(present, absent))

    def test_an_undeclared_extra_is_ignored(self) -> None:
        """A call site passing a stray key must not change the vector width."""
        a = fh.featurize([{"text": "x", "extra": {"motion": 0.5}}],
                         name="frame_keep", embed=fake_embed)
        b = fh.featurize([{"text": "x", "extra": {"motion": 0.5,
                                                  "bogus": 9.9}}],
                         name="frame_keep", embed=fake_embed)
        self.assertTrue(np.array_equal(a, b))

    def test_heads_with_different_extras_have_different_widths(self) -> None:
        wide = fh.featurize([{"text": "x"}], name="frame_keep", embed=fake_embed)
        narrow = fh.featurize([{"text": "x"}], name="extract_triage",
                              embed=fake_embed)
        self.assertEqual(wide.shape[1], narrow.shape[1] + 2)

    def test_scalars_are_bounded(self) -> None:
        """Unbounded features make a linear model hostage to one outlier."""
        feats = fh.scalar_features("word " * 100_000, speaker_known=True,
                                   wake_word=True)
        self.assertTrue(all(0.0 <= f <= 1.0 for f in feats), feats)


class TrainingTests(_Base):
    def test_a_head_trains_and_separates(self) -> None:
        model, metrics = fh.train(self.rows(), name="extract_triage",
                                  embed=fake_embed)
        self.assertGreater(metrics["auc"], 0.9)
        self.assertEqual(metrics["miss_rate"], 0.0)

    def test_one_class_input_refuses_to_fit(self) -> None:
        """A head trained on only-positives would skip nothing, or worse."""
        rows = [{"text": "ACTION x", "label": 1} for _ in range(40)]
        with self.assertRaises(ValueError):
            fh.train(rows, name="extract_triage", embed=fake_embed)

    def test_too_few_labels_refuses_to_fit(self) -> None:
        with self.assertRaises(ValueError):
            fh.train(self.rows(4), name="extract_triage", embed=fake_embed)

    def test_train_head_declines_below_the_label_floor(self) -> None:
        out = fh.train_head("extract_triage", store=self.store,
                            embed=fake_embed)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "insufficient_labels")
        self.assertEqual(out["need"], fh._cfg().min_labels)

    def test_an_unknown_head_is_refused_not_created(self) -> None:
        self.assertFalse(fh.train_head("not_a_head")["ok"])

    def test_the_holdout_split_is_deterministic(self) -> None:
        a = fh._holdout_split(self.rows())
        b = fh._holdout_split(self.rows())
        self.assertEqual([r["text"] for r in a[1]], [r["text"] for r in b[1]])

    def test_persist_and_reload_round_trip(self) -> None:
        model, metrics = fh.train(self.rows(), name="extract_triage",
                                  embed=fake_embed)
        fh.save("extract_triage", model, metrics, n_labels=80)
        self.assertEqual(fh.latest_version("extract_triage"), 1)
        loaded, meta = fh.load_latest("extract_triage")
        self.assertIsNotNone(loaded)
        self.assertEqual(meta["n_labels"], 80)
        self.assertEqual(meta["head"], "extract_triage")

    def test_versions_increment(self) -> None:
        model, metrics = fh.train(self.rows(), name="extract_triage",
                                  embed=fake_embed)
        fh.save("extract_triage", model, metrics, n_labels=80)
        fh.save("extract_triage", model, metrics, n_labels=90)
        self.assertEqual(fh.latest_version("extract_triage"), 2)
        self.assertEqual(fh.load_latest("extract_triage")[1]["n_labels"], 90)


class LabelSourcingTests(_Base):
    """Labels come from the existing loop — no new labeling workflow."""

    def _pair(self, **kw):
        base = {"task_type": "extraction", "input_text": "ACTION send deck",
                "verdict": "accepted", "final_target": '{"tasks": [1]}',
                "human_confirmed": True}
        base.update(kw)
        return base

    def test_an_accepted_pair_with_output_means_the_model_was_needed(self) -> None:
        spec = fh.HEADS["extract_triage"]
        self.assertEqual(fh._pair_label(spec, self._pair()), 1)

    def test_an_accepted_pair_with_no_output_means_it_was_not(self) -> None:
        spec = fh.HEADS["extract_triage"]
        for empty in ("", "{}", "[]", "null", "   "):
            self.assertEqual(
                fh._pair_label(spec, self._pair(final_target=empty,
                                                local_output="")), 0, empty)

    def test_a_rejected_pair_is_a_negative(self) -> None:
        spec = fh.HEADS["extract_triage"]
        self.assertEqual(fh._pair_label(spec, self._pair(verdict="rejected")), 0)

    def test_an_unlabeled_verdict_is_skipped_not_guessed(self) -> None:
        spec = fh.HEADS["extract_triage"]
        self.assertIsNone(fh._pair_label(spec, self._pair(verdict="pending")))


class BandTests(_Base):
    def test_only_confident_no_skips(self) -> None:
        self.assertEqual(fh.heads.band("extract_triage", 0.01), fh.SKIP)
        self.assertEqual(fh.heads.band("extract_triage", 0.5), fh.RUN)
        self.assertEqual(fh.heads.band("extract_triage", 0.99), fh.RUN)

    def test_the_uncertain_middle_runs_the_model(self) -> None:
        """Precision-first: between the bands, the model runs. Anything else
        turns an unsure head into a silent dropper."""
        t_low, t_high = fh.thresholds("extract_triage")
        mid = (t_low + t_high) / 2
        self.assertEqual(fh.heads.band("extract_triage", mid), fh.RUN)

    def test_no_model_means_run(self) -> None:
        self.assertEqual(fh.heads.band("extract_triage", None), fh.NO_MODEL)

    def test_an_untrained_head_never_skips(self) -> None:
        with patch.dict(os.environ, {"QUILL_HEADS": "active"}, clear=False):
            d = fh.consult("extract_triage", "anything at all")
            self.assertFalse(d["skip"])
            self.assertEqual(d["band"], fh.NO_MODEL)


class ShadowIsInfluenceFreeTests(_Base):
    """The property that makes the rollout safe."""

    def _train_and_load(self) -> None:
        model, metrics = fh.train(self.rows(), name="extract_triage",
                                  embed=fake_embed)
        fh.save("extract_triage", model, metrics, n_labels=80)
        fh.heads._versions.clear()

    @staticmethod
    def _fake_embedder():
        """Patch the embedder featurize reaches for, NOT featurize itself:
        replacing featurize wholesale changes the vector width and the model
        then fails to score, which looks exactly like a head declining to
        skip."""
        from app.services import embeddings
        return patch.object(embeddings.embedder, "encode_many", fake_embed)

    def test_shadow_predicts_but_never_instructs_a_skip(self) -> None:
        self._train_and_load()
        with patch.dict(os.environ, {"QUILL_HEADS": "shadow"}, clear=False), \
                self._fake_embedder():
            d = fh.consult("extract_triage", "filler chatter")
        self.assertEqual(d["mode"], "shadow")
        self.assertTrue(d["would_skip"])   # it believes there is nothing here
        self.assertFalse(d["skip"])        # ...and the model runs anyway

    def test_active_turns_the_same_belief_into_an_instruction(self) -> None:
        self._train_and_load()
        with patch.dict(os.environ, {"QUILL_HEADS": "active"}, clear=False), \
                self._fake_embedder():
            d = fh.consult("extract_triage", "filler chatter")
        self.assertTrue(d["would_skip"])
        self.assertTrue(d["skip"])

    def test_a_broken_model_fails_open_to_running_the_llm(self) -> None:
        """Failing closed would drop user data; failing open costs a call."""
        with patch.dict(os.environ, {"QUILL_HEADS": "active"}, clear=False), \
                patch.object(fh.heads, "_ensure",
                             side_effect=RuntimeError("corrupt joblib")):
            d = fh.consult("extract_triage", "anything")
        self.assertFalse(d["skip"])
        self.assertIsNone(d["p"])


class ActivationGateTests(_Base):
    """Disagreement is measured over the skip population only."""

    def setUp(self) -> None:
        super().setUp()
        # Observations without a trained model is a state that cannot occur:
        # no model means no predictions to observe. Train one first, so these
        # tests exercise the gate rather than the "not trained yet" branch.
        model, metrics = fh.train(self.rows(), name="extract_triage",
                                  embed=fake_embed)
        fh.save("extract_triage", model, metrics, n_labels=80)

    def _obs(self, would_skip: bool, needed: bool, n: int = 1) -> None:
        for _ in range(n):
            self.store.record_head_observation(
                head="extract_triage", mode="shadow", p=0.05,
                band=fh.SKIP if would_skip else fh.RUN,
                would_skip=would_skip, skipped=False, needed_model=needed)

    def test_disagreement_counts_only_would_have_skipped_events(self) -> None:
        self._obs(would_skip=True, needed=False, n=98)   # correct skips
        self._obs(would_skip=True, needed=True, n=2)     # the silent drops
        self._obs(would_skip=False, needed=False, n=500)  # irrelevant to the gate
        obs = self.store.head_observations("extract_triage")
        self.assertEqual(obs["would_skip"], 100)
        self.assertEqual(obs["would_skip_but_needed"], 2)

    def test_the_gate_opens_at_two_percent(self) -> None:
        self._obs(would_skip=True, needed=False, n=198)
        self._obs(would_skip=True, needed=True, n=2)      # exactly 1%
        st = fh.status(store=self.store)
        row = next(h for h in st["heads"] if h["head"] == "extract_triage")
        self.assertEqual(row["disagreement"], 0.01)
        self.assertTrue(row["ready_to_activate"])

    def test_the_gate_stays_shut_above_the_threshold(self) -> None:
        self._obs(would_skip=True, needed=False, n=190)
        self._obs(would_skip=True, needed=True, n=10)     # 5%
        row = next(h for h in fh.status(store=self.store)["heads"]
                   if h["head"] == "extract_triage")
        self.assertFalse(row["ready_to_activate"])
        self.assertIn("exceeds", row["blocked_by"])

    def test_a_clean_but_thin_shadow_window_does_not_open_the_gate(self) -> None:
        """Ten clean events is not evidence; the window size is part of the
        gate, not a formality."""
        self._obs(would_skip=True, needed=False, n=10)
        row = next(h for h in fh.status(store=self.store)["heads"]
                   if h["head"] == "extract_triage")
        self.assertEqual(row["disagreement"], 0.0)
        self.assertFalse(row["ready_to_activate"])
        self.assertIn("more shadow events", row["blocked_by"])

    def test_status_says_why_each_head_is_blocked(self) -> None:
        st = fh.status(store=self.store)
        self.assertEqual(set(h["head"] for h in st["heads"]), set(fh.HEADS))
        for h in st["heads"]:
            self.assertFalse(h["ready_to_activate"])
            self.assertTrue(h["blocked_by"])

    def test_an_untrained_head_says_so_first(self) -> None:
        """Most actionable blocker first: there is nothing to measure until
        something is trained."""
        row = next(h for h in fh.status(store=self.store)["heads"]
                   if h["head"] == "frame_keep")
        self.assertEqual(row["blocked_by"], "no model trained")


class OutcomeRecordingTests(_Base):
    def test_an_outcome_is_stored_against_the_head(self) -> None:
        d = {"head": "extract_triage", "mode": "shadow", "p": 0.04,
             "band": fh.SKIP, "would_skip": True, "skip": False}
        fh.record_outcome(d, needed_model=True, store=self.store)
        obs = self.store.head_observations("extract_triage")
        self.assertEqual(obs["events"], 1)
        self.assertEqual(obs["would_skip_but_needed"], 1)

    def test_an_off_mode_decision_records_nothing(self) -> None:
        fh.record_outcome({"head": "extract_triage", "mode": "off"},
                          needed_model=True, store=self.store)
        self.assertEqual(
            self.store.head_observations("extract_triage")["events"], 0)

    def test_recording_never_raises_into_the_serving_path(self) -> None:
        fh.record_outcome(None, needed_model=True, store=self.store)
        fh.record_outcome({}, needed_model=True, store=self.store)
        fh.record_outcome({"head": "x", "mode": "shadow"}, needed_model=True,
                          store=object())

    def test_no_input_text_is_ever_stored(self) -> None:
        """Same rule as the usage ledger: numbers and enum-ish strings only."""
        fh.record_outcome(
            {"head": "extract_triage", "mode": "shadow", "p": 0.04,
             "band": fh.SKIP, "would_skip": True, "skip": False},
            needed_model=False, store=self.store)
        cols = {r[1] for r in self.store._conn.execute(
            "PRAGMA table_info(head_observations)")}
        self.assertEqual(cols & {"text", "input_text", "raw", "summary"}, set())
        row = self.store._conn.execute(
            "SELECT * FROM head_observations").fetchone()
        blob = " ".join(str(v) for v in dict(row).values())
        self.assertNotIn("send", blob.lower())


class CallSiteTests(_Base):
    """The two wired heads must be inert with the flag off."""

    def test_extractor_consults_the_head_and_records_the_outcome(self) -> None:
        src = (Path(__file__).resolve().parent.parent
               / "app" / "services" / "extractor.py").read_text()
        self.assertIn('_heads.consult("extract_triage"', src)
        self.assertIn("_heads.record_outcome(head, needed_model=bool(n))", src)

    def test_desktop_capture_consults_before_the_vlm(self) -> None:
        src = (Path(__file__).resolve().parent.parent
               / "app" / "services" / "desktop_capture.py").read_text()
        self.assertIn('_heads.consult("frame_keep"', src)
        # The consult must precede the VLM call, or it gates nothing.
        self.assertLess(src.index('_heads.consult("frame_keep"'),
                        src.index("from app.services.vlm import vlm"))

    def test_heads_cannot_raise_cloud_spend(self) -> None:
        """A head only ever removes calls, and never chooses a rung."""
        src = (Path(__file__).resolve().parent.parent
               / "app" / "services" / "fast_heads.py").read_text()
        for forbidden in ("claude", "anthropic", "escalate("):
            self.assertNotIn(forbidden, src.lower().replace(
                "cloud spend", "").replace("claude.", ""))


if __name__ == "__main__":
    unittest.main()
