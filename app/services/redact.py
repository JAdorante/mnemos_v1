"""Secret/PII redaction — the last gate before anything leaves the machine
or lands in a durable log.

The screen and webcam see whatever the user sees: an open .env, a password
field, a private key in an editor. Nothing here should ever reach a cloud
model or a training log. Three layers use this module:

  1. desktop_capture — windows whose TITLES look like secret material
     (.env, key files, password managers) are never captured at all, so the
     frame can't leak even on the local-VLM-down path where no OCR exists
     before the cloud call.
  2. VLMRouter.describe — when the free local pass reads secret-shaped text,
     the cloud escalation is skipped entirely (no image bytes leave the
     machine for that frame) and the local result is returned redacted.
  3. escalate_log.record — every distill row is redacted before it is
     written, whatever the caller (vision or text), so the trail
     that later feeds LoRA training can never carry a raw credential.

Detection is regex-only and intentionally biased toward false positives:
the cost of a hit is one skipped frame or a [REDACTED:...] span, never a
lost credential.
"""
from __future__ import annotations

import re
from typing import Any

# (kind, pattern) — order matters: more specific providers before generic
# shapes so the redaction label names the real thing.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key",
     re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----"
                r"(?:[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----)?")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("aws_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token",
     re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("jwt",
     re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{16,}")),
    # user:password@ inside any URL — the whole credential pair goes.
    ("url_credentials",
     re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]{1,64}:[^\s@]{4,}@")),
    # KEY=value / TOKEN: value lines the way an .env or config renders them.
    # Whole line is replaced — OCR'd values don't reliably match provider
    # shapes, so the assignment context is the signal. Case-SENSITIVE and the
    # keyword must end the word ((?![A-Za-z0-9])) so NEWAPI_KEYWORD_LOC or
    # prose like "monkey: banana" never match.
    ("env_assignment",
     re.compile(r"(?m)^[ \t]*(?:export[ \t]+|set[ \t]+)?[A-Z0-9_]*"
                r"(?:KEYS?|TOKENS?|SECRETS?|PASSW(?:OR)?DS?|PWDS?|CREDENTIALS?)"
                r"(?![A-Za-z0-9])[A-Z0-9_]*[ \t]*[=:][ \t]*\S{6,}.*$")),
    # Lowercase config lines: only unambiguous credential words — bare
    # "key:"/"token:"/"secret:" stay legal prose.
    ("kv_secret",
     re.compile(r"(?im)^[ \t]*[\"']?(?:api[ _-]?key|access[ _-]?key|"
                r"secret[ _-]?key|private[ _-]?key|auth[ _-]?token|"
                r"access[ _-]?token|refresh[ _-]?token|client[ _-]?secret|"
                r"passwords?|passwd|passphrase|credentials?)[\"']?"
                r"[ \t]*[=:][ \t]*\S{6,}.*$")),
    # Guards: not part of a longer digit/id/decimal chain on either side.
    ("ssn", re.compile(r"(?<!\d)(?<!\d-)(?<!\d\.)\b\d{3}-\d{2}-\d{4}\b(?!-?\d)(?!\.\d)")),
]

# Card-shaped digit runs: consistently grouped (4-4-4-4 / Amex 4-6-5) or a
# plain 13-16 digit run, NOT adjacent to more digits, decimals, dashed id
# chains (timestamps, float mantissas, "1151-1784749803…" version strings),
# or letters/underscores (hex hashes, base64 blobs, garbled OCR tokens).
# Only a hit when the Luhn checksum ALSO agrees.
_CARD_CANDIDATE = re.compile(
    r"(?<![\w.])(?<!\d\.)(?<!\d-)(?:"
    r"\d{4}([ \-])\d{4}\1\d{4}\1\d{4}"
    r"|\d{4}([ \-])\d{6}\2\d{5}"
    r"|\d{13,16}"
    r")(?![\-.]?\d)(?![A-Za-z_])")

# Window titles that mean "the screen is showing secret material". Skipping a
# frame is cheap, so err broad — but stay away from everyday words that would
# blind the product (plain 'password' appears in normal web-page titles more
# often than in leaks, yet a login page is exactly a capture we shouldn't keep).
_SENSITIVE_WINDOW = re.compile(
    r"(?i)(?:"
    r"\.env\b|\benv\.local\b|\.pem\b|\.ppk\b|\.key\b|"
    r"id_rsa|id_ed25519|id_ecdsa|known_hosts|authorized_keys|"
    r"private[ _-]?key|secrets?\.(?:json|ya?ml|toml|env|py)|"
    r"credentials?\b|passwords?\b|passphrase|"
    r"1password|keepass|bitwarden|lastpass|dashlane|nordpass|protonpass|"
    r"authenticator"
    r")")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _card_hits(text: str) -> list[re.Match]:
    hits = []
    for m in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"[ \-]", "", m.group())
        # Monotone runs (0000…, 1111…) Luhn-pass but are never real cards.
        if (13 <= len(digits) <= 16 and len(set(digits)) > 1
                and _luhn_ok(digits)):
            hits.append(m)
    return hits


def scan(text: str) -> list[str]:
    """Kinds of secrets present in `text`, in pattern order; [] when clean."""
    t = text or ""
    if not t:
        return []
    kinds = [kind for kind, pat in _PATTERNS if pat.search(t)]
    if _card_hits(t):
        kinds.append("card_number")
    return kinds


def contains_secret(text: str) -> bool:
    return bool(scan(text))


def redact_text(text: str) -> str:
    """`text` with every secret span replaced by [REDACTED:<kind>]."""
    t = text or ""
    if not t:
        return t
    for kind, pat in _PATTERNS:
        t = pat.sub(f"[REDACTED:{kind}]", t)
    for m in reversed(_card_hits(t)):
        t = t[:m.start()] + "[REDACTED:card_number]" + t[m.end():]
    return t


def scan_payload(obj: Any) -> list[str]:
    """scan() over every string inside a nested dict/list payload (deduped)."""
    kinds: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            for k in scan(node):
                if k not in kinds:
                    kinds.append(k)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(obj)
    return kinds


def redact_payload(obj: Any) -> Any:
    """Same-shape copy of a nested payload with every string redacted.
    Non-string leaves (numbers, None, bools) pass through untouched."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_payload(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_payload(v) for v in obj)
    return obj


def is_sensitive_window(title: str) -> bool:
    """True when a window title suggests secret material is on screen —
    the frame should not be captured, stored, or sent anywhere."""
    return bool(_SENSITIVE_WINDOW.search(title or ""))
