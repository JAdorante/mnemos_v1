"""Enroll a named speaker's voiceprint so transcripts get tagged with their name.

    python scripts/enroll_speaker.py Marc            # record 10s from the mic
    python scripts/enroll_speaker.py Marc 15         # record 15s
    python scripts/enroll_speaker.py Marc path.wav   # enroll from a WAV file

Re-running for an existing name averages the new sample in (more robust).
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app.config import settings
from app.services.speakers import speakers

SR = settings.audio.sample_rate


def record(seconds: float) -> np.ndarray:
    import sounddevice as sd

    print(f"[enroll] recording {seconds:.0f}s — speak now ...")
    buf = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="float32")
    sd.wait()
    print("[enroll] done recording.")
    return buf[:, 0]


def from_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        if wf.getframerate() != SR:
            raise SystemExit(f"WAV must be {SR} Hz mono; got {wf.getframerate()} Hz")
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    return (pcm.astype(np.float32) / 32768.0)


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        raise SystemExit("usage: enroll_speaker.py <name> [seconds | file.wav]")
    name = argv[1]
    arg = argv[2] if len(argv) > 2 else "10"
    audio = from_wav(arg) if arg.lower().endswith(".wav") else record(float(arg))
    speakers.enroll(name, audio, SR)
    print(f"[enroll] '{name}' enrolled. Known voiceprints: {speakers.enrolled_names()}")


if __name__ == "__main__":
    main(sys.argv)
