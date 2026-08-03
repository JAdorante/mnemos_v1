"""Heuristic prediction baselines (Track F) — next app, next contact, next doc.

These are the models-to-beat: every scorer is a PURE function over a history
the caller passes in, so the bench can replay it at any historical instant
(no peeking) and a future learned model competes on identical inputs. The
live `predict()` wrappers just assemble history from the store.

Console-only: predictions render on /console/predictors and feed nothing
else. Surfacing belongs to anticipation.py / the shell, behind their own
gates — a predictor that interrupts is a regression here (I-9).

Registry: exactly one active model per task (predictor_models). Heuristics
seed as v1; a learned model may only take over through the bench promote
gate (predictor_bench.promote) and can always be rolled back.
"""
from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from typing import Any

TASKS = ("next_app", "next_contact", "next_document")
HEURISTIC_VERSION = "heuristic-v1"

# Recency half-life for frequency x recency scorers (contacts, documents).
CONTACT_HALF_LIFE_S = 7 * 86400.0
DOC_HALF_LIFE_S = 7 * 86400.0

# next_app blend: what you usually open after THIS app, at THIS hour, overall.
W_TRANSITION, W_HOUR, W_OVERALL = 0.5, 0.3, 0.2


def _cfg():
    from app.config import settings
    return settings.predictors


# --------------------------------------------------------------------------
# Pure scorers — history in, ranked [(key, score)] out. No store, no clock.
# --------------------------------------------------------------------------

def score_next_app(blocks: list[dict], *, now: float,
                   prev_app: str | None = None, k: int = 5
                   ) -> list[tuple[str, float]]:
    """Rank candidate next apps from chronological activity blocks.

    blocks: [{start, end, app}] ascending; entries at/after `now` must already
    be excluded by the caller (the bench guarantees this — no peeking).
    """
    apps = [(b.get("app") or "").strip() for b in blocks]
    apps = [(a, b) for a, b in zip(apps, blocks) if a and a.lower() != "desktop"]
    if not apps:
        return []

    overall: Counter[str] = Counter()
    hour_counts: Counter[str] = Counter()
    trans: Counter[tuple[str, str]] = Counter()
    hour = time.localtime(now).tm_hour
    prev = None
    for name, b in apps:
        overall[name] += 1
        bh = time.localtime(float(b.get("start") or 0)).tm_hour
        if min((bh - hour) % 24, (hour - bh) % 24) <= 1:   # same hour ±1
            hour_counts[name] += 1
        if prev and prev.lower() != name.lower():
            trans[(prev, name)] += 1
        prev = name

    prev_app = (prev_app or "").strip() or (apps[-1][0] if apps else "")
    from_prev = {b: c for (a, b), c in trans.items() if a == prev_app}
    n_overall = sum(overall.values()) or 1
    n_hour = sum(hour_counts.values()) or 1
    n_from = sum(from_prev.values()) or 1

    scores: dict[str, float] = defaultdict(float)
    for name in overall:
        if name.lower() == (prev_app or "").lower():
            continue           # predicting a SWITCH; staying put is not a call
        scores[name] += W_TRANSITION * from_prev.get(name, 0) / n_from
        scores[name] += W_HOUR * hour_counts.get(name, 0) / n_hour
        scores[name] += W_OVERALL * overall.get(name, 0) / n_overall
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(n, round(s, 4)) for n, s in ranked[:k] if s > 0]


def score_next_contact(interactions: list[tuple[float, int]], *, now: float,
                       boosts: dict[int, float] | None = None, k: int = 5,
                       half_life_s: float = CONTACT_HALF_LIFE_S
                       ) -> list[tuple[int, float]]:
    """Rank person ids by frequency x recency of interaction, plus optional
    boosts (e.g. an attendee on an upcoming calendar event)."""
    scores: dict[int, float] = defaultdict(float)
    for ts, pid in interactions:
        if ts >= now:
            continue
        scores[int(pid)] += math.pow(0.5, (now - float(ts)) / half_life_s)
    if not scores and not boosts:
        return []
    top = max(scores.values()) if scores else 1.0
    for pid, extra in (boosts or {}).items():
        scores[int(pid)] += float(extra) * max(top, 1e-6)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(p, round(s, 4)) for p, s in ranked[:k]]


def score_next_document(opens: list[tuple[float, str]], *, now: float,
                        k: int = 5, half_life_s: float = DOC_HALF_LIFE_S
                        ) -> list[tuple[str, float]]:
    """Rank document keys (path/name) by frequency x recency of appearance."""
    scores: dict[str, float] = defaultdict(float)
    for ts, key in opens:
        key = (key or "").strip()
        if not key or ts >= now:
            continue
        scores[key] += math.pow(0.5, (now - float(ts)) / half_life_s)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(d, round(s, 4)) for d, s in ranked[:k]]


# --------------------------------------------------------------------------
# History assembly from the store (shared by live predict + bench replay).
# --------------------------------------------------------------------------

def app_history(store, limit: int = 2000) -> list[dict]:
    """Chronological activity blocks (oldest first)."""
    try:
        rows = store.recent_activities(limit=limit)
    except Exception:
        return []
    return sorted(rows, key=lambda b: float(b.get("start") or 0))


def contact_history(store, limit: int = 4000) -> list[tuple[float, int]]:
    """(ts, person_id) interaction stream from graph relations touching people."""
    try:
        rels = store.all_relations()
    except Exception:
        return []
    out: list[tuple[float, int]] = []
    for r in rels:
        ts = float(r.get("ts") or 0)
        if not ts:
            continue
        if r.get("source_type") == "person":
            out.append((ts, int(r["source_id"])))
        if r.get("target_type") == "person":
            out.append((ts, int(r["target_id"])))
    out.sort(key=lambda x: x[0])
    return out[-limit:]


def document_history(store, limit: int = 1000) -> list[tuple[float, str]]:
    """(ts, doc key) from documents.* events; key = meta path, else summary."""
    try:
        events = store.recent_events(source_substr="documents", limit=limit)
    except Exception:
        return []
    out: list[tuple[float, str]] = []
    for ev in events:
        meta = ev.get("meta") or {}
        if isinstance(meta, str):
            try:
                import json
                meta = json.loads(meta)
            except Exception:
                meta = {}
        key = (meta.get("path") or meta.get("name") or ev.get("summary")
               or "").strip()
        if key:
            out.append((float(ev.get("time") or 0), key))
    out.sort(key=lambda x: x[0])
    return out


def _calendar_contact_boosts(store, *, now: float) -> dict[int, float]:
    """People named on calendar events in the next 90 min get a boost —
    reuses the Horizon strip's parsing so both surfaces agree."""
    try:
        from app.services import horizon as _h
        events = _h._next_calendar_events(store, now=now, horizon_s=90 * 60)
        people = _h._people_index(store)
    except Exception:
        return {}
    boosts: dict[int, float] = {}
    for ev in events:
        for typ, pid, _name in _h._resolve_in_text(ev["text"], people, []):
            if typ == "person":
                boosts[int(pid)] = max(boosts.get(int(pid), 0.0), 0.5)
    return boosts


# --------------------------------------------------------------------------
# Live prediction + registry
# --------------------------------------------------------------------------

def ensure_registry(store) -> None:
    """Seed the heuristic as the active v1 model for any task missing one."""
    for task in TASKS:
        try:
            if store.active_predictor_model(task) is None:
                store.save_predictor_model(
                    task=task, version=HEURISTIC_VERSION, kind="heuristic",
                    note="seeded baseline", activate=True)
        except Exception as exc:
            print(f"[predictors] registry seed skipped for {task} ({exc}).")


def predict(task: str, store, *, now: float | None = None,
            k: int = 5) -> list[dict]:
    """Ranked predictions for one task, with human-readable labels/reasons.
    The active registry model is heuristic-v1 for every task today; a learned
    model would dispatch here on its version once promoted."""
    if not _cfg().enabled:
        return []
    now = float(now if now is not None else time.time())
    out: list[dict] = []
    if task == "next_app":
        blocks = [b for b in app_history(store)
                  if float(b.get("start") or 0) < now]
        prev = blocks[-1]["app"] if blocks else None
        for name, s in score_next_app(blocks, now=now, prev_app=prev, k=k):
            out.append({"key": name, "label": name, "p": s,
                        "reason": f"often follows {prev}" if prev else "frequent"})
    elif task == "next_contact":
        inter = [(ts, pid) for ts, pid in contact_history(store) if ts < now]
        boosts = _calendar_contact_boosts(store, now=now)
        for pid, s in score_next_contact(inter, now=now, boosts=boosts, k=k):
            try:
                p = store.get_person(pid)
                label = (p or {}).get("name") or (p or {}).get(
                    "canonical_name") or f"person:{pid}"
            except Exception:
                label = f"person:{pid}"
            reason = ("on your calendar soon" if pid in boosts
                      else "recent frequent contact")
            out.append({"key": f"person:{pid}", "label": label, "p": s,
                        "reason": reason})
    elif task == "next_document":
        opens = [(ts, key) for ts, key in document_history(store) if ts < now]
        for key, s in score_next_document(opens, now=now, k=k):
            out.append({"key": key, "label": key.rsplit("\\", 1)[-1]
                        .rsplit("/", 1)[-1], "p": s,
                        "reason": "recently and repeatedly open"})
    return out
