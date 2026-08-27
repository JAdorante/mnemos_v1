#!/usr/bin/env python
"""Idle footprint of the always-on listen path — the Phase B baseline.

The perception plan's target table asks for "CPU % and RAM of the always-on
listen path" and Phase B's acceptance is "≤ half of baseline". Neither means
anything without the baseline, and no existing harness produces it:
`bench_perception_overhead.py` measures whole-process cost under a *synthetic*
audio burn, which is a contention test, not a measurement of what listening
costs when nobody is speaking.

What "always-on" costs today
----------------------------
Two things run whether or not anyone is talking:

* **VAD**, on every 32 ms chunk, forever. Its cost is a *share of one core* —
  the fraction of real time the CPU must spend just to keep up with the
  microphone. That is the number to quote, not milliseconds per call, because
  it is the only form that says whether the laptop fan comes on.
* **The ASR model's resident memory.** The engine is loaded on `start()` and
  held for the process lifetime, so its weights sit in RAM through hours of
  silence. This is the figure Phase B's two-tier ladder attacks: a 120 M wake
  model resident instead of a 0.6 B one, with the big model woken only for
  confirmed speech.

So the report separates them. `vad_cpu_share` is the always-on compute;
`engine_rss_mb` is the always-on memory. A ladder that halves the second while
leaving the first alone still wins, and the report has to be able to show that.

Deliberately synthetic input
----------------------------
Frames are fed straight into the capture callback instead of opened from a
microphone. That is not a shortcut — it is what makes the number *comparable*:
the same silence, the same frame count, on any machine, in CI, headless, with
no permission prompt and no room tone varying between runs. Real-microphone
overhead (PortAudio, the driver) is not measured here and is not what Phase B
changes.

    python scripts/bench_listen_idle.py                    # 20 s of silence
    python scripts/bench_listen_idle.py --seconds 60 --with-speech
    python scripts/bench_listen_idle.py --json data/listen_idle_baseline.json
    python scripts/bench_listen_idle.py --baseline data/listen_idle_baseline.json
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.config import settings                                    # noqa: E402

SR = 16_000


# ---------------------------------------------------------------------------
# process sampling
# ---------------------------------------------------------------------------
def _proc():
    try:
        import psutil
        return psutil.Process(os.getpid())
    except Exception:
        return None


def rss_mb(proc) -> float | None:
    if proc is None:
        return None
    try:
        return round(proc.memory_info().rss / (1024 * 1024), 1)
    except Exception:
        return None


def cpu_seconds(proc) -> float | None:
    """Process CPU time, user + system.

    CPU *time*, not psutil's `cpu_percent`: percent is sampled over an interval
    and on a mostly-idle path it rounds to zero or jitters with whatever else
    the machine is doing. Accumulated CPU seconds over a known wall-clock window
    divides cleanly into "share of one core" and does not need a warm-up call.
    """
    if proc is None:
        return None
    try:
        t = proc.cpu_times()
        return float(t.user + t.system)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# signal
# ---------------------------------------------------------------------------
def silence(n: int, rng) -> np.ndarray:
    """Room-tone-ish near-silence, not digital zero. Silero's cost is roughly
    input-independent, but a zero vector is not audio any microphone produces
    and it invites a future optimisation that special-cases it."""
    return (0.0006 * rng.standard_normal(n)).astype(np.float32)


def speech_like(n: int, rng) -> np.ndarray:
    """A voiced-sounding burst: harmonic stack under an envelope. It is not
    speech and will not transcribe — it exists to make the VAD fire, so the
    wake path's cost can be separated from the idle path's."""
    t = np.arange(n) / SR
    f0 = 130.0
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 12))
    env = 0.5 * (1 - np.cos(2 * np.pi * np.clip(t / (n / SR), 0, 1)))
    return (0.25 * env * sig / 3.0).astype(np.float32)


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------
def measure_vad(seconds: float, *, realtime: bool, with_speech: bool) -> dict:
    """Push `seconds` of audio through the real VADIterator at the real frame
    size and report what keeping up costs."""
    from silero_vad import load_silero_vad, VADIterator

    cfg = settings.audio
    proc = _proc()
    rng = np.random.default_rng(4242)

    gc.collect()
    rss_before = rss_mb(proc)
    t_load = time.perf_counter()
    model = load_silero_vad(onnx=True)
    vad = VADIterator(model, threshold=cfg.vad_threshold, sampling_rate=SR,
                      min_silence_duration_ms=cfg.min_silence_ms,
                      speech_pad_ms=cfg.speech_pad_ms)
    load_ms = (time.perf_counter() - t_load) * 1000.0
    gc.collect()
    rss_after = rss_mb(proc)

    n = cfg.frame_samples
    frames = int(seconds * SR / n)
    frame_s = n / SR
    # A speech burst in the middle third, when asked for one.
    speech_from = int(frames / 3) if with_speech else frames + 1
    speech_to = int(2 * frames / 3) if with_speech else frames + 1

    per_frame_ms: list[float] = []
    events = 0
    cpu0 = cpu_seconds(proc)
    wall0 = time.perf_counter()
    for i in range(frames):
        chunk = (speech_like(n, rng) if speech_from <= i < speech_to
                 else silence(n, rng))
        t = time.perf_counter()
        out = vad(chunk, return_seconds=False)
        per_frame_ms.append((time.perf_counter() - t) * 1000.0)
        if out is not None:
            events += 1
        if realtime:
            # Pace to the microphone's rate so the CPU-share figure describes
            # a machine that is listening, not one racing through a file.
            slack = frame_s - (time.perf_counter() - t)
            if slack > 0:
                time.sleep(slack)
    wall = time.perf_counter() - wall0
    cpu = (cpu_seconds(proc) or 0.0) - (cpu0 or 0.0)
    vad.reset_states()

    audio_s = frames * frame_s
    return {
        "frames": frames,
        "audio_s": round(audio_s, 2),
        "wall_s": round(wall, 2),
        "realtime": realtime,
        "with_speech": with_speech,
        "vad_events": events,
        "load_ms": round(load_ms, 1),
        "rss_delta_mb": (None if rss_before is None or rss_after is None
                         else round(rss_after - rss_before, 1)),
        "per_frame_ms": {
            "mean": round(statistics.fmean(per_frame_ms), 3),
            "p50": round(statistics.median(per_frame_ms), 3),
            "p95": round(sorted(per_frame_ms)[int(0.95 * (len(per_frame_ms) - 1))], 3),
        },
        # The headline: fraction of one core spent keeping up with the mic.
        # Derived from summed per-call time, so it is unaffected by whatever
        # else the machine was doing during a realtime pace.
        "vad_cpu_share": round(sum(per_frame_ms) / 1000.0 / audio_s, 5),
        "process_cpu_share": (round(cpu / wall, 5) if cpu and wall else None),
    }


def measure_engine(engine_name: str | None) -> dict:
    """Load the configured ASR engine and report what it costs to keep resident.

    Loaded and left loaded, because that is what the pipeline does: `start()`
    loads the engine and holds it for the process lifetime, so its weights sit
    in RAM through hours of silence. That residency, not its transcribe time, is
    the always-on cost Phase B's ladder is trying to shrink.
    """
    from app.services import asr

    proc = _proc()
    gc.collect()
    before = rss_mb(proc)
    t0 = time.perf_counter()
    try:
        engine = asr.make_engine(engine_name)
    except Exception as exc:
        return {"error": f"{exc}"}
    load_ms = (time.perf_counter() - t0) * 1000.0
    gc.collect()
    after = rss_mb(proc)
    return {
        "engine_id": getattr(engine, "engine_id", engine_name or "?"),
        "load_ms": round(load_ms, 1),
        "rss_before_mb": before,
        "rss_after_mb": after,
        "rss_delta_mb": (None if before is None or after is None
                         else round(after - before, 1)),
    }


def run(seconds: float, *, engine: str | None, realtime: bool,
        with_speech: bool, skip_engine: bool) -> dict:
    proc = _proc()
    baseline_rss = rss_mb(proc)
    vad = measure_vad(seconds, realtime=realtime, with_speech=with_speech)
    eng = {"skipped": True} if skip_engine else measure_engine(engine)
    always_on_rss = None
    if not skip_engine and eng.get("rss_after_mb") is not None:
        always_on_rss = eng["rss_after_mb"]
    return {
        "time": round(time.time(), 3),
        "python_rss_mb": baseline_rss,
        "config": {
            "frame_ms": settings.audio.frame_ms,
            "vad_threshold": settings.audio.vad_threshold,
            "min_silence_ms": settings.audio.min_silence_ms,
            "asr_engine": settings.audio.asr_engine,
            "whisper_model": settings.audio.whisper_model,
            "compute_type": settings.audio.compute_type,
            "device": settings.audio.device,
        },
        "vad": vad,
        "engine": eng,
        "always_on": {
            # What a listening Mnemos costs while nobody speaks.
            "cpu_share_of_one_core": vad["vad_cpu_share"],
            "rss_mb": always_on_rss,
            "engine_rss_mb": eng.get("rss_delta_mb"),
        },
    }


# ---------------------------------------------------------------------------
def print_report(res: dict, baseline: dict | None = None) -> None:
    # Pacing changes what the CPU numbers MEAN, so a cross-paced comparison is
    # refused rather than printed with a caveat nobody reads. Racing through
    # frames back-to-back keeps the ONNX session cache-hot; listening leaves a
    # 32 ms gap before every call, and the measured per-frame cost is several
    # times higher as a result. The paced figure is the true one — it is what
    # the machine actually does — and comparing it against a `--fast` run shows
    # a large improvement that is entirely an artefact of the harness.
    paced_match = (not baseline
                   or (baseline.get("vad") or {}).get("realtime")
                   == res["vad"]["realtime"])

    def delta(path: list[str], value, lower_is_better=True, cpu=False):
        if not baseline or not isinstance(value, (int, float)):
            return ""
        if cpu and not paced_match:
            return "   (not compared: different pacing)"
        node = baseline
        for k in path:
            if not isinstance(node, dict) or k not in node:
                return ""
            node = node[k]
        if not isinstance(node, (int, float)) or node == 0:
            return ""
        pct = 100.0 * (value - node) / abs(node)
        if abs(pct) < 0.5:
            return "   = baseline"
        better = (pct < 0) if lower_is_better else (pct > 0)
        return f"   {'▼' if pct < 0 else '▲'} {abs(pct):5.1f}% vs baseline" \
               + ("" if better else "  (worse)")

    v, e, a = res["vad"], res["engine"], res["always_on"]
    c = res["config"]
    print("\n=== always-on listen path ===")
    print(f"  engine        {c['asr_engine']} / {c['whisper_model']} "
          f"({c['compute_type']}, {c['device']})")
    print(f"  frame         {c['frame_ms']} ms · vad threshold "
          f"{c['vad_threshold']} · min silence {c['min_silence_ms']} ms")
    print(f"  audio         {v['audio_s']}s over {v['wall_s']}s wall"
          f"{' (paced to real time)' if v['realtime'] else ' (as fast as possible)'}"
          f" · {v['vad_events']} vad events")

    print("\n  --- compute (runs forever, whether or not anyone speaks) ---")
    share = a["cpu_share_of_one_core"]
    print(f"  vad cpu       {share * 100:6.2f}% of one core"
          + delta(["always_on", "cpu_share_of_one_core"], share, cpu=True))
    if not v["realtime"]:
        print("                ! --fast: frames ran back-to-back, so the ONNX "
              "session stayed\n                  cache-hot and this "
              "understates the real listening cost.\n"
              "                  Re-run paced (drop --fast) for a number to "
              "quote or freeze.")
    print(f"  vad per frame {v['per_frame_ms']['p50']:.3f} ms p50 · "
          f"{v['per_frame_ms']['p95']:.3f} ms p95 "
          f"(budget {settings.audio.frame_ms} ms)")
    if v.get("process_cpu_share") is not None and v["realtime"]:
        print(f"  process cpu   {v['process_cpu_share'] * 100:6.2f}% of one core "
              f"(whole process, includes this harness)")

    print("\n  --- memory (resident through every silent hour) ---")
    if e.get("skipped"):
        print("  engine        skipped (--no-engine)")
    elif e.get("error"):
        print(f"  engine        ! {e['error']}")
    else:
        print(f"  engine        {e['engine_id']} · loaded in "
              f"{e['load_ms'] / 1000:.1f}s")
        print(f"  engine rss    {e['rss_delta_mb']} MB"
              + delta(["always_on", "engine_rss_mb"], e["rss_delta_mb"]))
        print(f"  total rss     {e['rss_after_mb']} MB"
              + delta(["always_on", "rss_mb"], a["rss_mb"]))
    print(f"  vad rss       {v['rss_delta_mb']} MB")

    print("\n  Phase B halves both of these: a small wake model carries the "
          "always-on\n  duty and the big engine loads only for confirmed "
          "speech. Freeze this run\n  as the baseline before changing the "
          "ladder, or the target has no meaning.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Idle footprint of the always-on listen path")
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="seconds of audio to push through VAD")
    ap.add_argument("--fast", action="store_true",
                    help="don't pace to real time — quicker, but the cpu-share "
                         "then understates the real listening cost; use it for "
                         "a smoke run, never for a baseline")
    ap.add_argument("--with-speech", action="store_true",
                    help="include a voiced burst so the wake path fires")
    ap.add_argument("--engine", default=None,
                    help="ASR engine to load (default: QUILL_ASR_ENGINE)")
    ap.add_argument("--no-engine", action="store_true",
                    help="skip loading the model (VAD numbers only)")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--baseline", type=Path, default=None,
                    help="a previous --json run to compare against")
    args = ap.parse_args(argv)

    res = run(args.seconds, engine=args.engine, realtime=not args.fast,
              with_speech=args.with_speech, skip_engine=args.no_engine)

    baseline = None
    if args.baseline and args.baseline.is_file():
        try:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ! unreadable baseline: {exc}")
    print_report(res, baseline)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(f"\n[idle] wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
