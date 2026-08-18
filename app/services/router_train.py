"""Escalation-router training (Workstream D.1/D.2/D.3).

Labels come free from the stores the other workstreams built:
  local_sufficient = 1  local output accepted un-edited (a learning pair with
                        verdict=accepted and NO parent side), or a shadow
                        grade of "agree"
  local_sufficient = 0  edited / rejected / dismissed pairs, shadow
                        minor/major disagreements, and escalation pairs where
                        the PARENT's answer was the one accepted (the local
                        attempt wasn't sufficient)

Features (D.2 — boring and cheap, <10ms/call on CPU): task one-hot, input
length in ~tokens, entity-density regex proxy, the local model's own
confidence signal (with a has-confidence flag so None is information, not
zero), hour-of-day, and the shared input embedding (the same MiniLM that
embeds memory search — reused, not a second stack).

Model (v1, deliberately un-gold-plated): scikit-learn LogisticRegression on
[scalars ⊕ embedding], probability-calibrated with CalibratedClassifierCV
(D.3). Persisted with versioned filenames under data/router/. A SetFit-style
upgrade is the documented later step if LR plateaus.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from app.config import settings

TASKS = ("chat", "extract", "reflect", "activity", "other")

_ENTITYISH = re.compile(r"\b[A-Z][a-z]+\b|\b\d[\d./:-]*\b")


def _cfg():
    return settings.router


# ----------------------------- labels ---------------------------------------
def _pair_label(pair: dict) -> tuple[str, str, int] | None:
    """(task, input_text, label) from one learning pair, or None if the pair
    carries no local-attempt signal (e.g. person merges, KG confirms)."""
    task = str((pair.get("source_refs") or {}).get("task") or "")
    if not task:
        # Non-escalation surfaces: fact review labels the extractor.
        task = ("extract" if str(pair.get("task_type", "")).startswith(
            "extraction") else "")
    if not task:
        return None
    text = str(pair.get("input_text") or "")
    if not text:
        return None
    v = str(pair.get("verdict"))
    if v == "accepted":
        # Accepted WITH a parent side = the parent rescued it → local failed.
        label = 0 if pair.get("parent_output") else 1
    elif v in ("edited", "rejected", "dismissed", "shadow_disagree"):
        label = 0
    else:
        return None
    return task, text, label


def build_dataset(store=None) -> list[dict]:
    """The training-set extractor (D.1): learning_pairs + shadow grades →
    [{task, text, confidence, ts, label}]."""
    rows: list[dict] = []
    try:
        if store is None:
            from app.storage import get_store
            store = get_store()
        for pair in store.list_learning_pairs(limit=10000):
            lab = _pair_label(pair)
            if lab is None:
                continue
            task, text, label = lab
            rows.append({"task": task, "text": text, "confidence": None,
                         "ts": float(pair.get("created_at") or 0),
                         "label": label})
    except Exception as exc:
        print(f"[router_train] pair labels skipped ({exc}).")
    # Shadow grades: agree → 1, disagreements are already pairs (skip here to
    # avoid double-counting — only agrees add signal).
    try:
        p = Path(settings.shadow.grades_path)
        if p.is_file():
            for ln in p.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                try:
                    g = json.loads(ln)
                except Exception:
                    continue
                if str(g.get("verdict")) != "agree":
                    continue
                text = str(g.get("input") or "")
                if not text:
                    continue
                rows.append({"task": str(g.get("task") or "chat"),
                             "text": text,
                             "confidence": g.get("confidence"),
                             "ts": float(g.get("ts") or 0), "label": 1})
    except Exception as exc:
        print(f"[router_train] shadow labels skipped ({exc}).")
    return rows


# ----------------------------- features -------------------------------------
def scalar_features(task: str, text: str, confidence: float | None,
                    ts: float | None = None) -> list[float]:
    t = task if task in TASKS else "other"
    one_hot = [1.0 if t == k else 0.0 for k in TASKS]
    n_tokens = len(text) / 4.0
    words = max(1, len(text.split()))
    entity_density = len(_ENTITYISH.findall(text)) / words
    has_conf = confidence is not None
    hour = time.localtime(ts if ts else time.time()).tm_hour / 24.0
    return one_hot + [
        min(n_tokens, 4000.0) / 4000.0,
        min(entity_density, 1.0),
        1.0 if has_conf else 0.0,
        float(confidence) if has_conf else 0.0,
        hour,
    ]


def featurize(rows: list[dict], *, embed=None):
    """Feature matrix [scalars ⊕ embedding]. `embed` is patchable for tests;
    default is the shared embedder (already loaded if retrieval ran)."""
    import numpy as np
    if embed is None:
        from app.services.embeddings import embedder
        embed = embedder.encode_many
    vecs = embed([r["text"] for r in rows])
    X = []
    for r, v in zip(rows, vecs):
        X.append(scalar_features(r["task"], r["text"], r.get("confidence"),
                                 r.get("ts")) + [float(x) for x in v])
    return np.asarray(X, dtype=float)


# ----------------------------- train / eval ----------------------------------
def _holdout_split(rows: list[dict], pct: int = 25) -> tuple[list, list]:
    """Deterministic split on a content hash — stable across runs."""
    import hashlib
    train, held = [], []
    for r in rows:
        h = int(hashlib.md5(r["text"].encode("utf-8")).hexdigest()[:8], 16)
        (held if h % 100 < pct else train).append(r)
    return train, held


def evaluate(model, rows: list[dict], *, embed=None) -> dict:
    """Metrics that gate promotion (D.5): miss rate (silent failures the
    router lets stay local at t_high) and escalation rate (Claude spend)."""
    import numpy as np
    if not rows:
        return {"n": 0}
    X = featurize(rows, embed=embed)
    y = np.asarray([r["label"] for r in rows])
    p_fail = model.predict_proba(X)[:, 1]
    t_high = float(_cfg().t_high)
    escalated = p_fail >= t_high
    fails = (y == 0)
    n_fail = int(fails.sum())
    miss = int((fails & ~escalated).sum())
    return {
        "n": len(rows),
        "fail_base_rate": round(n_fail / len(rows), 4),
        "miss_rate": round(miss / n_fail, 4) if n_fail else None,
        "escalation_rate": round(float(escalated.mean()), 4),
        "auc": _auc(y, p_fail),
    }


def _auc(y, p) -> float | None:
    try:
        from sklearn.metrics import roc_auc_score
        if len(set(int(v) for v in y)) < 2:
            return None
        # label 0 = fail = the positive class for p_fail.
        return round(float(roc_auc_score(1 - y, p)), 4)
    except Exception:
        return None


def train(rows: list[dict], *, embed=None):
    """Fit the calibrated v1 model. Returns (model, holdout_metrics)."""
    import numpy as np
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.linear_model import LogisticRegression

    fit_rows, held = _holdout_split(rows)
    if len(fit_rows) < 10 or len({r["label"] for r in fit_rows}) < 2:
        raise ValueError("not enough labeled rows (or one-class) to fit")
    X = featurize(fit_rows, embed=embed)
    # y=1 means "local FAILS" so p_fail is the positive-class probability.
    y = np.asarray([1 - r["label"] for r in fit_rows])
    base = LogisticRegression(max_iter=1000, class_weight="balanced")
    cv = min(3, int(min((y == 0).sum(), (y == 1).sum())))
    if cv >= 2:
        model = CalibratedClassifierCV(base, method="sigmoid", cv=cv)
        model.fit(X, y)
    else:
        model = base.fit(X, y)
    return model, evaluate(model, held or fit_rows, embed=embed)


# ----------------------------- persistence -----------------------------------
def model_dir() -> Path:
    return Path(_cfg().dir)


def latest_version() -> int:
    d = model_dir()
    best = 0
    if d.is_dir():
        for p in d.glob("router_v*.joblib"):
            try:
                best = max(best, int(p.stem.split("_v")[1]))
            except Exception:
                continue
    return best


def save(model, metrics: dict, *, n_labels: int) -> Path:
    import joblib
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    version = latest_version() + 1
    path = d / f"router_v{version}.joblib"
    joblib.dump(model, path)
    meta = {"version": version, "trained_at": time.time(),
            "n_labels": n_labels, "holdout": metrics,
            "t_low": _cfg().t_low, "t_high": _cfg().t_high}
    (d / f"router_v{version}.json").write_text(json.dumps(meta, indent=2),
                                               encoding="utf-8")
    return path


def load_latest():
    """(model, meta) for the newest version, or (None, {})."""
    import joblib
    v = latest_version()
    if not v:
        return None, {}
    d = model_dir()
    try:
        model = joblib.load(d / f"router_v{v}.joblib")
        meta = json.loads((d / f"router_v{v}.json").read_text(encoding="utf-8"))
        return model, meta
    except Exception as exc:
        print(f"[router_train] load failed ({exc}).")
        return None, {}
