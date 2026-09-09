#!/usr/bin/env python3
"""WS2d Phase -1 — what does requesting accessibility cost the USER'S browser?

The brief calls this the one unknown that could change the approach, so it is
a committed script next to bench_perception_overhead.py rather than a one-off.

Method: launch the browser with a heavy tab set, sample the whole browser
PROCESS TREE (browser + renderers + GPU) for CPU and RSS with accessibility
OFF, then again with it ON, and report the delta.

  Linux   "on" = org.a11y.Status IsEnabled + ScreenReaderEnabled.
          MEASURED: with ScreenReaderEnabled false, Chromium publishes only a
          frame with an unpopulated stub child — no document, no URL. Setting
          it true builds the web-contents tree. The tree really is lazy, and
          on this platform the switch is a coarse DESKTOP-WIDE flag, not a
          per-read request.
  Windows "on" = actually running the shipped UIA read loop against the
          browser. That is the granular per-request path, which is why the
          Windows number is the one that decides.

    python3 scripts/bench_browser_a11y.py --browser chromium --seconds 45
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HEAVY_TABS = [
    "https://en.wikipedia.org/wiki/Public_Suffix_List",
    "https://developer.mozilla.org/en-US/docs/Web/API",
    "https://github.com/python/cpython",
    "https://news.ycombinator.com/",
    "https://www.gov.uk/",
    "https://www.bbc.co.uk/news",
]

# First-read judgement bands, not gospel — the point is which bucket we land in.
BANDS = ((1.0, 50.0, "negligible"), (5.0, 250.0, "bounded"))


def _psutil():
    try:
        import psutil
        return psutil
    except Exception as exc:                      # pragma: no cover
        raise SystemExit("psutil required: pip install psutil\n" + str(exc))


def _browser_cmd(browser: str, urls: list[str]) -> list[str] | None:
    home = Path.home()
    chromium = home / ".cache/ms-playwright/chromium-1228/chrome-linux/chrome"
    table = {
        "chromium": [str(chromium), "--no-sandbox", "--no-first-run",
                     "--no-default-browser-check",
                     f"--user-data-dir=/tmp/ws2d-bench-{os.getpid()}"],
        "chrome": ["google-chrome", "--no-first-run",
                   f"--user-data-dir=/tmp/ws2d-bench-{os.getpid()}"],
        "msedge": ["msedge", "--no-first-run"],
        "firefox": ["firefox", "--new-instance", "--profile",
                    f"/tmp/ws2d-bench-ff-{os.getpid()}"],
    }
    cmd = table.get(browser)
    if cmd is None:
        return None
    if browser == "chromium" and not chromium.exists():
        return None
    return cmd + urls


def _sample_tree(psutil, root_pid: int, seconds: float,
                 interval: float = 0.5) -> dict:
    """CPU% and RSS across the browser's whole process tree."""
    try:
        root = psutil.Process(root_pid)
    except Exception:
        return {"error": f"pid {root_pid} gone"}
    cpu, rss, procs = [], [], []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            tree = [root] + root.children(recursive=True)
        except Exception:
            break
        total_cpu, total_rss = 0.0, 0
        alive = 0
        for p in tree:
            try:
                total_cpu += p.cpu_percent(interval=None)
                total_rss += p.memory_info().rss
                alive += 1
            except Exception:
                continue
        cpu.append(total_cpu)
        rss.append(total_rss / (1024 * 1024))
        procs.append(alive)
        time.sleep(interval)
    if not cpu:
        return {"error": "no samples"}
    cpu_used = cpu[1:] or cpu          # first psutil reading is always 0.0
    return {
        "cpu_mean_pct": round(statistics.mean(cpu_used), 2),
        "cpu_max_pct": round(max(cpu_used), 2),
        "rss_mean_mb": round(statistics.mean(rss), 1),
        "rss_max_mb": round(max(rss), 1),
        "processes": max(procs),
        "samples": len(cpu_used),
    }


def _set_linux_a11y(on: bool) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from ws2d.url_sources import AtspiSource
    AtspiSource.set_accessibility(on)
    AtspiSource.set_screen_reader(on)


def _windows_read_loop(seconds: float, every_s: float) -> int:
    """The 'on' condition for Windows: actually run the shipped reader."""
    import ctypes

    from app.perception.uia_url import read_url
    n, deadline = 0, time.time() + seconds
    while time.time() < deadline:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if hwnd:
            read_url(hwnd)
            n += 1
        time.sleep(every_s)
    return n


def _verdict(d_cpu: float, d_rss: float) -> str:
    for cpu_lim, rss_lim, label in BANDS:
        if d_cpu < cpu_lim and d_rss < rss_lim:
            return label
    return "severe"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", default="chromium",
                    help="chromium|chrome|msedge|firefox")
    ap.add_argument("--seconds", type=float, default=45.0,
                    help="sampling window per phase")
    ap.add_argument("--settle", type=float, default=20.0,
                    help="seconds to let the tab set finish loading")
    ap.add_argument("--tabs", type=int, default=len(HEAVY_TABS))
    ap.add_argument("--read-every", type=float, default=2.0,
                    help="Windows: seconds between UIA reads in the on phase")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    psutil = _psutil()
    urls = HEAVY_TABS[:max(1, args.tabs)]
    cmd = _browser_cmd(args.browser, urls)
    if cmd is None:
        print(f"no launch command for {args.browser!r} on this platform")
        return 1

    windows = os.name == "nt"
    if not windows:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from ws2d.url_sources import AtspiSource
        was = (AtspiSource.accessibility_enabled(),
               AtspiSource.screen_reader_enabled())
        print(f"desktop a11y flags before: IsEnabled={was[0]} "
              f"ScreenReader={was[1]}")
        _set_linux_a11y(False)          # OFF phase must start genuinely off
    else:
        was = None

    print(f"launching {args.browser} with {len(urls)} tabs …")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    result: dict = {"browser": args.browser, "platform": os.name,
                    "tabs": len(urls), "seconds_per_phase": args.seconds}
    try:
        time.sleep(args.settle)
        print(f"phase A — accessibility OFF, sampling {args.seconds:.0f}s")
        result["off"] = _sample_tree(psutil, proc.pid, args.seconds)
        print("  ", result["off"])

        print("phase B — accessibility ON")
        if windows:
            import threading
            reads: list = []
            t = threading.Thread(
                target=lambda: reads.append(
                    _windows_read_loop(args.seconds, args.read_every)),
                daemon=True)
            t.start()
            result["on"] = _sample_tree(psutil, proc.pid, args.seconds)
            t.join(timeout=5)
            result["uia_reads"] = reads[0] if reads else 0
        else:
            _set_linux_a11y(True)
            time.sleep(6.0)             # let the renderers build their trees
            result["on"] = _sample_tree(psutil, proc.pid, args.seconds)
        print("  ", result["on"])
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        if not windows and was is not None:
            AtspiSource.set_accessibility(bool(was[0]))
            AtspiSource.set_screen_reader(bool(was[1]))
            print(f"restored desktop a11y flags to {was}")

    off, on = result.get("off", {}), result.get("on", {})
    if "error" in off or "error" in on:
        print(json.dumps(result, indent=2))
        return 1
    d_cpu = round(on["cpu_mean_pct"] - off["cpu_mean_pct"], 2)
    d_rss = round(on["rss_mean_mb"] - off["rss_mean_mb"], 1)
    result["delta"] = {"cpu_pp": d_cpu, "rss_mb": d_rss}
    result["verdict"] = _verdict(d_cpu, d_rss)
    result["consequence"] = {
        "negligible": "proceed as written",
        "bounded": "cut read frequency further, or read only on allowlisted hosts",
        "severe": "escalate — the plan changes shape",
    }[result["verdict"]]

    print("\n" + json.dumps(result, indent=2))
    print(f"\nPHASE -1 VERDICT: {result['verdict'].upper()} "
          f"(ΔCPU {d_cpu:+} pp, ΔRSS {d_rss:+} MB) → {result['consequence']}")
    if not windows:
        print("NOTE: Linux measures the coarse desktop a11y flag. Windows UIA "
              "is a per-request path — rerun there for the deciding number.")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
