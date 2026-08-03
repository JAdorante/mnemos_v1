"""Learned attention ranking weights β (Track A4 / Field §12).

Priors = shipped GRAVITY weights. When QUILL_ATTENTION_LEARN=1, each closed
impression runs one SGD step on a logistic loss, L2-anchored to the prior,
with a daily drift cap. Thompson sampling draws β̃ ~ N(μ, diag(σ²)) once per
WM rebuild. Kill switch (learn=0) freezes at prior.
"""
from __future__ import annotations

import math
import random
import threading
import time
from typing import Any

# Feature keys that mirror GRAVITY["w"] (plus act).
FEATURES = (
    "pin", "pros", "rel", "fut", "unres", "cent", "sem", "rep", "temp", "unc", "act",
)

_POSITIVE = frozenset({
    "pin", "click", "dwell", "used", "edited", "reclassify", "accepted",
})
_NEGATIVE = frozenset({
    "dismiss", "hide", "miss", "rejected", "unpin",
})

_lock = threading.RLock()
_cached: dict[str, Any] | None = None
_day_key: str | None = None
_day_drift: float = 0.0
_thompson: dict[str, float] | None = None


def _learn_enabled() -> bool:
    try:
        from app.config import settings
        return bool(settings.attention.learn)
    except Exception:
        import os
        return os.environ.get("QUILL_ATTENTION_LEARN", "0") not in (
            "0", "false", "False")


def _lr() -> float:
    try:
        from app.config import settings
        return float(settings.attention.learn_lr)
    except Exception:
        return 0.02


def _max_daily_drift() -> float:
    try:
        from app.config import settings
        return float(settings.attention.learn_max_daily_drift)
    except Exception:
        return 0.05


def prior_beta() -> dict[str, float]:
    from app.services.graph import GRAVITY
    w = dict(GRAVITY["w"])
    for k in FEATURES:
        w.setdefault(k, 0.5 if k != "unc" else 0.8)
    return {k: float(w[k]) for k in FEATURES}


def prior_var() -> dict[str, float]:
    """Diagonal Laplace-ish prior variance (wide enough to move, tight enough)."""
    return {k: 0.08 for k in FEATURES}


def _ensure_day() -> None:
    global _day_key, _day_drift
    key = time.strftime("%Y-%m-%d")
    if key != _day_key:
        _day_key = key
        _day_drift = 0.0


def load(store=None, *, force: bool = False) -> dict[str, Any]:
    """Active model or fresh prior."""
    global _cached
    with _lock:
        if _cached is not None and not force:
            return dict(_cached)
    prior = prior_beta()
    model = {
        "beta": dict(prior),
        "beta_var": prior_var(),
        "prior": dict(prior),
        "n_updates": 0,
        "drift": 0.0,
        "version": "prior",
        "status": "prior",
        "ts": None,
        "id": None,
    }
    if store is not None:
        try:
            row = store.active_ranking_model()
            if row and isinstance(row.get("beta"), dict) and row["beta"]:
                beta = {k: float(row["beta"].get(k, prior[k])) for k in FEATURES}
                var = row.get("beta_var") or prior_var()
                model.update({
                    "beta": beta,
                    "beta_var": {k: float(var.get(k, 0.08)) for k in FEATURES},
                    "prior": row.get("prior") or prior,
                    "n_updates": int(row.get("n_updates") or 0),
                    "drift": float(row.get("drift") or 0),
                    "version": row.get("version") or "v1",
                    "status": "active",
                    "ts": row.get("ts"),
                    "id": row.get("id"),
                })
        except Exception as exc:
            print(f"[ranking_learn] load skipped ({exc}).")
    with _lock:
        _cached = dict(model)
    return model


def save(store, model: dict[str, Any], *, note: str | None = None) -> int:
    rid = store.save_ranking_model(
        beta=model["beta"],
        beta_var=model.get("beta_var"),
        prior=model.get("prior") or prior_beta(),
        version=model.get("version") or "v1",
        n_updates=int(model.get("n_updates") or 0),
        drift=model.get("drift"),
        note=note,
        activate=True,
    )
    with _lock:
        global _cached
        _cached = dict(model)
        _cached["id"] = rid
        _cached["status"] = "active"
    return rid


def features_from_decomp(decomp: dict | None) -> dict[str, float]:
    """Map ledger decomposition → feature vector in [0,1]-ish space."""
    d = decomp or {}
    return {
        "pin": float(d.get("pin") or 0.0),
        "pros": float(d.get("pros") or 0.0),
        "rel": float(d.get("rel") or 0.0),
        "fut": float(d.get("fut") or 0.0),
        "unres": float(d.get("unres") or 0.0),
        "cent": float(d.get("cent") or 0.0),
        "sem": float(d.get("V") if d.get("V") is not None else d.get("sem") or 0.0),
        "rep": float(d.get("rep") or 0.0),
        "temp": float(d.get("B") if d.get("B") is not None else d.get("temp") or 0.0),
        "unc": float(d.get("unc") or 0.0),
        "act": float(d.get("act") or 0.0),
    }


def score_raw(beta: dict[str, float], x: dict[str, float]) -> float:
    s = 0.0
    for k in FEATURES:
        s += float(beta.get(k, 0)) * float(x.get(k, 0))
    # unc is a penalty in GRAVITY (subtracted); keep sign convention:
    # β_unc is positive weight but feature contributes negatively in raw GRAVITY.
    # We store unc as the positive weight and apply -β*unc in the linear score
    # for continuity with shadow_score.
    return s - 2.0 * float(beta.get("unc", 0)) * float(x.get("unc", 0))


def _sigmoid(z: float) -> float:
    z = max(-20.0, min(20.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def thompson_beta(store=None) -> dict[str, float]:
    """Draw β̃ once per rebuild; reuse until next explicit refresh."""
    global _thompson
    if not _learn_enabled():
        return prior_beta()
    model = load(store)
    with _lock:
        if _thompson is not None:
            return dict(_thompson)
        drawn = {}
        for k in FEATURES:
            mu = float(model["beta"].get(k, 0))
            var = max(1e-6, float((model.get("beta_var") or {}).get(k, 0.08)))
            drawn[k] = mu + random.gauss(0.0, math.sqrt(var))
            # Bound to [0, 2×prior]
            p = float((model.get("prior") or prior_beta()).get(k, mu))
            lo, hi = 0.0, max(0.05, 2.0 * abs(p) if p else 2.0)
            drawn[k] = max(lo, min(hi, drawn[k]))
        _thompson = drawn
        return dict(drawn)


def refresh_thompson(store=None) -> dict[str, float]:
    global _thompson
    with _lock:
        _thompson = None
    return thompson_beta(store)


def current_beta(store=None) -> dict[str, float]:
    if not _learn_enabled():
        return prior_beta()
    return thompson_beta(store)


def update_from_outcome(store, *, decomp: dict | None, outcome: str,
                        engaged: bool | None = None) -> dict[str, Any] | None:
    """One SGD step. No-op when learn is off."""
    if not _learn_enabled() or store is None:
        return None
    oc = (outcome or "").strip().lower()
    if engaged is None:
        if oc in _POSITIVE:
            engaged = True
        elif oc in _NEGATIVE:
            engaged = False
        else:
            return None

    x = features_from_decomp(decomp)
    model = load(store)
    beta = dict(model["beta"])
    var = dict(model.get("beta_var") or prior_var())
    prior = dict(model.get("prior") or prior_beta())
    lr = _lr()
    y = 1.0 if engaged else 0.0

    # Continuity-compatible raw: unc handled inside score_raw
    z = 0.0
    for k in FEATURES:
        if k == "unc":
            continue
        z += beta[k] * x[k]
    z -= beta["unc"] * x["unc"]
    p = _sigmoid(z)
    err = y - p

    _ensure_day()
    global _day_drift
    delta_norm = 0.0
    updates = {}
    for k in FEATURES:
        xk = -x[k] if k == "unc" else x[k]
        # Gradient of logistic + L2 to prior
        g = err * xk - 0.05 * (beta[k] - prior[k])
        step = lr * g
        # Bound step
        step = max(-0.05, min(0.05, step))
        new = beta[k] + step
        lo, hi = 0.0, max(0.05, 2.0 * abs(prior[k]) if prior[k] else 2.0)
        new = max(lo, min(hi, new))
        updates[k] = new - beta[k]
        delta_norm += (new - beta[k]) ** 2
        beta[k] = new
        # Shrink variance slightly with evidence
        var[k] = max(0.01, float(var[k]) * 0.995)

    delta_norm = math.sqrt(delta_norm)
    with _lock:
        if _day_drift + delta_norm > _max_daily_drift():
            return {"skipped": True, "reason": "daily_drift_cap",
                    "day_drift": _day_drift}
        _day_drift += delta_norm

    model["beta"] = beta
    model["beta_var"] = var
    model["n_updates"] = int(model.get("n_updates") or 0) + 1
    model["drift"] = round(float(model.get("drift") or 0) + delta_norm, 5)
    model["version"] = "online"
    try:
        save(store, model, note=f"sgd outcome={oc}")
    except Exception as exc:
        print(f"[ranking_learn] save skipped ({exc}).")
        return None
    with _lock:
        global _thompson
        _thompson = None  # redraw next rebuild
    return {
        "ok": True,
        "outcome": oc,
        "engaged": engaged,
        "delta": {k: round(v, 5) for k, v in updates.items() if abs(v) > 1e-6},
        "n_updates": model["n_updates"],
        "drift": model["drift"],
    }


def explain(store=None) -> dict[str, Any]:
    """Console transparency: β vs prior, kill switch, drift."""
    prior = prior_beta()
    model = load(store)
    beta = model["beta"]
    diffs = {
        k: round(float(beta.get(k, 0)) - float(prior.get(k, 0)), 4)
        for k in FEATURES
    }
    return {
        "learn_enabled": _learn_enabled(),
        "kill_switch": not _learn_enabled(),
        "status": model.get("status"),
        "version": model.get("version"),
        "n_updates": model.get("n_updates"),
        "drift": model.get("drift"),
        "day_drift": _day_drift,
        "max_daily_drift": _max_daily_drift(),
        "beta": {k: round(float(beta[k]), 4) for k in FEATURES},
        "prior": {k: round(float(prior[k]), 4) for k in FEATURES},
        "delta_vs_prior": diffs,
        "ts": model.get("ts"),
        "id": model.get("id"),
    }


def revert_to_prior(store) -> dict[str, Any]:
    model = {
        "beta": prior_beta(),
        "beta_var": prior_var(),
        "prior": prior_beta(),
        "n_updates": 0,
        "drift": 0.0,
        "version": "prior",
    }
    if store is not None:
        save(store, model, note="revert_to_prior")
    with _lock:
        global _thompson, _day_drift
        _thompson = None
        _day_drift = 0.0
    return explain(store)
