"""Fast, deterministic Phone Link goal parsing — no LLM.

Covers the common typed/spoken shapes ("text Abby I'm late", "text Mom that
I'll call", "open Phone Link") so the agent can skip `parse_phone_goal` and
often `clean_message`. Falls back to the model for anything ambiguous.
"""
from __future__ import annotations

import re

# Leading verb for outgoing SMS.
_TEXT_VERB = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
    r"(?:text|sms|i-?message|imessage|message|msg)\s+",
    re.I,
)
_REPLY_VERB = re.compile(
    r"^\s*(?:please\s+)?(?:reply(?:\s+to)?|respond(?:\s+to)?)\s+",
    re.I,
)
_READ_VERB = re.compile(
    r"^\s*(?:please\s+)?(?:read|show|check|list)\s+"
    r"(?:my\s+)?(?:texts?|messages?|sms)\b",
    re.I,
)
_OPEN_PHONE = re.compile(
    r"^\s*(?:please\s+)?(?:open|launch|start)\s+"
    r"(?:the\s+)?(?:phone\s*link|phonelink)\b",
    re.I,
)
# Explicit body separators after the recipient.
_BODY_SEP = re.compile(
    r"^(?P<who>.+?)\s+(?:that|saying|to say|:|—|-)\s+(?P<body>.+)$",
    re.I | re.S,
)
_FROM_WHO = re.compile(r"\bfrom\s+(.+)$", re.I)
# Tokens that start a message body, not a surname.
_BODY_START = frozenset({
    "i", "i'm", "i’m", "im", "ill", "i'll", "i’ll", "ive", "i've", "i’ve",
    "we", "we're", "we’re", "you", "he", "she", "they", "it", "a", "an", "the",
    "please", "hey", "hi", "hello", "dear", "ok", "okay", "can", "can't",
    "cant", "could", "would", "will", "just", "on", "at", "in", "for", "to",
    "my", "your", "thanks", "thank", "sorry", "yes", "no", "see", "meet",
    "running", "love", "miss", "call", "calling", "be", "am", "is", "are",
})
# Anaphors that refer back to earlier conversation ("text that to Justin") —
# never a contact name, and the content they stand for needs the model's
# context / session transcript to resolve.
_ANAPHOR = re.compile(r"^(?:that|this|it)\b", re.I)
# Bodies that point at a prior assistant reply ("the message you just told me")
# — never send these strings literally as SMS.
_PRIOR_REPLY_BODY = re.compile(
    r"^(?:"
    r"that|this|it|"
    r"what\s+you\s+just\s+(?:said|told|wrote|answered)|"
    r"(?:the\s+)?(?:message|text|sms|reply|answer|summary|description|"
    r"overview|explanation|thing|one)"
    r"(?:\s+of\s+[\w\s'’-]{1,60}?)?"          # "the message OF YOUR DESCRIPTION …"
    r"(?:\s+(?:that\s+|which\s+)?you\s+(?:just\s+)?"   # "… THAT you gave …"
    r"(?:told|said|gave|sent|wrote|shared)"
    r"(?:\s+(?:me|us))?(?:\s+(?:above|earlier|before))?)?"
    r")\s*[.?!]*$",
    re.I,
)
# Whole-goal cues that the user is referring to prior session content
# ("recall what you just said", "text Hugh that message you just told me").
_REFERS_TO_PRIOR = re.compile(
    r"(?:"
    r"\b(?:the\s+)?(?:message|text|sms|reply|answer|summary|description|"
    r"overview|explanation)"
    r"(?:\s+of\s+[\w\s'’-]{1,60}?)?"          # "the message of your description…"
    r"\s+(?:that\s+|which\s+)?you\s+(?:just\s+)?(?:told|said|gave|sent|wrote)\b"
    r"|\bwhat\s+you\s+just\s+(?:said|told|wrote|answered)\b"
    r"|\b(?:that|this)\s+(?:message|text|sms|reply|answer|summary)\b"
    r"|\b(?:recall|remember|repeat)\s+(?:what|the)\b"
    r"|\bjust\s+told\s+me\b"
    r"|\byou\s+(?:just\s+)?(?:told|gave)\s+me\s+(?:above|earlier|before)\b"
    r")",
    re.I,
)
# Directive bodies ("with an introduction of yourself") describe content to
# compose, not literal words to send.
_COMPOSE_BODY = re.compile(r"^(?:with|about|asking|telling)\b", re.I)
# Reversed shape "text <content> to <Name>" — recipient trails the body.
_TRAILING_TO_NAME = re.compile(
    r"\bto\s+[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,2}"
    r"\s*(?:actually|instead|please)?\s*[.?!]*$"
)
# STT cleanup is only worth an LLM hop when these show up.
_STT_NEEDS_CLEAN = re.compile(
    r"\b(um+|uh+|er+|hmm+)\b|"
    r"\b(at|meet(?:ing)? at)\s+(too|for|ate|won|to)\b|"
    r"\b(too|for|ate|won)\s*(o'?clock|pm|am)\b|"
    r"\b(their|there|they're)\b.*\b(going|house|car)\b",
    re.I,
)


def message_looks_clean(text: str) -> bool:
    """True when a message body should skip the LLM STT cleanup pass."""
    t = (text or "").strip()
    if not t or len(t) > 400:
        return False
    if _STT_NEEDS_CLEAN.search(t):
        return False
    # Typed / already-clean: ordinary letters, spaces, light punctuation.
    if not re.fullmatch(r"[\w\s.,!?\"'’\-@%&/+:;()$]*", t, re.UNICODE):
        return False
    return True


def is_anaphoric_body(text: str) -> bool:
    """True when `text` is a pointer to prior conversation, not sendable content."""
    t = (text or "").strip()
    if not t:
        return False
    if _ANAPHOR.match(t) and len(t.split()) <= 3:
        return True
    return bool(_PRIOR_REPLY_BODY.match(t))


def refers_to_prior_reply(text: str) -> bool:
    """True when the goal/body refers to something said earlier in-session."""
    t = (text or "").strip()
    if not t:
        return False
    if is_anaphoric_body(t):
        return True
    return bool(_REFERS_TO_PRIOR.search(t))


def try_parse_phone_goal(goal: str) -> dict | None:
    """Return {action, recipient, message} when the shape is unambiguous.

    Adds `_parsed: "heuristic"` so logs/tests can see the fast path fired.
    Returns None when the model should parse instead.
    """
    raw = (goal or "").strip()
    if not raw or len(raw) > 400:
        return None

    if _OPEN_PHONE.search(raw):
        return _ok("open", "", "")

    m = _READ_VERB.search(raw)
    if m:
        rest = raw[m.end():].strip()
        who = ""
        fm = _FROM_WHO.search(rest) if rest else None
        if fm:
            who = fm.group(1).strip().rstrip(".")
        elif rest.lower().startswith("from "):
            who = rest[5:].strip().rstrip(".")
        return _ok("read_messages", who, "")

    m = _REPLY_VERB.match(raw)
    if m:
        rest = raw[m.end():].strip()
        parsed = _split_who_body(rest)
        if not parsed:
            return None
        who, body = parsed
        # Anaphoric body → keep recipient, leave message empty for session fill.
        if is_anaphoric_body(body) or refers_to_prior_reply(body):
            return _ok("reply", who, "")
        return _ok("reply", who, body)

    m = _TEXT_VERB.match(raw)
    if m:
        rest = raw[m.end():].strip()
        # "text Abby" (no body) or "text Abby I'm late"
        if not rest:
            return None
        # "text that to <Name>" — recipient order needs the model.
        if _ANAPHOR.match(rest):
            return None
        # Bare recipient only (1–3 tokens, no sentence body).
        if re.fullmatch(r"[A-Za-z][\w'’-]*(?:\s+[A-Za-z][\w'’-]*){0,2}\.?", rest):
            return _ok("send_sms", rest.rstrip("."), "")
        parsed = _split_who_body(rest)
        if not parsed:
            # e.g. whole rest is anaphoric without a clear who
            if refers_to_prior_reply(rest):
                return None
            return None
        who, body = parsed
        if not who:
            return None
        # "Text Hugh the message you just told me" → Hugh + empty body;
        # orchestrator fills from the last assistant reply.
        if is_anaphoric_body(body) or refers_to_prior_reply(body):
            return _ok("send_sms", who, "")
        return _ok("send_sms", who, body)

    return None


def _ok(action: str, recipient: str, message: str) -> dict:
    return {
        "action": action,
        "recipient": (recipient or "").strip(),
        "message": (message or "").strip(),
        "_parsed": "heuristic",
    }


def _split_who_body(rest: str) -> tuple[str, str] | None:
    rest = (rest or "").strip()
    if not rest:
        return None
    m = _BODY_SEP.match(rest)
    if m:
        who = m.group("who").strip().strip(",").strip()
        body = m.group("body").strip()
        # "…the message of your description THAT you gave me above": here
        # "that" is a relative pronoun, not a body separator — fall through to
        # the token walk so the name doesn't swallow the noun phrase (observed
        # live: recipient "Hugh Salva the message of your description").
        rel = re.match(r"^(?:which\s+)?you\s+(?:just\s+)?"
                       r"(?:told|said|gave|sent|wrote|shared)\b", body, re.I)
        if who and body and not rel:
            return who, body
    tokens = rest.split()
    if len(tokens) < 2:
        return None
    name_toks: list[str] = []
    for i, tok in enumerate(tokens):
        low = re.sub(r"[^\w']", "", tok.lower())
        # Body starts: pronouns, greetings, or a clearly non-name token.
        if name_toks and (low in _BODY_START or low.startswith("i'")
                          or low.startswith("i’")):
            break
        if name_toks and tok[:1].islower():
            break
        # First token may be lowercase ("abby"); later name tokens should look
        # like name words (letter-leading).
        if not re.match(r"^[A-Za-z]", tok):
            break
        name_toks.append(tok)
        if len(name_toks) >= 3:
            break
    if not name_toks:
        return None
    if _ANAPHOR.match(name_toks[0]):
        return None
    body = " ".join(tokens[len(name_toks):]).strip()
    if not body:
        return None
    # A directive body describes content to compose — the model writes it.
    if _COMPOSE_BODY.match(body):
        return None
    # Reversed "send <content> to <Name>" shape ("happy birthday to Mom"):
    # a lowercase lead token with a trailing capitalized to-recipient is
    # body, not a name.
    if name_toks[0][:1].islower() and _TRAILING_TO_NAME.search(body):
        return None
    # Lone Propercase leftover is probably still part of the name — ambiguous.
    if re.fullmatch(r"[A-Z][\w'’-]*", body) and len(body) < 20:
        return None
    return " ".join(name_toks), body
