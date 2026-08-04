"""Deterministic answer-check (plan 3.2).

Every name / date / price token in an assistant answer must appear in the
retrieval context block. LLM entailment runs only for money / date /
commitment answers *after* the token gate passes — it never rescues a
fabricated token. Failure rewrites to an evidence dump and exposes
confirmed / likely / conflicting / missing buckets for the response compiler.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# Reuse the same normalized containment spirit as cog_telemetry.span_is_faithful.
_WORD = re.compile(r"[a-z0-9$]+")

_PRICE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
_ISO_DATE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)\b"
)
_MONTH_DATE = re.compile(
    r"\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?)\b",
    re.I,
)
_RELATIVE_DATE = re.compile(
    r"\b(today|tomorrow|tonight|yesterday|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday)\b",
    re.I,
)
# Multi-word proper names + standalone capitalized given names (not sentence starts only —
# we accept any Capitalized token ≥3 chars that isn't a stopword).
_MULTI_NAME = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_SINGLE_NAME = re.compile(r"\b([A-Z][a-z]{2,})\b")

_NAME_STOP = {
    "the", "this", "that", "these", "those", "here", "there", "what", "when",
    "where", "which", "who", "whom", "whose", "why", "how", "and", "but", "for",
    "with", "from", "into", "about", "after", "before", "during", "under",
    "over", "between", "through", "also", "just", "only", "even", "still",
    "already", "again", "once", "yes", "yeah", "okay", "ok", "hey", "hi",
    "hello", "thanks", "thank", "please", "sorry", "sure", "well", "now",
    "then", "next", "last", "first", "second", "third", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday", "january",
    "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "today", "tomorrow", "tonight",
    "yesterday", "i", "you", "we", "they", "he", "she", "it", "my", "your",
    "our", "their", "his", "her", "its", "me", "him", "us", "them", "a", "an",
    "retrieved", "memories", "memory", "context", "question", "answer",
    "here's", "heres", "found", "evidence", "according", "based", "said",
    "says", "told", "tell", "ask", "asked", "will", "would", "could", "should",
    "can", "may", "might", "must", "shall", "need", "needs", "wanted", "want",
    "got", "get", "getting", "have", "has", "had", "been", "being", "are",
    "was", "were", "is", "am", "do", "does", "did", "done", "not", "no",
    "none", "all", "any", "some", "each", "every", "both", "few", "more",
    "most", "other", "such", "than", "too", "very", "really", "quite",
    "actually", "probably", "maybe", "perhaps", "likely", "confirmed",
    "missing", "conflicting", "price", "cost", "costs", "due", "date",
    "dollar", "dollars", "usd", "note", "notes", "summary", "takeaway",
}

_MONEY_OR_COMMIT = re.compile(
    r"\b(price|priced|cost|costs|\$|dollar|pay|paid|owe|owed|invoice|"
    r"due|deadline|by\s+(?:monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|tomorrow|today)|commit(?:ment|ted)?|promise[ds]?|"
    r"I'll|I will|we'll|we will|owe[sd]?)\b",
    re.I,
)

_DOWNGRADE_LEAD = "Here's what I found, with the evidence:"


def _norm(s: str) -> str:
    return " ".join(_WORD.findall((s or "").lower()))


def _in_context(token: str, ctx_norm: str) -> bool:
    t = _norm(token)
    if not t:
        return False
    return t in ctx_norm


def extract_price_tokens(text: str) -> list[str]:
    out: list[str] = []
    for m in _PRICE.finditer(text or ""):
        tok = f"${m.group(1)}"
        if tok not in out:
            out.append(tok)
    return out


def extract_date_tokens(text: str) -> list[str]:
    out: list[str] = []
    for rx in (_ISO_DATE, _MONTH_DATE, _RELATIVE_DATE):
        for m in rx.finditer(text or ""):
            tok = m.group(1).strip()
            # Keep canonical display; dedupe case-insensitively.
            if not any(tok.lower() == x.lower() for x in out):
                out.append(tok)
    return out


def extract_name_tokens(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in _MULTI_NAME.finditer(text or ""):
        tok = m.group(1).strip()
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    for m in _SINGLE_NAME.finditer(text or ""):
        tok = m.group(1).strip()
        key = tok.lower()
        if key in _NAME_STOP or key in seen:
            continue
        # Skip if already covered by a multi-word name.
        if any(key in s for s in seen if " " in s):
            continue
        seen.add(key)
        out.append(tok)
    return out


def extract_check_tokens(text: str) -> dict[str, list[str]]:
    return {
        "names": extract_name_tokens(text),
        "dates": extract_date_tokens(text),
        "prices": extract_price_tokens(text),
    }


def looks_money_date_commitment(question: str = "", answer: str = "") -> bool:
    blob = f"{question or ''}\n{answer or ''}"
    if _PRICE.search(blob) or _ISO_DATE.search(blob) or _MONTH_DATE.search(blob):
        return True
    return bool(_MONEY_OR_COMMIT.search(blob))


def _evidence_bullets(context: str, sources: list | None, limit: int = 8) -> list[str]:
    items: list[str] = []
    for s in sources or []:
        for it in s.get("items") or []:
            t = (it or "").strip()
            if t and t not in items:
                items.append(t)
            if len(items) >= limit:
                return items
    for line in (context or "").splitlines():
        t = line.strip().lstrip("-•").strip()
        if not t or t.startswith("RELEVANT ") or t.startswith("ABOUT "):
            continue
        if t.startswith("DRAFTING RULE"):
            continue
        if t not in items:
            items.append(t)
        if len(items) >= limit:
            break
    return items


def _conflict_prices(context: str) -> list[str]:
    prices = extract_price_tokens(context or "")
    if len(set(prices)) >= 2:
        return sorted(set(prices))
    return []


def _default_entail(
    answer: str, context: str, *, question: str = ""
) -> bool | None:
    """Optional LLM entailment. Returns True/False, or None if skipped."""
    # Opt-in — deterministic token gate is the hard AC; entailment is extra
    # for money/date/commitment answers when QUILL_ANSWER_ENTAIL=1.
    flag = (os.environ.get("QUILL_ANSWER_ENTAIL") or "0").strip().lower()
    if flag in ("0", "false", "off", "no", ""):
        return None
    try:
        from app.services.model_router import router
    except Exception:
        return None
    prompt = (
        "Does the ANSWER follow from the CONTEXT for money, dates, or "
        "commitments? Reply with exactly YES or NO.\n\n"
        f"QUESTION: {question}\n\nCONTEXT:\n{context[:3000]}\n\n"
        f"ANSWER:\n{answer[:1500]}"
    )
    try:
        reply = router.complete(
            "chat",
            system="You are a strict entailment checker. Answer YES or NO only.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8,
        ).strip().upper()
    except Exception:
        return None
    if reply.startswith("YES"):
        return True
    if reply.startswith("NO"):
        return False
    return None


@dataclass
class AnswerCheck:
    ok: bool
    status: str  # "ok" | "downgraded" | "skipped"
    fabricated: list[str] = field(default_factory=list)
    buckets: dict[str, list[str]] = field(default_factory=dict)
    text: str = ""
    entailment: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "fabricated": list(self.fabricated),
            "buckets": {k: list(v) for k, v in self.buckets.items()},
            "text": self.text,
            "entailment": self.entailment,
        }


def buckets_to_sections(buckets: dict[str, list[str]] | None) -> list[dict[str, Any]]:
    """Typed sections for response_compiler / UI."""
    order = ("confirmed", "likely", "conflicting", "missing")
    labels = {
        "confirmed": "Confirmed",
        "likely": "Likely",
        "conflicting": "Conflicting",
        "missing": "Missing",
    }
    out: list[dict[str, Any]] = []
    for key in order:
        items = [x for x in (buckets or {}).get(key) or [] if (x or "").strip()]
        if not items:
            continue
        out.append({
            "type": key,
            "title": labels[key],
            "items": items,
        })
    return out


def _downgrade_text(
    context: str,
    sources: list | None,
    buckets: dict[str, list[str]],
) -> str:
    bullets = _evidence_bullets(context, sources)
    lines = [_DOWNGRADE_LEAD, ""]
    if bullets:
        for b in bullets:
            lines.append(f"- {b}")
    else:
        lines.append("- (no matching memories in context)")
    # Fence evidence buckets so the compiler can parse them even without the
    # evidence= kwarg path.
    for key in ("confirmed", "likely", "conflicting", "missing"):
        items = buckets.get(key) or []
        if not items:
            continue
        lines.append("")
        lines.append(f":::{key}")
        for it in items:
            lines.append(f"- {it}")
        lines.append(":::")
    return "\n".join(lines).strip()


def check_answer(
    answer: str,
    context: str,
    *,
    question: str = "",
    sources: list | None = None,
    entail: Callable[..., bool | None] | None = None,
) -> AnswerCheck:
    """Gate answer tokens against context; downgrade on fabricated tokens.

    When context is empty, or the answer has no name/date/price tokens, the
    check is skipped (general-knowledge / non-personal replies).
    """
    text = (answer or "").strip()
    ctx = (context or "").strip()
    if not text:
        return AnswerCheck(ok=True, status="skipped", text=text,
                           buckets={"confirmed": [], "likely": [],
                                    "conflicting": [], "missing": []})
    if not ctx:
        return AnswerCheck(ok=True, status="skipped", text=text,
                           buckets={"confirmed": [], "likely": [],
                                    "conflicting": [], "missing": []})

    tokens = extract_check_tokens(text)
    flat = tokens["names"] + tokens["dates"] + tokens["prices"]
    if not flat:
        return AnswerCheck(ok=True, status="skipped", text=text,
                           buckets={"confirmed": [], "likely": [],
                                    "conflicting": [], "missing": []})

    ctx_norm = _norm(ctx)
    confirmed: list[str] = []
    missing: list[str] = []
    for tok in flat:
        if _in_context(tok, ctx_norm):
            if tok not in confirmed:
                confirmed.append(tok)
        else:
            if tok not in missing:
                missing.append(tok)

    conflicting = _conflict_prices(ctx)
    # If the answer asserts one of several conflicting context prices, keep it
    # confirmed but surface the conflict set.
    likely: list[str] = []

    buckets = {
        "confirmed": confirmed,
        "likely": likely,
        "conflicting": conflicting,
        "missing": missing,
    }
    fabricated = list(missing)

    if fabricated:
        downgraded = _downgrade_text(ctx, sources, buckets)
        return AnswerCheck(
            ok=False,
            status="downgraded",
            fabricated=fabricated,
            buckets=buckets,
            text=downgraded,
            entailment=None,
        )

    entailment: bool | None = None
    if looks_money_date_commitment(question, text):
        fn = entail if entail is not None else _default_entail
        try:
            entailment = fn(text, ctx, question=question)
        except TypeError:
            try:
                entailment = fn(text, ctx)
            except Exception:
                entailment = None
        except Exception:
            entailment = None
        if entailment is False:
            buckets["likely"] = list(confirmed)
            buckets["confirmed"] = []
            buckets["missing"] = buckets.get("missing") or []
            downgraded = _downgrade_text(ctx, sources, buckets)
            return AnswerCheck(
                ok=False,
                status="downgraded",
                fabricated=[],
                buckets=buckets,
                text=downgraded,
                entailment=False,
            )

    return AnswerCheck(
        ok=True,
        status="ok",
        fabricated=[],
        buckets=buckets,
        text=text,
        entailment=entailment,
    )
