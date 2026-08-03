"""Attention modes — goal conditioning as Scorer context (Field §9 / WS4).

Semantics (binding): mode chips set the **context vector** fed to the Scorer
(kind multipliers + quiet). They *reweight gravity*; they do not filter the
candidate set. Auto = infer from calendar / foreground activity.

Manual chip selection wins for 2h; otherwise deterministic inference.
Applied inside ranking.Scorer via PipelineContext.mode (not a pipeline fork).
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

# Manual override TTL
MANUAL_TTL_S = 2 * 3600.0

# kind → multiplier. Unlisted kinds stay at 1.0.
_MODES: dict[str, dict[str, Any]] = {
    "meeting": {
        "label": "Meeting",
        "quiet": False,
        "kind": {
            "person": 1.25, "commitment": 1.30, "task": 1.15,
            "project": 1.20, "tool": 0.85, "idea": 0.90, "place": 1.05,
        },
    },
    "writing": {
        "label": "Writing",
        "quiet": False,
        "kind": {
            "person": 0.90, "commitment": 1.05, "task": 1.10,
            "project": 1.15, "tool": 1.05, "idea": 1.25, "place": 0.85,
        },
    },
    "coding": {
        "label": "Coding",
        "quiet": False,
        "kind": {
            "person": 0.80, "commitment": 0.95, "task": 1.10,
            "project": 1.20, "tool": 1.30, "idea": 1.10, "place": 0.80,
        },
    },
    "research": {
        "label": "Research",
        "quiet": False,
        "kind": {
            "person": 0.95, "commitment": 0.90, "task": 1.00,
            "project": 1.15, "tool": 1.10, "idea": 1.30, "place": 0.90,
        },
    },
    "planning": {
        "label": "Planning",
        "quiet": False,
        "kind": {
            "person": 1.10, "commitment": 1.25, "task": 1.20,
            "project": 1.25, "tool": 0.90, "idea": 1.15, "place": 1.00,
        },
    },
    "errand": {
        "label": "Errand",
        "quiet": False,
        "kind": {
            "person": 1.05, "commitment": 1.15, "task": 1.20,
            "project": 0.85, "tool": 0.90, "idea": 0.80, "place": 1.30,
        },
    },
    "off": {
        "label": "Off / Family",
        "quiet": True,
        "kind": {
            "person": 1.10, "commitment": 1.15, "task": 0.70,
            "project": 0.50, "tool": 0.40, "idea": 0.45, "place": 1.00,
        },
    },
}

_IDE = re.compile(
    r"\b(code|cursor|vscode|visual studio|intellij|pycharm|xcode|terminal|"
    r"iterm|windows terminal|sublime|neovim|vim)\b", re.I)
_WRITE = re.compile(
    r"\b(word|docs|notion|obsidian|bear|ulysses|ia writer|pages|evernote|"
    r"onenote|google docs)\b", re.I)
_RESEARCH = re.compile(
    r"\b(chrome|firefox|safari|edge|arxiv|scholar|wikipedia|reader|pdf)\b",
    re.I)

_lock = threading.RLock()
_manual: dict[str, Any] | None = None  # {name, until, set_at}


def registry() -> list[dict[str, Any]]:
    return [
        {"id": k, "label": v["label"], "quiet": bool(v.get("quiet"))}
        for k, v in _MODES.items()
    ]


def set_manual(name: str | None, *, ttl_s: float = MANUAL_TTL_S) -> dict[str, Any]:
    """Manual chip selection. Pass None / '' / 'auto' to clear."""
    global _manual
    now = time.time()
    key = (name or "").strip().lower()
    with _lock:
        if not key or key in ("auto", "clear", "none"):
            _manual = None
            return current(store=None, now=now)
        if key not in _MODES:
            raise ValueError(f"unknown mode: {name}")
        _manual = {"name": key, "until": now + float(ttl_s), "set_at": now}
        return current(store=None, now=now)


def _manual_active(now: float) -> str | None:
    with _lock:
        if not _manual:
            return None
        if float(_manual.get("until") or 0) < now:
            return None
        return _manual.get("name")


def _infer_from_calendar(store, now: float) -> tuple[str | None, float]:
    """Meeting if a calendar event is within ±30m / next 90m."""
    if store is None:
        return None, 0.0
    try:
        events = store.recent_events(source_substr="calendar", limit=40)
    except Exception:
        return None, 0.0
    best = None
    best_w = 0.0
    for ev in events:
        meta = ev.get("meta") or {}
        if isinstance(meta, str):
            try:
                import json
                meta = json.loads(meta)
            except Exception:
                meta = {}
        start = meta.get("start")
        start_ts = None
        if isinstance(start, (int, float)):
            start_ts = float(start)
        elif isinstance(start, str) and start.strip():
            try:
                from datetime import datetime
                start_ts = datetime.fromisoformat(
                    start.replace("Z", "+00:00")).timestamp()
            except Exception:
                start_ts = None
        if start_ts is None:
            continue
        dt = start_ts - now
        if -30 * 60 <= dt <= 90 * 60:
            w = 0.85 if abs(dt) < 15 * 60 else 0.65
            if w > best_w:
                best, best_w = "meeting", w
    return best, best_w


def _infer_from_activity(store) -> tuple[str | None, float]:
    if store is None:
        return None, 0.0
    try:
        from app.services.activity import describe_recent
        lines = describe_recent(store, limit=3)
    except Exception:
        return None, 0.0
    blob = " ".join(lines)
    if not blob:
        return None, 0.0
    if _IDE.search(blob):
        return "coding", 0.70
    if _WRITE.search(blob):
        return "writing", 0.65
    if _RESEARCH.search(blob):
        return "research", 0.55
    return None, 0.0


def current(store=None, *, now: float | None = None) -> dict[str, Any]:
    """Resolved mode: manual > calendar > activity > planning default."""
    now = float(now if now is not None else time.time())
    manual = _manual_active(now)
    if manual:
        m = _MODES[manual]
        with _lock:
            until = _manual.get("until") if _manual else None
        return {
            "id": manual,
            "label": m["label"],
            "source": "manual",
            "confidence": 1.0,
            "quiet": bool(m.get("quiet")),
            "until": until,
            "kind_multipliers": dict(m.get("kind") or {}),
        }

    cal_id, cal_w = _infer_from_calendar(store, now)
    act_id, act_w = _infer_from_activity(store)
    if cal_id and cal_w >= act_w:
        chosen, conf, src = cal_id, cal_w, "calendar"
    elif act_id:
        chosen, conf, src = act_id, act_w, "activity"
    else:
        chosen, conf, src = "planning", 0.35, "default"

    m = _MODES[chosen]
    return {
        "id": chosen,
        "label": m["label"],
        "source": src,
        "confidence": round(conf, 3),
        "quiet": bool(m.get("quiet")),
        "until": None,
        "kind_multipliers": dict(m.get("kind") or {}),
    }


def apply_to_candidates(ranked: list[dict], mode: dict[str, Any] | None = None,
                        *, store=None) -> list[dict]:
    """Multiply gravity by mode kind weights; quiet keeps pins + urgent only."""
    mode = mode or current(store=store)
    mults = mode.get("kind_multipliers") or {}
    quiet = bool(mode.get("quiet"))
    out: list[dict] = []
    for n in ranked:
        nn = dict(n)
        kind = nn.get("kind") or ""
        m = float(mults.get(kind, 1.0))
        g = float(nn.get("gravity") or 0.0) * m
        if quiet and not nn.get("pinned"):
            risk = float(nn.get("prospective_risk") or 0.0)
            if risk < 0.75:
                g *= 0.25  # dim non-urgent work in Off/Family
        nn["gravity"] = round(g, 4)
        nn["_mode_mult"] = m
        # Keep linear prominence in sync until softmax reallocates focus.
        nn["prominence"] = round(min(1.9, 0.4 + g * 1.5), 3)
        out.append(nn)
    out.sort(key=lambda n: (-int(bool(n.get("pinned"))), -float(n.get("gravity") or 0)))
    return out
