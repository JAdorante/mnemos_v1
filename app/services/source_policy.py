"""Central source-policy layer for People Intelligence (v2).

Maps an event's source/window/text into a source_class, then looks up what
that class is allowed to contribute (mentions, person candidates, contacts,
…). Config-driven via data/source_policies.json — unit-testable, no
scattered if-social / if-terminal checks in extractors.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.services import surface_filters as sf

log = logging.getLogger(__name__)

_POLICY_PATH = Path(__file__).resolve().parents[2] / "data" / "source_policies.json"

# Fallback when data/source_policies.json is missing or unreadable. A safety
# table's absence must never mean "allow": deny all minting (people,
# commitments, claims), contacts, and identity/relationship evidence, keeping
# only passive observation (mentions, knowledge entities). The shipped JSON is
# the real posture; if this fallback is live, policies_loaded() is False and
# the console preflight warns.
_DEFAULT_POLICY = {
    "extract_mentions": True,
    "create_person_candidates": False,
    "relationship_evidence": False,
    "extract_contacts": False,
    "create_commitments": False,
    "create_claims": False,
    "update_people": False,
    "identity_evidence": False,
    "knowledge_entities": True,
}

_NEWS_WINDOW = re.compile(
    r"\b(?:nytimes|new york times|washington post|wsj|wall street journal|"
    r"reuters|bloomberg|cnn|bbc|the guardian|techcrunch|arxiv|wikipedia|wiki|"
    r"tmz|people\.com|eonline|e!\s*news|page\s*six|daily\s*mail|foxnews|"
    r"fox news|msnbc|nbcnews|nbc news|abc news|cbs news|politico|thehill|"
    r"the hill|axios|forbes|business\s*insider|buzzfeed|vice news|"
    r"variety|hollywood\s*tonight|hollywood\s*weekly|hollywood\s*digest|"
    r"hollywood|ap news|associated press|npr|pbs|aljazeera|al jazeera|"
    r"the verge|wired|vox|slate|salon|huffpost|huffington)\b",
    re.I,
)
# Content chrome common on news / celebrity article pages (window title alone
# often just says "Chrome" or the headline without the publisher brand).
_NEWS_CONTENT = re.compile(
    r"\b(?:breaking(?:\s+news)?|celebrity|gossip|exclusive(?:\s+report)?|"
    r"reports?\s+that|according\s+to\s+(?:sources?|officials?)|"
    r"subscribe\s+to\s+continue|sign\s+in\s+to\s+continue|"
    r"cookie\s+policy|advertisement|sponsored\s+content|"
    r"related\s+stories|trending\s+stories|top\s+stories|"
    r"share\s+this\s+(?:article|story)|comments?\s+section)\b",
    re.I,
)
# Plan 2.4: "the article mentioned…" must mint-deny (knowledge-only).
_ARTICLE_MENTIONED = re.compile(
    r"\b(?:the\s+)?article\s+mentioned\b|"
    r"\bas\s+(?:mentioned|reported)\s+in\s+(?:the\s+)?(?:article|story|press)\b|"
    r"\b(?:according\s+to|per)\s+(?:the\s+)?(?:article|story|report)\b",
    re.I,
)
# Desktop + webmail titles: classic Outlook, New Outlook, Gmail tab, Apple Mail.
_EMAIL_WINDOW = re.compile(
    r"\b(?:(?:new\s+)?outlook(?:\s+mail)?|outlook\.office(?:365)?|"
    r"mail\.google|gmail|google\s*mail|yahoo\s*mail|hotmail|proton\s*mail|"
    r"thunderbird|apple\s*mail|\bmail\b|\binbox\b|"
    r"microsoft\s*365.*mail|ola\.office)\b",
    re.I,
)
# OCR / transcript chrome that marks a captured email even when the window
# title is just "Chrome" or "Edge". Require header-shaped lines — a lone
# address must NOT reclassify news / docs as email.
_EMAIL_HEADERS = re.compile(
    r"(?:^|\n)\s*(?:from|to|cc|bcc|subject)\s*:\s*\S",
    re.I,
)
_EMAIL_CHROME = re.compile(
    r"\b(?:reply\s+all|forward(?:ed)?\s+message)\b",
    re.I,
)
_CODE_WINDOW = re.compile(
    r"\b(?:visual studio code|vscode|\bcursor\b|intellij|pycharm|sublime|"
    r"atom|neovim|\bvim\b|xcode)\b",
    re.I,
)
_DOC_WINDOW = re.compile(
    r"\b(?:acrobat|pdf|powerpoint|keynote|google slides|google docs|"
    r"word\b|notion|onenote)\b",
    re.I,
)


@dataclass(frozen=True)
class SourcePolicy:
    source_class: str
    extract_mentions: bool = True
    create_person_candidates: bool = False
    relationship_evidence: bool = False
    extract_contacts: bool = False
    create_commitments: bool = False
    create_claims: bool = False
    update_people: bool = False
    identity_evidence: bool = False
    knowledge_entities: bool = True


@lru_cache(maxsize=1)
def _raw_policies() -> dict:
    try:
        data = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
        classes = data.get("classes") or {}
        if not classes:
            log.error(
                "source_policies.json at %s has no 'classes' — running on the "
                "restrictive fallback policy (no minting, no contacts).",
                _POLICY_PATH)
        return classes
    except Exception as exc:
        log.error(
            "Could not load source policy table %s (%s) — running on the "
            "restrictive fallback policy (no minting, no contacts). Ship "
            "data/source_policies.json to restore per-class policies.",
            _POLICY_PATH, exc)
        return {}


def policies_loaded() -> bool:
    """True when the shipped policy table is in effect (not the fallback)."""
    return bool(_raw_policies())


def policy_version() -> str:
    try:
        data = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
        return str(data.get("version") or "1")
    except Exception:
        return "fallback"


def classify_source(
    *,
    event_source: str = "",
    window: str = "",
    text: str = "",
    content_type: str = "",
) -> str:
    """Map observation metadata → source_class (see source_policies.json)."""
    src = (event_source or "").lower()
    win = window or ""
    blob = f"{win}\n{(text or '')[:800]}"

    if src.startswith("exhaust"):
        return "exhaust"
    if src.startswith("audio"):
        return "meeting_transcript" if "system" in src else "private_conversation"
    if "calendar" in src:
        return "calendar"
    if "notif" in src:
        return "notification"
    if src.startswith("peer"):
        return "peer_answer"   # a teammate's Mnemos answered over the peer channel
    if src.startswith("org"):
        return "org_coordinator"
    if "chat" in src:
        return "direct_message"

    if sf.is_console_window(win) or sf.is_log_or_cli_surface(win, "", text, ""):
        return "terminal"
    if sf.is_activity_only_social(win, "", text, text):
        return "social_feed"
    if sf.is_user_social_compose(win, "", text, ""):
        return "social_composer"
    if _EMAIL_WINDOW.search(win):
        return "email"
    # News / article markers win over header heuristics (contacts must stay deny).
    if (_NEWS_WINDOW.search(win) or _NEWS_WINDOW.search(blob)
            or _NEWS_CONTENT.search(blob)
            or _ARTICLE_MENTIONED.search(blob)):
        return "news_page"
    # Browser chrome often drops the product name from the tab; trust
    # From/To/Subject OCR when the window is a generic browser frame.
    if (_EMAIL_HEADERS.search(blob) or _EMAIL_CHROME.search(blob)) and (
            _EMAIL_WINDOW.search(blob)
            or re.search(r"\b(?:chrome|edge|firefox|brave|safari|opera)\b", win, re.I)
            or src.startswith("desktop")):
        return "email"
    if _CODE_WINDOW.search(win) and (content_type or "").lower() == "code":
        return "code_editor"
    if _DOC_WINDOW.search(win):
        if "pdf" in win.lower() or "acrobat" in win.lower():
            return "shared_document"
        if "powerpoint" in win.lower() or "slides" in win.lower() or "keynote" in win.lower():
            return "presentation"
        return "user_authored_document"
    if src.startswith("desktop"):
        return "unknown"
    if src.startswith("document") or src.startswith("docs"):
        return "user_authored_document"
    return "unknown"


def policy_for(source_class: str) -> SourcePolicy:
    raw = _raw_policies().get(source_class) or _raw_policies().get("unknown") or _DEFAULT_POLICY
    merged = {**_DEFAULT_POLICY, **raw}
    return SourcePolicy(source_class=source_class, **{
        k: bool(merged.get(k, _DEFAULT_POLICY.get(k, False)))
        for k in _DEFAULT_POLICY
    })


def policy_for_event(
    *,
    event_source: str = "",
    window: str = "",
    text: str = "",
    content_type: str = "",
) -> SourcePolicy:
    return policy_for(classify_source(
        event_source=event_source, window=window, text=text,
        content_type=content_type))
