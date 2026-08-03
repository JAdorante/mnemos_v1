"""Data-growth watchdog — catch runaway disk usage while it's still small.

The 107 GB lesson: `data/lance` grew ~2 MB per event for months and nothing
noticed until the disk filled. This module walks the repo's data-bearing roots
and flags anything that crossed a size threshold, so growth is surfaced at
boot (a console line) instead of discovered at 100x.

Checks (all env-tunable, QUILL_DATA_WATCH=0 disables):
  * any audited root larger than QUILL_WATCH_MAX_ROOT_GB   (default 10 GB)
  * any single file larger than QUILL_WATCH_MAX_FILE_MB    (default 500 MB)
  * Lance version-manifest overhead above QUILL_WATCH_MAX_LANCE_OVERHEAD_MB
    (default 500 MB) — the specific quadratic-growth signature we were bitten
    by; with vectorstore self-maintenance on, this should never trip.

`startup_check()` runs the walk on a daemon thread (the roots are small when
healthy, so this is cheap; when unhealthy, a slow scan is the least of it).
`scripts/data_audit.py --check` runs the same checks from the CLI/scheduler.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent

# Data-bearing locations relative to the repo root. Non-existent ones skip.
_WATCH_ROOTS = ("data", "data_boot", "data_bridge", "data_test", "data_ui",
                "sessions", "Exec.AI_v1/sessions", "desktop_agent/sessions")


def _cfg_gb(key: str, default: str) -> float:
    return float(os.environ.get(key, default))


def check(root: Path | None = None) -> list[str]:
    """Walk the data roots; return human-readable warnings (empty = healthy)."""
    base = root or _ROOT
    max_root = _cfg_gb("QUILL_WATCH_MAX_ROOT_GB", "10") * 1024 ** 3
    max_file = _cfg_gb("QUILL_WATCH_MAX_FILE_MB", "500") * 1024 ** 2
    max_lance_overhead = _cfg_gb("QUILL_WATCH_MAX_LANCE_OVERHEAD_MB", "500") * 1024 ** 2

    warnings: list[str] = []
    for rel in _WATCH_ROOTS:
        top = base / rel
        if not top.is_dir():
            continue
        total = 0
        big_files: list[tuple[int, str]] = []
        stack = [top]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            size = e.stat().st_size
                            total += size
                            if size > max_file:
                                big_files.append((size, os.path.relpath(e.path, base)))
            except OSError:
                continue
        if total > max_root:
            warnings.append(
                f"{rel} is {total / 1024**3:.1f} GB "
                f"(threshold {max_root / 1024**3:.0f} GB)")
        for size, path in sorted(big_files, reverse=True)[:5]:
            warnings.append(
                f"large file: {path} is {size / 1024**2:.0f} MB "
                f"(threshold {max_file / 1024**2:.0f} MB)")

    # The specific failure mode we've seen: Lance version manifests dwarfing
    # the vectors they describe. Overhead = everything outside data/ fragments.
    lance = base / os.environ.get("QUILL_DATA_DIR", "data") / "lance"
    for tdir in lance.glob("*.lance") if lance.is_dir() else ():
        overhead = sum(
            f.stat().st_size
            for sub in ("_versions", "_transactions")
            if (tdir / sub).is_dir()
            for f in (tdir / sub).iterdir() if f.is_file())
        if overhead > max_lance_overhead:
            warnings.append(
                f"Lance table '{tdir.stem}' holds {overhead / 1024**2:.0f} MB of "
                f"version manifests — self-maintenance may be off "
                f"(QUILL_LANCE_OPTIMIZE_EVERY); run scripts/data_audit.py")
    return warnings


def summarize(problems: list[str], cap: int = 4) -> str | None:
    """One chat-sized message from the warning list (None when healthy).
    Console logging keeps the full list; chat gets the worst few — observed
    live: the 34 GB LoRA-run bloat printed at every boot for 10 days and
    nobody saw it, because a console line is not a surface the user reads."""
    if not problems:
        return None
    shown = problems[:cap]
    more = f"\n(+{len(problems) - cap} more)" if len(problems) > cap else ""
    return ("Heads-up: my data folder is growing beyond normal bounds.\n- "
            + "\n- ".join(shown) + more
            + "\nInspect with: python scripts/data_audit.py")


def startup_check(notify=None) -> None:
    """Boot-time check on a daemon thread; prints, never raises, never blocks.
    `notify(msg)` (optional) surfaces the summary where the user actually
    looks — e.g. the chat pane — instead of only the server console."""
    if os.environ.get("QUILL_DATA_WATCH", "1") in ("0", "false", "False"):
        return

    def _run() -> None:
        try:
            problems = check()
        except Exception as exc:  # pragma: no cover
            print(f"[data_watch] check skipped ({exc}).")
            return
        if problems:
            for p in problems:
                print(f"[data_watch] WARNING: {p}")
            print("[data_watch] inspect with: python scripts/data_audit.py")
            if notify is not None:
                try:
                    msg = summarize(problems)
                    if msg:
                        notify(msg)
                except Exception as exc:
                    print(f"[data_watch] notify skipped ({exc}).")
        else:
            print("[data_watch] data footprint ok.")

    t = threading.Thread(target=_run, name="data_watch", daemon=True)
    t.start()
