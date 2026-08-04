"""Evidence-anchored verification registry (plan 5.1).

LLM opinion alone never yields `verified`. Read-backs:

  * email    — Sent-folder / sent-toast / optional mail_query
  * calendar — CalDAV GET by event href/uid
  * file     — os.stat after write

Statuses written to `agent_steps.status`:
  verified | failed | done | outcome_uncertain
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Canonical step statuses (free-text column; soft allowlist).
VERIFIED = "verified"
FAILED = "failed"
DONE = "done"
OUTCOME_UNCERTAIN = "outcome_uncertain"

ALLOWED_STATUSES = frozenset({VERIFIED, FAILED, DONE, OUTCOME_UNCERTAIN})

# Evidence sources (written into verification notes / step meta).
SRC_LLM = "llm"
SRC_DOM = "dom"
SRC_SENT = "sent_folder"
SRC_MAIL = "mail_query"
SRC_STAT = "os.stat"
SRC_CAL = "calendar_get"
SRC_LOW_RISK = "low_risk"
SRC_SYSCALL = "syscall"

_SENT_URL = re.compile(
    r"(?:/|#)sent(?:items)?(?:/|$)|sent\s*items|sent\s*mail", re.I)
_SENT_LABEL = re.compile(
    r"\b(?:sent\s+items|sent\s+mail|sent\s+folder|in\s+sent)\b", re.I)


@dataclass
class Evidence:
    """One verification attempt."""
    ok: bool
    source: str
    note: str = ""
    status: str = OUTCOME_UNCERTAIN
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "note": self.note,
            "status": self.status,
            "meta": dict(self.meta or {}),
        }


def normalize_status(status: str | None, *, default: str = OUTCOME_UNCERTAIN) -> str:
    """Empty → default; known statuses lowercased; unknown passthrough."""
    s = (status or "").strip().lower()
    if not s:
        return default
    if s in ALLOWED_STATUSES:
        return s
    return s


def status_from_evidence(ev: Evidence | None, *,
                         llm_satisfied: bool | None = None) -> str:
    """Map evidence (+ optional LLM) to agent_steps.status.

    Evidence wins. LLM-only success → outcome_uncertain (never verified).
    """
    if ev is not None and ev.source not in (SRC_LLM, ""):
        if ev.ok and ev.status == VERIFIED:
            return VERIFIED
        if not ev.ok and ev.status == FAILED:
            return FAILED
        if ev.status in ALLOWED_STATUSES:
            return ev.status
    if llm_satisfied is True:
        return OUTCOME_UNCERTAIN
    if llm_satisfied is False:
        return FAILED
    return OUTCOME_UNCERTAIN


def verify_file(path: str | Path, *, expect_bytes: int | None = None,
                min_mtime: float | None = None) -> Evidence:
    """Read-back via os.stat after a write."""
    p = Path(path) if not isinstance(path, Path) else path
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return Evidence(False, SRC_STAT, f"file missing after write: {p}",
                        status=FAILED, meta={"path": str(p)})
    except OSError as exc:
        return Evidence(False, SRC_STAT, f"stat failed: {exc}",
                        status=FAILED, meta={"path": str(p)})
    meta = {
        "path": str(p),
        "size": int(st.st_size),
        "mtime": float(st.st_mtime),
    }
    if expect_bytes is not None and int(st.st_size) != int(expect_bytes):
        return Evidence(
            False, SRC_STAT,
            f"size mismatch: got {st.st_size}, expected {expect_bytes}",
            status=FAILED, meta=meta)
    if min_mtime is not None and float(st.st_mtime) + 1e-3 < float(min_mtime):
        return Evidence(
            False, SRC_STAT,
            f"mtime {st.st_mtime} older than write start {min_mtime}",
            status=FAILED, meta=meta)
    return Evidence(
        True, SRC_STAT,
        f"os.stat ok ({st.st_size} bytes)",
        status=VERIFIED, meta=meta)


def verify_calendar_event(
    href: str | None = None, *, uid: str | None = None,
    calendar: str = "Home",
    getter: Callable[..., dict] | None = None,
) -> Evidence:
    """CalDAV GET read-back by href or uid."""
    if getter is None:
        try:
            from app.services import icloud_calendar as cal
            getter = cal.get_event
        except Exception as exc:
            return Evidence(
                False, SRC_CAL, f"calendar module unavailable: {exc}",
                status=OUTCOME_UNCERTAIN)
    try:
        res = getter(href or uid or "", calendar=calendar) if href or uid else {
            "ok": False, "error": "no href/uid"}
    except TypeError:
        # Some getters take only href_or_uid
        try:
            res = getter(href or uid or "")
        except Exception as exc:
            return Evidence(False, SRC_CAL, f"calendar GET failed: {exc}",
                            status=FAILED)
    except Exception as exc:
        return Evidence(False, SRC_CAL, f"calendar GET failed: {exc}",
                        status=FAILED)
    if not isinstance(res, dict):
        return Evidence(False, SRC_CAL, "calendar GET returned non-dict",
                        status=FAILED)
    if res.get("ok"):
        return Evidence(
            True, SRC_CAL,
            f"calendar GET ok ({res.get('uid') or href or uid})",
            status=VERIFIED,
            meta={"uid": res.get("uid"), "href": res.get("href")
                  or href, "status": res.get("status")})
    return Evidence(
        False, SRC_CAL,
        f"calendar GET miss: {res.get('error') or res.get('status')}",
        status=FAILED,
        meta={"href": href, "uid": uid, "status": res.get("status")})


def verify_email_sent(
    *,
    page_text: str = "",
    url: str = "",
    title: str = "",
    drafted: list[str] | None = None,
    mail_query: Callable[[dict], dict | None] | None = None,
    query: dict | None = None,
) -> Evidence:
    """Sent-folder / sent-toast / mail-query evidence for a send.

    DOM "composer cleared" or "text in thread" alone is NOT enough — that is
    LLM/DOM opinion. Prefer Sent toast, Sent-folder URL/label, or mail_query.
    """
    blob = " ".join(x for x in (page_text, url, title) if x)
    drafted = [d for d in (drafted or []) if d]

    # (1) Explicit sent toast / Sent-folder OCR language
    try:
        from app.services.commitment_complete import looks_like_sent_toast
        if looks_like_sent_toast(blob):
            return Evidence(
                True, SRC_SENT,
                "Sent-folder/toast evidence in page text",
                status=VERIFIED,
                meta={"match": "sent_toast"})
    except Exception:
        pass

    # (2) URL or chrome indicates Sent folder, optionally with draft snippet
    in_sent = bool(_SENT_URL.search(url or "") or _SENT_LABEL.search(blob))
    if in_sent:
        if drafted and any(d in (page_text or "") for d in drafted):
            return Evidence(
                True, SRC_SENT,
                "draft text found in Sent folder view",
                status=VERIFIED,
                meta={"match": "sent_folder_body"})
        if in_sent and _SENT_LABEL.search(page_text or ""):
            return Evidence(
                True, SRC_SENT,
                "Sent folder chrome visible after send",
                status=VERIFIED,
                meta={"match": "sent_folder_chrome"})

    # (3) Optional mail provider query (injectable for tests / IMAP later)
    if mail_query is not None:
        try:
            hit = mail_query(dict(query or {}))
        except Exception as exc:
            return Evidence(
                False, SRC_MAIL, f"mail_query error: {exc}",
                status=OUTCOME_UNCERTAIN)
        if hit:
            return Evidence(
                True, SRC_MAIL,
                f"mail query found sent message",
                status=VERIFIED,
                meta={"hit": hit if isinstance(hit, dict) else {"raw": str(hit)[:200]}})

    return Evidence(
        False, SRC_SENT,
        "no Sent-folder / mail-query evidence",
        status=OUTCOME_UNCERTAIN,
        meta={"url": (url or "")[:160]})


def llm_only_evidence(satisfied: bool, reason: str = "") -> Evidence:
    """LLM judge result — never maps to verified."""
    return Evidence(
        bool(satisfied), SRC_LLM,
        (reason or "llm judge")[:300],
        status=OUTCOME_UNCERTAIN if satisfied else FAILED)


def low_risk_evidence(ok: bool, note: str = "") -> Evidence:
    return Evidence(
        bool(ok), SRC_LOW_RISK,
        note or "low-risk action",
        status=DONE if ok else FAILED)


def dom_evidence(ok: bool, note: str = "") -> Evidence:
    """Deterministic DOM/signature check (navigate rendered, etc.)."""
    return Evidence(
        bool(ok), SRC_DOM,
        note or ("dom check ok" if ok else "dom check failed"),
        status=VERIFIED if ok else FAILED)


def step_record_status(
    *,
    evidence: Evidence | None = None,
    llm_satisfied: bool | None = None,
    fallback_verified: bool | None = None,
) -> str:
    """Status string for `agent_steps.status`."""
    if evidence is not None:
        if evidence.source == SRC_LLM:
            return OUTCOME_UNCERTAIN if evidence.ok else FAILED
        if evidence.status in ALLOWED_STATUSES:
            return evidence.status
    if llm_satisfied is not None:
        return OUTCOME_UNCERTAIN if llm_satisfied else FAILED
    if fallback_verified is True:
        # Legacy hist without evidence tagging → treat as uncertain, not verified
        return OUTCOME_UNCERTAIN
    if fallback_verified is False:
        return FAILED
    return OUTCOME_UNCERTAIN
