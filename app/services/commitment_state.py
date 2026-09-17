"""Commitment lifecycle state machine (plan 4.1; extended by the connector
capture & task fulfillment spec, September 2026).

`commitments.state` is the rich lifecycle; `commitments.status` remains the
derived compat view so existing `list_facts(status='open')` callers stay
untouched. The 2026-09 extension adds three states that ALSO surface as their
own status values, because the Tasks board must be able to tell a task that
is waiting for data, or waiting for the user to confirm weak evidence, from a
task that is merely open:

  awaiting_data  the task's completion is defined by data that does not exist
                 in memory yet (an open slot — see services/slots.py)
  uncertain      capture produced evidence too weak to close on; a question
                 is pending for the user and it never auto-closes
  declined       the user said no; terminal for capture (never re-proposed
                 from the same source thread)

`completed` is only reachable with cited evidence or an explicit user actor.
"""
from __future__ import annotations

import json
from typing import Any

# detected → active → in_progress → waiting → completed
#                 ↘ cancelled / superseded / declined
#                 ↘ awaiting_data → active | completed | cancelled | declined
#                 ↘ uncertain → completed (user yes) | active (user not yet)
# detected → cancelled (dismiss-as-noise)
# completed|cancelled → active (reopen)
STATES = (
    "detected",
    "active",
    "in_progress",
    "waiting",
    "awaiting_data",
    "uncertain",
    "completed",
    "cancelled",
    "superseded",
    "declined",
)

OPEN_STATES = frozenset({"detected", "active", "in_progress", "waiting",
                         "awaiting_data", "uncertain"})
DONE_STATES = frozenset({"completed"})
CANCELLED_STATES = frozenset({"cancelled", "superseded", "declined"})
TERMINAL_STATES = frozenset({"declined"})

# Actors that may write a transition. `user` is the human; `capture` is the
# completion detector acting on a persisted event; `agent` is a verified
# agent execution; `peer` is a paired Sparrow (slot delivery / resolution).
ACTORS = ("user", "capture", "agent", "peer")

_COMMON_EXITS = frozenset({"awaiting_data", "uncertain", "declined"})

# from_state → allowed to_states
LEGAL: dict[str, frozenset[str]] = {
    "detected": frozenset({"active", "cancelled", "superseded",
                           "in_progress", "waiting", "completed"}) | _COMMON_EXITS,
    "active": frozenset({"in_progress", "waiting", "completed",
                         "cancelled", "superseded"}) | _COMMON_EXITS,
    "in_progress": frozenset({"waiting", "active", "completed",
                              "cancelled", "superseded"}) | _COMMON_EXITS,
    "waiting": frozenset({"active", "in_progress", "completed",
                          "cancelled", "superseded"}) | _COMMON_EXITS,
    # A slot: fill found + confirmed → active (or straight to completed when
    # the fill IS the completion, e.g. delivered to the requester); a sibling
    # slot resolved elsewhere → cancelled; Drop on the Horizon strip → declined.
    "awaiting_data": frozenset({"active", "completed", "cancelled",
                                "declined", "uncertain"}),
    # The user answers the question: yes → completed, not yet → active.
    "uncertain": frozenset({"completed", "active", "cancelled", "declined"}),
    "completed": frozenset({"active"}),  # reopen
    "cancelled": frozenset({"active"}),  # reopen
    # Compat reopen via set_fact_status('open'); facts.state undo stays primary.
    "superseded": frozenset({"active"}),
    # Decline is terminal for capture: nothing re-proposes it. A new mention
    # mints a NEW task linked to this one (storage.add_commitment).
    "declined": frozenset(),
}

# Compat status ← state
STATUS_FOR_STATE = {
    "detected": "open",
    "active": "open",
    "in_progress": "open",
    "waiting": "open",
    "awaiting_data": "awaiting_data",
    "uncertain": "uncertain",
    "completed": "done",
    "cancelled": "cancelled",
    "superseded": "cancelled",
    "declined": "declined",
}

# set_fact_status / review_fact / POST /tasks/{id}/status → target state
STATE_FOR_STATUS = {
    "open": "active",
    "done": "completed",
    "cancelled": "cancelled",
    "awaiting_data": "awaiting_data",
    "uncertain": "uncertain",
    "declined": "declined",
}

# Every status value the Tasks board may show.
STATUSES = ("open", "awaiting_data", "uncertain", "done", "declined", "cancelled")
# Statuses that still count as open work (the board's default view).
OPEN_STATUSES = frozenset({"open", "awaiting_data", "uncertain"})


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


def evidence_id_of(evidence: Any) -> int | None:
    """The cited event id, when the evidence names one."""
    ev = normalize_evidence(evidence)
    raw = ev.get("evidence_event_id")
    if raw in (None, ""):
        raw = ev.get("evidence_id")
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def done_is_attributable(transition: dict | None) -> bool:
    """The fulfillment metric counts `done` only when the closing transition
    carries an evidence id or was the user's own decision — never an
    unevidenced automatic close."""
    if not transition:
        return False
    if (transition.get("to_state") or "") != "completed":
        return False
    if transition.get("evidence_id") is not None:
        return True
    if (transition.get("actor") or "").lower() == "user":
        return True
    return evidence_id_of(transition.get("evidence")
                          or transition.get("evidence_json")) is not None
