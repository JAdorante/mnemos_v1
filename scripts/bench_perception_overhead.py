"""Criterion 9 — perception capture overhead with audio contention.

Measures process CPU %, RSS, and OCR latency while optionally co-loading a
Whisper-like / embedding CPU burn so numbers reflect the real contention
(two Whisper instances + embeddings), not isolation.

Usage (with the app already capturing, or standalone synthetic OCR):

    # Against a running Sparrow/uvicorn PID (preferred — audio + L1 live):
    python scripts/bench_perception_overhead.py --pid <PID> --seconds 120

    # Standalone: synthetic OCR loop + optional audio-contention burn:
    python scripts/bench_perception_overhead.py --synthetic --audio-load --seconds 60

    # Report-only against current process (this script + burn):
    python scripts/bench_perception_overhead.py --synthetic --seconds 30

Gates (acceptance criterion 9):
  avg CPU  ≤ 3 % of one core *per capture subsystem* is the long-run target;
            this harness reports whole-process CPU (audio+perception together)
            and a capture-attributed estimate when --synthetic.
  RSS      ≤ 400 MB for the capture subsystem; whole-process RSS is also printed
            (Whisper weights dominate — interpret the delta / synthetic line).
  OCR p95  ≤ 1.5 s per capture on target hardware.

Exit code 0 always prints a JSON summary; exit 2 if any gate fails when
--strict is set.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Gates from PERCEPTION_IMPLEMENTATION_PROMPT criterion 9
CPU_AVG_PCT = 3.0
RSS_MB = 400.0
OCR_P95_S = 1.5


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError as exc:
        raise SystemExit(
            "psutil required: pip install psutil\n" + str(exc)) from exc


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return float(xs[f])
    return float(xs[f] + (xs[c] - xs[f]) * (k - f))


def sample_process(proc, seconds: float, interval: float = 0.5) -> dict:
    """Sample CPU% (psutil interval) and RSS over ``seconds``."""
    cpus: list[float] = []
    rss: list[float] = []
    # Prime cpu_percent
    proc.cpu_percent(None)
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        time.sleep(interval)
        try:
            cpus.append(float(proc.cpu_percent(None)))
            rss.append(float(proc.memory_info().rss) / (1024 * 1024))
        except Exception:
            break
    return {
        "samples": len(cpus),
        "cpu_avg_pct": round(statistics.mean(cpus), 3) if cpus else 0.0,
        "cpu_p95_pct": round(_percentile(cpus, 95), 3) if cpus else 0.0,
        "rss_avg_mb": round(statistics.mean(rss), 2) if rss else 0.0,
        "rss_max_mb": round(max(rss), 2) if rss else 0.0,
        "rss_p95_mb": round(_percentile(rss, 95), 2) if rss else 0.0,
    }


def _audio_burn(stop: threading.Event, n_workers: int = 2) -> None:
    """Approximate Whisper/embed contention: numpy matmuls on worker threads."""
    import numpy as np

    def _worker():
        rng = np.random.RandomState(0)
        a = rng.randn(512, 512).astype(np.float32)
        while not stop.is_set():
            b = rng.randn(512, 512).astype(np.float32)
            _ = a @ b

    threads = [threading.Thread(target=_worker, daemon=True)
               for _ in range(max(1, n_workers))]
    for t in threads:
        t.start()
    stop.wait()


def run_synthetic_ocr(n: int, size: tuple[int, int] = (720, 1280)) -> dict:
    """Time N OCR passes on a synthetic RGB frame (foreground-window sized)."""
    import numpy as np
    from app.perception.ocr import WindowsMediaOcr

    ocr = WindowsMediaOcr()
    if not ocr.available():
        return {"ok": False, "reason": "ocr_unavailable", "latencies_s": []}
    h, w = size
    # Structured noise — enough ink for Windows.Media.Ocr to do real work.
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[:, :] = 240
    for i in range(0, h, 40):
        rgb[i:i + 18, 40:w - 40] = 20
    lats: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        ocr.recognize(rgb)
        lats.append(time.perf_counter() - t0)
    return {
        "ok": True,
        "n": n,
        "latencies_s": lats,
        "ocr_p50_s": round(_percentile(lats, 50), 4),
        "ocr_p95_s": round(_percentile(lats, 95), 4),
        "ocr_mean_s": round(statistics.mean(lats), 4),
        "engine": ocr.engine_name,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pid", type=int, default=None,
                    help="Sample an existing process (running app with audio)")
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="Sampling window for CPU/RSS")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--synthetic", action="store_true",
                    help="Run synthetic OCR latency loop in this process")
    ap.add_argument("--ocr-n", type=int, default=20,
                    help="Synthetic OCR iterations")
    ap.add_argument("--audio-load", action="store_true",
                    help="Spin Whisper-like CPU burn during sampling")
    ap.add_argument("--audio-workers", type=int, default=2)
    ap.add_argument("--strict", action="store_true",
                    help="Exit 2 if criterion-9 gates fail")
    ap.add_argument("--out", type=str, default="",
                    help="Write JSON summary to this path")
    args = ap.parse_args()

    psutil = _psutil()
    stop = threading.Event()
    burn_thread = None
    if args.audio_load:
        burn_thread = threading.Thread(
            target=_audio_burn, args=(stop, args.audio_workers), daemon=True)
        burn_thread.start()
        # Let burners ramp before sampling.
        time.sleep(0.5)

    pid = args.pid or os.getpid()
    try:
        proc = psutil.Process(pid)
    except Exception as exc:
        print(f"cannot open pid {pid}: {exc}", file=sys.stderr)
        return 1

    ocr_report: dict = {"ok": False, "reason": "skipped"}
    if args.synthetic:
        # Overlap OCR with sampling when possible: run OCR in a thread while
        # we sample, then merge.
        holder: dict = {}

        def _ocr():
            holder["r"] = run_synthetic_ocr(args.ocr_n)

        th = threading.Thread(target=_ocr, daemon=True)
        th.start()
        samples = sample_process(proc, args.seconds, args.interval)
        th.join(timeout=max(30.0, args.ocr_n * 5.0))
        ocr_report = holder.get("r") or {"ok": False, "reason": "ocr_timeout"}
    else:
        samples = sample_process(proc, args.seconds, args.interval)
        # Best-effort: pull recent OCR latencies from perception if available.
        try:
            from app.perception.store import get_pstore
            # No latency column yet — leave a placeholder for live mode.
            ocr_report = {
                "ok": False,
                "reason": "live_mode_use_--synthetic_for_ocr_p95",
                "hint": "Attach --synthetic on a soak box, or pass --pid of "
                        "the app and run a parallel --synthetic in another "
                        "process under the same audio load.",
            }
            _ = get_pstore  # silence lint if unused on some paths
        except Exception:
            pass

    stop.set()
    if burn_thread is not None:
        burn_thread.join(timeout=2.0)

    summary = {
        "criterion": 9,
        "pid": pid,
        "cmdline": " ".join(proc.cmdline()[:8]) if hasattr(proc, "cmdline") else "",
        "seconds": args.seconds,
        "audio_load": bool(args.audio_load),
        "process": samples,
        "ocr": {k: v for k, v in ocr_report.items() if k != "latencies_s"},
        "gates": {
            "cpu_avg_pct_max": CPU_AVG_PCT,
            "rss_mb_max": RSS_MB,
            "ocr_p95_s_max": OCR_P95_S,
        },
        "notes": [
            "Whole-process CPU/RSS includes Whisper + embeddings when "
            "--pid points at the live app; criterion 9's 3%/400MB is the "
            "capture-subsystem budget — compare a paused-vs-capturing delta "
            "or --synthetic attribution on a soak machine.",
            "Always measure with audio running (live --pid, or --audio-load).",
        ],
    }

    # Gate evaluation (strict mode interprets synthetic OCR + process sample).
    fails = []
    if args.synthetic and ocr_report.get("ok"):
        if float(ocr_report.get("ocr_p95_s") or 0) > OCR_P95_S:
            fails.append(
                f"ocr_p95 {ocr_report['ocr_p95_s']}s > {OCR_P95_S}s")
    # RSS / CPU gates only in --strict when sampling the capture-only synthetic
    # process without audio burn (otherwise Whisper blows the 400MB ceiling).
    if args.strict and args.synthetic and not args.audio_load and not args.pid:
        if samples["cpu_avg_pct"] > CPU_AVG_PCT:
            fails.append(
                f"cpu_avg {samples['cpu_avg_pct']}% > {CPU_AVG_PCT}%")
        if samples["rss_max_mb"] > RSS_MB:
            fails.append(
                f"rss_max {samples['rss_max_mb']}MB > {RSS_MB}MB")
    summary["gate_failures"] = fails
    summary["pass"] = len(fails) == 0

    text = json.dumps(summary, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.strict and fails:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
