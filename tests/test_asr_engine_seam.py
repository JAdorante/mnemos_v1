"""Phase A — the ASR engine seam.

The swap this seam exists for is only safe if three things hold: the incumbent
still runs exactly as it did, the engine is genuinely interchangeable at the
call site, and a confidence scale from one engine can never be silently read as
another's. That last one is the quiet failure mode — `ingest_filter`'s
thresholds were calibrated against Whisper's `avg_logprob`, and an engine whose
confidence means something else would change what becomes a memory without
changing a line of filter code.
"""
from __future__ import annotations

import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from app.services import asr
from app.services import audio as audio_mod


class _StubEngine:
    engine_id = "stub:v1"
    model_id = "stub"
    supports_context = True
    confidence_kind = asr.AVG_LOGPROB

    def __init__(self, text="the design review moved to thursday", **kw):
        self.text = text
        self.calls: list[tuple[int, int, str | None]] = []
        self.kw = kw

    def transcribe(self, samples, sample_rate, context=None):
        self.calls.append((len(samples), sample_rate, context))
        return asr.ASRResult(
            text=self.text, avg_confidence=-0.21,
            confidence_kind=self.confidence_kind, engine_id=self.engine_id,
            word_timestamps=self.kw.get("words", []))


class _NoContextEngine(_StubEngine):
    engine_id = "stub-nocontext:v1"
    supports_context = False


class RegistryTests(unittest.TestCase):
    def tearDown(self) -> None:
        asr.reset_shared()
        asr.ENGINES.pop("stub", None)

    def test_whisper_is_the_default_and_the_rollback_target(self) -> None:
        self.assertEqual(asr.configured(), "whisper")
        self.assertIn("whisper", asr.available())

    def test_the_flag_selects_the_engine(self) -> None:
        """QUILL_ASR_ENGINE is the whole swap mechanism: no argument threading,
        no import change, and `whisper` is one restart away at any time."""
        asr.register("stub", _StubEngine)
        # settings is a frozen dataclass, so swap the module's reference rather
        # than mutating the instance every other test also reads.
        flagged = SimpleNamespace(audio=SimpleNamespace(asr_engine="stub"))
        with patch.object(asr, "settings", flagged):
            self.assertEqual(asr.configured(), "stub")
            self.assertIsInstance(asr.get_engine(), _StubEngine)

    def test_a_blank_flag_falls_back_to_whisper(self) -> None:
        blank = SimpleNamespace(audio=SimpleNamespace(asr_engine=""))
        with patch.object(asr, "settings", blank):
            self.assertEqual(asr.configured(), "whisper")

    def test_unknown_engine_names_the_alternatives(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            asr.make_engine("parakeet-onnx")
        self.assertIn("whisper", str(ctx.exception))

    def test_get_engine_shares_one_instance_per_name(self) -> None:
        """Mic and loopback are two pipelines and must not load two models —
        that doubled RAM and made them fight for cores."""
        asr.register("stub", _StubEngine)
        a, b = asr.get_engine("stub"), asr.get_engine("stub")
        self.assertIs(a, b)

    def test_make_engine_does_not_install_the_shared_instance(self) -> None:
        """The eval harness holds its own engine; it must not become the one the
        live path picks up."""
        asr.register("stub", _StubEngine)
        fresh = asr.make_engine("stub")
        self.assertIsNot(fresh, asr.get_engine("stub"))

    def test_name_matching_is_case_and_space_insensitive(self) -> None:
        asr.register("stub", _StubEngine)
        self.assertIsInstance(asr.make_engine("  STUB "), _StubEngine)


class ResultContractTests(unittest.TestCase):
    def test_defaults_are_safe_for_ingest_filter(self) -> None:
        """An engine with no per-segment confidence must degrade to the
        text-only checks, not crash the pipeline."""
        from app.services.ingest_filter import assess

        res = asr.ASRResult(text="the report is due friday")
        verdict = assess(res.text, res.segments)
        self.assertTrue(verdict.action)

    def test_confidence_kind_defaults_to_whispers_scale(self) -> None:
        """Because that is the scale ingest_filter's thresholds assume. An
        engine on a different scale has to say so explicitly."""
        self.assertEqual(asr.ASRResult().confidence_kind, asr.AVG_LOGPROB)

    def test_engines_declare_a_confidence_kind(self) -> None:
        for name in asr.available():
            factory = asr.ENGINES[name]
            self.assertTrue(getattr(factory, "confidence_kind", None),
                            f"{name} does not declare confidence_kind")


class _LoopHarness:
    """Drives one utterance through `_transcribe_loop` with a stub engine."""

    def __init__(self, engine, **cfg_over):
        self.events = []
        self.pipeline = audio_mod.AudioPipeline(sink=self.events.append)
        self.pipeline._engine = engine
        base = audio_mod.settings
        self.fake_settings = SimpleNamespace(
            audio_quality=SimpleNamespace(enabled=False, skip_bad=False),
            denoise=SimpleNamespace(enabled=False, routes=(), rescore=False),
            asr_bias=SimpleNamespace(enabled=cfg_over.get("bias", False),
                                     recent_turns=3),
            ingest=base.ingest,
            storage=SimpleNamespace(save_audio=False),
            speakers=SimpleNamespace(enabled=False),
            telemetry=SimpleNamespace(enabled=False),
        )

    def run(self, samples=None, timeout=5.0):
        import time as _t

        p = self.pipeline
        samples = (samples if samples is not None
                   else np.zeros(16000, dtype=np.float32))
        p._utterances = queue.Queue()
        p._utterances.put((samples, _t.time(), _t.time(), 3.0))
        with patch.object(audio_mod, "settings", self.fake_settings):
            th = threading.Thread(target=p._transcribe_loop, daemon=True)
            th.start()
            deadline = _t.time() + timeout
            while not self.events and _t.time() < deadline:
                _t.sleep(0.02)
            p._stop.set()
            th.join(timeout=2.0)
        return self.events


class CallSiteTests(unittest.TestCase):
    """The capture path must not know which engine it got."""

    def test_a_stub_engine_produces_a_transcript_event(self) -> None:
        engine = _StubEngine()
        events = _LoopHarness(engine).run()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].raw, "the design review moved to thursday")
        self.assertEqual(engine.calls[0][1], 16000)

    def test_the_event_records_which_engine_produced_it(self) -> None:
        events = _LoopHarness(_StubEngine()).run()
        self.assertEqual(events[0].meta["asr_engine"], "stub:v1")
        self.assertEqual(events[0].meta["confidence_kind"], asr.AVG_LOGPROB)

    def test_word_timestamps_are_additive_and_absent_by_default(self) -> None:
        """Nothing downstream requires the key — Whisper does not pay for it,
        and Parakeet's come free."""
        plain = _LoopHarness(_StubEngine()).run()
        self.assertNotIn("word_timestamps", plain[0].meta)
        words = [{"word": "thursday", "start": 0.9, "end": 1.4}]
        with_words = _LoopHarness(_StubEngine(words=words)).run()
        self.assertEqual(with_words[0].meta["word_timestamps"], words)

    def test_an_engine_without_context_is_never_given_one(self) -> None:
        """Forcing an initial_prompt onto an engine with no context hook either
        errors or is ignored silently; both are worse than not sending it."""
        engine = _NoContextEngine()
        _LoopHarness(engine, bias=True).run()
        self.assertIsNone(engine.calls[0][2])

    def test_an_engine_failure_drops_the_utterance_not_the_pipeline(self) -> None:
        class _Broken(_StubEngine):
            def transcribe(self, samples, sample_rate, context=None):
                raise RuntimeError("model exploded")

        events = _LoopHarness(_Broken()).run(timeout=1.5)
        self.assertEqual(events, [])          # dropped, and no exception escaped

    def test_empty_text_is_dropped_before_anything_downstream(self) -> None:
        events = _LoopHarness(_StubEngine(text="")).run(timeout=1.5)
        self.assertEqual(events, [])


class WhisperEngineTests(unittest.TestCase):
    """The incumbent's own wiring, without loading a model."""

    def test_it_declares_the_contract_the_call_site_relies_on(self) -> None:
        self.assertTrue(asr.WhisperEngine.supports_context)
        self.assertEqual(asr.WhisperEngine.confidence_kind, asr.AVG_LOGPROB)

    def test_it_satisfies_the_protocol(self) -> None:
        self.assertTrue(hasattr(asr.WhisperEngine, "transcribe"))
        self.assertTrue(isinstance(_StubEngine(), asr.ASREngine))


if __name__ == "__main__":
    unittest.main()
