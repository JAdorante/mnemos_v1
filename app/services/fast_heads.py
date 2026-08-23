"""Classifier heads — a new bottom rung under the local LLM (Phase 2).

The ladder today is local-LLM → Claude. Most of what the ambient pipeline asks
the local model is not a hard question: "is this utterance task-bearing at
all?", "is this frame worth describing?". A trained binary classifier answers
those from a MiniLM embedding plus a few scalars in well under a millisecond,
and the LLM only runs when the answer is "maybe".

This is not a new pattern. `utterance_router.py` already short-circuits the
model with hand-written rules; this is the same idea learned rather than
authored, trained on labels the learning loop already produces, and rolled out
under the same three-mode contract as `escalation_router.py`:

    off     (default) inert
    shadow  predicts and logs beside what the LLM actually did; the LLM always
            still runs, so influence on behavior is provably zero
    active  p(needs-LLM) < t_low skips the model; everything else runs it

**Precision-first, deliberately asymmetric.** A head that wrongly runs the LLM
costs a few hundred milliseconds of local compute. A head that wrongly skips it
silently drops a real commitment out of someone's memory, and nothing in the
system will ever notice. So the bands are not symmetric: only confident
"nothing here" skips, the uncertain middle runs the model, and the activation
gate is measured on exactly the population that would have been skipped —
disagreement anywhere else is not what can hurt a user.

**Cost.** Heads only ever *remove* model calls, and they run against the local
tier, so they cannot raise cloud spend. The escalation policy above them is
untouched: a head decides whether the ladder is entered, never which rung.

**What a consultation actually costs.** Measured on the reference machine:

    logistic regression ......  0.43 ms
    MiniLM encode (1 text) ... 25.86 ms
    consultation total ....... ~27 ms

The classifier is as cheap as advertised; the embedding it depends on is not,
and it is paid on EVERY event whether or not the head skips. So a head only
pays for itself above a break-even skip rate:

    break_even_skip_rate = consultation_cost / llm_cost

    extract  (10,300 ms mean) -> 0.3%   worth it at almost any skip rate
    vision   ( 4,710 ms mean) -> 0.6%   worth it
    chat     (   860 ms mean) -> 3.1%   worth it
    a 100 ms call             -> 27%    almost certainly not worth it

Do not add a head over a cheap call without doing that arithmetic first; the
embedding makes the floor much higher than "one millisecond" suggests. Where a
call site has already embedded its text for another reason, pass the vector in
and the floor collapses to the 0.43 ms figure.

Everything is best-effort. A head that raises, has no model, or has not been
trained yields "run the LLM" — the behavior with this module absent.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.config import settings

# Bands
SKIP = "skip"           # confident: no model needed
RUN = "run"             # the model runs (uncertain, or confidently needed)
NO_MODEL = "no_model"   # nothing trained yet


@dataclass(frozen=True)
class HeadSpec:
    """One head: which decision it short-circuits and where its labels live.

    `label_task_types` are the `learning_pairs.task_type` values whose verdicts
    teach this head. Reusing the existing loop is the point — a head that
    needed its own labeling workflow would never be trained.
    """
    name: str
    decision: str
    label_task_types: tuple[str, ...]
    rationale: str
    # Extra scalar features this head reads, by name. DECLARED rather than
    # inferred from whatever the call site happened to pass: the feature
    # vector's length and order must be identical at fit time and at predict
    # time, and a caller-driven dict silently breaks that the first time a
    # site omits a key. Missing values become 0.0 alongside a has-flag, so
    # "absent" stays distinguishable from "zero".
    extra_features: tuple[str, ...] = ()


# Ranked by measured call volume x mean latency over data/model_calls.jsonl
# (245 calls): vision 282.8 s total across 60 calls, extract 93.0 s across 9 at
# a 10.3 s mean. Those two are the whole opportunity; the rest are listed
# because the brief named them and they cost nothing to declare.
HEADS: dict[str, HeadSpec] = {
    # NOTE on this head's inputs. Every other head embeds the thing it is
    # judging; this one cannot — the shared embedder is MiniLM over text and
    # the subject is pixels, and standing up an image encoder to save a local
    # VLM call would cost more than the call. What IS available before the VLM
    # runs is the window title (text) and the frame-difference scalar the
    # capture loop already computes. OCR text would be the strongest signal
    # and is unavailable here by definition: producing it is the work being
    # gated. So this head reads title + motion, and its ceiling is lower than
    # the text heads' — which is exactly what the shadow week will show.
    "frame_keep": HeadSpec(
        name="frame_keep",
        decision="Is this frame worth sending to the VLM? (window title + motion)",
        label_task_types=("vision", "escalation.vision"),
        rationale="57% of all local model time. Most frames are an idle "
                  "desktop or an unchanged editor.",
        extra_features=("motion",),
    ),
    "extract_triage": HeadSpec(
        name="extract_triage",
        decision="Does this turn contain anything extractable at all?",
        label_task_types=("extraction", "escalation.text"),
        rationale="Slowest call in the system at a 10.3 s mean; most ambient "
                  "speech carries no task, commitment or claim.",
    ),
    "query_route": HeadSpec(
        name="query_route",
        decision="Does this query need the model, or is it a lookup?",
        label_task_types=("query_route", "escalation.text"),
        rationale="High call volume on the interactive path, where latency is "
                  "felt directly.",
    ),
    "mention_detect": HeadSpec(
        name="mention_detect",
        decision="Does this text mention a person at all?",
        label_task_types=("entity_resolution", "contact_attribution"),
        rationale="Runs ahead of people_pipeline on every turn.",
    ),
}


def _cfg():
    return settings.heads


def mode() -> str:
    """Global mode, env-first at call time (the `vector_gc` precedent)."""
    m = (os.environ.get("QUILL_HEADS") or _cfg().mode or "off").strip().lower()
    return m if m in ("off", "shadow", "active") else "off"


def head_mode(name: str) -> str:
    """Per-head override, so one head can go active while others stay in
    shadow. A head may never exceed the global mode: `QUILL_HEADS=shadow`
    caps everything at shadow regardless of per-head settings, which is what
    makes the global flag a usable kill switch.
    """
    glob = mode()
    if glob == "off":
        return "off"
    raw = os.environ.get(f"QUILL_HEAD_{name.upper()}_MODE")
    per = (raw or glob).strip().lower()
    if per not in ("off", "shadow", "active"):
        per = glob
    if glob == "shadow" and per == "active":
        return "shadow"
    return per


def thresholds(name: str) -> tuple[float, float]:
    """(t_low, t_high) for one head, per-head overridable."""
    cfg = _cfg()

    def _f(key: str, default: float) -> float:
        try:
            raw = os.environ.get(f"QUILL_HEAD_{name.upper()}_{key}")
            return float(raw) if raw is not None else float(default)
        except (TypeError, ValueError):
            return float(default)

    return _f("T_LOW", cfg.t_low), _f("T_HIGH", cfg.t_high)


# --------------------------------------------------------------------------
# features — cheap scalars + the already-resident MiniLM embedding
# --------------------------------------------------------------------------
_ENTITYISH = re.compile(r"\b[A-Z][a-z]+\b|\b\d[\d./:-]*\b")
_ACTIONISH = re.compile(
    r"\b(will|i'?ll|let'?s|need to|should|have to|remind|send|call|email|"
    r"schedule|book|follow up|by (mon|tue|wed|thu|fri|sat|sun|tomorrow|today))\b",
    re.I)


def scalar_features(text: str, *, speaker_known: bool | None = None,
                    wake_word: bool | None = None,
                    ts: float | None = None,
                    extra: dict[str, float] | None = None,
                    extra_keys: tuple[str, ...] = ()) -> list[float]:
    """The engineered half. Deliberately boring: length, entity density, an
    action-phrase proxy, whether the speaker is known, whether a wake word
    fired, and hour-of-day. Each is one regex or one lookup — the whole point
    is that computing them cannot cost more than the call they save.
    """
    t = text or ""
    n_tokens = len(t.split())
    entity_density = (len(_ENTITYISH.findall(t)) / n_tokens) if n_tokens else 0.0
    hour = time.localtime(ts if ts else time.time()).tm_hour / 24.0
    feats = [
        min(n_tokens, 400.0) / 400.0,
        min(entity_density, 1.0),
        1.0 if _ACTIONISH.search(t) else 0.0,
        # has-flag alongside the value, so "unknown" is information rather
        # than a silent zero (the router's has_confidence precedent).
        1.0 if speaker_known is not None else 0.0,
        1.0 if speaker_known else 0.0,
        1.0 if wake_word is not None else 0.0,
        1.0 if wake_word else 0.0,
        hour,
    ]
    # Declared keys only, in declared order — see HeadSpec.extra_features.
    src = extra or {}
    for key in extra_keys:
        v = src.get(key)
        feats.append(1.0 if isinstance(v, (int, float)) else 0.0)
        feats.append(float(v) if isinstance(v, (int, float)) else 0.0)
    return feats


def featurize(rows: list[dict], *, name: str | None = None,
              embed: Callable | None = None):
    """[scalars ⊕ embedding] for a batch of {text, ...} rows.

    `name` selects the head whose declared extra features are emitted, so the
    vector has the same shape at fit time and at predict time even when a
    training row has no value for one of them.
    """
    import numpy as np
    if embed is None:
        from app.services.embeddings import embedder
        embed = embedder.encode_many
    spec = HEADS.get(name or "")
    extra_keys = spec.extra_features if spec else ()
    vecs = embed([r.get("text") or "" for r in rows])
    X = []
    for r, v in zip(rows, vecs):
        X.append(scalar_features(
            r.get("text") or "",
            speaker_known=r.get("speaker_known"),
            wake_word=r.get("wake_word"),
            ts=r.get("ts"),
            extra=r.get("extra"),
            extra_keys=extra_keys,
        ) + [float(x) for x in v])
    return np.asarray(X, dtype=float)


# --------------------------------------------------------------------------
# labels — from the loop that already exists
# --------------------------------------------------------------------------
def _pair_label(spec: HeadSpec, pair: dict) -> int | None:
    """1 = this input needed the model, 0 = it did not.

    A confirmed pair whose local output was accepted un-edited and produced
    *something* is evidence the model was needed. A pair whose final target is
    empty — the extractor found nothing, the frame said nothing, the human
    dismissed it — is evidence it was not. Those are exactly the two
    populations a head has to tell apart, and both fall out of verdicts the
    user is already giving.
    """
    verdict = str(pair.get("verdict") or "")
    target = (pair.get("final_target") or pair.get("local_output") or "").strip()
    if verdict in ("rejected", "dismissed"):
        return 0
    if verdict in ("accepted", "edited"):
        return 1 if target and target not in ("{}", "[]", "null", "none") else 0
    return None


def labels_for(spec: HeadSpec, *, store=None, limit: int = 5000) -> list[dict]:
    """Training rows for one head, drawn from confirmed learning pairs."""
    if store is None:
        from app.storage import get_store
        store = get_store()
    rows: list[dict] = []
    seen: set[str] = set()
    for task_type in spec.label_task_types:
        try:
            pairs = store.list_learning_pairs(task_type=task_type,
                                              human_confirmed=True,
                                              limit=limit)
        except Exception as exc:
            print(f"[fast_heads] label read skipped ({exc}).")
            continue
        for pair in pairs:
            pid = str(pair.get("id") or "")
            if pid in seen:
                continue
            label = _pair_label(spec, pair)
            text = str(pair.get("input_text") or "")
            if label is None or not text:
                continue
            seen.add(pid)
            rows.append({"text": text, "label": label,
                         "ts": pair.get("created_at")})
    return rows


# --------------------------------------------------------------------------
# persistence — mirrors router_train's versioned artifacts
# --------------------------------------------------------------------------
def model_dir() -> Path:
    return Path(_cfg().dir)


def latest_version(name: str) -> int:
    d = model_dir()
    best = 0
    if d.is_dir():
        for p in d.glob(f"{name}_v*.joblib"):
            try:
                best = max(best, int(p.stem.split("_v")[1]))
            except Exception:
                continue
    return best


def save(name: str, model, metrics: dict, *, n_labels: int) -> Path:
    import joblib
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    version = latest_version(name) + 1
    path = d / f"{name}_v{version}.joblib"
    joblib.dump(model, path)
    t_low, t_high = thresholds(name)
    (d / f"{name}_v{version}.json").write_text(json.dumps({
        "head": name, "version": version, "trained_at": time.time(),
        "n_labels": n_labels, "holdout": metrics,
        "t_low": t_low, "t_high": t_high,
    }, indent=2), encoding="utf-8")
    return path


def load_latest(name: str):
    import joblib
    v = latest_version(name)
    if not v:
        return None, {}
    d = model_dir()
    try:
        model = joblib.load(d / f"{name}_v{v}.joblib")
        meta = json.loads((d / f"{name}_v{v}.json").read_text(encoding="utf-8"))
        return model, meta
    except Exception as exc:
        print(f"[fast_heads] load failed for {name} ({exc}).")
        return None, {}


# --------------------------------------------------------------------------
# train
# --------------------------------------------------------------------------
def _holdout_split(rows: list[dict], pct: int = 25) -> tuple[list, list]:
    """Deterministic content-hash split — stable across runs (router precedent)."""
    import hashlib
    train, held = [], []
    for r in rows:
        h = int(hashlib.md5(r["text"].encode("utf-8")).hexdigest()[:8], 16)
        (held if h % 100 < pct else train).append(r)
    return train, held


def evaluate(model, rows: list[dict], *, name: str,
             embed: Callable | None = None) -> dict:
    """Metrics that gate activation.

    `miss_rate` is the one that matters: of the inputs that genuinely needed
    the model, what share would this head have skipped? That is the silent
    failure, and it is measured on the skip population alone.
    """
    import numpy as np
    if not rows:
        return {"n": 0}
    t_low, _ = thresholds(name)
    X = featurize(rows, name=name, embed=embed)
    y = np.asarray([r["label"] for r in rows])       # 1 = needed the model
    p_need = model.predict_proba(X)[:, 1]
    skipped = p_need < t_low
    needed = (y == 1)
    n_needed = int(needed.sum())
    n_skipped = int(skipped.sum())
    return {
        "n": len(rows),
        "need_base_rate": round(float(needed.mean()), 4),
        "skip_rate": round(float(skipped.mean()), 4),
        # The number the activation gate reads.
        "miss_rate": (round(float((needed & skipped).sum()) / n_needed, 4)
                      if n_needed else None),
        "skip_precision": (round(float(((~needed) & skipped).sum()) / n_skipped, 4)
                           if n_skipped else None),
        "auc": _auc(y, p_need),
    }


def _auc(y, p) -> float | None:
    try:
        from sklearn.metrics import roc_auc_score
        if len(set(int(v) for v in y)) < 2:
            return None
        return round(float(roc_auc_score(y, p)), 4)
    except Exception:
        return None


def train(rows: list[dict], *, name: str, embed: Callable | None = None):
    """Fit one head. Returns (model, holdout_metrics)."""
    import numpy as np
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.linear_model import LogisticRegression

    fit_rows, held = _holdout_split(rows)
    if len(fit_rows) < 10 or len({r["label"] for r in fit_rows}) < 2:
        raise ValueError("not enough labeled rows (or one-class) to fit")
    X = featurize(fit_rows, name=name, embed=embed)
    y = np.asarray([r["label"] for r in fit_rows])   # 1 = needs the model
    base = LogisticRegression(max_iter=1000, class_weight="balanced")
    cv = min(3, int(min((y == 0).sum(), (y == 1).sum())))
    model = (CalibratedClassifierCV(base, method="sigmoid", cv=cv)
             if cv >= 2 else base)
    model.fit(X, y)
    return model, evaluate(model, held or fit_rows, name=name, embed=embed)


def train_head(name: str, *, store=None, embed: Callable | None = None) -> dict:
    """Train + persist one head if it has enough labels. Never raises."""
    spec = HEADS.get(name)
    if spec is None:
        return {"ok": False, "reason": f"unknown head {name!r}"}
    rows = labels_for(spec, store=store)
    need = int(_cfg().min_labels)
    if len(rows) < need:
        return {"ok": False, "head": name, "reason": "insufficient_labels",
                "labels": len(rows), "need": need}
    try:
        model, metrics = train(rows, name=name, embed=embed)
        path = save(name, model, metrics, n_labels=len(rows))
    except Exception as exc:
        return {"ok": False, "head": name, "reason": str(exc),
                "labels": len(rows)}
    return {"ok": True, "head": name, "labels": len(rows),
            "path": str(path), "holdout": metrics}


def train_all(*, store=None) -> dict:
    """Idle-scheduler entry point — the router's retrain cadence, per head."""
    return {name: train_head(name, store=store) for name in HEADS}


# --------------------------------------------------------------------------
# runtime
# --------------------------------------------------------------------------
class FastHeads:
    """Lazy per-head model cache. Never raises into a serving path."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models: dict[str, Any] = {}
        self._versions: dict[str, int] = {}

    def _ensure(self, name: str):
        v = latest_version(name)
        with self._lock:
            if self._versions.get(name) != v:
                model, _ = load_latest(name)
                self._models[name] = model
                self._versions[name] = v
            return self._models.get(name)

    def predict(self, name: str, text: str, **features) -> float | None:
        """p(this input needs the model), or None when nothing is trained."""
        try:
            model = self._ensure(name)
            if model is None or not (text or "").strip():
                return None
            X = featurize([{"text": text, **features}], name=name)
            return float(model.predict_proba(X)[:, 1][0])
        except Exception as exc:
            print(f"[fast_heads] predict skipped for {name} ({exc}).")
            return None

    def band(self, name: str, p: float | None) -> str:
        if p is None:
            return NO_MODEL
        t_low, _ = thresholds(name)
        return SKIP if p < t_low else RUN

    def consult(self, name: str, text: str, **features) -> dict[str, Any]:
        """One consultation. Returns
        {head, mode, p, band, skip, would_skip}.

        `skip` is the caller's instruction and is True only in active mode.
        `would_skip` is what the head believes regardless of mode — that is
        what shadow logging measures, and the two being separate fields is
        what makes shadow mode provably influence-free.
        """
        m = head_mode(name)
        out = {"head": name, "mode": m, "p": None, "band": NO_MODEL,
               "skip": False, "would_skip": False}
        if m == "off" or name not in HEADS:
            return out
        p = self.predict(name, text, **features)
        band = self.band(name, p)
        out["p"] = p
        out["band"] = band
        out["would_skip"] = bool(band == SKIP)
        if m == "active" and band == SKIP:
            out["skip"] = True
        return out


heads = FastHeads()


def consult(name: str, text: str, **features) -> dict[str, Any]:
    """Module-level shorthand for the singleton."""
    return heads.consult(name, text, **features)


def record_outcome(decision: dict[str, Any], *, needed_model: bool,
                   store=None) -> None:
    """Log what the model actually concluded, beside what the head predicted.

    `needed_model` is the ground truth for this event: did the LLM produce
    anything? A head that would have skipped an input the model turned into a
    fact is the failure this whole rollout is designed to catch, so that pair
    is the row the activation gate counts.

    Best-effort and silent: this runs after the real work, on the serving
    path, and must never affect it.
    """
    try:
        if not isinstance(decision, dict):
            return
        name = str(decision.get("head") or "")
        if not name or decision.get("mode") == "off":
            return
        if store is None:
            from app.storage import get_store
            store = get_store()
        store.record_head_observation(
            head=name, mode=str(decision.get("mode") or ""),
            p=decision.get("p"), band=str(decision.get("band") or ""),
            would_skip=bool(decision.get("would_skip")),
            skipped=bool(decision.get("skip")),
            needed_model=bool(needed_model))
    except Exception as exc:  # pragma: no cover - telemetry never raises
        print(f"[fast_heads] outcome log skipped ({exc}).")


# --------------------------------------------------------------------------
# rollout status — what the Console panel reads
# --------------------------------------------------------------------------
def status(*, store=None, window_s: float = 7 * 86400.0) -> dict[str, Any]:
    """Per-head volume, skip rate, shadow disagreement, and readiness.

    `ready` encodes the activation gate from the brief in one place, so the
    Console can offer the flip rather than a human eyeballing a log: enough
    shadow events, and disagreement below the threshold on the population the
    head would have skipped.
    """
    cfg = _cfg()
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "heads": []}
    out = []
    for name, spec in HEADS.items():
        try:
            obs = store.head_observations(name, window_s=window_s)
        except Exception as exc:
            obs = {"error": str(exc)}
        try:
            labels = len(labels_for(spec, store=store))
        except Exception:
            labels = 0
        n_would_skip = int(obs.get("would_skip") or 0)
        misses = int(obs.get("would_skip_but_needed") or 0)
        disagreement = (misses / n_would_skip) if n_would_skip else None
        _, meta = load_latest(name)
        enough = n_would_skip >= int(cfg.min_shadow_events)
        ready = bool(enough and disagreement is not None
                     and disagreement <= float(cfg.max_disagreement))
        t_low, t_high = thresholds(name)
        out.append({
            "head": name,
            "decision": spec.decision,
            "rationale": spec.rationale,
            "mode": head_mode(name),
            "trained_version": meta.get("version"),
            "trained_at": meta.get("trained_at"),
            "labels": labels,
            "labels_needed": int(cfg.min_labels),
            "t_low": t_low, "t_high": t_high,
            "events": int(obs.get("events") or 0),
            "would_skip": n_would_skip,
            "skip_rate": (round(n_would_skip / obs["events"], 4)
                          if obs.get("events") else None),
            "skipped": int(obs.get("skipped") or 0),
            "disagreement": (round(disagreement, 4)
                             if disagreement is not None else None),
            "disagreement_n": misses,
            "max_disagreement": float(cfg.max_disagreement),
            "min_shadow_events": int(cfg.min_shadow_events),
            # Why this head cannot be activated yet, in the user's terms.
            "ready_to_activate": ready,
            "blocked_by": (
                None if ready else
                "no model trained" if meta.get("version") is None else
                f"needs {int(cfg.min_shadow_events) - n_would_skip} more "
                f"shadow events" if not enough else
                f"disagreement {disagreement:.1%} exceeds "
                f"{float(cfg.max_disagreement):.1%}"),
        })
    return {"ok": True, "mode": mode(), "window_days": round(window_s / 86400, 1),
            "heads": out}
