"""Walk-forward bench + promote gate for predictors (Track F).

Replay discipline: decision points inside the trailing holdout window are the
exam; at each point the scorer sees ONLY history strictly before that point.
Metrics are hit@1, hit@3, and MRR. Results persist to predictor_bench_runs.

Promotion is the ranking_promote pattern applied to predictors: a candidate
(non-active registry row carrying bench metrics on the SAME holdout) is
activated only if it beats the active model's latest bench on hit@3 (MRR as
tie-break); otherwise hold. rollback() re-activates the previous row. With
only heuristics in the registry, promote() always holds — the harness ships
before the learners, so a future model trains into an exam that already
exists.
"""
from __future__ import annotations

import time
from typing import Any

from app.services import predictors as P


def _cfg():
    from app.config import settings
    return settings.predictors


def _metrics(points: list[tuple[list, Any]], scorer) -> dict[str, Any]:
    """points: [(ranked_keys, truth)] -> hit@1/@3, MRR. scorer already ran."""
    hit1 = hit3 = 0
    rr_sum = 0.0
    for ranked, truth in points:
        keys = [k for k, _s in ranked]
        if not keys:
            continue
        if keys[0] == truth:
            hit1 += 1
        if truth in keys[:3]:
            hit3 += 1
        if truth in keys:
            rr_sum += 1.0 / (keys.index(truth) + 1)
    n = len(points)
    return {
        "n_points": n,
        "hit1": round(hit1 / n, 4) if n else None,
        "hit3": round(hit3 / n, 4) if n else None,
        "mrr": round(rr_sum / n, 4) if n else None,
    }


def _bench_next_app(store, *, now: float, holdout_s: float) -> list:
    blocks = P.app_history(store)
    blocks = [b for b in blocks
              if (b.get("app") or "").strip()
              and (b.get("app") or "").lower() != "desktop"]
    points = []
    for i in range(1, len(blocks)):
        t = float(blocks[i].get("start") or 0)
        truth = (blocks[i].get("app") or "").strip()
        prev = (blocks[i - 1].get("app") or "").strip()
        if t < now - holdout_s or t > now:
            continue
        if not truth or truth.lower() == prev.lower():
            continue                       # only app SWITCHES are calls
        history = blocks[:i]               # strictly before — no peeking
        ranked = P.score_next_app(history, now=t, prev_app=prev, k=5)
        points.append((ranked, truth))
    return points


def _bench_next_contact(store, *, now: float, holdout_s: float) -> list:
    inter = P.contact_history(store)
    points = []
    prev_pid = None
    for i, (t, pid) in enumerate(inter):
        if pid == prev_pid:
            prev_pid = pid
            continue                       # consecutive same-person = one touch
        if now - holdout_s <= t <= now:
            history = [x for x in inter[:i] if x[0] < t]
            ranked = P.score_next_contact(history, now=t, k=5)
            points.append((ranked, int(pid)))
        prev_pid = pid
    return points


def _bench_next_document(store, *, now: float, holdout_s: float) -> list:
    opens = P.document_history(store)
    points = []
    prev_key = None
    for i, (t, key) in enumerate(opens):
        if key == prev_key:
            prev_key = key
            continue
        if now - holdout_s <= t <= now:
            history = [x for x in opens[:i] if x[0] < t]
            ranked = P.score_next_document(history, now=t, k=5)
            points.append((ranked, key))
        prev_key = key
    return points


_BENCHES = {
    "next_app": _bench_next_app,
    "next_contact": _bench_next_contact,
    "next_document": _bench_next_document,
}


def run(task: str | None = None, store=None, *,
        now: float | None = None) -> dict[str, Any]:
    """Bench one task (or all). Persists a run row per task. Never raises."""
    cfg = _cfg()
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            return {"status": "error", "reason": f"no_store:{exc}"}
    now = float(now if now is not None else time.time())
    tasks = [task] if task else list(P.TASKS)
    P.ensure_registry(store)
    holdout_s = cfg.holdout_days * 86400.0

    results: dict[str, Any] = {"ts": now, "tasks": {}}
    for t in tasks:
        active = None
        try:
            active = store.active_predictor_model(t)
        except Exception:
            pass
        model = (active or {}).get("version") or P.HEURISTIC_VERSION
        try:
            points = _BENCHES[t](store, now=now, holdout_s=holdout_s)
        except Exception as exc:
            print(f"[predictor_bench] {t} failed ({exc}).")
            results["tasks"][t] = {"status": "error", "reason": str(exc)}
            continue
        m = _metrics(points, None)
        status = "ok" if m["n_points"] >= cfg.min_points else "insufficient"
        row = {"ts": now, "task": t, "model": model, "status": status, **m,
               "holdout_days": cfg.holdout_days}
        try:
            store.add_predictor_bench_run(row)
        except Exception as exc:
            print(f"[predictor_bench] persist skipped ({exc}).")
        results["tasks"][t] = row
    return results


def promote(task: str, store) -> dict[str, Any]:
    """Activate the newest non-active candidate iff it beats the active model
    on the latest holdout (hit@3, MRR tie-break). Hold otherwise."""
    P.ensure_registry(store)
    active = store.active_predictor_model(task)
    history = store.predictor_model_history(task, limit=10)
    candidates = [m for m in history
                  if not m.get("active") and m.get("metrics")]
    if not candidates:
        return {"status": "hold", "reason": "no_candidate", "task": task,
                "active": (active or {}).get("version")}
    import json as _json
    cand = candidates[0]
    try:
        cand_m = (_json.loads(cand["metrics"])
                  if isinstance(cand["metrics"], str) else cand["metrics"]) or {}
    except Exception:
        cand_m = {}
    last = store.last_predictor_bench_run(task) or {}
    if last.get("status") != "ok":
        return {"status": "hold", "reason": "insufficient_bench", "task": task}
    a_hit3, a_mrr = float(last.get("hit3") or 0), float(last.get("mrr") or 0)
    c_hit3, c_mrr = float(cand_m.get("hit3") or 0), float(cand_m.get("mrr") or 0)
    beats = c_hit3 > a_hit3 or (c_hit3 == a_hit3 and c_mrr > a_mrr)
    if not beats:
        return {"status": "hold", "reason": "does_not_beat_active",
                "task": task, "active": {"hit3": a_hit3, "mrr": a_mrr},
                "candidate": {"hit3": c_hit3, "mrr": c_mrr}}
    store.activate_predictor_model(int(cand["id"]))
    return {"status": "promoted", "task": task,
            "version": cand.get("version"),
            "candidate": {"hit3": c_hit3, "mrr": c_mrr},
            "previous": (active or {}).get("version")}


def rollback(task: str, store) -> dict[str, Any]:
    """Re-activate the most recent previously-active model (automatic-rollback
    companion; also the manual undo for a promotion that went sour)."""
    history = store.predictor_model_history(task, limit=10)
    active_id = None
    for m in history:
        if m.get("active"):
            active_id = int(m["id"])
            break
    prior = [m for m in history
             if m.get("activated_at") and int(m["id"]) != active_id]
    if not prior:
        return {"status": "hold", "reason": "nothing_to_roll_back_to",
                "task": task}
    prior.sort(key=lambda m: -float(m.get("activated_at") or 0))
    target = prior[0]
    store.activate_predictor_model(int(target["id"]))
    return {"status": "rolled_back", "task": task,
            "version": target.get("version")}


def due_for(store=None) -> bool:
    if not _cfg().enabled:
        return False
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception:
            return False
    try:
        last = store.last_predictor_bench_run()
    except Exception:
        return False
    if not last:
        return True
    return (time.time() - float(last["ts"])) > _cfg().bench_due_s


def status(store=None) -> dict[str, Any]:
    """Console payload: per task — active model, latest bench, preview."""
    cfg = _cfg()
    out: dict[str, Any] = {"enabled": cfg.enabled,
                           "holdout_days": cfg.holdout_days,
                           "min_points": cfg.min_points, "tasks": {}}
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            out["error"] = str(exc)
            return out
    P.ensure_registry(store)
    for t in P.TASKS:
        entry: dict[str, Any] = {}
        try:
            entry["active"] = store.active_predictor_model(t)
        except Exception:
            entry["active"] = None
        try:
            entry["last_bench"] = store.last_predictor_bench_run(t)
        except Exception:
            entry["last_bench"] = None
        try:
            entry["preview"] = P.predict(t, store, k=3)
        except Exception as exc:
            entry["preview"] = []
            entry["preview_error"] = str(exc)
        out["tasks"][t] = entry
    out["due"] = due_for(store)
    return out
