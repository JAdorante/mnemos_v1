"""Agent-step distillation trail — the imitation-learning substrate.

Every executor step is one (observation -> action) decision by a cloud model.
Logged with its verify outcome, those pairs are exactly the data a local rung
needs to learn routine agent steps — the same idea as the text side's
data/escalate_distill.jsonl, kept as a SEPARATE file so agent rows can never
pollute the text/vision training trail.

Rows are append-only JSONL at <sessions>/agent_distill.jsonl:
  step rows  (task="browser.act"): observation text, chosen action, model,
              escalated flag, verify verdict — the training pair.
  run rows   (task="browser.run"): final status per session, so a harvester
              can weight steps by whether the whole run succeeded.

Redaction: args go through memory.redact (secret keys); observation text goes
through app.perception.redaction at TIER_LOG when the app is importable (the
normal case), else the row is written without the observation — a pair with no
observation is useless for training but the outcome row still counts.
Never raises: logging must not break the task.
"""
from __future__ import annotations

import json
import threading
import time
import uuid

from . import config as cfg
from .memory import redact

_lock = threading.Lock()


def _path():
    return cfg.SESSIONS_ROOT / "agent_distill.jsonl"


def _redact_text(text: str) -> str | None:
    """App-side PII/secret redaction; None when unavailable (standalone use)."""
    try:
        from app.perception import redaction as _r
        cleaned, _hits = _r.redact_text(text, _r.TIER_LOG)
        return cleaned
    except Exception:
        return None


def _redact_args(args: dict) -> dict:
    """Secret-key redaction always; app-side PII redaction on top when the
    app is importable (free-text args like a typed message can carry PII)."""
    out = redact(args or {})
    try:
        from app.perception import redaction as _r
        out, _hits = _r.redact(out, _r.TIER_LOG)
    except Exception:
        pass
    return out


def _append(row: dict) -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with _lock:
            with p.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception as exc:
        print(f"[agent_distill] write skipped ({exc}).")


def log_step(*, session_id: str, step: int, url: str, observation: str,
             action: str, args: dict | None, model: str | None,
             escalated: bool, pixel: bool, vision: bool,
             verified: bool | None, vnote: str, step_status: str | None,
             intent: str, site: str) -> None:
    """One executor decision. `observation` is the exact text prompt the model
    chose from (screenshot bytes are never stored — the step's shot on disk is
    referenced by session/step already)."""
    if not cfg.DISTILL:
        return
    obs = _redact_text((observation or "")[: cfg.DISTILL_OBS_CAP])
    row = {
        "id": uuid.uuid4().hex,
        "time": time.time(),
        "task": "browser.act",
        "session_id": session_id,
        "step": step,
        "url": (url or "")[:300],
        "intent": intent,
        "site": site,
        "model": model or "",
        "escalated": bool(escalated),
        "pixel": bool(pixel),
        "vision": bool(vision),
        "observation": obs,          # None => app redaction unavailable
        "action": {"name": action, "args": _redact_args(args)},
        "verified": verified,
        "vnote": (vnote or "")[:300],
        "step_status": step_status or "",
    }
    _append(row)


def log_run(*, session_id: str, status: str, steps: int, replans: int,
            intent: str, site: str, escalations: int) -> None:
    """Run outcome row — lets a harvester weight this session's step pairs
    (imitate successful runs; treat failed-run steps as negatives or skip)."""
    if not cfg.DISTILL:
        return
    _append({
        "id": uuid.uuid4().hex,
        "time": time.time(),
        "task": "browser.run",
        "session_id": session_id,
        "status": status,
        "steps": steps,
        "replans": replans,
        "escalations": escalations,
        "intent": intent,
        "site": site,
    })
