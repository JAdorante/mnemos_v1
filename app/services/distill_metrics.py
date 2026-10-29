"""The weekly distillation number: parent calls avoided, at equal or better
quality.

Read-only over one seat's data dir; computes, never writes app state:

  avoided   On local-first tasks every answer starts local, and a parent call
            happens only when the local answer was not kept (or local was
            down). So avoided = local calls - parent calls, per task, from
            data/model_calls.jsonl, which logs every call whatever flags are
            set. Conservative: a straight-to-parent call (local down) counts
            against the seat. Valued at the seat's own mean parent cost per
            call for that task over the whole log.
  quality   Human verdicts on answers that STAYED local (learning pairs with
            no parent_output): chat outcomes and fact reviews. Plus shadow
            grades (data/shadow_grades.jsonl) where a cloud grader exists.
  held      This week's local acceptance vs the previous four weeks. Fewer
            than MIN_VERDICTS verdicts in either window -> "too_few_verdicts",
            never a guess.
  exemplars Calls that injected exemplars, and how many used a teammate's
            (pair ids prefixed "peer:"), from data/exemplar_uses.jsonl.

Weeks are ISO weeks in UTC ("2026-W44").
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Tasks the router (and the VLM router for vision) try locally first.
LOCAL_FIRST = ("chat", "extract", "reflect", "enhance", "vision")
LOCAL_PROVIDERS = frozenset({"ollama", "local"})
MIN_VERDICTS = 5
BASELINE_WEEKS = 4
# A drop larger than this against the baseline is "dropped", not noise.
TOLERANCE = 0.05

_POSITIVE = ("accepted",)
_NEGATIVE = ("edited", "rejected", "dismissed")
_VERDICT_SOURCES = ("chat.outcome", "facts.review")


def week_of(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts, timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(week: str) -> tuple[float, float]:
    y, w = week.split("-W")
    start = datetime.fromisocalendar(int(y), int(w), 1).replace(
        tzinfo=timezone.utc)
    return start.timestamp(), (start + timedelta(days=7)).timestamp()


def _jsonl(path: Path):
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _calls(data: Path) -> list[dict]:
    return [r for r in _jsonl(data / "model_calls.jsonl")
            if r.get("ok") and r.get("time")]


def _parent_cost_per_call(calls: list[dict]) -> dict[str, float]:
    tot, n = defaultdict(float), Counter()
    for r in calls:
        if r.get("provider") not in LOCAL_PROVIDERS:
            tot[r["task"]] += float(r.get("cost_usd") or 0.0)
            n[r["task"]] += 1
    return {t: tot[t] / n[t] for t in n}


def _calls_in(calls: list[dict], t0: float, t1: float) -> dict:
    local, parent = Counter(), Counter()
    cost = 0.0
    for r in calls:
        if not t0 <= r["time"] < t1 or r.get("task") not in LOCAL_FIRST:
            continue
        if r.get("provider") in LOCAL_PROVIDERS:
            local[r["task"]] += 1
        else:
            parent[r["task"]] += 1
            cost += float(r.get("cost_usd") or 0.0)
    return {"local": local, "parent": parent, "parent_cost_usd": cost}


def _verdicts(data: Path, t0: float, t1: float) -> Counter:
    """Human verdicts on local answers in [t0, t1)."""
    out: Counter = Counter()
    db = data / "quill.db"
    if not db.is_file():
        return out
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT verdict, COUNT(*) FROM learning_pairs "
                "WHERE created_at >= ? AND created_at < ? "
                "AND (parent_output IS NULL OR parent_output = '') "
                f"AND verdict_source IN ({','.join('?' * len(_VERDICT_SOURCES))}) "
                "GROUP BY verdict",
                (t0, t1, *_VERDICT_SOURCES)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    for verdict, n in rows:
        out[str(verdict)] += int(n)
    return out


def _acceptance(v: Counter) -> tuple[float | None, int]:
    n = sum(v[k] for k in (*_POSITIVE, *_NEGATIVE))
    if not n:
        return None, 0
    return sum(v[k] for k in _POSITIVE) / n, n


def _shadow(data: Path, t0: float, t1: float) -> Counter:
    return Counter(str(r.get("verdict")) for r in
                   _jsonl(data / "shadow_grades.jsonl")
                   if t0 <= float(r.get("ts") or 0) < t1)


def _exemplar_uses(data: Path, t0: float, t1: float) -> dict:
    calls = peer = 0
    for r in _jsonl(data / "exemplar_uses.jsonl"):
        if not t0 <= float(r.get("ts") or 0) < t1:
            continue
        calls += 1
        if any(str(p or "").startswith("peer:")
               for p in r.get("pair_ids") or []):
            peer += 1
    return {"calls_with_exemplars": calls, "calls_with_peer_exemplars": peer}


def report(data_dir: str | Path, week: str | None = None, *,
           now: float | None = None) -> dict:
    """The weekly number for one seat. `week` defaults to the last complete
    ISO week."""
    data = Path(data_dir)
    if week is None:
        ref = datetime.fromtimestamp(now, timezone.utc) if now else \
            datetime.now(timezone.utc)
        week = week_of((ref - timedelta(days=7)).timestamp())
    t0, t1 = week_bounds(week)
    calls = _calls(data)
    unit = _parent_cost_per_call(calls)
    c = _calls_in(calls, t0, t1)

    by_task = {}
    avoided = 0
    avoided_usd = 0.0
    for task in LOCAL_FIRST:
        lo, pa = c["local"][task], c["parent"][task]
        if not (lo or pa):
            continue
        a = max(0, lo - pa)
        avoided += a
        avoided_usd += a * unit.get(task, 0.0)
        by_task[task] = {"local": lo, "parent": pa, "avoided": a}
    parent_calls = sum(c["parent"].values())
    served = avoided + parent_calls

    v_now = _verdicts(data, t0, t1)
    v_base = _verdicts(data, t0 - BASELINE_WEEKS * 7 * 86400, t0)
    acc_now, n_now = _acceptance(v_now)
    acc_base, n_base = _acceptance(v_base)
    if n_now < MIN_VERDICTS or n_base < MIN_VERDICTS:
        held = "too_few_verdicts"
    elif acc_now >= acc_base - TOLERANCE:
        held = "held"
    else:
        held = "dropped"

    shadow = _shadow(data, t0, t1)
    graded = sum(shadow.values())
    return {
        "week": week,
        "parent_calls_avoided": avoided,
        "parent_calls": parent_calls,
        "local_share": round(avoided / served, 3) if served else None,
        "avoided_usd_est": round(avoided_usd, 4),
        "parent_spend_usd": round(c["parent_cost_usd"], 4),
        "by_task": by_task,
        "quality": {
            "local_acceptance": None if acc_now is None else round(acc_now, 3),
            "verdicts": n_now,
            "baseline_acceptance": None if acc_base is None
            else round(acc_base, 3),
            "baseline_verdicts": n_base,
            "held": held,
            "shadow_graded": graded,
            "shadow_agree": round(shadow["agree"] / graded, 3) if graded
            else None,
        },
        "exemplars": _exemplar_uses(data, t0, t1),
    }


def headline(r: dict) -> str:
    q = r["quality"]
    acc = ("n/a" if q["local_acceptance"] is None
           else f"{q['local_acceptance']:.0%} of {q['verdicts']}")
    share = "n/a" if r["local_share"] is None else f"{r['local_share']:.0%}"
    return (f"{r['week']}: {r['parent_calls_avoided']} parent calls avoided "
            f"({share} served locally, ~${r['avoided_usd_est']:.2f} saved), "
            f"local answers accepted {acc}, quality {q['held']}")
