"""M4 — speaking. Local, offline text-to-speech so vinceo.ai can talk back.

Memory/agent -> text -> TTS -> laptop speaker. Local-first, in keeping with the
rest of the stack: no API key, no network, runs on the CPU. On Windows it drives
SAPI5 directly through `SAPI.SpVoice` (rock-solid and reusable — pyttsx3's event
loop stops speaking after the first utterance when reused); other platforms fall
back to pyttsx3.

Design:
  * ONE dedicated speech thread drains a queue, so utterances never overlap and
    TTS never blocks capture or an HTTP request.
  * Best-effort everywhere: no engine / bad config degrades to a no-op with a
    reason, never a crash.
  * A cloud backend (ElevenLabs/Cartesia) can slot in behind the same speak()
    seam later — the queue + thread stay the same, only `_make_backend` changes.

Config (app/config.py VoiceConfig):
  QUILL_TTS                auto | sapi | pyttsx3 | off   (default auto)
  QUILL_TTS_VOICE          voice-name substring, e.g. Zira / David
  QUILL_TTS_RATE           speed, -10 (slow) .. 10 (fast)   [SAPI]
  QUILL_TTS_VOLUME         0.0 .. 1.0
  QUILL_TTS_MAX_CHARS      cap a single spoken utterance    (default 400)
  QUILL_TTS_SPEAK_REPLIES  1/0 auto-speak the agent's chat replies (default 1)
  QUILL_TTS_SPEAK_KINDS    which reply kinds to speak       (default result,ask)
"""
from __future__ import annotations

import os
import queue
import re
import threading
from typing import Callable

from app.config import settings

_WS = re.compile(r"\s+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
# Strip leading bracketed status tags ("[frame captured]") and markdown emphasis
# so the spoken line sounds like speech, not a log.
_LEAD_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")
_MD = re.compile(r"[*_`#>]+")

# Curated en-US neural voices (edge-tts). Friendly name -> full id. Andrew / Ava /
# Emma / Brian are the newest, most natural ("HD"); Aria is a warm default.
_EDGE_VOICES = {
    "aria": "en-US-AriaNeural", "jenny": "en-US-JennyNeural",
    "guy": "en-US-GuyNeural", "michelle": "en-US-MichelleNeural",
    "ana": "en-US-AnaNeural", "eric": "en-US-EricNeural",
    "christopher": "en-US-ChristopherNeural", "roger": "en-US-RogerNeural",
    "steffan": "en-US-SteffanNeural", "andrew": "en-US-AndrewNeural",
    "ava": "en-US-AvaNeural", "emma": "en-US-EmmaNeural",
    "brian": "en-US-BrianNeural",
}
_EDGE_DEFAULT = "en-US-AriaNeural"


def _clean(text: str) -> str:
    if not text:
        return ""
    t = _LEAD_TAG.sub("", str(text))
    t = _MD.sub("", t)
    return _WS.sub(" ", t).strip()


# --- spoken-text registry (self-echo guard) ---------------------------------
# The speakers play whatever TTS says; the mic AND the system-loopback capture
# both hear it, and the transcription loop would ingest the app's own reply as
# heard speech — a self-referential feedback loop into memory. Every utterance
# is registered here at actual speech start (in the worker thread), so audio
# ingest can ask "did I just say this myself?" before persisting a transcript.
_WORD_RE = re.compile(r"[a-z0-9']+")
_SPOKEN_LOCK = threading.Lock()
_spoken: list[tuple[frozenset, float]] = []   # (word tokens, expires_at_ts)
_SPOKEN_KEEP = 24                             # bounded — utterances, not history

# Offer/ask boilerplate Whisper repeatedly hears back from the speakers.
# Two+ markers ⇒ treat as self-echo even when the registry match is weak
# (live failure July 30 2026: long glued chunks of "I noticed a to-do…" offers).
_OFFER_MARKERS = (
    re.compile(r"\breply\s+['\"]?yes\b", re.I),
    re.compile(r"\bor\s+['\"]?no\s+to\s+skip\b", re.I),
    re.compile(r"\bpause\s+for\s+(?:your\s+)?approval\b", re.I),
    re.compile(r"\bpause\s+before\s+anything\s+irreversible\b", re.I),
    re.compile(r"\bweb[-\s]?doable\b", re.I),
    re.compile(r"\bi\s+noticed\s+a\s+to[-\s]?do\s+list\b", re.I),
    re.compile(r"\breply\s+['\"]?yes\s+to\s+add\s+it\b", re.I),
    re.compile(r"\badd\s+this\s+to\s+your\s+home\s+calendar\b", re.I),
)


def _tokens(text: str) -> frozenset:
    return frozenset(_WORD_RE.findall((text or "").lower()))


def _looks_like_own_offer(text: str) -> bool:
    """True when the transcript is clearly our spoken yes/no offer boilerplate."""
    hits = sum(1 for rx in _OFFER_MARKERS if rx.search(text or ""))
    return hits >= 2


def register_spoken(text: str, now: float | None = None) -> None:
    """Record an utterance the TTS is about to play. TTL covers the estimated
    speech duration plus transcription lag (Whisper windows arrive late)."""
    import time as _time
    toks = _tokens(text)
    if not toks:
        return
    now = _time.time() if now is None else now
    ttl = now + max(30.0, len(text) / 12.0) + 90.0
    with _SPOKEN_LOCK:
        _spoken.append((toks, ttl))
        del _spoken[:-_SPOKEN_KEEP]


def recently_spoken(text: str, *, min_tokens: int = 3, overlap: float = 0.72,
                    now: float | None = None) -> bool:
    """True when `text` looks like a fragment of something TTS just said.

    Checks three signals (any one is enough):
      1. Most of the transcript's tokens appear in one registered utterance
         (Whisper cut a short chunk of our speech).
      2. Most of one registered utterance appears in the transcript
         (Whisper glued our speech into a longer / multi-offer chunk).
      3. Offer-boilerplate fingerprint (≥2 of reply-yes / pause-for-approval /
         web-doable / noticed a to-do / …).

    Segments under `min_tokens` words are never attributed (too little signal).
    QUILL_TTS_ECHO_GUARD=0 disables."""
    if os.environ.get("QUILL_TTS_ECHO_GUARD", "1") in ("0", "false", "False"):
        return False
    raw = text or ""
    if _looks_like_own_offer(raw):
        return True
    toks = _tokens(raw)
    if len(toks) < min_tokens:
        return False
    import time as _time
    now = _time.time() if now is None else now
    with _SPOKEN_LOCK:
        live = [(t, e) for (t, e) in _spoken if e > now]
        _spoken[:] = live
        for spoken, _ in live:
            if not spoken:
                continue
            inter = len(toks & spoken)
            # A: transcript ⊆ spoken (short Whisper fragment of our line)
            if inter / len(toks) >= overlap:
                return True
            # B: spoken ⊆ transcript (our line buried in a longer chunk)
            if inter / len(spoken) >= overlap:
                return True
            # C: long transcripts — accept a slightly looser Jaccard when both
            # sides are substantial (garbled multi-offer glue).
            if len(toks) >= 12 and len(spoken) >= 8:
                union = len(toks | spoken)
                if union and inter / union >= 0.45:
                    return True
        return False


def _rm(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _mci_play(path: str) -> None:
    """Play an audio file (incl. MP3) via Windows MCI — no extra deps. Blocks
    until playback finishes, so the serial speech thread stays in order."""
    import ctypes

    buf = ctypes.create_unicode_buffer(255)

    def cmd(s: str) -> int:
        return ctypes.windll.winmm.mciSendStringW(s, buf, 254, 0)

    alias = "vinceo_tts"
    cmd(f"close {alias}")                       # clear any stale handle
    if cmd(f'open "{path}" type mpegvideo alias {alias}') != 0:
        if cmd(f'open "{path}" alias {alias}') != 0:
            return
    try:
        cmd(f"play {alias} wait")
    finally:
        cmd(f"close {alias}")


class Speaker:
    def __init__(self) -> None:
        self.cfg = settings.voice
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=64)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._broken = False
        self._backend_name: str | None = None

    # --- public API --------------------------------------------------------
    def speak(self, text: str) -> dict:
        """Queue `text` to be spoken aloud. Non-blocking; returns immediately."""
        text = _clean(text)
        if not text:
            return {"spoken": False, "reason": "empty text"}
        if not self.cfg.enabled:
            return {"spoken": False, "reason": "TTS disabled (QUILL_TTS=off)"}
        if self._broken:
            return {"spoken": False, "reason": "no speech engine available"}
        spoken = self._truncate(text)
        self._ensure_thread()
        try:
            self._q.put_nowait(spoken)
        except queue.Full:
            return {"spoken": False, "reason": "still speaking (queue full)"}
        return {"spoken": True, "text": spoken,
                "backend": self._backend_name or self.cfg.backend}

    def maybe_speak_reply(self, kind: str, text: str) -> None:
        """Hook for the agent's _emit: speak the assistant's replies aloud, if
        auto-speak is on and this is a kind we voice (default: result / ask)."""
        if not (self.cfg.enabled and self.cfg.speak_replies):
            return
        if kind not in self.cfg.speak_kinds:
            return
        self.speak(text)

    def voices(self) -> list[str]:
        """Available voice names, for discovery / setting QUILL_TTS_VOICE. Lists
        the offline OS voices plus the neural (edge) options."""
        names: list[str] = []
        try:
            if os.name == "nt":
                import pythoncom
                import win32com.client as wc

                pythoncom.CoInitialize()
                spv = wc.Dispatch("SAPI.SpVoice")
                names += [f"[offline] {t.GetDescription()}" for t in spv.GetVoices()]
            else:
                import pyttsx3
                eng = pyttsx3.init()
                names += [f"[offline] {v.name}" for v in eng.getProperty("voices")]
        except Exception as exc:
            print(f"[voice] could not list local voices ({exc}).")
        try:
            import edge_tts  # noqa: F401
            names += [f"[neural] {k}" for k in sorted(_EDGE_VOICES)]
        except Exception:
            pass
        return names

    def stop(self) -> None:
        """Drop anything queued (best-effort). The current utterance finishes."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    # --- internals ---------------------------------------------------------
    def _truncate(self, text: str) -> str:
        cap = max(40, self.cfg.max_chars)
        if len(text) <= cap:
            return text
        head = text[:cap]
        parts = _SENT_SPLIT.split(head)
        if len(parts) > 1:                      # cut at the last full sentence
            return " ".join(parts[:-1]).strip()
        return head.rsplit(" ", 1)[0].rstrip() + "…"

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="tts",
                                            daemon=True)
            self._thread.start()

    def _run(self) -> None:
        say = self._make_backend()
        if say is None:
            self._broken = True
            return
        print(f"[voice] speaking enabled (backend={self._backend_name}, "
              f"voice={self.cfg.voice or 'default'}).")
        while True:
            text = self._q.get()
            if not text:
                continue
            register_spoken(text)   # self-echo guard: audio ingest checks this
            try:
                say(text)
            except Exception as exc:
                print(f"[voice] speak error ({exc}).")

    def _make_backend(self) -> Callable[[str], None] | None:
        """Resolve and initialize a backend on THIS (worker) thread; return a
        say(text) callable, or None if no engine is available.

        `auto` prefers the neural (edge) voice for the most human sound, then
        falls back to the offline SAPI / pyttsx3 voices if it's unavailable
        (no network, package missing, non-Windows)."""
        inits = {"edge": self._init_edge, "sapi": self._init_sapi,
                 "pyttsx3": self._init_pyttsx3}
        order = [self.cfg.backend] if self.cfg.backend in inits \
            else ["edge", "sapi", "pyttsx3"]
        for name in order:
            try:
                say = inits[name]()
            except Exception as exc:
                print(f"[voice] backend {name!r} init error ({exc}).")
                say = None
            if say is not None:
                self._backend_name = name
                return say
        return None

    def _init_edge(self) -> Callable[[str], None] | None:
        """Neural (online) voice via Microsoft Edge TTS — free, no API key, sounds
        genuinely human. Renders to MP3 and plays through Windows MCI. Probes once
        so we fall back to the offline voice cleanly when there's no network."""
        if os.name != "nt":
            return None                          # MCI playback is Windows-only
        try:
            import edge_tts  # noqa: F401
        except Exception as exc:
            print(f"[voice] edge-tts not installed ({exc}).")
            return None
        import tempfile

        voice_id = self._resolve_edge_voice()
        rate, vol = self._edge_rate(), self._edge_volume()
        probe = os.path.join(tempfile.gettempdir(), "vinceo_tts_probe.mp3")
        try:
            self._edge_render("Ready.", voice_id, rate, vol, probe)
        except Exception as exc:
            print(f"[voice] edge-tts unavailable ({exc}); using offline voice.")
            return None
        finally:
            _rm(probe)

        def say(text: str) -> None:
            import tempfile
            import uuid
            path = os.path.join(tempfile.gettempdir(),
                                f"vinceo_tts_{uuid.uuid4().hex}.mp3")
            try:
                self._edge_render(text, voice_id, rate, vol, path)
                _mci_play(path)
            finally:
                _rm(path)

        print(f"[voice] neural voice: {voice_id}")
        return say

    def _edge_render(self, text: str, voice: str, rate: str, volume: str,
                     path: str) -> None:
        import asyncio

        import edge_tts

        async def _go() -> None:
            kw = {}
            if rate:
                kw["rate"] = rate
            if volume:
                kw["volume"] = volume
            await edge_tts.Communicate(text, voice, **kw).save(path)

        asyncio.run(_go())

    def _resolve_edge_voice(self) -> str:
        want = (self.cfg.voice or "").strip().lower()
        if not want:
            return _EDGE_DEFAULT
        if "neural" in want:                     # a full id was given
            return self.cfg.voice.strip()
        for key, vid in _EDGE_VOICES.items():
            if want == key or want in key:
                return vid
        return _EDGE_DEFAULT

    def _edge_rate(self) -> str:
        pct = int(self.cfg.rate) * 5             # -10..10 -> -50%..+50%
        return f"{pct:+d}%" if pct else ""

    def _edge_volume(self) -> str:
        v = max(0.0, min(1.0, self.cfg.volume))
        return f"-{int(round((1.0 - v) * 100))}%" if v < 1.0 else ""

    def _init_sapi(self) -> Callable[[str], None] | None:
        try:
            import pythoncom
            import win32com.client as wc

            pythoncom.CoInitialize()             # COM must be init'd on this thread
            spv = wc.Dispatch("SAPI.SpVoice")
        except Exception as exc:
            print(f"[voice] SAPI unavailable ({exc}).")
            return None
        try:
            spv.Rate = max(-10, min(10, int(self.cfg.rate)))
            spv.Volume = max(0, min(100, int(round(self.cfg.volume * 100))))
            want = (self.cfg.voice or "").strip().lower()
            if want:
                for tok in spv.GetVoices():
                    if want in tok.GetDescription().lower():
                        spv.Voice = tok
                        break
        except Exception as exc:
            print(f"[voice] SAPI config skipped ({exc}).")

        def say(text: str) -> None:
            spv.Speak(text)                      # synchronous on this thread
        return say

    def _init_pyttsx3(self) -> Callable[[str], None] | None:
        # Fallback for non-Windows. pyttsx3's engine can't be reused across
        # utterances (it stops speaking after the first), so build a fresh one
        # per utterance. Best-effort; Windows uses the SAPI path above.
        try:
            import pyttsx3
            pyttsx3.init()                       # probe that a driver exists
        except Exception as exc:
            print(f"[voice] pyttsx3 unavailable ({exc}); speech disabled.")
            return None

        wpm = int(200 + self.cfg.rate * 12)      # map SAPI-ish rate -> words/min

        def say(text: str) -> None:
            import pyttsx3
            eng = pyttsx3.init()
            try:
                eng.setProperty("rate", wpm)
                eng.setProperty("volume", max(0.0, min(1.0, self.cfg.volume)))
                want = (self.cfg.voice or "").strip().lower()
                if want:
                    for v in eng.getProperty("voices"):
                        if want in v.name.lower():
                            eng.setProperty("voice", v.id)
                            break
                eng.say(text)
                eng.runAndWait()
            finally:
                try:
                    eng.stop()
                except Exception:
                    pass
        return say


speaker = Speaker()


# --- module-level entry points (used by routes + agent_bridge) --------------
def speak(text: str) -> dict:
    return speaker.speak(text)


def maybe_speak_reply(kind: str, text: str) -> None:
    speaker.maybe_speak_reply(kind, text)


def voices() -> list[str]:
    return speaker.voices()


def stop() -> None:
    speaker.stop()
