"""Perception redaction — the mandatory stage before any log write or egress.

Two tiers over the existing secrets redactor (app/services/redact.py):

  SECRETS — API keys, tokens, private keys, cards, SSNs, credential
            assignments. Masked (or the call skipped) EVERYWHERE: local
            stores, logs, egress. A secret is never useful memory.
  PII     — email addresses and phone numbers. Masked in durable LOGS
            (escalate_distill, telemetry) and in perception-layer egress,
            but NOT in the product's memory stores: remembering contacts is
            the product (person_details mines emails/phones from facts), so
            store-side email masking would blind it. This split is
            deliberate and documented in PERCEPTION.md.

API: redact(obj, tier=...) -> (redacted_obj, hits). `hits` is a list of
pattern kinds — never the matched text. A module counter tracks hit counts;
nothing here ever logs a secret.
"""
from __future__ import annotations

import re
import threading
from collections import Counter
from typing import Any

from app.services import redact as _secrets

TIER_SECRETS = "secrets"
TIER_LOG = "log"        # secrets + PII: distill rows, telemetry trails
TIER_EGRESS = "egress"  # secrets + PII: anything leaving the machine (L1/L3)

# PII patterns. Phone is deliberately conservative: separators or a +country
# prefix are required so digit runs (ids, timestamps, versions) never match.
_PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}\b")),
    ("phone", re.compile(
        r"(?<![\w.\-])(?:"
        r"\+\d{1,3}[ .\-]?\(?\d{2,4}\)?[ .\-]?\d{3,4}[ .\-]?\d{3,4}"   # +intl
        r"|\(\d{3}\)[ .\-]?\d{3}[ .\-]\d{4}"                            # (555) 123-4567
        r"|\d{3}[ .\-]\d{3}[ .\-]\d{4}"                                 # 555-123-4567
        r")(?![\w.\-])")),
]

_counter_lock = threading.Lock()
counters: Counter[str] = Counter()


def _count(kinds: list[str]) -> None:
    if not kinds:
        return
    with _counter_lock:
        counters["redaction"] += len(kinds)
        for k in kinds:
            counters[f"redaction.{k}"] += 1


def _pii_scan(text: str) -> list[str]:
    t = text or ""
    return [kind for kind, pat in _PII_PATTERNS if pat.search(t)]


def _pii_mask(text: str) -> str:
    t = text or ""
    for kind, pat in _PII_PATTERNS:
        t = pat.sub(f"[REDACTED:{kind}]", t)
    return t


def redact_text(text: str, tier: str = TIER_LOG) -> tuple[str, list[str]]:
    """Redacted text + list of pattern kinds hit (never the matched text)."""
    hits = _secrets.scan(text)
    out = _secrets.redact_text(text)
    if tier in (TIER_LOG, TIER_EGRESS):
        hits += _pii_scan(out)
        out = _pii_mask(out)
    _count(hits)
    return out, hits


def redact(obj: Any, tier: str = TIER_LOG) -> tuple[Any, list[str]]:
    """Same-shape copy of a str/dict/list payload with every string redacted
    at the given tier. Non-string leaves pass through untouched."""
    hits: list[str] = []

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            out, h = redact_text(node, tier)
            hits.extend(h)
            return out
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, tuple):
            return tuple(walk(v) for v in node)
        return node

    return walk(obj), hits


def secret_kinds(text_or_payload: Any) -> list[str]:
    """Secret-tier kinds present (skip-the-model-call gate). PII does not
    trigger a skip — a visible email is maskable; a visible key means the
    frame itself must not travel."""
    if isinstance(text_or_payload, str):
        return _secrets.scan(text_or_payload)
    return _secrets.scan_payload(text_or_payload)


def stats() -> dict[str, int]:
    with _counter_lock:
        return dict(counters)
