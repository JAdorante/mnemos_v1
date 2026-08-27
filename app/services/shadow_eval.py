"""Idle shadow evaluation — silent-failure mining (Workstream B).

The escalation trigger only catches failures the local model KNOWS about
(low confidence, parse errors, suspect shapes). The confident-but-wrong
quadrant never escalates and never gets labeled. This service closes that
blind spot: model_router logs every kept (non-escalated) local output to a
lightweight append log, and while the machine is idle a small nightly batch
is re-graded by Claude against a per-task rubric. Disagreements become
LearningPair rows flagged `human_confirmed=false` — eligible for exemplar/
router training only after a one-click confirm in the Learning tab (or the
explicitly-documented QUILL_SHADOW_AUTOTRUST=1).

Privacy invariant (tested): rows whose content classes personal/sensitive/
never-send are stamped shadow_eligible=0 AT LOG TIME (fail-closed on
classifier errors) and are never sampled — no personal content leaves the
machine through this path. Cost invariant: a hard daily token budget
(QUILL_SHADOW_BUDGET_TOKENS) stops the job mid-batch and logs the cutoff.

Scheduling: rides the idle trainer's hourly tick (idle_trainer.tick calls
maybe_run_idle BEFORE the training-eligibility check — shadow eval feeds the
same store training reads). One run per calendar day, sampling anything
ungraded within QUILL_SHADOW_LOOKBACK_DAYS (default 30) — NOT just the last
24h, which used to discard every kept output a busy day produced beyond the
batch size. `batch` is the throughput/spend knob; the window only decides what
is still reachable.
"""
from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from app.config import settings

# Router task -> canonical learning task_type for disagreement pairs.
TASK_TYPE_MAP = {
    "chat": "escalation.text",
    "extract": "extraction.claim",
    "reflect": "brief.section",
    "activity": "brief.section",
}

_VERDICTS = ("agree", "minor_disagree", "major_disagree")
_REASON_CODES = ("missed_content", "wrong_content", "hallucination",
                 "format", "incomplete", "other")

# Fixed per-task rubric prompts (B.3). Strict JSON out, bounded tokens.
_RUBRIC_BASE = (
    "You are auditing a small local model's output. Compare the LOCAL OUTPUT "
    "against the INPUT for the task '{task}'.\n{task_rubric}\n"
    "Reply with STRICT JSON only, no prose:\n"
    '{{"verdict": "agree|minor_disagree|major_disagree", '
    '"corrected_output": "<your corrected output — required unless verdict '
    'is agree>", "reason_code": "missed_content|wrong_content|hallucination|'
    'format|incomplete|other"}}'
)
_TASK_RUBRICS = {
    "chat": ("agree = the answer is correct and grounded in the input context; "
             "minor_disagree = right substance, wrong emphasis/format or small "
             "omissions; major_disagree = factually wrong, ungrounded, or "
             "missed the question."),
    "extract": ("agree = the extraction captures the tasks/commitments/claims "
                "actually present; minor_disagree = misses secondary items or "
                "wording is off; major_disagree = invents items or misses the "
                "primary one."),
    "reflect": ("agree = the insight is supported by the input; minor_disagree "
                "= supported but poorly stated; major_disagree = unsupported."),
    "activity": ("agree = the summary reflects the activity trail; "
                 "minor_disagree = partial; major_disagree = wrong."),
}


def _cfg():
    return settings.shadow


def enabled() -> bool:
    import os
    v = os.environ.get("QUILL_SHADOW_EVAL")
    if v is not None:
        return v not in ("0", "false", "False")
    return bool(_cfg().enabled)


def _budget_tokens() -> int:
    """Env read at call time (frozen settings bake at import; the budget must
    be adjustable per-run in tests and from a console toggle)."""
    import os
    v = os.environ.get("QUILL_SHADOW_BUDGET_TOKENS")
    return int(v) if v else int(_cfg().budget_tokens)


def _batch_size() -> int:
    import os
    v = os.environ.get("QUILL_SHADOW_BATCH")
    return int(v) if v else int(_cfg().batch)


def _lookback_s() -> float:
    """How far back the sampler may reach, in seconds.

    Was a hard-coded 24h, which quietly threw labels away: one run per
    calendar day grading at most `batch` rows means any busier day's surplus
    aged out of the window before the next run could reach it, and no backlog
    could ever be drained. Widening it makes `batch` the only throughput
    limit — which is the knob that should govern spend anyway.
    """
    import os
    v = os.environ.get("QUILL_SHADOW_LOOKBACK_DAYS")
    days = float(v) if v else float(getattr(_cfg(), "lookback_days", 30.0))
    return max(1.0, days) * 86400.0


# ---------------------------------------------------------------------------
# The local_outputs log (written by model_router on every KEPT local answer)
# ---------------------------------------------------------------------------

def _last_user_text(messages: list | None) -> str:
    if not messages:
        return ""
    try:
        from app.services.ollama_text import _flatten
        for m in reversed(messages):
            if m.get("role", "user") == "user":
                return _flatten(m.get("content"))
    except Exception:
        pass
    return ""


def log_local_output(task: str, *, messages: list | None, text: str | None,
                     confidence: float | None, model_tag: str | None,
                     shadow_priority: bool = False,
                     retrieval: dict | None = None) -> str | None:
    """Append one kept local output to the sample pool. Redacted at the write
    boundary; privacy-classed on the RAW text (fail-closed) so the nightly
    sampler can exclude personal rows without re-reading anything. Never
    raises — logging must not break the local answer path."""
    try:
        if not enabled():
            return None
        input_raw = _last_user_text(messages)
        output_raw = str(text or "")
        if not input_raw or not output_raw:
            return None
        # Classify BEFORE redaction (redaction strips the PII the classifier
        # keys on); store redacted. Fail CLOSED: classifier error → ineligible.
        try:
            from app.services.privacy_class import classify_text, max_class
            cls = max_class(classify_text(input_raw), classify_text(output_raw))
            eligible = cls not in ("personal", "sensitive", "never-send")
        except Exception:
            cls, eligible = "internal", False
        from app.perception import redaction
        input_clean, _ = redaction.redact_text(input_raw, redaction.TIER_LOG)
        output_clean, _ = redaction.redact_text(output_raw, redaction.TIER_LOG)
        row = {
            "id": uuid.uuid4().hex,
            "ts": time.time(),
            "task": str(task),
            "input": input_clean,
            "output": output_clean,
            "confidence": confidence,
            "model_tag": model_tag,
            "privacy_class": cls,
            "shadow_eligible": bool(eligible),
            # D.3 middle band: the router flagged this kept answer as
            # uncertain — B.2's sampler puts it first in line.
            "shadow_priority": bool(shadow_priority),
            # D.2b router features as measured at call time. Numeric stats
            # only — no retrieved content — so this rides the same privacy
            # stamp as the rest of the row without widening what it exposes.
            "retrieval": retrieval or None,
        }
        p = Path(_cfg().local_outputs_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row["id"]
    except Exception as exc:
        print(f"[shadow_eval] local-output log skipped ({exc}).")
        return None


def _read_rows(*, since: float) -> list[dict]:
    p = Path(_cfg().local_outputs_path)
    if not p.is_file():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
        except Exception:
            continue
        if float(row.get("ts") or 0) >= since:
            out.append(row)
    return out


def sample(rows: list[dict], batch: int, *,
           graded_ids: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """B.2 sampling strategy: eligible rows only, then (1) router-uncertainty
    first — until Workstream D feeds a shadow_priority flag, low/missing local
    confidence is the uncertainty proxy — (2) stratified round-robin across
    task types so no type starves, (3) stable order within strata."""
    pool = [r for r in rows
            if r.get("shadow_eligible") and r.get("id") not in graded_ids]
    strata: dict[str, list[dict]] = {}
    for r in pool:
        strata.setdefault(str(r.get("task")), []).append(r)
    for rows_t in strata.values():
        # Priority-flagged rows first (D.3 band), then least-confident first;
        # missing confidence = most uncertain.
        rows_t.sort(key=lambda r: (
            0 if r.get("shadow_priority") else 1,
            r.get("confidence") if r.get("confidence") is not None else -1.0))
    picked: list[dict] = []
    keys = sorted(strata.keys())
    i = 0
    while len(picked) < max(0, int(batch)) and any(strata.values()):
        k = keys[i % len(keys)]
        if strata[k]:
            picked.append(strata[k].pop(0))
        i += 1
        if i > 10000:            # defensive: never loop forever
            break
    return picked


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def _anthropic_call(system: str, user: str, *, model: str,
                    max_tokens: int) -> tuple[str, int, int]:
    """One grading call. Returns (text, input_tokens, output_tokens). The
    indirection lets tests inject a fake without touching the network."""
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}])
    u = getattr(resp, "usage", None)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return (text, int(getattr(u, "input_tokens", 0) or 0),
            int(getattr(u, "output_tokens", 0) or 0))


def _parse_verdict(text: str) -> dict | None:
    try:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        v = json.loads(text[start:end + 1])
        if v.get("verdict") not in _VERDICTS:
            return None
        if v.get("reason_code") not in _REASON_CODES:
            v["reason_code"] = "other"
        return v
    except Exception:
        return None


def grade_one(row: dict, call: Callable, *, model: str,
              max_tokens: int) -> tuple[dict | None, int, int]:
    task = str(row.get("task") or "chat")
    system = _RUBRIC_BASE.format(
        task=task, task_rubric=_TASK_RUBRICS.get(task, _TASK_RUBRICS["chat"]))
    user = (f"INPUT:\n{row.get('input')}\n\n"
            f"LOCAL OUTPUT:\n{row.get('output')}")
    try:
        text, tok_in, tok_out = call(system, user, model=model,
                                     max_tokens=max_tokens)
    except Exception as exc:
        print(f"[shadow_eval] grade call failed ({exc}).")
        return None, 0, 0
    return _parse_verdict(text), tok_in, tok_out


# ---------------------------------------------------------------------------
# The nightly run
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(Path(_cfg().state_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        from app.atomic_json import write_json
        write_json(Path(_cfg().state_path), state)
    except Exception:
        p = Path(_cfg().state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state), encoding="utf-8")


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def run_nightly(now: float | None = None, *, call: Callable | None = None,
                store=None) -> dict:
    """One shadow-eval batch. Returns a summary dict (also appended to the
    report file). Safe to call every tick — day gating and the token budget
    live here, not in the scheduler."""
    if not enabled():
        return {"skipped": "disabled"}
    now = float(now if now is not None else time.time())
    today = _day(now)
    state = _load_state()
    if state.get("last_day") == today:
        return {"skipped": "already ran today"}
    # Ambient cloud budget (perception.spend_cap) still applies — best-effort.
    try:
        from app.perception.spend_cap import spend_cap
        spend_cap.check("shadow_eval")
    except ImportError:
        pass
    except Exception as exc:
        return {"skipped": f"spend cap: {exc}"}

    cfg = _cfg()
    budget = _budget_tokens()
    call = call or _anthropic_call
    graded_ids = frozenset(state.get("graded_ids") or [])
    rows = _read_rows(since=now - _lookback_s())
    batch = sample(rows, _batch_size(), graded_ids=graded_ids)

    spent = 0
    cutoff = False
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    by_task: dict[str, Counter] = {}
    pair_ids: list[str] = []
    newly_graded: list[str] = []
    for row in batch:
        # Hard daily ceiling — stop mid-batch and log the cutoff (B.3).
        if spent + cfg.max_grade_tokens + 1000 > budget:
            cutoff = True
            print(f"[shadow_eval] budget hit after {spent} tokens — "
                  f"{len(batch) - len(newly_graded)} rows left ungraded.")
            break
        verdict, tok_in, tok_out = grade_one(
            row, call, model=cfg.model, max_tokens=cfg.max_grade_tokens)
        spent += tok_in + tok_out
        try:
            from app.services.model_log import model_log
            model_log.log_call(task="shadow_eval", provider="claude",
                               model=cfg.model, latency_s=0.0, ok=verdict is not None,
                               input_tokens=tok_in, output_tokens=tok_out)
        except Exception:
            pass
        newly_graded.append(str(row.get("id")))
        if verdict is None:
            counts["unparseable"] += 1
            continue
        v = str(verdict["verdict"])
        # Per-row grade log — agrees included: they're the router's
        # local_sufficient=1 labels (Workstream D.1). Already-redacted text.
        try:
            gp = Path(cfg.grades_path)
            gp.parent.mkdir(parents=True, exist_ok=True)
            with gp.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "id": row.get("id"), "ts": now, "task": row.get("task"),
                    "verdict": v, "input": row.get("input"),
                    "confidence": row.get("confidence"),
                    # Carried through so router_train.build_dataset sees the
                    # same features the call itself was routed on.
                    "retrieval": row.get("retrieval"),
                    "model_tag": row.get("model_tag"),
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        counts[v] += 1
        by_task.setdefault(str(row.get("task")), Counter())[v] += 1
        if v == "agree":
            continue
        reasons[str(verdict.get("reason_code") or "other")] += 1
        corrected = str(verdict.get("corrected_output") or "").strip()
        if not corrected:
            continue
        # B.4: the disagreement is a labeled pair — unconfirmed until a human
        # (or explicit autotrust) promotes it.
        try:
            from app.services import learning_store
            pid = learning_store.record(
                task_type=TASK_TYPE_MAP.get(str(row.get("task")),
                                            "escalation.text"),
                input_text=str(row.get("input") or ""),
                local_output=str(row.get("output") or ""),
                parent_output=corrected,
                final_target=corrected,
                verdict="shadow_disagree",
                verdict_source="shadow_eval",
                human_confirmed=False,
                model_tag=row.get("model_tag"),
                source_refs={"local_output_id": row.get("id"),
                             "task": row.get("task"),
                             "grader_verdict": v,
                             "reason_code": verdict.get("reason_code")},
                store=store,
            )
            if pid:
                pair_ids.append(pid)
        except Exception as exc:
            print(f"[shadow_eval] pair record skipped ({exc}).")

    summary = {
        "day": today, "sampled": len(batch), "graded": len(newly_graded),
        "verdicts": dict(counts), "reason_codes": dict(reasons),
        "by_task": {k: dict(v) for k, v in by_task.items()},
        "tokens_spent": spent, "budget_tokens": budget,
        "cutoff": cutoff, "pairs_recorded": len(pair_ids),
    }
    _append_report(summary)
    state["last_day"] = today
    # Dedupe memory must cover the whole lookback window or rows get re-graded
    # (and re-paid for) once their id falls off the end. A fixed cap cannot do
    # that once the window is configurable — so instead of counting ids, keep
    # exactly those still REACHABLE: an id whose row has aged out of the window
    # can never be sampled again, so forgetting it is safe. That bounds the set
    # at the window's own size with no arbitrary cap and no re-grading.
    visible = {str(r.get("id")) for r in rows}
    kept = [i for i in (state.get("graded_ids") or []) if str(i) in visible]
    state["graded_ids"] = kept + [i for i in newly_graded if i not in kept]
    _save_state(state)
    return summary


def _append_report(summary: dict) -> None:
    try:
        p = Path(_cfg().report_path)
        days = []
        if p.is_file():
            try:
                days = json.loads(p.read_text(encoding="utf-8")).get("days") or []
            except Exception:
                days = []
        days = [d for d in days if d.get("day") != summary["day"]][-30:]
        days.append(summary)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"days": days}, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[shadow_eval] report write skipped ({exc}).")


def report(days: int = 7) -> dict:
    """Weekly rollup (B.5): agreement rate by task_type + top reason codes.
    Agreement-rate-by-type is the metric that later justifies (or kills)
    per-type LoRA runs (E.2)."""
    try:
        p = Path(_cfg().report_path)
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except Exception:
        data = {}
    window = (data.get("days") or [])[-max(1, int(days)):]
    by_task: dict[str, Counter] = {}
    reasons: Counter[str] = Counter()
    spent = 0
    for d in window:
        for task, verdicts in (d.get("by_task") or {}).items():
            c = by_task.setdefault(task, Counter())
            for v, n in verdicts.items():
                c[v] += int(n)
        for r, n in (d.get("reason_codes") or {}).items():
            reasons[r] += int(n)
        spent += int(d.get("tokens_spent") or 0)
    agreement = {}
    for task, c in by_task.items():
        total = sum(c.values())
        agreement[task] = {
            "graded": total,
            "agree_rate": round(c.get("agree", 0) / total, 3) if total else None,
            "verdicts": dict(c),
        }
    return {"enabled": enabled(), "window_days": len(window),
            "agreement_by_task": agreement,
            "top_reason_codes": reasons.most_common(5),
            "tokens_spent": spent, "days": window}


def maybe_run_idle(*, idle_s: float, on_ac: bool) -> dict | None:
    """The idle-scheduler hook (B.1): gate on idle + AC, then let run_nightly
    apply its own day/budget gating. Never raises."""
    try:
        if not enabled():
            return None
        if idle_s < _cfg().min_idle_s or not on_ac:
            return None
        return run_nightly()
    except Exception as exc:
        print(f"[shadow_eval] idle run skipped ({exc}).")
        return None
