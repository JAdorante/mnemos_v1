"""Portable trust-layer core — risk table + hash-bind + memory-never-authorizes.

No imports from capture, memory, or graph. See docs/trust-layer.md.
Callers historically imported these from agent_planner; that module re-exports
them so existing tests keep working.
"""
from __future__ import annotations

import os

# A precise, inspectable table beats an LLM guess for the safety-critical
# decision. `blocked` never reaches an execution surface; `high` always
# forces the approval gate.
RISK_TABLE: dict[str, str] = {
    "read":     "low",
    "search":   "low",
    "draft":    "low",       # preparing is safe; only committing is not
    "summarize": "low",
    "follow_through": "low",   # Track D: commitment brief (no send)
    "check_in": "low",         # Track D: relationship brief / draft-only
    "schedule": "medium",
    "book":     "medium",
    "post":     "medium",
    "send":     "high",
    "reply":    "high",
    "buy":      "high",
    "purchase": "high",
    "pay":      "high",
    "delete":   "blocked",
    "remove":   "blocked",
}

SENSITIVE = ("medical", "health", "financial", "bank", "ssn", "password")

# Sources that may inform a draft but must never count as approval.
NON_AUTHORIZING_PREFIXES = (
    "omi:", "external:", "phone.", "exhaust.", "peer.", "org.",
)


def source_can_authorize(source: str = "", meta: dict | None = None) -> bool:
    """Invariant 3: retrieved memory never authorizes an action.

    Live human replies go through /chat/answer, not through this function.
    External capture, exhaust ingest, peer answers, and phone notes are all
    observed-tier context.
    """
    meta = meta or {}
    if meta.get("never_authorizes") or meta.get("external_source"):
        return False
    src = (source or "").lower()
    if any(src.startswith(p) for p in NON_AUTHORIZING_PREFIXES):
        return False
    return False  # memory is never command authority, regardless of source


def classify_risk(action_kind: str, *, goal: str = "") -> tuple[str, bool]:
    """(risk_level, approval_required). approval_required is True for anything
    at/above medium, or any brush with a sensitive domain.

    `blocked` still reports approval_required=True for back-compat callers, but
    execution surfaces must call `is_policy_blocked` / `execution_allowed` —
    autonomous mode bypasses the ask, never a blocked class (plan 0.7).
    """
    kind = (action_kind or "").strip().lower()
    risk = RISK_TABLE.get(kind, "medium")
    if any(w in (goal or "").lower() for w in SENSITIVE) and risk == "low":
        risk = "medium"
    approval = risk in ("medium", "high", "blocked")
    return risk, approval


def approval_binding_is_enforce() -> bool:
    """True when plan 0.4 binding is default-on (enforce). Used by graduation
    checks — planner code default assumes this posture."""
    raw = os.environ.get("QUILL_APPROVAL_BIND")
    if raw is None or not str(raw).strip():
        return True  # code default
    v = str(raw).strip().lower()
    if v in ("0", "off", "false", "no", "shadow", "log"):
        return False
    return v in ("enforce", "on", "1", "true", "yes")
