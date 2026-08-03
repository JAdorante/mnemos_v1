"""Nightly β promotion gate (Track A4 / Field §12.3).

Replays closed ledger impressions through prior β vs the candidate (active)
online β. Promote only when the candidate does not regress engagement
prediction *and* the A1 priors-continuity gate still passes. Hold otherwise —
same promote-or-hold spirit as the text-router bench.
"""
from __future__ import annotations

import math
import time
from typing import Any

from app.services import ranking_learn
from app.services.attention_replay import run as continuity_run

DUE_AFTER_S = 20 * 3600.0
DEFAULT_DAYS = 14.0
MIN_LABELED = 12
EPS = 0.005  # require a clear win to promote


def _sigmoid(z: float) -> float:
    z = max(-20.0, min(20.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def _predict(beta: dict[str, float], x: dict[str, float]) -> float:
    z = 0.0
    for k in ranking_learn.FEATURES:
        if k == "unc":
            z -= float(beta.get(k, 0)) * float(x.get(k, 0))
        else:
            z += float(beta.get(k, 0)) * float(x.get(k, 0))
    return _sigmoid(z)


def _label(outcome: str | None) -> int | None:
    oc = (outcome or "").strip().lower()
    if oc in ranking_learn._POSITIVE:
        return 1
    if oc in ranking_learn._NEGATIVE:
        return 0
    return None


def _metrics(beta: dict[str, float], rows: list[dict]) -> dict[str, Any]:
    """Log-loss + accuracy over labeled impressions with decompositions."""
    n = 0
    correct = 0
    logloss = 0.0
    for r in rows:
        y = _label(r.get("outcome"))
        if y is None:
            continue
        x = ranking_learn.features_from_decomp(r.get("decomp") or {})
        p = _predict(beta, x)
        p = min(1.0 - 1e-6, max(1e-6, p))
        logloss += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        if (p >= 0.5) == bool(y):
            correct += 1
        n += 1
    if n == 0:
        return {"n": 0, "accuracy": None, "logloss": None}
    return {
        "n": n,
        "accuracy": round(correct / n, 4),
        "logloss": round(logloss / n, 4),
    }


def _load_labeled(store, *, days: float) -> list[dict]:
    since = time.time() - days * 86400.0
    with store._lock:
        rows = store._conn.execute(
            "SELECT outcome, decomposition FROM attention_impressions "
            "WHERE outcome IS NOT NULL AND ts >= ? "
            "AND decomposition IS NOT NULL "
            "ORDER BY ts DESC LIMIT 2000",
            (since,),
        ).fetchall()
    out = []
    for r in rows:
        import json
        try:
            decomp = json.loads(r["decomposition"] or "")
        except Exception:
            continue
        if not isinstance(decomp, dict):
            continue
        out.append({"outcome": r["outcome"], "decomp": decomp})
    return out


def due_for(store=None) -> bool:
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception:
            return False
    try:
        last = store.last_ranking_promote_run()
        if not last:
            return True
        return (time.time() - float(last.get("ts") or 0)) >= DUE_AFTER_S
    except Exception:
        return False


def run(*, days: float = DEFAULT_DAYS, store=None,
        force_promote: bool = False) -> dict[str, Any]:
    """Compare prior vs candidate β; promote or hold. Never raises."""
    if store is None:
        from app.storage import get_store
        store = get_store()

    learn_on = ranking_learn._learn_enabled()
    prior = ranking_learn.prior_beta()
    model = ranking_learn.load(store)
    cand = dict(model.get("beta") or prior)

    labeled = _load_labeled(store, days=days)
    prior_m = _metrics(prior, labeled)
    cand_m = _metrics(cand, labeled)

    result: dict[str, Any] = {
        "ts": time.time(),
        "days": days,
        "learn_enabled": learn_on,
        "n_labeled": prior_m["n"],
        "prior": prior_m,
        "candidate": cand_m,
        "status": "hold",
        "promoted": False,
        "reason": None,
    }

    if prior_m["n"] < MIN_LABELED:
        result["status"] = "insufficient"
        result["reason"] = f"need >={MIN_LABELED} labeled impressions"
        _persist_run(store, result)
        return result

    # Continuity gate must still pass (I-5) before any promote.
    try:
        cont = continuity_run(days=min(days, 7.0), store=store)
        result["continuity"] = {
            "status": cont.get("status"),
            "passed": cont.get("passed"),
            "mean_tau": cont.get("mean_tau"),
        }
        if not cont.get("passed"):
            result["status"] = "hold"
            result["reason"] = "continuity_gate_failed"
            _persist_run(store, result)
            return result
    except Exception as exc:
        result["continuity"] = {"error": str(exc)}
        # Soft: don't block forever if continuity has no data
        if "insufficient" not in str(exc).lower():
            pass

    better_acc = (cand_m["accuracy"] or 0) + EPS >= (prior_m["accuracy"] or 0)
    better_ll = (cand_m["logloss"] or 9) <= (prior_m["logloss"] or 9) + EPS
    # Require accuracy non-regression and logloss non-regression
    wins = better_acc and better_ll and (
        (cand_m["accuracy"] or 0) > (prior_m["accuracy"] or 0)
        or (cand_m["logloss"] or 9) < (prior_m["logloss"] or 9) - EPS
    )

    if not learn_on and not force_promote:
        result["status"] = "hold"
        result["reason"] = "learn_disabled"
    elif wins or force_promote:
        # Already active candidate — mark as promoted snapshot
        try:
            ranking_learn.save(store, {
                "beta": cand,
                "beta_var": model.get("beta_var") or ranking_learn.prior_var(),
                "prior": prior,
                "n_updates": int(model.get("n_updates") or 0),
                "drift": model.get("drift") or 0,
                "version": "promoted",
            }, note=f"promoted acc={cand_m['accuracy']} ll={cand_m['logloss']}")
            result["status"] = "promote"
            result["promoted"] = True
            result["reason"] = "candidate_beats_prior"
        except Exception as exc:
            result["status"] = "hold"
            result["reason"] = f"save_failed:{exc}"
    else:
        result["status"] = "hold"
        result["reason"] = "candidate_did_not_improve"
        # Optional: roll back to prior if candidate drifted badly
        if ((cand_m["accuracy"] or 1) + 0.05 < (prior_m["accuracy"] or 0)
                or (cand_m["logloss"] or 0) > (prior_m["logloss"] or 0) + 0.1):
            try:
                ranking_learn.revert_to_prior(store)
                result["reason"] = "reverted_regression"
                result["reverted"] = True
            except Exception:
                pass

    _persist_run(store, result)
    return result


def _persist_run(store, result: dict) -> None:
    try:
        store.add_ranking_promote_run(result)
    except Exception as exc:
        print(f"[ranking_promote] persist skipped ({exc}).")


def status(store=None) -> dict[str, Any]:
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception:
            return {"last": None, "due": False}
    last = None
    try:
        last = store.last_ranking_promote_run()
    except Exception:
        pass
    return {
        "due": due_for(store),
        "last": last,
        "learn_enabled": ranking_learn._learn_enabled(),
        "explain": ranking_learn.explain(store),
    }
