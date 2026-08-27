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

D.2b adds the signal that most determines whether a GROUNDED local answer can
succeed: did retrieval find the answer? Max/mean cosine over the retrieved
chunks, the chunk count, and an entity-coverage score (how much of the
question's entity vocabulary actually appears in the grounding block). All are
computable before the local call, so they cost nothing extra.

These MUST be captured at call time and carried on the row — never
reconstructed at training time. Retrieval is nondeterministic as the memory
store grows, so a replayed value would be a subtly different feature from the
one production saw. Rows logged before D.2b simply carry has-value flags of 0;
the training set heals forward and no backfill is needed.

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

# Bumped whenever the feature vector's LAYOUT changes. A model fitted under a
# different version has a different input width, so loading it would either
# raise inside predict (silently reverting to the heuristic with no
# explanation) or, worse, line up by accident and read garbage. load_latest
# refuses the mismatch out loud instead; the next retrain replaces it.
FEATURE_VERSION = 2

# Order is the wire format of the retrieval block — appending is safe, but
# reordering or inserting requires a FEATURE_VERSION bump.
RETRIEVAL_KEYS = ("n_chunks", "max_sim", "mean_sim", "entity_coverage")

# Chunk counts are normalized against this; beyond it "lots of hits" carries
# no extra information.
_CHUNKS_FULL_SCALE = 20.0

# Entity-coverage only. Sentence-initial capitalization is grammar, not
# entity-hood, and `_ENTITYISH` happily matches the "Did"/"What"/"Where" a
# question opens with. Those words are absent from any grounding block, so
# leaving them in puts a fixed downward bias on coverage for precisely the
# well-formed questions the feature exists to score. `entity_density` keeps
# the unfiltered regex — it is a length-normalized proxy, not a claim that
# each match is an entity.
_NON_ENTITY_WORDS = frozenset("""
a an the and but or if for from with about of to in on at as
did do does is are was were has have had can could should would will shall
what when where who whom whose which why how may might
i we you they he she it my our your their this that these those
tell show list give find remember
""".split())


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
            # Captured at call time and carried through the distill row's meta
            # into source_refs (learning_store.from_distill_row) — absent on
            # pairs from before D.2b and on non-escalation surfaces.
            refs = pair.get("source_refs") or {}
            rows.append({"task": task, "text": text, "confidence": None,
                         "ts": float(pair.get("created_at") or 0),
                         "retrieval": refs.get("retrieval"),
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
                             "retrieval": g.get("retrieval"),
                             "ts": float(g.get("ts") or 0), "label": 1})
    except Exception as exc:
        print(f"[router_train] shadow labels skipped ({exc}).")
    return rows


# ----------------------------- features -------------------------------------
def retrieval_stats(question: str, *, hits: list | None,
                    block: str | None) -> dict:
    """Grounding retrieval → the D.2b feature dict, computed at call time.

    `hits` is grounding.compose()'s semantic layer (each carrying a `score`
    cosine) and `block` is the assembled grounding text. Entity coverage asks
    a different question from cosine: cosine says the retrieved chunks LOOK
    like the query, coverage says the specific names and numbers the question
    asks about are actually present in what the model will read. A question
    with no entity-ish tokens has no coverage to measure, so that stays None
    rather than being scored as a zero.
    """
    hits = hits or []
    scores = [float(h["score"]) for h in hits
              if isinstance(h, dict) and h.get("score") is not None]
    ents = {e.lower() for e in _ENTITYISH.findall(question or "")
            } - _NON_ENTITY_WORDS
    coverage = None
    if ents:
        low = (block or "").lower()
        coverage = sum(1 for e in ents if e in low) / len(ents)
    return {"n_chunks": len(hits),
            "max_sim": max(scores) if scores else None,
            "mean_sim": sum(scores) / len(scores) if scores else None,
            "entity_coverage": coverage}


def _retrieval_features(retrieval: dict | None) -> list[float]:
    """8 slots: a has-value flag + the value for each of RETRIEVAL_KEYS.

    Same None-is-information convention as `confidence` above. A call with no
    grounding at all (extract, reflect) and a grounded call that retrieved
    NOTHING are genuinely different events, and the flags keep them apart:
    the first has every flag at 0, the second has has_n_chunks=1 with a count
    of 0. Rows predating D.2b look like the first, which is correct — nobody
    measured retrieval for them.
    """
    r = retrieval or {}
    out: list[float] = []
    n = r.get("n_chunks")
    has_n = isinstance(n, (int, float)) and not isinstance(n, bool)
    out += [1.0 if has_n else 0.0,
            min(float(n), _CHUNKS_FULL_SCALE) / _CHUNKS_FULL_SCALE
            if has_n else 0.0]
    for key in RETRIEVAL_KEYS[1:]:
        v = r.get(key)
        has = isinstance(v, (int, float)) and not isinstance(v, bool)
        out += [1.0 if has else 0.0,
                max(0.0, min(1.0, float(v))) if has else 0.0]
    return out


def scalar_features(task: str, text: str, confidence: float | None,
                    ts: float | None = None,
                    retrieval: dict | None = None) -> list[float]:
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
    ] + _retrieval_features(retrieval)


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
                                 r.get("ts"), r.get("retrieval"))
                 + [float(x) for x in v])
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
            "feature_version": FEATURE_VERSION,
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
        meta = json.loads((d / f"router_v{v}.json").read_text(encoding="utf-8"))
        # Pre-D.2b models carry no stamp, so an absent field means version 1.
        fv = int(meta.get("feature_version") or 1)
        if fv != FEATURE_VERSION:
            print(f"[router_train] router_v{v} was fitted on feature version "
                  f"{fv}, this build emits {FEATURE_VERSION} — not loading. "
                  f"The next retrain replaces it; the heuristic routes until "
                  f"then.")
            return None, {}
        model = joblib.load(d / f"router_v{v}.joblib")
        return model, meta
    except Exception as exc:
        print(f"[router_train] load failed ({exc}).")
        return None, {}
