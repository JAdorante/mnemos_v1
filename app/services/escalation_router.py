"""Escalation router runtime (Workstream D) — the first trained model.

Predicts "will the local model fail on this input?" and turns that into the
three-band policy (D.3):

    p(fail) <  t_low   → local only
    t_low ≤ p < t_high → local, flagged for shadow-eval priority sampling
    p ≥ t_high         → escalate to Claude directly

Rollout safety (D.4, invariant 4 applied to models):
  * QUILL_ROUTER=off     (default) — module is inert
  * QUILL_ROUTER=shadow  — decisions are LOGGED next to the heuristic's
                           (data/router/shadow_log.jsonl); the heuristic
                           still routes. Provably no routing influence.
  * QUILL_ROUTER=active  — explicit user flip (Console offers it once the
                           weekly report shows the router beating the
                           heuristic; rollback is QUILL_ROUTER=shadow).
                           Hard safety gates (high-stakes tasks, parse
                           failures, suspect answers) always escalate —
                           the router augments the confidence gate only.

HARD RULE (invariant 3): this module influences the local-vs-parent choice
only. It must never be imported by the decide/approval layer — risk stays a
lookup table. tests/test_escalation_router.py asserts the import ban.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from app.config import settings


def _cfg():
    return settings.router


def mode() -> str:
    import os
    m = (os.environ.get("QUILL_ROUTER") or _cfg().mode or "off").strip().lower()
    return m if m in ("off", "shadow", "active") else "off"


class EscalationRouter:
    """Lazy-loaded latest model + per-call banding. Never raises into the
    routing path — any failure means 'no prediction' and the heuristic rules."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self._meta: dict = {}
        self._version_loaded = -1

    def _ensure(self):
        from app.services import router_train
        v = router_train.latest_version()
        with self._lock:
            if v != self._version_loaded:
                self._model, self._meta = router_train.load_latest()
                self._version_loaded = v
        return self._model

    def predict(self, task: str, text: str,
                confidence: float | None) -> float | None:
        """Calibrated p(local fails), or None when no model is trained."""
        try:
            model = self._ensure()
            if model is None or not text:
                return None
            from app.services import router_train
            rows = [{"task": task, "text": text, "confidence": confidence,
                     "ts": time.time()}]
            X = router_train.featurize(rows)
            return float(model.predict_proba(X)[:, 1][0])
        except Exception as exc:
            print(f"[escalation_router] predict skipped ({exc}).")
            return None

    def band(self, p_fail: float | None) -> str:
        if p_fail is None:
            return "no_model"
        cfg = _cfg()
        if p_fail >= float(cfg.t_high):
            return "escalate"
        if p_fail >= float(cfg.t_low):
            return "shadow_priority"
        return "local"

    def decide(self, task: str, messages: list | None,
               confidence: float | None,
               *, heuristic_escalates: bool,
               heuristic_reason: str | None = None) -> dict:
        """One routing consult. Returns
        {mode, p_fail, band, escalate, shadow_priority} where `escalate` is
        the FINAL local-vs-parent decision this module endorses:
          off/shadow → always the heuristic's decision (shadow only logs)
          active     → heuristic hard gates win; the router adds
                       p≥t_high escalations and can keep a low-p answer
                       local when the only trigger was low confidence.
        """
        m = mode()
        out = {"mode": m, "p_fail": None, "band": "no_model",
               "escalate": heuristic_escalates, "shadow_priority": False}
        if m == "off":
            return out
        text = ""
        try:
            from app.services.few_shot import query_focus, query_text
            text = query_focus(query_text(messages))
        except Exception:
            pass
        p = self.predict(task, text, confidence)
        band = self.band(p)
        out["p_fail"], out["band"] = p, band
        if m == "shadow":
            self._log_shadow(task, p, band, heuristic_escalates,
                             heuristic_reason)
            return out                      # provably: decision untouched
        # active: hard gates already filtered by the caller; here the router
        # augments the confidence gate.
        if band == "escalate":
            out["escalate"] = True
        elif band == "local" and heuristic_reason == "low_confidence":
            # High-confidence prediction that the local answer is fine —
            # this is the spend-reduction half of the router's job.
            out["escalate"] = False
        if band == "shadow_priority" and not out["escalate"]:
            out["shadow_priority"] = True
        return out

    # ------------------------------ shadow log ---------------------------
    def _shadow_log_path(self) -> Path:
        return Path(_cfg().dir) / "shadow_log.jsonl"

    def _log_shadow(self, task: str, p_fail: float | None, band: str,
                    heuristic_escalates: bool, reason: str | None) -> None:
        try:
            p = self._shadow_log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(), "task": task, "p_fail": p_fail,
                    "band": band, "heuristic_escalated": heuristic_escalates,
                    "heuristic_reason": reason,
                }) + "\n")
        except Exception:
            pass

    # ------------------------------ retraining ---------------------------
    def maybe_retrain(self, store=None) -> dict | None:
        """D.5: cheap enough to be aggressive — retrain when ≥N new labels
        accrued since the last fit; always shadow-compare vs the incumbent.
        The new version only becomes the loaded model when it does not
        regress the incumbent's holdout miss/escalation metrics (promote-by-
        report; flipping QUILL_ROUTER=active remains the user's explicit
        choice either way). Never raises."""
        try:
            if mode() == "off":
                return None
            from app.services import router_train
            rows = router_train.build_dataset(store=store)
            cfg = _cfg()
            if len(rows) < int(cfg.min_labels):
                return {"skipped": f"{len(rows)} labels < {cfg.min_labels}"}
            _, meta = router_train.load_latest()
            last_n = int(meta.get("n_labels") or 0)
            if len(rows) - last_n < int(cfg.retrain_new_labels):
                return {"skipped": f"only {len(rows) - last_n} new labels"}
            model, metrics = router_train.train(rows)
            incumbent = meta.get("holdout") or {}
            better = _beats(metrics, incumbent)
            if better:
                path = router_train.save(model, metrics, n_labels=len(rows))
                print(f"[escalation_router] new model {path.name} "
                      f"(holdout: {metrics}).")
            else:
                print(f"[escalation_router] challenger did not beat the "
                      f"incumbent ({metrics} vs {incumbent}) — kept.")
            return {"trained": True, "promoted": bool(better),
                    "n_labels": len(rows), "holdout": metrics}
        except Exception as exc:
            print(f"[escalation_router] retrain skipped ({exc}).")
            return None

    def report(self) -> dict:
        """Weekly report row (D.5): router vs heuristic on the shadow log +
        the latest fit's holdout metrics. This is the evidence the Console
        shows before offering QUILL_ROUTER=active."""
        _, meta = router_train_meta()
        log_rows = []
        try:
            p = self._shadow_log_path()
            if p.is_file():
                week_ago = time.time() - 7 * 86400
                for ln in p.read_text(encoding="utf-8").splitlines():
                    if not ln.strip():
                        continue
                    try:
                        r = json.loads(ln)
                    except Exception:
                        continue
                    if float(r.get("ts") or 0) >= week_ago:
                        log_rows.append(r)
        except Exception:
            pass
        n = len(log_rows)
        with_model = [r for r in log_rows if r.get("p_fail") is not None]
        agree = sum(1 for r in with_model
                    if (r["band"] == "escalate") == bool(r["heuristic_escalated"]))
        router_esc = sum(1 for r in with_model if r["band"] == "escalate")
        heur_esc = sum(1 for r in log_rows if r.get("heuristic_escalated"))
        # Silent-failure candidates the router flagged and the heuristic kept
        # local — shadow-eval priority sampling validates these (B.2 ↔ D.3).
        flagged = sum(1 for r in with_model
                      if r["band"] in ("escalate", "shadow_priority")
                      and not r.get("heuristic_escalated"))
        return {
            "mode": mode(), "version": meta.get("version"),
            "n_labels": meta.get("n_labels"), "holdout": meta.get("holdout"),
            "week": {"decisions": n, "with_model": len(with_model),
                     "agreement": round(agree / len(with_model), 3)
                     if with_model else None,
                     "router_escalation_rate": round(router_esc / len(with_model), 3)
                     if with_model else None,
                     "heuristic_escalation_rate": round(heur_esc / n, 3)
                     if n else None,
                     "silent_failure_flags": flagged},
            "thresholds": {"t_low": _cfg().t_low, "t_high": _cfg().t_high},
        }


def _beats(challenger: dict, incumbent: dict) -> bool:
    """First model always lands; afterwards the challenger must not regress
    miss rate or escalation rate (ties allowed — data volume grew)."""
    if not incumbent:
        return True
    c_miss = challenger.get("miss_rate")
    i_miss = incumbent.get("miss_rate")
    c_esc = challenger.get("escalation_rate")
    i_esc = incumbent.get("escalation_rate")
    if c_miss is None or i_miss is None:
        return True
    return c_miss <= i_miss and (c_esc is None or i_esc is None
                                 or c_esc <= i_esc + 0.05)


def router_train_meta() -> tuple[Any, dict]:
    from app.services import router_train
    return router_train.load_latest()


escalation_router = EscalationRouter()
