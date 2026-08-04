"""Deterministic privacy_class taxonomy (plan 6.1).

Classes (ascending severity):
  public < internal < personal < sensitive < never-send

Stamped onto events at insert time; enforced in `model_router` before any
Claude/cloud call. Complements the 3-layer `redact.py` / privacy_gate —
those still block capture; this labels what *did* land and gates egress.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Canonical labels (persist in event.meta["privacy_class"]).
PUBLIC = "public"
INTERNAL = "internal"
PERSONAL = "personal"
SENSITIVE = "sensitive"
NEVER_SEND = "never-send"

CLASSES = (PUBLIC, INTERNAL, PERSONAL, SENSITIVE, NEVER_SEND)
_RANK = {c: i for i, c in enumerate(CLASSES)}

# Pasted credential heuristic (shared with browser_agent._looks_like_secret).
_SECRETISH = re.compile(
    r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9!@#$%^&*()_+\-=\[\]{};':\",.<>/?\\|`~]{8,128}$"
)

# Health / finance keyword cues (deterministic, precision-biased).
_HEALTH_FINANCE = re.compile(
    r"(?i)\b(?:"
    r"medical|diagnosis|prescription|hipaa|patient|"
    r"ssn|social\s*security|"
    r"bank\s*account|routing\s*number|iban|swift\s*code|"
    r"credit\s*card|debit\s*card|cvv|account\s*balance|"
    r"wire\s*transfer|ach\s*transfer|"
    r"401k|ira\b|brokerage|"
    r"diagnosis\s*code|icd-?\d"
    r")\b"
)

# Personal PII that is not automatically a secret.
_PERSONAL_PII = re.compile(
    r"(?i)(?:"
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b|"  # email
    r"(?<!\d)(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}(?!\d)"  # phone
    r")"
)

# Sources that are explicitly public / shared outward.
_PUBLIC_SOURCES = frozenset({
    "web.public", "rss", "news", "calendar.public",
})


class PrivacyRefuse(RuntimeError):
    """Raised when a cloud call would send never-send content."""

    def __init__(self, message: str = "", *, privacy_class: str = NEVER_SEND,
                 kinds: list[str] | None = None) -> None:
        super().__init__(message or "privacy_class=never-send — cloud call refused")
        self.privacy_class = privacy_class
        self.kinds = list(kinds or [])


def normalize(cls: str | None) -> str:
    c = (cls or "").strip().lower().replace("_", "-")
    if c in _RANK:
        return c
    if c in ("never", "never_send", "neversend"):
        return NEVER_SEND
    return INTERNAL


def rank(cls: str | None) -> int:
    return _RANK.get(normalize(cls), _RANK[INTERNAL])


def max_class(*classes: str | None) -> str:
    best = INTERNAL
    for c in classes:
        if c is None:
            continue
        n = normalize(c)
        if rank(n) > rank(best):
            best = n
    return best


def looks_like_secret(text: str) -> bool:
    """Single-token password/secret paste (no spaces, not URL/email)."""
    t = (text or "").strip()
    if not t or " " in t or "@" in t or t.lower().startswith("http"):
        return False
    return bool(_SECRETISH.match(t))


def classify_text(
    text: str,
    *,
    source: str = "",
    app: str = "",
    title: str = "",
    url_domain: str = "",
) -> str:
    """Deterministic class for a text blob (+ optional surface cues)."""
    t = text or ""
    title = title or ""
    app = (app or "").strip().lower()
    domain = (url_domain or "").strip().lower()
    src = (source or "").strip().lower()

    # --- never-send --------------------------------------------------------
    try:
        from app.services import redact as _redact
        kinds = _redact.scan(t)
        if kinds:
            return NEVER_SEND
        if _redact.is_sensitive_window(title):
            return NEVER_SEND
    except Exception:
        pass
    if looks_like_secret(t):
        return NEVER_SEND
    # Excluded capture surfaces that somehow still produced text.
    try:
        from app.perception.privacy_gate import gate as _pgate
        rule = _pgate.check(window_title=title, app_exe=app,
                            url_domain=domain or None)
        if rule and ("sensitive" in rule or "credential" in rule
                     or "banking" in rule or "private" in rule):
            return NEVER_SEND
        if rule:
            return SENSITIVE
    except Exception:
        pass

    # --- sensitive ---------------------------------------------------------
    if _HEALTH_FINANCE.search(t) or _HEALTH_FINANCE.search(title):
        return SENSITIVE
    try:
        from app.perception import privacy_gate as _pg
        if domain and domain in getattr(_pg, "_BANKING_DOMAINS", ()):
            return SENSITIVE
    except Exception:
        pass

    # --- personal ----------------------------------------------------------
    if _PERSONAL_PII.search(t):
        return PERSONAL

    # --- public / internal -------------------------------------------------
    if src in _PUBLIC_SOURCES or src.startswith("web.public"):
        return PUBLIC
    return INTERNAL


def classify_event(event) -> str:
    """Class for an Event from raw/summary + meta surface cues."""
    meta = getattr(event, "meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    blob = " ".join(
        str(x) for x in (
            getattr(event, "raw", ""),
            getattr(event, "summary", ""),
            " ".join(getattr(event, "people", None) or []),
            " ".join(getattr(event, "tasks", None) or []),
        ) if x
    )
    return classify_text(
        blob,
        source=getattr(event, "source", "") or "",
        app=str(meta.get("app") or meta.get("app_exe") or ""),
        title=str(meta.get("title") or meta.get("window_title") or ""),
        url_domain=str(meta.get("url_domain") or meta.get("domain") or ""),
    )


def stamp_event(event) -> Any:
    """Write meta['privacy_class'] if missing; keep a higher existing class."""
    if event is None:
        return event
    if getattr(event, "meta", None) is None:
        event.meta = {}
    computed = classify_event(event)
    existing = event.meta.get("privacy_class")
    if existing:
        event.meta["privacy_class"] = max_class(existing, computed)
    else:
        event.meta["privacy_class"] = computed
    return event


def flatten_messages(system: str | None, messages: list | None) -> str:
    parts: list[str] = []
    if system:
        parts.append(str(system))
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    parts.append(block)
        elif c is not None:
            parts.append(str(c))
    return "\n".join(parts)


def _redact_messages(messages: list) -> list:
    from app.services.redact import redact_text, redact_payload
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        m2 = dict(m)
        c = m2.get("content")
        if isinstance(c, str):
            m2["content"] = redact_text(c)
        else:
            m2["content"] = redact_payload(c)
        out.append(m2)
    return out


def gate_cloud(
    system: str,
    messages: list,
    *,
    declared_class: str | None = None,
) -> tuple[str, list, str, str]:
    """Enforce privacy before a remote model call.

    Returns (system, messages, privacy_class, action) where action is
    'allow' | 'redact' | 'refuse'.

    - never-send → refuse (raises PrivacyRefuse)
    - sensitive / personal → redact then allow
    - internal / public → allow (still secret-scan; secrets escalate class)

    Set QUILL_PRIVACY_CLOUD=0 to disable (tests / emergency only).
    """
    if os.environ.get("QUILL_PRIVACY_CLOUD", "1") in ("0", "false", "False"):
        return system, messages, normalize(declared_class) or INTERNAL, "allow"

    from app.services.redact import redact_text, scan

    blob = flatten_messages(system, messages)
    computed = classify_text(blob)
    cls = max_class(declared_class, computed)

    if cls == NEVER_SEND:
        kinds = scan(blob)
        raise PrivacyRefuse(
            f"privacy_class=never-send — refusing cloud call"
            + (f" ({', '.join(kinds)})" if kinds else ""),
            privacy_class=NEVER_SEND, kinds=kinds)

    if cls in (SENSITIVE, PERSONAL):
        return (redact_text(system or ""), _redact_messages(messages or []),
                cls, "redact")

    # Defense in depth: even internal/public prompts get secret redaction if
    # a weak pattern slipped past classify (should be rare).
    kinds = scan(blob)
    if kinds:
        raise PrivacyRefuse(
            f"privacy_class escalated to never-send ({', '.join(kinds)})",
            privacy_class=NEVER_SEND, kinds=kinds)

    return system, messages, cls, "allow"
