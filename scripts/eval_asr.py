"""Perception Phase 0 — the ASR acceptance harness.

`eval_voice.py` (#8) answers "did a *pipeline* change make Mnemos better?" on a
synthetic golden set. This answers a different question, the one the perception
upgrade turns on: **does a different ASR *engine* win on our audio?** Leaderboard
WER is measured on read speech; Mnemos hears laptop mics in rooms with fans.

So this harness is deliberately engine-agnostic. Every engine implements the
`ASREngine` protocol from `app/services/asr.py` — the same objects the live path
runs, not a harness-local copy — is selected by `--engine`, and is scored on the
same fixtures with the same downstream stages. Whatever engine ships, this file
is how it earned it.

What it measures, and why each metric is here
---------------------------------------------
  WER                  the headline. Per category, because a win on close-mic
                       dictation that loses on far-field meetings is not a win.
  hallucination rate   non-empty transcripts on audio with no speech in it.
                       Reported twice: `raw` (what the engine emitted) and
                       `post_filter` (what survived ingest_filter to become a
                       memory). The gap is how much work the filter is doing —
                       an engine that stops hallucinating lets us loosen it.
  RTF + per-utterance  compute cost and the offline share of the utterance-end
                       -> event budget. Queue wait is a live-path number; see
                       `offline_utterance_ms` below for what this can honestly
                       claim.
  boundary metrics     fused / split / boundary error against ground-truth
                       utterance spans. Phase C ("many attribution errors are
                       segmentation errors") is a guess until this is measured;
                       it is measured here, before anyone touches speakers.py.
  attribution error    wrong-speaker rate with speakers.py in the loop.

Usage
-----
    python scripts/eval_asr.py bootstrap        # synthetic probes -> runnable today
    python scripts/eval_asr.py check            # validate the manifest + clips
    python scripts/eval_asr.py smoke            # CI: no ghost may become a memory
    python scripts/eval_asr.py run --tag whisper-baseline
    python scripts/eval_asr.py run --engine parakeet-onnx --tag parakeet
    python scripts/eval_asr.py compare data/eval/asr/report_whisper-baseline.json \
                                       data/eval/asr/report_parakeet.json --gate

Real fixtures (the ones that decide anything) go in tests/fixtures/asr_eval/ —
see the README there for the manifest schema and the recording checklist.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from app.config import settings                                    # noqa: E402
from app.services import asr as _asr                               # noqa: E402
from app.services.asr import ASREngine, ASRResult                  # noqa: E402
# Reuse the #8 harness's pure helpers rather than growing a second copy of WER.
from eval_voice import (read_wav, write_wav, wer, norm_words,       # noqa: E402
                        entity_recall, add_noise, far_field)

FIXTURE_DIR = Path(os.environ.get("QUILL_ASR_EVAL_DIR")
                   or _ROOT / "tests" / "fixtures" / "asr_eval")
MANIFEST = FIXTURE_DIR / "manifest.jsonl"
REPORT_DIR = _ROOT / "data" / "eval" / "asr"
SR = 16_000

# Categories the manifest may declare. Not enforcement for its own sake: the
# report is broken down by category, and a typo'd category silently creates a
# one-clip bucket that looks like a real result.
CATEGORIES = ("close_mic", "laptop_meeting", "far_field", "fan_noise",
              "loopback", "no_speech")
CHANNELS = ("mic", "loopback")


# ---------------------------------------------------------------------------
# the engine seam
# ---------------------------------------------------------------------------
# This harness scores the SAME engine objects the live path runs — the protocol,
# the registry and the Whisper adapter live in app/services/asr.py. A harness
# with its own copy of the engine would eventually score an engine nobody ships.
def get_engine(name: str, **kw) -> ASREngine:
    """The registry lookup, with a message that names the alternatives.

    `make_engine`, not `get_engine`: the eval must not install its instance as
    the process-wide one, because a `run` with `--word-timestamps` would then
    hand a differently-configured engine to anything else in the process.
    """
    try:
        return _asr.make_engine(name, **kw)
    except ValueError as exc:
        raise SystemExit(
            f"[eval-asr] {exc}. Register new engines in app/services/asr.py.")


# ---------------------------------------------------------------------------
# segmentation — the live path's VAD, run offline over a whole clip
# ---------------------------------------------------------------------------
@dataclass
class Span:
    start: float          # seconds into the clip
    end: float
    samples: np.ndarray


def vad_segment(x: np.ndarray, sr: int, cfg=None) -> tuple[list[Span], float]:
    """Cut a clip into utterances with the same Silero VADIterator the capture
    thread uses, at the same thresholds. Returns (spans, vad_wall_ms).

    Fidelity matters more than convenience here: if the harness segmented
    differently from production, its boundary and per-utterance latency numbers
    would describe a pipeline nobody runs.
    """
    from silero_vad import load_silero_vad, VADIterator

    cfg = cfg or settings.audio
    model = load_silero_vad(onnx=True)
    it = VADIterator(model, threshold=cfg.vad_threshold, sampling_rate=sr,
                     min_silence_duration_ms=cfg.min_silence_ms,
                     speech_pad_ms=cfg.speech_pad_ms)
    n = cfg.frame_samples                     # 512 @ 16 kHz — Silero requires exact
    max_n = int(cfg.max_utterance_s * sr) if cfg.max_utterance_s > 0 else 0
    spans: list[Span] = []
    in_speech = False
    start_i = 0
    t0 = time.perf_counter()
    for i in range(0, len(x) - n + 1, n):
        chunk = x[i:i + n]
        out = it(chunk, return_seconds=False)
        if in_speech and max_n and (i + n - start_i) >= max_n:
            # Mirror the capture thread's force-cut of long meeting turns.
            spans.append(Span(start_i / sr, (i + n) / sr, x[start_i:i + n]))
            start_i = i + n
        if out is None:
            continue
        if "start" in out:
            in_speech, start_i = True, int(out["start"])
        elif "end" in out and in_speech:
            in_speech = False
            end_i = int(out["end"])
            if end_i > start_i:
                spans.append(Span(start_i / sr, end_i / sr, x[start_i:end_i]))
    if in_speech and len(x) > start_i:        # clip ended mid-speech
        spans.append(Span(start_i / sr, len(x) / sr, x[start_i:]))
    vad_ms = (time.perf_counter() - t0) * 1000.0
    it.reset_states()
    return spans, vad_ms


# ---------------------------------------------------------------------------
# boundary + attribution scoring (pure — unit-testable without audio)
# ---------------------------------------------------------------------------
def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def boundary_metrics(pred: list[dict], truth: list[dict],
                     min_overlap_s: float = 0.1) -> dict:
    """How well did VAD's cuts match the real utterance boundaries?

    `fused_rate` is the one Phase C hangs on: the share of predicted segments
    that span two or more different ground-truth speakers. A fused segment
    produces ONE speaker embedding for two people, so it cannot be attributed
    correctly no matter how good the speaker stack is. If this is ~0 and
    attribution is still wrong, the problem is not segmentation and Sortformer
    would not have helped.
    """
    if not truth:
        return {}
    fused = 0
    for p in pred:
        spk = {t.get("speaker") for t in truth
               if _overlap(p["start"], p["end"], t["start"], t["end"]) >= min_overlap_s}
        spk.discard(None)
        if len(spk) > 1:
            fused += 1
    split = 0
    start_err: list[float] = []
    end_err: list[float] = []
    for t in truth:
        hits = [p for p in pred
                if _overlap(p["start"], p["end"], t["start"], t["end"]) >= min_overlap_s]
        if len(hits) > 1:
            split += 1
        if hits:
            best = max(hits, key=lambda p: _overlap(p["start"], p["end"],
                                                    t["start"], t["end"]))
            start_err.append(abs(best["start"] - t["start"]) * 1000.0)
            end_err.append(abs(best["end"] - t["end"]) * 1000.0)
    covered = sum(1 for t in truth
                  if any(_overlap(p["start"], p["end"], t["start"], t["end"])
                         >= min_overlap_s for p in pred))
    return {
        "n_pred": len(pred), "n_truth": len(truth),
        "fused_rate": round(fused / len(pred), 3) if pred else None,
        "split_rate": round(split / len(truth), 3),
        "missed_rate": round(1 - covered / len(truth), 3),
        "start_mae_ms": round(sum(start_err) / len(start_err), 1) if start_err else None,
        "end_mae_ms": round(sum(end_err) / len(end_err), 1) if end_err else None,
    }


def attribution_error(pred: list[dict], truth: list[dict],
                      min_overlap_s: float = 0.1) -> dict:
    """Wrong-speaker rate over ground-truth utterances.

    speakers.py hands back anonymous labels ("Speaker 1") unless a voiceprint is
    enrolled, so a label is only right or wrong *relative to a mapping*. We take
    the mapping most favourable to the system — greedy 1:1 by contingency count,
    the standard diarization convention — and count what it still gets wrong.
    That deliberately understates the error rate rather than inventing one, so an
    improvement measured here is real.
    """
    if not truth:
        return {}
    pairs: list[tuple[dict, str | None]] = []
    for t in truth:
        hits = [p for p in pred
                if _overlap(p["start"], p["end"], t["start"], t["end"]) >= min_overlap_s]
        best = (max(hits, key=lambda p: _overlap(p["start"], p["end"],
                                                 t["start"], t["end"]))
                if hits else None)
        pairs.append((t, best.get("label") if best else None))

    counts: dict[tuple[str, str], int] = {}
    for t, label in pairs:
        if label and t.get("speaker"):
            k = (label, t["speaker"])
            counts[k] = counts.get(k, 0) + 1
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for (label, spk), _n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if label in mapping or spk in used:
            continue
        mapping[label] = spk
        used.add(spk)

    wrong = unlabeled = 0
    for t, label in pairs:
        if label is None:
            unlabeled += 1
        elif mapping.get(label) != t.get("speaker"):
            wrong += 1
    n = len(pairs)
    return {
        "n_truth": n,
        "attribution_error_rate": round((wrong + unlabeled) / n, 3),
        "wrong_speaker_rate": round(wrong / n, 3),
        "unlabeled_rate": round(unlabeled / n, 3),
        "n_labels": len({l for _t, l in pairs if l}),
        "n_speakers": len({t.get("speaker") for t in truth if t.get("speaker")}),
    }


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def load_manifest(path: Path | None = None) -> list[dict]:
    # Resolved at call time, not bound as a default: a default argument freezes
    # the module global at import, which makes the fixture location impossible
    # to redirect and quietly ignores anything that sets it later.
    path = Path(path) if path else MANIFEST
    if not path.is_file():
        raise SystemExit(
            f"[eval-asr] no manifest at {path}.\n"
            f"           Run `python scripts/eval_asr.py bootstrap` for the "
            f"synthetic probe set, and see {FIXTURE_DIR / 'README.md'} for how "
            f"to add real recordings.")
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"[eval-asr] manifest line {i} is not JSON: {exc}")
    return rows


def validate_manifest(rows: list[dict], base: Path | None = None) -> list[str]:
    """Return a list of problems. Empty list = the fixture set is usable.

    Called by `check` and by `run`, because the failure this prevents is the
    expensive kind: an hour of ASR followed by the discovery that half the clips
    had no ground truth and were silently scored as perfect.
    """
    base = Path(base) if base else FIXTURE_DIR
    problems: list[str] = []
    seen: set[str] = set()
    for i, r in enumerate(rows, 1):
        cid = r.get("id") or f"<row {i}>"
        if not r.get("id"):
            problems.append(f"row {i}: missing 'id'")
        elif cid in seen:
            problems.append(f"{cid}: duplicate id")
        seen.add(cid)
        audio = r.get("audio")
        if not audio:
            problems.append(f"{cid}: missing 'audio'")
        elif not (base / audio).is_file():
            problems.append(f"{cid}: audio not found: {base / audio}")
        cat = r.get("category")
        if cat not in CATEGORIES:
            problems.append(f"{cid}: category {cat!r} not in {CATEGORIES}")
        ch = r.get("channel", "mic")
        if ch not in CHANNELS:
            problems.append(f"{cid}: channel {ch!r} not in {CHANNELS}")
        expect_speech = r.get("expect_speech", cat != "no_speech")
        if expect_speech and not (r.get("reference") or "").strip():
            problems.append(f"{cid}: expects speech but has no 'reference' transcript")
        if not expect_speech and (r.get("reference") or "").strip():
            problems.append(f"{cid}: no-speech clip must have an empty 'reference'")
        for u in r.get("utterances") or []:
            if u.get("start") is None or u.get("end") is None:
                problems.append(f"{cid}: an utterance is missing start/end")
                break
            if u["end"] <= u["start"]:
                problems.append(f"{cid}: utterance end <= start at {u['start']}")
                break
    return problems


# ---------------------------------------------------------------------------
# evaluation — mirrors the live path's stage order
# ---------------------------------------------------------------------------
class Evaluator:
    def __init__(self, engine: ASREngine, *, speakers: bool = False,
                 speakers_live: bool = False, bias: str = "none") -> None:
        self.engine = engine
        self.bias = bias
        self._spk = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        if speakers:
            from app.services.speakers import SpeakerIdentifier
            if speakers_live:
                self._spk = SpeakerIdentifier()
            else:
                # An eval must not enroll clusters into the user's real voiceprint
                # store — it would teach the live system from test fixtures.
                self._tmp = tempfile.TemporaryDirectory(prefix="asr_eval_spk_")
                self._spk = SpeakerIdentifier(voiceprint_dir=Path(self._tmp.name))

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()

    def _reset_speakers(self) -> None:
        """Fresh cluster state per clip (the model stays loaded — reloading ECAPA
        per clip would dominate the runtime). Clusters are per-conversation in
        the live path too; carrying them across unrelated fixtures would let clip
        N's labels depend on clip N-1's."""
        if self._spk is None:
            return
        self._spk._clusters, self._spk._next_id = {}, 1
        self._spk._remote_clusters, self._spk._remote_next_id = {}, 1

    def clip(self, row: dict, base: Path | None = None, *,
             use_vad: bool = True) -> dict:
        from app.services.audio_quality import score as aq_score
        from app.services.ingest_filter import assess
        from app.services.denoise import enhance

        base = Path(base) if base else FIXTURE_DIR
        x, sr = read_wav(base / row["audio"])
        if sr != SR:
            return {"id": row["id"], "error": f"sample rate {sr} != {SR}"}
        dur_s = len(x) / sr
        self._reset_speakers()

        if use_vad:
            spans, vad_ms = vad_segment(x, sr)
        else:
            spans, vad_ms = [Span(0.0, dur_s, x)], 0.0

        recent: list[str] = []
        segs_out: list[dict] = []
        kept_texts: list[str] = []
        raw_texts: list[str] = []
        for sp in spans:
            t_utt = time.perf_counter()
            aq = aq_score(sp.samples, sr) if settings.audio_quality.enabled else None
            asr_audio, denoised = sp.samples, False
            if (aq and settings.denoise.enabled
                    and aq["quality"] in settings.denoise.routes):
                try:
                    y, info = enhance(sp.samples, sr)
                    if info.get("applied"):
                        asr_audio, denoised = y, True
                except Exception as exc:
                    print(f"[eval-asr] denoise error on {row['id']}: {exc}")

            context = " ".join(recent[-3:]) if self.bias == "session" and recent else None
            t_asr = time.perf_counter()
            res = self.engine.transcribe(asr_audio, sr, context=context)
            asr_ms = (time.perf_counter() - t_asr) * 1000.0

            action, kept, no_speech = "empty", False, None
            if res.text and settings.ingest.enabled:
                v = assess(res.text, res.segments)
                action = v.action
                kept = v.action in ("keep", "keep_low_confidence", "needs_user_review")
                # Both filter inputs are recorded, not just the verdict: the
                # confidence calibration (§3.3) re-derives thresholds from these
                # distributions, and it cannot do that from a decision alone.
                no_speech = v.no_speech_prob
            elif res.text:
                action, kept = "keep", True

            label = None
            spk_ms = 0.0
            if self._spk is not None and kept:
                t_spk = time.perf_counter()
                try:
                    label = self._spk.identify(
                        sp.samples, sr, aq=aq,
                        space="remote" if row.get("channel") == "loopback" else "default",
                    )["label"]
                except Exception as exc:
                    print(f"[eval-asr] speaker id error on {row['id']}: {exc}")
                spk_ms = (time.perf_counter() - t_spk) * 1000.0

            raw_texts.append(res.text)
            if kept:
                kept_texts.append(res.text)
                recent.append(res.text)
            segs_out.append({
                "start": round(sp.start, 3), "end": round(sp.end, 3),
                "audio_ms": round(1000.0 * len(sp.samples) / sr, 1),
                "text": res.text, "action": action, "kept": kept, "label": label,
                "quality": aq["quality"] if aq else None, "denoised": denoised,
                "avg_confidence": res.avg_confidence,
                "confidence_kind": res.confidence_kind,
                "no_speech_prob": no_speech,
                "asr_ms": round(asr_ms, 1),
                "n_words": len(res.word_timestamps),
                # The offline share of the utterance-end -> event budget: quality,
                # denoise, ASR, ingest filter, speaker ID. It excludes queue wait
                # and publish, which only exist on the live path — so it is a
                # floor for the §1 target, never a claim to have met it.
                "offline_utterance_ms": round((time.perf_counter() - t_utt) * 1000.0, 1),
                "speaker_ms": round(spk_ms, 1),
            })

        hyp = " ".join(t for t in kept_texts if t).strip()
        expect_speech = row.get("expect_speech", row.get("category") != "no_speech")
        out: dict[str, Any] = {
            "id": row["id"], "category": row.get("category"),
            "channel": row.get("channel", "mic"),
            "expect_speech": expect_speech,
            "duration_s": round(dur_s, 2),
            "vad_ms": round(vad_ms, 1),
            "n_segments": len(spans),
            "segments": segs_out,
            "hyp": hyp,
        }
        if expect_speech:
            out["wer"] = round(wer(row.get("reference", ""), hyp), 3)
            found, total = entity_recall(row.get("entities"), hyp)
            out["ent_found"], out["ent_total"] = found, total
        else:
            # Two numbers, deliberately: what the engine emitted, and what got
            # through. Only the second becomes a false memory, but the first is
            # what a new engine is supposed to fix at the source.
            out["raw_hallucinated"] = any(t.strip() for t in raw_texts)
            out["post_filter_hallucinated"] = bool(hyp)
            out["hallucinated_segments"] = sum(1 for s in segs_out if s["kept"])
        truth = row.get("utterances") or []
        if truth:
            out["boundary"] = boundary_metrics(segs_out, truth)
            if self._spk is not None and any(u.get("speaker") for u in truth):
                out["attribution"] = attribution_error(
                    [s for s in segs_out if s["kept"]], truth)
        return out


def _pct(vals: list[float], q: float) -> float | None:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    i = min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))
    return round(vals[i], 1)


def _mean(vals) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def aggregate(per: list[dict]) -> dict:
    ok = [r for r in per if "error" not in r]
    scored = [r for r in ok if "wer" in r]
    probes = [r for r in ok if not r.get("expect_speech")]
    segs = [s for r in ok for s in r.get("segments", [])]
    asr_ms = [s["asr_ms"] for s in segs]
    utt_ms = [s["offline_utterance_ms"] for s in segs]
    audio_ms = sum(s["audio_ms"] for s in segs)
    ent_f = sum(r.get("ent_found", 0) for r in ok)
    ent_t = sum(r.get("ent_total", 0) for r in ok)
    bounds = [r["boundary"] for r in ok if r.get("boundary")]
    attrs = [r["attribution"] for r in ok if r.get("attribution")]
    agg = {
        "n_clips": len(ok),
        "n_segments": len(segs),
        "audio_minutes": round(audio_ms / 60_000.0, 2),
        "wer": _mean([r.get("wer") for r in scored]),
        "entity_recall": round(ent_f / ent_t, 3) if ent_t else None,
        "raw_hallucination_rate":
            round(sum(1 for r in probes if r["raw_hallucinated"]) / len(probes), 3)
            if probes else None,
        "post_filter_hallucination_rate":
            round(sum(1 for r in probes if r["post_filter_hallucinated"]) / len(probes), 3)
            if probes else None,
        "asr_ms_p50": _pct(asr_ms, 0.5),
        "asr_ms_p90": _pct(asr_ms, 0.9),
        "offline_utterance_ms_p50": _pct(utt_ms, 0.5),
        "offline_utterance_ms_p90": _pct(utt_ms, 0.9),
        # Real-time factor over the whole set: <1 means the engine keeps up with
        # a live stream on this machine. The single most portable cost number.
        "rtf": round(sum(asr_ms) / audio_ms, 3) if audio_ms else None,
    }
    if bounds:
        agg.update(
            fused_rate=_mean([b.get("fused_rate") for b in bounds]),
            split_rate=_mean([b.get("split_rate") for b in bounds]),
            missed_rate=_mean([b.get("missed_rate") for b in bounds]),
            start_mae_ms=_mean([b.get("start_mae_ms") for b in bounds]),
            end_mae_ms=_mean([b.get("end_mae_ms") for b in bounds]),
        )
    if attrs:
        agg.update(
            attribution_error_rate=_mean([a["attribution_error_rate"] for a in attrs]),
            wrong_speaker_rate=_mean([a["wrong_speaker_rate"] for a in attrs]),
            unlabeled_rate=_mean([a["unlabeled_rate"] for a in attrs]),
        )
    return agg


def by_category(per: list[dict]) -> dict:
    cats: dict[str, list[dict]] = {}
    for r in per:
        if "error" not in r:
            cats.setdefault(r.get("category") or "?", []).append(r)
    out = {}
    for cat, rows in sorted(cats.items()):
        scored = [r for r in rows if "wer" in r]
        probes = [r for r in rows if not r.get("expect_speech")]
        segs = [s for r in rows for s in r.get("segments", [])]
        out[cat] = {
            "n": len(rows),
            "wer": _mean([r["wer"] for r in scored]),
            "rtf": (round(sum(s["asr_ms"] for s in segs)
                          / sum(s["audio_ms"] for s in segs), 3)
                    if segs and sum(s["audio_ms"] for s in segs) else None),
            "hallucination_rate":
                round(sum(1 for r in probes if r["post_filter_hallucinated"])
                      / len(probes), 3) if probes else None,
        }
    return out


def config_snapshot(engine: ASREngine) -> dict:
    s = settings
    return {
        "engine_id": getattr(engine, "engine_id", "?"),
        "whisper_model": s.audio.whisper_model,
        "compute_type": s.audio.compute_type, "device": s.audio.device,
        "beam_size": s.audio.beam_size, "language": s.audio.language,
        "vad_threshold": s.audio.vad_threshold,
        "min_silence_ms": s.audio.min_silence_ms,
        "speech_pad_ms": s.audio.speech_pad_ms,
        "aq_enabled": s.audio_quality.enabled, "skip_bad": s.audio_quality.skip_bad,
        "denoise_enabled": s.denoise.enabled, "denoise_routes": list(s.denoise.routes),
        "ingest_enabled": s.ingest.enabled,
        "speakers_enabled": s.speakers.enabled,
    }


def summarize(report: dict) -> None:
    o = report["overall"]
    print(f"\n=== {report.get('tag')} · {report['config']['engine_id']} ===")
    for k, v in o.items():
        print(f"  {k:32s} {v}")
    print(f"\n  {'category':16s} {'n':>3s} {'wer':>7s} {'rtf':>6s} {'halluc':>7s}")
    for cat, m in report["by_category"].items():
        w = "" if m["wer"] is None else f"{m['wer']:.3f}"
        rt = "" if m["rtf"] is None else f"{m['rtf']:.2f}"
        h = "" if m["hallucination_rate"] is None else f"{m['hallucination_rate']:.2f}"
        print(f"  {cat:16s} {m['n']:>3d} {w:>7s} {rt:>6s} {h:>7s}")
    errs = [r for r in report["per_clip"] if "error" in r]
    for r in errs:
        print(f"  ! {r['id']}: {r['error']}")


# Lower is better for these; everything else is higher-is-better.
LOWER_IS_BETTER = {
    "wer", "raw_hallucination_rate", "post_filter_hallucination_rate",
    "asr_ms_p50", "asr_ms_p90", "offline_utterance_ms_p50",
    "offline_utterance_ms_p90", "rtf", "fused_rate", "split_rate", "missed_rate",
    "start_mae_ms", "end_mae_ms", "attribution_error_rate", "wrong_speaker_rate",
    "unlabeled_rate",
}


def compare(path_a: str, path_b: str, *, gate: bool = False,
            wer_gain: float = 0.20) -> int:
    """A/B two reports. With --gate, exit non-zero unless B clears Phase A's bar:
    a >=20 % relative WER reduction and no hallucination regression."""
    a = json.loads(Path(path_a).read_text(encoding="utf-8"))
    b = json.loads(Path(path_b).read_text(encoding="utf-8"))
    oa, ob = a["overall"], b["overall"]
    print(f"A = {path_a}  [{a['config']['engine_id']}]  tag={a.get('tag')}")
    print(f"B = {path_b}  [{b['config']['engine_id']}]  tag={b.get('tag')}\n")
    print(f"  {'metric':32s} {'A':>10s} {'B':>10s} {'d(B-A)':>10s}  verdict")
    for k in sorted(set(oa) | set(ob)):
        va, vb = oa.get(k), ob.get(k)
        if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
            continue
        d = round(vb - va, 4)
        if abs(d) < 1e-9:
            verdict = "same"
        elif k in LOWER_IS_BETTER:
            verdict = "BETTER" if d < 0 else "worse"
        else:
            verdict = "BETTER" if d > 0 else "worse"
        print(f"  {k:32s} {va:>10} {vb:>10} {d:>+10}  {verdict}")
    if not gate:
        return 0
    fails = []
    if oa.get("wer") and ob.get("wer") is not None:
        rel = (oa["wer"] - ob["wer"]) / oa["wer"]
        print(f"\n  relative WER reduction: {rel:+.1%} (need >= {wer_gain:.0%})")
        if rel < wer_gain:
            fails.append(f"WER gain {rel:.1%} < {wer_gain:.0%}")
    for k in ("post_filter_hallucination_rate", "attribution_error_rate"):
        va, vb = oa.get(k), ob.get(k)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and vb > va:
            fails.append(f"{k} regressed {va} -> {vb}")
    if fails:
        print("\n  GATE FAILED: " + "; ".join(fails))
        return 1
    print("\n  GATE PASSED")
    return 0


# ---------------------------------------------------------------------------
# bootstrap — a runnable fixture set before any real audio exists
# ---------------------------------------------------------------------------
def bootstrap(tts: bool = True) -> list[dict]:
    """Generate the synthetic part of the fixture set so the harness runs today.

    The no-speech probes are the honest half: silence, hiss, and a tone are
    exactly the audio Whisper hallucinates "Thank you." over, and they need no
    ground truth beyond "there are no words in here". The TTS clips (Windows
    SAPI, when available) give WER *plumbing* a number to print — they are read
    speech, so treat that number as a smoke test, never as the domain WER the
    engine decision rests on. That number only comes from real recordings.
    """
    rng = np.random.default_rng(20260826)
    clips = FIXTURE_DIR / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    probes = {
        "silence_1": 0.0006 * rng.standard_normal(int(SR * 3.0)),
        "silence_2": 0.0003 * rng.standard_normal(int(SR * 2.0)),
        "fan_hiss": 0.02 * rng.standard_normal(int(SR * 4.0)),
        "keyboard": np.zeros(int(SR * 3.0), dtype=np.float32),
        "tone": 0.05 * np.sin(2 * np.pi * 440
                              * np.arange(int(SR * 2.0)) / SR),
    }
    # crude keystroke clicks: short bursts on an otherwise silent floor
    kb = probes["keyboard"]
    for t in rng.uniform(0.1, 2.9, 18):
        i = int(t * SR)
        kb[i:i + 240] += 0.25 * rng.standard_normal(min(240, len(kb) - i))
    for name, sig in probes.items():
        write_wav(clips / f"{name}.wav", sig.astype(np.float32), SR)
        rows.append({"id": name, "audio": f"clips/{name}.wav",
                     "category": "no_speech", "channel": "mic",
                     "expect_speech": False, "reference": "",
                     "notes": "synthetic hallucination probe (bootstrap)"})

    if tts:
        from eval_voice import sapi_render
        lines = [
            ("close_mic", "The quarterly report is due on Friday afternoon.", []),
            ("close_mic", "Remind me to email Marc the pricing follow up.", ["Marc"]),
            ("far_field", "We should schedule the design review for next week.", []),
            ("fan_noise", "Abby and Justin are joining the Venture Pulse call.",
             ["Abby", "Justin"]),
        ]
        ok = None
        for i, (cat, text, ents) in enumerate(lines):
            base = clips / f"tts_{cat}_{i}.wav"
            if ok is None:
                ok = sapi_render(text, base)
            elif ok:
                sapi_render(text, base)
            if not ok:
                break
            x, _sr = read_wav(base)
            if cat == "far_field":
                x = far_field(x, rng)
                write_wav(base, x, SR)
            elif cat == "fan_noise":
                x = add_noise(x, 8, rng)
                write_wav(base, x, SR)
            rows.append({"id": f"tts_{cat}_{i}", "audio": f"clips/{base.name}",
                         "category": cat, "channel": "mic", "expect_speech": True,
                         "reference": text, "entities": ents,
                         "notes": "synthetic TTS — plumbing smoke test, NOT domain WER"})
        if ok is False:
            print("[eval-asr] SAPI TTS unavailable; wrote no-speech probes only.")

    existing = [r for r in (load_manifest() if MANIFEST.is_file() else [])
                if not (r.get("notes") or "").endswith("(bootstrap)")
                and not (r.get("notes") or "").startswith("synthetic")]
    with open(MANIFEST, "w", encoding="utf-8") as f:
        for r in rows + existing:
            f.write(json.dumps(r) + "\n")
    print(f"[eval-asr] bootstrapped {len(rows)} synthetic clips "
          f"(+{len(existing)} real kept) -> {MANIFEST}")
    return rows


# ---------------------------------------------------------------------------
# smoke — the CI-sized subset
# ---------------------------------------------------------------------------
def smoke(engine_name: str, *, limit: int = 3) -> int:
    """Run the no-speech probes through a real engine and fail if a ghost is
    stored. Returns a process exit code.

    Sized for CI, and deliberately checking the one thing that must hold for
    every engine on every build: **audio with no speech in it must not become a
    memory**. WER needs recorded fixtures and a reference machine; this needs
    neither, because the ground truth is "there are no words in here" and the
    probes are generated from a fixed seed.

    VAD is bypassed on purpose. On pure silence Silero correctly yields zero
    segments, so a VAD-on smoke would pass without ever calling the engine — it
    would be testing the gate in front of the thing it is supposed to test.
    Going straight to the engine asks the harder question: if a probe *does*
    reach ASR, does the filter still refuse it?
    """
    rows = [r for r in load_manifest()
            if not r.get("expect_speech", r.get("category") != "no_speech")]
    if not rows:
        print("[eval-asr] no no-speech probes in the manifest; run `bootstrap`.")
        return 2
    rows = rows[:limit]
    engine = get_engine(engine_name)
    ev = Evaluator(engine)
    print(f"[eval-asr] smoke: {len(rows)} no-speech probes through "
          f"{getattr(engine, 'engine_id', engine_name)}")
    leaked, raw = [], 0
    try:
        for row in rows:
            res = ev.clip(row, use_vad=False)
            if "error" in res:
                print(f"  ! {res['id']}: {res['error']}")
                return 2
            if res.get("raw_hallucinated"):
                raw += 1
            texts = [s["text"] for s in res["segments"] if s["kept"]]
            print(f"  {res['id']:24s} {'LEAKED' if texts else 'ok'}"
                  + (f"  {texts}" if texts else ""))
            if texts:
                leaked.append((res["id"], texts))
    finally:
        ev.close()

    print(f"\n[eval-asr] engine emitted text on {raw}/{len(rows)} probes; "
          f"{len(leaked)} survived the ingest filter.")
    if leaked:
        print("[eval-asr] FAIL - audio with no speech in it became a memory.")
        print("           Either the engine's confidence scale no longer "
              "matches the ingest thresholds (see "
              "scripts/calibrate_asr_confidence.py) or a filter rule regressed.")
        return 1
    print("[eval-asr] PASS - no ghosts stored.")
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="ASR engine acceptance harness (Phase 0)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bootstrap", help="generate the synthetic probe fixtures")
    b.add_argument("--no-tts", action="store_true")

    sub.add_parser("check", help="validate the manifest and its clips")

    r = sub.add_parser("run", help="score one engine on the fixture set")
    r.add_argument("--engine", default=os.environ.get("QUILL_ASR_ENGINE", "whisper"))
    r.add_argument("--tag", default=None, help="report name (default: engine id)")
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--category", action="append", default=None,
                   help="restrict to a category (repeatable)")
    r.add_argument("--speakers", action="store_true",
                   help="run speakers.py in the loop for attribution error")
    r.add_argument("--speakers-live", action="store_true",
                   help="use the REAL voiceprint dir (default: an isolated temp dir)")
    r.add_argument("--bias", choices=("none", "session"), default="none",
                   help="'session' feeds recent text back as ASR context, as the "
                        "live path does; 'none' keeps an engine A/B clean")
    r.add_argument("--no-vad", action="store_true",
                   help="one clip = one utterance (skip live-path segmentation)")
    r.add_argument("--word-timestamps", action="store_true")
    r.add_argument("-o", "--out", default=None)

    k = sub.add_parser("smoke", help="CI: no-speech probes must not be stored")
    k.add_argument("--engine", default=os.environ.get("QUILL_ASR_ENGINE",
                                                      "whisper"))
    k.add_argument("--limit", type=int, default=3)

    c = sub.add_parser("compare", help="A/B two reports")
    c.add_argument("a"); c.add_argument("b")
    c.add_argument("--gate", action="store_true",
                   help="exit 1 unless B clears the Phase A acceptance bar")
    c.add_argument("--wer-gain", type=float, default=0.20)

    args = ap.parse_args()

    if args.cmd == "bootstrap":
        bootstrap(tts=not args.no_tts)
        return 0

    if args.cmd == "check":
        rows = load_manifest()
        problems = validate_manifest(rows)
        speech = [r for r in rows if r.get("expect_speech",
                                           r.get("category") != "no_speech")]
        print(f"[eval-asr] {len(rows)} clips ({len(speech)} with speech, "
              f"{len(rows) - len(speech)} no-speech probes) in {MANIFEST}")
        cats: dict[str, int] = {}
        for r in rows:
            cats[r.get("category") or "?"] = cats.get(r.get("category") or "?", 0) + 1
        for k, v in sorted(cats.items()):
            print(f"           {k:16s} {v}")
        for p in problems:
            print(f"  ! {p}")
        return 1 if problems else 0

    if args.cmd == "smoke":
        return smoke(args.engine, limit=args.limit)

    if args.cmd == "compare":
        return compare(args.a, args.b, gate=args.gate, wer_gain=args.wer_gain)

    # --- run
    rows = load_manifest()
    problems = validate_manifest(rows)
    if problems:
        print("[eval-asr] manifest problems (run `check` for the full list):")
        for p in problems[:10]:
            print(f"  ! {p}")
        return 2
    if args.category:
        rows = [r for r in rows if r.get("category") in set(args.category)]
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("[eval-asr] no clips selected.")
        return 2

    kw = {"word_timestamps": True} if args.word_timestamps else {}
    engine = get_engine(args.engine, **kw)
    ev = Evaluator(engine, speakers=args.speakers,
                   speakers_live=args.speakers_live, bias=args.bias)
    tag = args.tag or getattr(engine, "engine_id", args.engine).replace(":", "-")
    print(f"[eval-asr] running {tag}: {len(rows)} clips")
    per = []
    t0 = time.perf_counter()
    try:
        for i, row in enumerate(rows, 1):
            try:
                res = ev.clip(row, use_vad=not args.no_vad)
            except Exception as exc:
                res = {"id": row.get("id"), "error": repr(exc)}
            per.append(res)
            mark = (f"wer={res['wer']}" if "wer" in res
                    else f"halluc={res.get('post_filter_hallucinated')}")
            print(f"  [{i}/{len(rows)}] {res['id']:24s} "
                  f"{res.get('n_segments', 0):>2} seg  {mark}")
    finally:
        ev.close()

    report = {
        "tag": tag,
        "config": config_snapshot(engine),
        "wall_s": round(time.perf_counter() - t0, 1),
        "options": {"vad": not args.no_vad, "bias": args.bias,
                    "speakers": args.speakers,
                    "word_timestamps": args.word_timestamps},
        "overall": aggregate(per),
        "by_category": by_category(per),
        "per_clip": per,
    }
    summarize(report)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else REPORT_DIR / f"report_{tag}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\n[eval-asr] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
