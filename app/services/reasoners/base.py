"""Shared reasoner proposal + readiness gate (Track D).

Reasoners never mutate approval gating, RISK_TABLE floors, or the trust gate
(I-4). They only *call* readiness.decide / for_task and route through the
existing agent_bridge offer queue.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Proposal:
    reasoner: str                 # commitment | relationship | scheduling
    goal: str                     # what 'yes' compiles / runs
    summary: str                  # short chat headline
    confidence: float = 0.7
    fact_id: int | None = None
    person: str | None = None
    why: list[str] = field(default_factory=list)
    deliverable_only: bool = False  # surface=none briefing on accept
    surface: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return f"reasoner_{self.reasoner}"

    def cooldown_key(self) -> str:
        raw = f"{self.reasoner}|{self.fact_id or ''}|{self.person or ''}|{self.goal[:80]}"
        return hashlib.sha1(raw.lower().encode()).hexdigest()


_recent: dict[str, float] = {}
_lock = threading.Lock()
_COOLDOWN_S = float(os.environ.get("QUILL_REASONER_COOLDOWN_S", "1800") or "1800")
# I-9 calm: hard cap on reasoner interruptions per calendar day.
_DAILY_MAX = int(os.environ.get("QUILL_REASONER_DAILY_MAX", "3") or "3")
_day_key: str | None = None
_day_count: int = 0


def enabled() -> bool:
    """Reasoners ON by default when the agent is on; QUILL_REASONERS=0 kills them."""
    if os.environ.get("QUILL_REASONERS", "1") in ("0", "false", "False"):
        return False
    if os.environ.get("QUILL_AGENT") in ("0", "false", "False"):
        return False
    return True


def clear_cooldown_for_tests() -> None:
    global _day_key, _day_count
    with _lock:
        _recent.clear()
        _day_key = None
        _day_count = 0


def _ensure_day() -> None:
    global _day_key, _day_count
    key = time.strftime("%Y-%m-%d")
    if key != _day_key:
        _day_key = key
        _day_count = 0


def daily_budget_remaining() -> int:
    with _lock:
        _ensure_day()
        return max(0, _DAILY_MAX - _day_count)


def daily_budget_exhausted() -> bool:
    return daily_budget_remaining() <= 0


def on_cooldown(prop: Proposal) -> bool:
    h = prop.cooldown_key()
    now = time.time()
    with _lock:
        last = _recent.get(h)
        return last is not None and (now - last) < _COOLDOWN_S


def mark_offered(prop: Proposal) -> None:
    global _day_count
    with _lock:
        _ensure_day()
        _recent[prop.cooldown_key()] = time.time()
        _day_count += 1


def gate(prop: Proposal) -> tuple[bool, Any]:
    """Run the existing readiness seam. Never invents a parallel bar."""
    from app.services.readiness import for_task
    v = for_task(prop.goal, prop.confidence)
    return bool(v.should_offer), v


def pick_best(candidates: list[Proposal]) -> Proposal | None:
    """Calm budget: one candidate — highest confidence, then longest why."""
    alive = [c for c in candidates if c.goal.strip() and not on_cooldown(c)]
    if not alive:
        return None
    alive.sort(key=lambda p: (-float(p.confidence), -len(p.why), p.reasoner))
    return alive[0]
