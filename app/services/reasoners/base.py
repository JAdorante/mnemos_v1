"""Shared reasoner proposal + readiness gate (Track D).

Reasoners never mutate approval gating, RISK_TABLE floors, or the trust gate
(I-4). They only *call* readiness.decide / for_task and route through the
existing agent_bridge offer queue.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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


# key -> unix expiry (absolute). Survives restarts via data/reasoner_cooldowns.json.
_recent: dict[str, float] = {}
_lock = threading.Lock()
_loaded = False
_COOLDOWN_S = float(os.environ.get("QUILL_REASONER_COOLDOWN_S", "1800") or "1800")
# After "Not now", stay quiet for days — not just until the next process start.
_DISMISS_COOLDOWN_S = float(
    os.environ.get("QUILL_REASONER_DISMISS_COOLDOWN_S", str(7 * 86400))
    or str(7 * 86400))
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


def _cooldown_path() -> Path:
    override = (os.environ.get("QUILL_REASONER_COOLDOWN_PATH") or "").strip()
    if override:
        return Path(override)
    try:
        from app.config import settings
        return Path(settings.data_dir) / "reasoner_cooldowns.json"
    except Exception:
        return Path("data") / "reasoner_cooldowns.json"


def _ensure_loaded() -> None:
    global _loaded, _day_key, _day_count
    if _loaded:
        return
    _loaded = True
    path = _cooldown_path()
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    now = time.time()
    entries = raw.get("until") if isinstance(raw, dict) else None
    if isinstance(entries, dict):
        for k, until in entries.items():
            try:
                u = float(until)
            except (TypeError, ValueError):
                continue
            if u > now:
                _recent[str(k)] = u
    if isinstance(raw, dict):
        dk = raw.get("day_key")
        if isinstance(dk, str) and dk == time.strftime("%Y-%m-%d"):
            _day_key = dk
            try:
                _day_count = int(raw.get("day_count") or 0)
            except (TypeError, ValueError):
                _day_count = 0


def _persist() -> None:
    now = time.time()
    alive = {k: u for k, u in _recent.items() if u > now}
    _recent.clear()
    _recent.update(alive)
    path = _cooldown_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "until": alive,
            "day_key": _day_key,
            "day_count": _day_count,
        }, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[reasoners] cooldown persist skipped ({exc}).")


def clear_cooldown_for_tests() -> None:
    global _day_key, _day_count, _loaded
    with _lock:
        _recent.clear()
        _day_key = None
        _day_count = 0
        _loaded = True  # skip disk reload mid-test
        # Only wipe an explicit test path — never the live data/ file.
        override = (os.environ.get("QUILL_REASONER_COOLDOWN_PATH") or "").strip()
        if override:
            try:
                Path(override).unlink(missing_ok=True)
            except Exception:
                pass


def _ensure_day() -> None:
    global _day_key, _day_count
    key = time.strftime("%Y-%m-%d")
    if key != _day_key:
        _day_key = key
        _day_count = 0


def daily_budget_remaining() -> int:
    with _lock:
        _ensure_loaded()
        _ensure_day()
        return max(0, _DAILY_MAX - _day_count)


def daily_budget_exhausted() -> bool:
    return daily_budget_remaining() <= 0


def on_cooldown(prop: Proposal) -> bool:
    h = prop.cooldown_key()
    now = time.time()
    with _lock:
        _ensure_loaded()
        until = _recent.get(h)
        return until is not None and now < until


def mark_offered(prop: Proposal) -> None:
    global _day_count
    with _lock:
        _ensure_loaded()
        _ensure_day()
        _recent[prop.cooldown_key()] = time.time() + _COOLDOWN_S
        _day_count += 1
        _persist()


def mark_dismissed(prop: Proposal | None = None, *,
                   reasoner: str | None = None,
                   fact_id: int | None = None,
                   person: str | None = None,
                   goal: str | None = None) -> None:
    """User said Not now — suppress this proposal across restarts."""
    if prop is None:
        prop = Proposal(
            reasoner=reasoner or "commitment",
            goal=goal or "",
            summary="",
            fact_id=fact_id,
            person=person,
        )
    with _lock:
        _ensure_loaded()
        _recent[prop.cooldown_key()] = time.time() + _DISMISS_COOLDOWN_S
        _persist()


def mark_dismissed_from_offer(offer: dict) -> None:
    """Bridge helper: pending_todo dict → long cooldown."""
    if not isinstance(offer, dict):
        return
    kind = str(offer.get("kind") or "")
    reasoner = kind.replace("reasoner_", "", 1) if kind.startswith("reasoner_") else ""
    if not reasoner:
        return
    items = offer.get("items") or []
    goal = (items[0] if items else "") or (offer.get("title") or "")
    mark_dismissed(
        reasoner=reasoner,
        fact_id=offer.get("fact_id"),
        person=offer.get("person"),
        goal=str(goal),
    )


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
