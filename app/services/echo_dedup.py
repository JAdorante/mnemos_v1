"""Cross-source echo dedupe — the mic hears what the speakers play.

With loopback capture on (QUILL_SYSTEM_AUDIO=1), the same meeting/video audio
lands twice: once clean via source=audio.system, and once through the room via
the mic — where speaker-ID then mis-attributes the remote voice to a local
voiceprint. Whichever transcript publishes FIRST wins; the second one, if its
text closely matches a recent counterpart from the other source group, is
dropped with drop_reason="echo" (telemetry keeps the count visible).

Matching is deliberately dumb and cheap: normalized text similarity or
containment within a sliding time window. Different VAD segmentation between
the two pipelines means one utterance can arrive as two fragments — that's why
containment counts as a match.

Off unless system audio is on; QUILL_ECHO_DEDUP=0 disables explicitly.
"""
from __future__ import annotations

import difflib
import re
import threading
import time

from app.config import settings

_lock = threading.Lock()
_recent: list[tuple[str, float, str]] = []   # (normalized_text, wall_ts, group)

_WORD = re.compile(r"[^a-z0-9 ]+")
# Below this length, only exact matches count — "okay"/"thank you" are common
# to both real speech and videos, and must not shadow the user's own words.
MIN_FUZZY_LEN = 12


def enabled() -> bool:
    sa = settings.system_audio
    return bool(sa.enabled and getattr(sa, "echo_dedup", True))


def clear() -> None:
    with _lock:
        _recent.clear()


def _norm(text: str) -> str:
    t = _WORD.sub(" ", (text or "").lower())
    return " ".join(t.split())


def _group(source: str) -> str:
    return "system" if (source or "").startswith("audio.system") else "mic"


def _matches(a: str, b: str, threshold: float) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) < MIN_FUZZY_LEN or len(b) < MIN_FUZZY_LEN:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if short in long_:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def check_and_register(text: str, source: str, *, now: float | None = None,
                       window_s: float | None = None,
                       threshold: float | None = None) -> str | None:
    """Return the OTHER source group's name if `text` echoes it (caller drops),
    else register the text for this group and return None (caller publishes)."""
    now = time.time() if now is None else now
    if window_s is None:
        window_s = float(getattr(settings.system_audio, "echo_window_s", 10.0))
    if threshold is None:
        threshold = float(getattr(settings.system_audio, "echo_similarity", 0.8))
    norm = _norm(text)
    group = _group(source)
    if not norm:
        return None
    with _lock:
        _recent[:] = [(t, ts, g) for t, ts, g in _recent if now - ts <= window_s]
        for t, _ts, g in _recent:
            if g != group and _matches(norm, t, threshold):
                return g
        _recent.append((norm, now, group))
        # An echo match is two-sided; keep the registry from growing unbounded
        # even under a transcript flood.
        if len(_recent) > 200:
            del _recent[:-200]
    return None
