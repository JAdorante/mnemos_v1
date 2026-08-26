"""Agent-distill harvest — sessions/agent_distill.jsonl → learning_pairs.

The browser agent appends one (observation -> action, verified) row per
executor step and one outcome row per run (browser_agent/distill.py). This
module folds that trail into the canonical learning substrate as task_type
"agent.act", so the Learning console counts it and a future agent-rung LoRA
can curate from SQLite like every other workstream.

Selection policy (deliberately strict — imitation data must be worth
imitating):
  * only steps whose OWN verification passed,
  * only from sessions whose run row says status == "success"
    (a verified click inside a run that then stalled may still have been the
    wrong move — whole-run success is the cheapest trustworthy filter),
  * only rows that carry an observation (redaction-unavailable rows are
    logged without one and teach nothing).

Row mapping: verdict="accepted" with verdict_source="shadow.agent_verified"
and human_confirmed=False — no human judged these steps; the "shadow." prefix
makes exemplar tiering classify them as machine-trusted (shadow_autotrust),
which the exemplar store refuses to ingest unless QUILL_SHADOW_AUTOTRUST=1.
Text-LoRA curation excludes agent.* explicitly (scripts/distill_curate.py),
so agent trajectories can never enter the text champion's adapter.

Idempotent: learning_pairs dedupes on (task_type, content_hash), and a
watermark file (agent_harvest_state.json next to the trail) skips rows already
scanned so re-harvesting doesn't re-run privacy classification over the whole
trail. Never raises — a broken harvest must never break the run that
triggered it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

VERDICT_SOURCE = "shadow.agent_verified"
TASK_TYPE = "agent.act"

# Re-scan this much wall-clock behind the watermark: a run row lands after its
# step rows, so steps just under the watermark may have been unharvestable
# (no outcome yet) on the previous pass.
_OVERLAP_S = 3600.0


def _trail_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    from browser_agent import config as agent_cfg
    return agent_cfg.SESSIONS_ROOT / "agent_distill.jsonl"


def _state_path(trail: Path) -> Path:
    return trail.with_name("agent_harvest_state.json")


def _load_watermark(trail: Path) -> float:
    try:
        return float(json.loads(_state_path(trail).read_text(
            encoding="utf-8")).get("watermark", 0.0))
    except Exception:
        return 0.0


def _save_watermark(trail: Path, watermark: float) -> None:
    try:
        _state_path(trail).write_text(
            json.dumps({"watermark": watermark, "saved_at": time.time()}),
            encoding="utf-8")
    except Exception as exc:
        print(f"[agent_harvest] state save skipped ({exc}).")


def _rows(trail: Path) -> list[dict]:
    out = []
    try:
        with trail.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
    except OSError:
        pass
    return out


def harvest(path: str | Path | None = None, store=None) -> dict[str, Any]:
    """Fold new trail rows into learning_pairs. Returns a count summary and
    never raises (errors surface in the summary instead)."""
    counts = {"scanned": 0, "harvested": 0, "skipped_unverified": 0,
              "skipped_run_not_success": 0, "skipped_no_observation": 0,
              "deduped_or_dropped": 0}
    try:
        from app.services import learning_store
        if not learning_store.enabled():
            counts["error"] = "learning disabled"
            return counts
        trail = _trail_path(path)
        rows = _rows(trail)
        if not rows:
            return counts
        # Run outcomes first: the whole-run success filter needs them all
        # regardless of the watermark (an old run row may govern new steps —
        # and vice versa on a re-run after a crash).
        run_status = {str(r.get("session_id")): str(r.get("status") or "")
                      for r in rows if r.get("task") == "browser.run"}
        watermark = _load_watermark(trail)
        cutoff = watermark - _OVERLAP_S
        newest = watermark
        for r in rows:
            if r.get("task") != "browser.act":
                continue
            t = float(r.get("time") or 0.0)
            newest = max(newest, t)
            if t <= cutoff:
                continue
            counts["scanned"] += 1
            if not r.get("verified"):
                counts["skipped_unverified"] += 1
                continue
            if run_status.get(str(r.get("session_id"))) != "success":
                counts["skipped_run_not_success"] += 1
                continue
            obs = r.get("observation")
            if not obs:
                counts["skipped_no_observation"] += 1
                continue
            action = r.get("action") or {}
            target = json.dumps(
                {"name": action.get("name"), "args": action.get("args") or {}},
                ensure_ascii=False, sort_keys=True)
            pid = learning_store.record(
                task_type=TASK_TYPE,
                input_text=str(obs),
                verdict="accepted",
                verdict_source=VERDICT_SOURCE,
                final_target=target,
                source_refs={
                    "distill_id": str(r.get("id") or ""),
                    "session_id": str(r.get("session_id") or ""),
                    "step": r.get("step"),
                    "url": str(r.get("url") or "")[:300],
                    "site": str(r.get("site") or ""),
                    "intent": str(r.get("intent") or ""),
                    "escalated": bool(r.get("escalated")),
                    "pixel": bool(r.get("pixel")),
                },
                model_tag=str(r.get("model") or "") or None,
                human_confirmed=False,
                created_at=t or None,
                store=store,
            )
            if pid:
                counts["harvested"] += 1
            else:
                counts["deduped_or_dropped"] += 1
        _save_watermark(trail, newest)
    except Exception as exc:
        counts["error"] = str(exc)[:200]
        print(f"[agent_harvest] harvest skipped ({exc}).")
    return counts
