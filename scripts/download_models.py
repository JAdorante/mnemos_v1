"""Pre-download Sparrow's models so the first live run starts instantly.

    python scripts/download_models.py            # downloads the configured model
    python scripts/download_models.py base small # downloads specific sizes
    python scripts/download_models.py --check    # report what is already cached
    python scripts/download_models.py --retries 5

Downloads:
  * faster-whisper model(s)  -> HF cache (Systran/faster-whisper-<size>)
  * Silero VAD (ONNX)        -> silero_vad package cache
  * ECAPA-TDNN speaker embedder and the MiniLM sentence embedder, when enabled

Safe to re-run, and *designed* to be re-run: retries resume from the partial
file rather than restarting. The logic lives in `app.services.model_fetch` so
the packaged desktop build's first-run page can call the same code — it cannot
shell out to this script, because `scripts/` is not in the bundle and a frozen
`sys.executable` is `Sparrow.exe`. This file is the CLI over it.

Exit codes: 0 everything cached, 1 something still missing after retries.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python scripts/download_models.py` from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.model_fetch import (  # noqa: E402
    DEFAULT_RETRIES, check, default_sizes, fetch_models,
)


def _print(msg: str) -> None:
    print(f"[models] {msg}", flush=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Pre-download (or check) Sparrow's local models.")
    ap.add_argument("sizes", nargs="*",
                    help="whisper sizes (default: the configured model)")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help=f"attempts per model (default {DEFAULT_RETRIES})")
    ap.add_argument("--check", action="store_true",
                    help="report what is cached and exit; downloads nothing")
    args = ap.parse_args(argv[1:])

    sizes = args.sizes or default_sizes()
    if args.check:
        missing = check(sizes, log=_print)
        if missing:
            _print(f"{len(missing)} model(s) still to download: "
                   f"{', '.join(missing)}")
            return 1
        _print("every model is cached — first run starts without downloading.")
        return 0

    ok = fetch_models(sizes=sizes, retries=args.retries, log=_print)
    if not ok:
        return 1
    _print("first `python run_audio.py` will now start without downloading.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
