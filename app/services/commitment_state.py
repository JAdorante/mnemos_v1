"""Commitment lifecycle state machine (plan 4.1).

`commitments.state` is the rich lifecycle; `commitments.status` remains the
derived compat view (`open`/`done`/`cancelled`) so existing
`list_facts(status='open')` callers stay untouched.
"""
from __future__ import annotations

import json
from typing import Any

# detected → active → in_progress → waiting → completed
#                 ↘ cancelled / superseded
# detected → cancelled (dismiss-as-noise)
# completed|cancelled → active (reopen)
STATES = (
    "detected",
    "active",
    "in_progress",
    "waiting",
    "completed",
    "cancelled",
    "superseded",
)

OPEN_STATES = frozenset({"detected", "active", "in_progress", "waiting"})
DONE_STATES = frozenset({"completed"})
CANCELLED_STATES = frozenset({"cancelled", "superseded"})

# from_state → allowed to_states
LEGAL: dict[str, frozenset[str]] = {
    "detected": frozenset({"active", "cancelled", "superseded",
                           "in_progress", "waiting", "completed"}),
    "active": frozenset({"in_progress", "waiting", "completed",
                         "cancelled", "superseded"}),
    "in_progress": frozenset({"waiting", "active", "completed",
                              "cancelled", "superseded"}),
    "waiting": frozenset({"active", "in_progress", "completed",
                          "cancelled", "superseded"}),
    "completed": frozenset({"active"}),  # reopen
    "cancelled": frozenset({"active"}),  # reopen
    # Compat reopen via set_fact_status('open'); facts.state undo stays primary.
    "superseded": frozenset({"active"}),
}

# Compat status ← state
STATUS_FOR_STATE = {
    "detected": "open",
    "active": "open",
    "in_progress": "open",
    "waiting": "open",
    "completed": "done",
    "cancelled": "cancelled",
    "superseded": "cancelled",
}

# set_fact_status / review_fact → target state
STATE_FOR_STATUS = {
    "open": "active",
    "done": "completed",
    "cancelled": "cancelled",
}


class TransitionError(ValueError):
    """Illegal commitment state transition."""


def status_for(state: str) -> str:
    s = (state or "").strip().lower()
    if s not in STATUS_FOR_STATE:
        raise TransitionError(f"unknown commitment state: {state!r}")
    return STATUS_FOR_STATE[s]


def state_for_status(status: str) -> str:
    st = (status or "").strip().lower()
    if st not in STATE_FOR_STATUS:
        raise TransitionError(f"unknown compat status: {status!r}")
    return STATE_FOR_STATUS[st]


def is_legal(from_state: str, to_state: str) -> bool:
    a = (from_state or "").strip().lower()
    b = (to_state or "").strip().lower()
    if a not in STATES or b not in STATES:
        return False
    if a == b:
        return True  # no-op ok
    return b in LEGAL.get(a, frozenset())


def require_legal(from_state: str, to_state: str) -> None:
    a = (from_state or "").strip().lower()
    b = (to_state or "").strip().lower()
    if a not in STATES:
        raise TransitionError(f"unknown from_state: {from_state!r}")
    if b not in STATES:
        raise TransitionError(f"unknown to_state: {to_state!r}")
    if a == b:
        return
    if b not in LEGAL.get(a, frozenset()):
        raise TransitionError(
            f"illegal commitment transition: {a!r} → {b!r}"
        )


def normalize_evidence(evidence: Any) -> dict[str, Any]:
    if evidence is None:
        return {}
    if isinstance(evidence, dict):
        return dict(evidence)
    if isinstance(evidence, str) and evidence.strip():
        try:
            parsed = json.loads(evidence)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {"note": evidence.strip()}
    return {}


def evidence_ok_for_completed(evidence: Any) -> bool:
    """Completed requires cited evidence (plan 4.1; 4.2 tightens sources)."""
    ev = normalize_evidence(evidence)
    if not ev:
        return False
    # Any non-empty cite key is enough for 4.1.
    for key in ("evidence_event_id", "source", "note", "source_fact_ids",
                "quote"):
        if ev.get(key) not in (None, "", [], {}):
            return True
    return bool(ev)
