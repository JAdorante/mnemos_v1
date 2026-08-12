"""People v3 P4 (WS-C) — recurrence-gated person minting.

"People must recur before they exist": one overheard "Kevin Doyle from the
vendor call" is not a relationship — it is a candidate for one. Behind
QUILL_MINT_RECURRENCE (default OFF):

- When `resolve_person_mention` would mint a NEW Person for an unmatched
  name, the mention parks in the pending pool instead — the EXISTING
  person_mentions ledger with resolution_status='pending_mint' (no new
  table). Every guard upstream still applies first: junk / OS-account /
  single-token names and policy-denied surfaces never reach the pool, and
  a name whose canonical spelling collides with a banned/hidden/absorbed
  person row leaves open — it must not pool toward a twin mint either.
- The Person is minted only once the same normalized name has been seen in
  >= min_sessions (default 2) DISTINCT sessions. Session identity comes from
  the sessions table (`sessions_in_range`); sightings not covered by any
  session row yet (consolidation is derived + rebuilt later) fall back to
  gap-grouping with the consolidation session gap, so two sightings in the
  same conversation never count twice.
- The mint is retroactive: pooled mentions are adopted (resolved onto the
  new person), their spellings become aliases, and typed task/commitment
  columns born from the pooled events are filled where still NULL and the
  fact's own span names the person (P3 rebind idiom) — no signal is lost.
- Pending mentions that never recur are archived after ttl_days
  (status='pending_expired', never deleted).

Composes with QUILL_PEOPLE_ESCROW (P3): escrow handles UNNAMED speakers
("Speaker 3") — those never reach resolution at all; this gate handles
named-but-new mentions, including a labeled speaker whose name is new.

Flag OFF = byte-identical behavior: the pipeline hook checks `enabled()`
first and touches none of the new storage.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.config import settings

PENDING_STATUS = "pending_mint"
EXPIRED_STATUS = "pending_expired"

_DEFAULT_MIN_SESSIONS = 2
_DEFAULT_TTL_DAYS = 30.0
_DEFAULT_SESSION_GAP_S = 300.0


def _cfg():
    return getattr(settings, "mint_recurrence", None)


def enabled() -> bool:
    """QUILL_MINT_RECURRENCE gate. getattr-chained: older suites patch
    settings sub-objects with SimpleNamespace, so never touch attributes
    directly."""
    return bool(getattr(_cfg(), "enabled", False))


def min_sessions() -> int:
    try:
        return max(1, int(getattr(_cfg(), "min_sessions",
                                  _DEFAULT_MIN_SESSIONS)))
    except Exception:
        return _DEFAULT_MIN_SESSIONS


def ttl_days() -> float:
    try:
        return float(getattr(_cfg(), "ttl_days", _DEFAULT_TTL_DAYS))
    except Exception:
        return _DEFAULT_TTL_DAYS


def _session_gap_s() -> float:
    cons = getattr(settings, "consolidation", None)
    try:
        return float(getattr(cons, "session_gap_s", _DEFAULT_SESSION_GAP_S))
    except Exception:
        return _DEFAULT_SESSION_GAP_S


def distinct_sessions(store, timestamps) -> int:
    """How many distinct sessions a set of sighting timestamps spans.

    A sighting inside (or within one session gap of) a sessions-table row
    takes that row's identity. Sightings no session row covers — the live
    case, where consolidation builds sessions after the fact — are
    gap-grouped among themselves: two timestamps within session_gap_s are
    the same conversation. Same-session repeats therefore never count twice,
    with or without derived session rows."""
    gap = _session_gap_s()
    keys: set[tuple] = set()
    uncovered: list[float] = []
    for ts in timestamps:
        ts = float(ts)
        rows = []
        try:
            rows = store.sessions_in_range(ts - gap, ts + gap)
        except Exception:
            rows = []
        if rows:
            keys.add(("session", int(rows[0]["id"])))
        else:
            uncovered.append(ts)
    n = len(keys)
    prev: float | None = None
    for ts in sorted(uncovered):
        if prev is None or ts - prev > gap:
            n += 1
        prev = ts
    return n


@dataclass
class MintGate:
    """Verdict for one would-be create_new under the recurrence gate."""
    action: str          # mint | pool | leave_open
    sessions_seen: int = 0
    pending: int = 0


def evaluate_mint(store, *, display: str, now: float,
                  mint_collides: bool = False) -> MintGate:
    """Decide what a would-be first-sight mint does.

    `mint_collides` is the caller's banned-canonical check: an exact-name
    person row exists that this resolution was not allowed to bind
    (hidden / absorbed / negative alias rule). Those must leave_open — never
    pool toward a twin canonical name.
    """
    sweep_expired(store, now=now)
    if mint_collides:
        return MintGate("leave_open")
    pending = []
    try:
        pending = store.pending_mint_mentions(display)
    except Exception as exc:
        print(f"[mint_recurrence] pool read skipped ({exc}).")
    stamps = [float(r["observed_at"]) for r in pending] + [float(now)]
    n = distinct_sessions(store, stamps)
    if n >= min_sessions():
        return MintGate("mint", sessions_seen=n, pending=len(pending))
    return MintGate("pool", sessions_seen=n, pending=len(pending))


def sweep_expired(store, *, now: float | None = None) -> int:
    """TTL pass over the whole pending pool. Archive-only and idempotent
    (rows flip to 'pending_expired'; evidence is never deleted). Runs lazily
    on every gated resolution, so no job wiring is needed."""
    now = float(now if now is not None else time.time())
    cutoff = now - ttl_days() * 86400.0
    try:
        return int(store.expire_pending_mint_mentions(cutoff, now))
    except Exception as exc:
        print(f"[mint_recurrence] expiry sweep skipped ({exc}).")
        return 0


def adopt_pending(store, *, person_id: int, display: str, ts: float) -> dict:
    """Retroactive mint: bind every pooled mention for `display` to the new
    person, record each pooled spelling as an alias, and fill typed
    task/commitment person columns for facts born from the pooled events
    (only where still NULL and the fact's own text names the person)."""
    rows = store.adopt_pending_mint_mentions(display, person_id, ts=ts)
    typed_linked = 0
    for r in rows:
        raw = (r.get("raw_text") or "").strip()
        if raw:
            try:
                store.touch_person(int(person_id), ts, alias=raw)
            except Exception:
                pass
        ev = r.get("event_id")
        if not ev:
            continue
        try:
            res = store.retro_link_person_rows(
                person_id=int(person_id), event_id=int(ev),
                role=(r.get("grammatical_role") or ""),
                name=(r.get("normalized_text") or display), ts=ts)
            typed_linked += int(res.get("linked", 0))
        except Exception as exc:
            print(f"[mint_recurrence] retro link skipped for mention "
                  f"{r.get('mention_id')} ({exc}).")
    out = {"adopted": len(rows), "typed_linked": typed_linked}
    if rows:
        print(f"[mint_recurrence] retro-mint person {person_id} "
              f"({display!r}): {out}")
    return out


def pool_status(store, display: str) -> dict:
    """Observability for tests/console: the pool for one identity."""
    rows = store.pending_mint_mentions(display)
    return {"pending": len(rows),
            "sessions": distinct_sessions(
                store, [r["observed_at"] for r in rows])}
