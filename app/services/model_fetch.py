"""Fetch the local model weights, with retries that resume.

This is roughly 700 MB over a tester's home connection, and the pilot blocker
is the half of it that arrives before a hotel wifi drop. Two properties:

* **Every step retries with backoff, and a retry resumes.** ``huggingface_hub``
  writes to ``*.incomplete`` blobs and continues from the byte it reached, so
  attempt two costs the remainder rather than another full download. A step
  that exhausts its retries does not abort the run — the remaining models are
  still fetched and the caller is told what is missing.

* **Cached steps are free and say so.** :func:`check` probes with the network
  forced off, so an installer or a support call can ask what is left to
  download without starting one.

It lives under ``app/services`` rather than in ``scripts/`` because the
packaged desktop build has to run it too: ``scripts/`` is not in the bundle,
and a frozen ``sys.executable`` is ``Sparrow.exe``, so the first-run
``/bootstrap`` page cannot shell out to a script. One implementation, imported
by both the CLI and the server.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Iterable

# Quiet the noisy first-run warnings, but keep the progress bar for the CLI.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "0")

DEFAULT_RETRIES = 3
BACKOFF_BASE_S = 4.0

Logger = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def download_whisper(size: str) -> None:
    from faster_whisper import WhisperModel

    # Constructing the model downloads + caches the weights.
    WhisperModel(size, device="cpu", compute_type="int8")


def download_vad() -> None:
    from silero_vad import load_silero_vad

    load_silero_vad(onnx=True)


def download_speaker_model() -> None:
    from app.services.speakers import speakers

    speakers._load()


def download_embedder() -> None:
    from app.services.embeddings import embedder

    embedder._load()


def default_sizes() -> list[str]:
    from app.config import settings
    return [settings.audio.whisper_model]


def steps(sizes: Iterable[str] | None = None) -> list[tuple[str, Callable[[], None]]]:
    """(label, callable) in download order — smallest first.

    VAD leads deliberately: it is seconds, and a tester watching a stalled
    progress line learns more from one thing finishing than from a 500 MB bar.
    """
    from app.config import settings

    sizes = list(sizes or default_sizes())
    out: list[tuple[str, Callable[[], None]]] = [("Silero VAD (ONNX)", download_vad)]
    for size in sizes:
        out.append((f"faster-whisper '{size}'", (lambda s=size: download_whisper(s))))
    if settings.speakers.enabled:
        out.append(("ECAPA-TDNN speaker embedder", download_speaker_model))
    if settings.memory.semantic:
        out.append(("sentence-transformers embedder", download_embedder))
    return out


def attempt(label: str, fn: Callable[[], None], *, retries: int,
            log: Logger = _noop) -> tuple[bool, str]:
    """Run one download step, retrying with backoff. Returns (ok, note)."""
    last = ""
    for n in range(1, max(1, retries) + 1):
        if n == 1:
            log(f"fetching {label} ...")
        else:
            # Say "resuming": a tester watching a large step appear to start
            # over for the third time is the moment they give up on the install.
            log(f"retry {n}/{retries} for {label} — resuming from what already "
                f"downloaded ...")
        try:
            fn()
            log(f"{label} ready.")
            return (True, "")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            log(f"{label} failed ({last}).")
            if n < retries:
                delay = BACKOFF_BASE_S * (2 ** (n - 1))
                log(f"waiting {delay:.0f}s before retrying ...")
                time.sleep(delay)
    return (False, last)


def fetch_models(*, sizes: Iterable[str] | None = None,
                 retries: int = DEFAULT_RETRIES,
                 log: Logger = _noop) -> bool:
    """Download everything missing. True when every step is cached."""
    plan = steps(sizes)
    failed: list[tuple[str, str]] = []
    for label, fn in plan:
        ok, note = attempt(label, fn, retries=retries, log=log)
        if not ok:
            failed.append((label, note))
    done = len(plan) - len(failed)
    log(f"{done}/{len(plan)} ready.")
    for label, note in failed:
        log(f"still missing — {label}: {note}")
    if failed:
        log("re-run to resume — completed downloads are kept and partial ones "
            "continue where they stopped.")
    return not failed


def check(sizes: Iterable[str] | None = None, *, log: Logger = _noop) -> list[str]:
    """Report which steps are cached, without touching the network.

    Returns the labels still missing. Forces the hubs offline first, so a
    "what is left?" question can never start a download.
    """
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    missing: list[str] = []
    for label, fn in steps(sizes):
        try:
            fn()
            log(f"cached      {label}")
        except KeyboardInterrupt:
            raise
        except Exception:
            log(f"NOT cached  {label}")
            missing.append(label)
    return missing
