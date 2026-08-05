"""#6 — utterance router: what KIND of speech was this?

The mic pipeline treats every utterance the same — transcribe, extract, maybe
offer. But "Mnemos, text Abby I'll be late" (a COMMAND to act), "note to self:
buy cabernet" (DICTATION to store verbatim), and "yeah the demo went well"
(CONVERSATION to remember) want different handling. This classifies each
transcript into one of four types so the rest of the pipeline can route on it:

    command       a direct instruction to Mnemos to DO something now. Wake-word
                  addressed ("Mnemos, …") or an assistant-directed frame ("can
                  you email …", "please book …"). -> the agent / offer path.
    dictation     verbatim content to capture, flagged by an explicit trigger
                  ("note to self", "take this down"). -> store as a note, and
                  DON'T mine it for tasks (it's content, not conversation).
    conversation  ambient talk worth remembering (the DEFAULT). -> extraction.
    noise         filler / non-speech fragment. -> advisory only (#7 drops).

Design (mirrors intent.py): a fast, local, no-LLM heuristic, and PRECISION-FIRST
on the non-default types. Conversation is the safe fallback, so a bare imperative
("book the venue Friday") stays CONVERSATION (the extractor/offer path already
handles tasks) — only a wake word, an assistant-directed frame, or an explicit
dictation trigger pulls an utterance OUT of conversation. Misrouting real talk to
command/dictation is the costly error, so those require an unambiguous signal.

Observational by default: audio.py stamps `meta["utterance_type"]` on every
transcript (QUILL_UTTERANCE_ROUTER, default on) but nothing ACTS on it until
QUILL_UTTERANCE_ROUTE=1 — same "measure before you change behavior" path #1
(audio_quality) and #3 (ASR bias) took.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

COMMAND = "command"
DICTATION = "dictation"
CONVERSATION = "conversation"
NOISE = "noise"

# Wake words that address Mnemos directly. QUILL heritage kept (env-overridable).
_WAKE = {w for w in os.environ.get("QUILL_WAKE_WORDS", "mnemos,Mnemos,quill").lower()
         .split(",") if w.strip()}
# Optional politeness lead-ins before the wake word ("hey mnemos", "ok quill").
_WAKE_LEADS = ("hey", "ok", "okay", "yo", "hi", "hello")

# Assistant-directed frames — a command WITHOUT a wake word, but only when the
# frame is immediately followed by a real action verb (so "can you believe it"
# stays conversation while "can you email Marc" is a command).
_DIRECTIVE_FRAMES = (
    "can you", "could you", "would you", "will you", "can u", "could u",
    "please", "i need you to", "i want you to", "i'd like you to",
    "help me", "go ahead and", "go and", "mind", "would you mind",
)

# Explicit dictation triggers — verbatim-capture intent. Matched near the start.
_DICTATION_TRIGGERS = (
    "note to self", "take a note", "make a note", "add a note", "new note",
    "take this down", "write this down", "jot this down", "jot down",
    "note that", "for the record", "memo", "dictate", "start dictation",
    "remember this", "capture this",
)

# Pure filler — an utterance of only these (and short) is noise.
_FILLER = {"um", "uh", "erm", "hmm", "mm", "mhm", "uhh", "ah", "oh", "eh", "huh",
           "yeah", "yep", "yup", "nope", "no", "ok", "okay", "so", "well", "like",
           "right", "sure", "haha", "hah", "lol", "anyway", "hey", "hi", "hello"}

_NONWORD = re.compile(r"[^a-z0-9' ]+")
_WS = re.compile(r"\s+")

# Min words below which an all-filler utterance is noise (real short commands like
# "Mnemos stop" are caught by the wake-word branch before this).
_NOISE_MAX_WORDS = int(os.environ.get("QUILL_NOISE_MAX_WORDS", "3"))


@dataclass
class RouteResult:
    type: str
    confidence: float
    signals: list[str] = field(default_factory=list)
    reason: str = ""
    content: str = ""          # command/dictation payload (wake word/trigger stripped)

    def as_meta(self) -> dict:
        m = {"type": self.type, "confidence": self.confidence, "reason": self.reason}
        if self.content:
            m["content"] = self.content
        return m


def enabled() -> bool:
    """Stamp the utterance type on transcripts? On by default (observational)."""
    return os.environ.get("QUILL_UTTERANCE_ROUTER", "1") not in ("0", "false", "False")


def route_enabled() -> bool:
    """ACT on the type (dictation skips extraction, command -> agent)? Off by
    default — stamping is safe, behavior change is opt-in."""
    return os.environ.get("QUILL_UTTERANCE_ROUTE", "0") not in ("0", "false", "False")


def _norm(text: str) -> str:
    return _WS.sub(" ", _NONWORD.sub(" ", (text or "").lower())).strip()


def _action_verbs() -> set[str]:
    try:
        from app.services import intent
        return intent._ACTION_VERBS
    except Exception:                       # keep the router self-sufficient
        return {"send", "email", "text", "call", "book", "buy", "schedule",
                "remind", "cancel", "reply", "message", "order", "pay", "add"}


def _strip_wake(raw: str, norm_words: list[str]) -> tuple[bool, str]:
    """Does the utterance open by addressing Mnemos? Returns (is_wake, content)
    with the lead-in + wake word stripped from the ORIGINAL text."""
    i = 0
    if norm_words and norm_words[0] in _WAKE_LEADS:
        i = 1
    if len(norm_words) > i and norm_words[i] in _WAKE:
        # Rebuild content from the raw text after the matched prefix.
        toks = (raw or "").split()
        content = " ".join(toks[i + 1:]).lstrip(",:;-— ").strip()
        return True, content
    return False, ""


def _dictation_content(norm: str, raw: str) -> str | None:
    """If a dictation trigger opens the utterance, return the content after it."""
    for trig in _DICTATION_TRIGGERS:
        # trigger must be at (or very near) the start — not buried mid-sentence,
        # so "I made a note earlier" isn't treated as a dictation command.
        idx = norm.find(trig)
        if idx != -1 and idx <= 6:
            after = norm[idx + len(trig):].lstrip(" ,:;-—.").strip()
            # Map back to raw text for verbatim content when possible.
            rawlow = (raw or "").lower()
            ridx = rawlow.find(trig)
            if ridx != -1:
                after_raw = (raw[ridx + len(trig):]).lstrip(" ,:;-—.").strip()
                return after_raw or after
            return after
    return None


def _directive_command(norm: str, verbs: set[str]) -> str | None:
    """An assistant-directed frame ('can you …', 'please …') immediately followed
    by an action verb -> a command. Returns the reason frame, else None."""
    for frame in _DIRECTIVE_FRAMES:
        if norm == frame or norm.startswith(frame + " "):
            tail = norm[len(frame):].strip().split()
            # the verb must come right after the frame (within 2 tokens), so a
            # long narrative that merely contains "please" doesn't qualify.
            if any(t in verbs for t in tail[:3]):
                return frame
    return None


def classify(text: str) -> RouteResult:
    """Classify one transcript into command | dictation | conversation | noise."""
    raw = (text or "").strip()
    norm = _norm(raw)
    words = norm.split()
    n = len(words)
    if n == 0:
        return RouteResult(NOISE, 1.0, ["empty"], "no text")

    # 1) COMMAND — wake word (strongest, unambiguous address).
    is_wake, wake_content = _strip_wake(raw, words)
    if is_wake:
        return RouteResult(COMMAND, 0.95, ["wake_word"],
                           "addressed Mnemos directly", content=wake_content)

    # 2) DICTATION — explicit verbatim-capture trigger at the start.
    dctx = _dictation_content(norm, raw)
    if dctx is not None:
        return RouteResult(DICTATION, 0.9, ["trigger"],
                           "explicit dictation trigger", content=dctx)

    # 3) COMMAND — assistant-directed frame + action verb (no wake word).
    verbs = _action_verbs()
    frame = _directive_command(norm, verbs)
    if frame:
        return RouteResult(COMMAND, 0.8, ["directive_frame"],
                           f"assistant-directed ('{frame} …')", content=raw)

    # 4) NOISE — a tiny all-filler fragment (real speech falls through).
    if n <= _NOISE_MAX_WORDS and all(w in _FILLER for w in words):
        return RouteResult(NOISE, 0.85, ["filler"], "short filler fragment")

    # 5) CONVERSATION — the default. Ambient talk (incl. bare-imperative tasks,
    #    which the extractor/offer path already handles).
    return RouteResult(CONVERSATION, 0.7, ["default"], "ambient conversation")
