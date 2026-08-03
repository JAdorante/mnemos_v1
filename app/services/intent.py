"""IntentRouter — classify a turn's intent so extraction runs only when it can pay.

Today every settled turn gets a full extraction LLM call, even "yeah haha anyway
that was a crazy weekend." That's cost + latency spent to (correctly) return
nothing, and it's extra surface for a false-proactive offer. This router reads a
turn with a fast, local, no-LLM heuristic and decides whether the extractor
should bother.

The one hard rule: **it must never cause a missed fact.** So the skip decision is
precision-only — a turn is skipped ONLY when it carries *zero* actionable/claim
signal (no task verb, no commitment phrase, no time, no number, no proper noun)
and isn't a long monologue. Anything with even a whiff of a signal is sent to the
extractor, which stays the real arbiter (and returns [] for non-facts anyway).
So the router can save calls but can't lose a task; when unsure, it extracts.

    r = classify("Can you text Abby tomorrow?")
    r.intent          # 'actionable'
    r.should_extract  # True
    classify("yeah, haha, so tired").should_extract   # False -> skip the LLM

Intents: actionable | informational | question | small_talk | statement | empty.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Clear transactional verbs. Deliberately NOT the ultra-common light verbs
# (get/make/tell/ask/go/do/have/want) — those live in filler too, and including
# them would send everything to the extractor, defeating the point. A missing
# one only costs a wasted call, never a missed fact.
_ACTION_VERBS = {
    "send", "email", "mail", "text", "call", "phone", "book", "buy", "purchase",
    "schedule", "reschedule", "remind", "cancel", "review", "finish", "submit",
    "pay", "order", "sign", "renew", "register", "ping", "message", "dm",
    "draft", "confirm", "forward", "reply", "respond", "deploy", "ship",
    "upload", "download", "print", "research", "invite", "apply", "contact",
    "notify", "deliver", "install", "fill", "return", "attend", "followup",
    "reach",
}

# Commitment / obligation phrases (substring match on the spaced-normalized text).
_COMMIT_PHRASES = (
    " need to ", " needs to ", " have to ", " has to ", " had to ", " got to ",
    " gotta ", " gonna ", " going to ", " want to ", " wanna ", " would like to ",
    " should ", " must ", " let's ", " lets ", " i'll ", " we'll ", " he'll ",
    " she'll ", " they'll ", " i will ", " we will ", " plan to ", " planning to ",
    " promise ", " don't forget ", " dont forget ", " remember to ",
    " make sure ", " supposed to ", " ought to ", " owe ", " remind me ",
    " follow up ", " reach out ", " by tomorrow ", " by friday ", " by monday ",
)

_TEMPORAL = {
    "today", "tonight", "tomorrow", "yesterday", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "morning", "afternoon", "evening",
    "noon", "midnight", "am", "pm", "oclock", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "weekend", "week", "month",
}

_NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety", "hundred", "thousand", "million", "billion", "dollars",
    "dollar", "bucks", "percent", "cents",
}

_QUESTION_STARTS = {
    "who", "what", "when", "where", "why", "how", "do", "does", "did", "can",
    "could", "would", "should", "is", "are", "was", "were", "will", "shall",
}

_CAP_STOP = {"i", "i'm", "i'll", "i've", "i'd", "ok", "okay", "oh", "ah", "um",
             "uh", "yeah", "well", "so", "hey", "hi", "hello"}

_DIGIT = re.compile(r"\d")
_NONWORD = re.compile(r"[^a-z0-9' ]+")
_WS = re.compile(r"\s+")

# A no-signal turn longer than this is still extracted (a long monologue may bury
# a claim); short no-signal turns are the safe skips.
MAX_SKIP_WORDS = int(os.environ.get("QUILL_INTENT_MAX_SKIP_WORDS", "40"))


def enabled() -> bool:
    return os.environ.get("QUILL_INTENT_ROUTER", "1") not in ("0", "false", "False")


@dataclass
class IntentResult:
    intent: str
    should_extract: bool
    signals: list[str] = field(default_factory=list)
    reason: str = ""


def _norm(text: str) -> str:
    return _WS.sub(" ", _NONWORD.sub(" ", (text or "").lower())).strip()


def _has_proper_noun(raw: str) -> bool:
    """A capitalized token AFTER the first word (sentence-initial caps aren't a
    signal), excluding 'I'/interjections — a weak proper-noun/name hint."""
    toks = (raw or "").split()
    for i, t in enumerate(toks):
        if i == 0:
            continue
        w = t.strip(".,!?;:'\"()[]")
        if len(w) >= 2 and w[0].isupper() and w.lower() not in _CAP_STOP:
            return True
    return False


def classify(text: str) -> IntentResult:
    """Classify a turn and decide whether the extractor should run on it."""
    raw = text or ""
    norm = _norm(raw)
    words = norm.split()
    n = len(words)
    if n == 0:
        return IntentResult("empty", False, reason="no text")

    padded = f" {norm} "
    wordset = set(words)
    has_action = bool(wordset & _ACTION_VERBS)
    has_commit = any(p in padded for p in _COMMIT_PHRASES)
    has_temporal = bool(wordset & _TEMPORAL)
    has_number = bool(_DIGIT.search(raw)) or bool(wordset & _NUMBER_WORDS)
    has_name = _has_proper_noun(raw)
    is_question = raw.strip().endswith("?") or words[0] in _QUESTION_STARTS

    signals: list[str] = []
    for name, present in (("action", has_action), ("commit", has_commit),
                          ("temporal", has_temporal), ("number", has_number),
                          ("name", has_name)):
        if present:
            signals.append(name)

    # Intent label (for telemetry/routing), most-decisive first.
    if has_action or has_commit:
        intent = "actionable"
    elif has_temporal or has_number:
        intent = "informational"      # a claim candidate (price/date/decision)
    elif is_question:
        intent = "question"
    elif has_name:
        intent = "statement"
    else:
        intent = "small_talk"

    # Skip ONLY with zero signal and not a long monologue. `name` counts as a
    # signal too, so social talk that names a person still gets extracted (it may
    # carry a relation/claim). Everything else that reaches here is safe to skip.
    strong = bool(signals)
    should_extract = strong or n > MAX_SKIP_WORDS
    reason = ("has " + ",".join(signals)) if strong else (
        "long monologue" if should_extract else "no actionable signal")
    return IntentResult(intent, should_extract, signals, reason)
