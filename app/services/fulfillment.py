"""Commitment-fulfillment tracker — the Month-1 baseline metric (Phase 0).

The Cognitive OS program is judged on outcomes, not features, and the wedge
outcome is follow-through: do commitments and tasks actually get DONE? This
module computes that baseline from the facts the substrate already keeps —
so when the attention track ships (working memory, horizon strip, at-risk
audits), "did fulfillment improve?" has a before-number to beat.

Pure function over plain fact dicts (no Store, no I/O) — the same testable
shape as person_details/entity_details. The route feeds it list_facts rows.
"""
from __future__ import annotations

import time


def _due_days(due, now: float) -> float | None:
    """Days until due relative to `now` (negative = overdue). Reuses the
    graph's tolerant parser so 'due' means the same thing everywhere."""
    from app.services.graph import _due_days as parse
    return parse(due, now)


def summarize(facts: list[dict], now: float | None = None,
              *, weeks: int = 8) -> dict:
    """Fulfillment picture across open work (tasks + commitments).

    `facts` are joined fact rows (any status). Definitions:
      resolved      status == done
      abandoned     status == cancelled
      fulfillment   done / (done + cancelled) — of the work that CLOSED,
                    how much closed by being done rather than dropped
      on_time       of done items that HAD a due date, done by that date
                    (updated_at vs due — updated_at moves on completion)
      overdue_open  open items past their due date
    """
    now = now or time.time()
    week_s = 7 * 86400.0

    counts = {"open": 0, "done": 0, "cancelled": 0}
    by_kind: dict[str, dict[str, int]] = {}
    overdue_open = 0
    open_ages: list[float] = []
    done_with_due = 0
    done_on_time = 0
    created_by_week = [0] * weeks
    resolved_by_week = [0] * weeks

    for f in facts:
        kind = f.get("kind") or "?"
        if kind not in ("task", "commitment"):
            continue
        status = (f.get("status") or "open").lower()
        if status not in counts:
            continue
        # A fact the human dismissed was judged NOISE, not abandoned work —
        # it must not drag the fulfillment rate down.
        if f.get("review") == "dismissed":
            continue
        counts[status] += 1
        by_kind.setdefault(kind, {"open": 0, "done": 0, "cancelled": 0})
        by_kind[kind][status] += 1

        created = f.get("extracted_at") or 0
        touched = f.get("updated_at") or created
        if status == "open":
            if created:
                open_ages.append((now - created) / 86400.0)
            dd = _due_days(f.get("due"), now)
            if dd is not None and dd < 0:
                overdue_open += 1
        elif status == "done":
            dd = _due_days(f.get("due"), touched or now)
            if dd is not None:
                done_with_due += 1
                if dd >= 0:
                    done_on_time += 1
            wk = int((now - (touched or now)) // week_s)
            if 0 <= wk < weeks:
                resolved_by_week[wk] += 1
        if created:
            wk = int((now - created) // week_s)
            if 0 <= wk < weeks:
                created_by_week[wk] += 1

    closed = counts["done"] + counts["cancelled"]
    open_ages.sort()
    median_age = open_ages[len(open_ages) // 2] if open_ages else None
    return {
        "counts": counts,
        "by_kind": by_kind,
        "fulfillment_rate": round(counts["done"] / closed, 4) if closed else None,
        "on_time_rate": (round(done_on_time / done_with_due, 4)
                         if done_with_due else None),
        "overdue_open": overdue_open,
        "median_open_age_days": (round(median_age, 1)
                                 if median_age is not None else None),
        "oldest_open_age_days": (round(open_ages[-1], 1) if open_ages else None),
        # index 0 = this week, 1 = last week, ... newest first.
        "weekly": {"created": created_by_week, "resolved": resolved_by_week},
    }


def _baseline_path():
    from pathlib import Path
    try:
        from app.config import settings
        return Path(settings.storage.data_dir) / "fulfillment_baseline.json"
    except Exception:
        return Path("data") / "fulfillment_baseline.json"


def stamp_baseline(summary: dict, *, note: str = "manual") -> dict:
    """Persist a Month-1-style before-number the wedge must beat. File-based
    (not SQLite) so Track C storage work stays untouched."""
    import json
    import time as _time
    path = _baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": _time.time(),
        "note": note,
        "fulfillment_rate": summary.get("fulfillment_rate"),
        "on_time_rate": summary.get("on_time_rate"),
        "overdue_open": summary.get("overdue_open"),
        "counts": summary.get("counts"),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_baseline() -> dict | None:
    import json
    path = _baseline_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def with_baseline(summary: dict) -> dict:
    """Attach baseline + delta so the console can show wedge progress."""
    out = dict(summary)
    base = load_baseline()
    out["baseline"] = base
    if base and summary.get("fulfillment_rate") is not None \
            and base.get("fulfillment_rate") is not None:
        out["fulfillment_delta"] = round(
            float(summary["fulfillment_rate"]) - float(base["fulfillment_rate"]), 4)
    else:
        out["fulfillment_delta"] = None
    return out
