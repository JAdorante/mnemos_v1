"""Pre-download Mnemos's models so the first live run starts instantly.

    python scripts/download_models.py            # downloads the configured model
    python scripts/download_models.py base small # downloads specific sizes

Downloads:
  * faster-whisper model(s)  -> HF cache (Systran/faster-whisper-<size>)
  * Silero VAD (ONNX)        -> silero_vad package cache

Safe to re-run: already-cached files are skipped.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running as `python scripts/download_models.py` from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Quiet the noisy first-run warnings.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "0")  # keep the progress bar

from app.config import settings  # noqa: E402


def download_whisper(size: str) -> None:
    from faster_whisper import WhisperModel

    print(f"[models] fetching faster-whisper '{size}' ...")
    # Constructing the model downloads + caches the weights.
    WhisperModel(size, device="cpu", compute_type="int8")
    print(f"[models] faster-whisper '{size}' ready.")


def download_vad() -> None:
    from silero_vad import load_silero_vad

    print("[models] fetching Silero VAD (ONNX) ...")
    load_silero_vad(onnx=True)
    print("[models] Silero VAD ready.")


def download_speaker_model() -> None:
    print("[models] fetching ECAPA-TDNN speaker embedder ...")
    from app.services.speakers import speakers

    speakers._load()
    print("[models] ECAPA speaker embedder ready.")


def main(argv: list[str]) -> None:
    sizes = argv[1:] or [settings.audio.whisper_model]
    download_vad()
    for size in sizes:
        download_whisper(size)
    if settings.speakers.enabled:
        download_speaker_model()
    if settings.memory.semantic:
        print("[models] fetching sentence-transformers embedder ...")
        from app.services.embeddings import embedder

        embedder._load()
        print("[models] embedder ready.")
    print(f"\n[models] done. cached: VAD + whisper {sizes}")
    print("[models] first `python run_audio.py` will now start without downloading.")


if __name__ == "__main__":
    main(sys.argv)
