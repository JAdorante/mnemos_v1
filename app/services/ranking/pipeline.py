"""Unified ranking pipeline: Scorer → Selector → Admitter → FocusSet.

`QUILL_FIELD_V2` selects only the Scorer. Quotas live solely in the Admitter.
`QUILL_WM=0` makes the Selector pure top-k; Admitter still runs.
"""
from __future__ import annotations

import time
from typing import Any

from app.services.ranking import admitter as _admitter
from app.services.ranking import selector as _selector
from app.services.ranking.scorer import Scorer, get_scorer
from app.services.ranking.types import PipelineContext, PipelineResult, ScoreBreakdown


def run(
    candidates: list[dict],
    *,
    ctx: PipelineContext | None = None,
    scorer: Scorer | None = None,
    focus_k: int | None = None,
    store: Any = None,
    now: float | None = None,
    mode: dict | None = None,
    persist_wm: bool = True,
) -> PipelineResult:
    """Score → select → admit. One call path to a focus set."""
    ctx = ctx or PipelineContext()
    if store is not None:
        ctx.store = store
    if now is not None:
        ctx.now = now
    elif ctx.now is None:
        ctx.now = time.time()
    if focus_k is not None:
        ctx.focus_k = focus_k
    if mode is not None:
        ctx.mode = mode
    ctx.persist_wm = persist_wm

    if ctx.wm_enabled is True and ctx.store is not None:
        # Honor kill-switch: QUILL_WM=0 → top-k Selector, Admitter still on.
        try:
            from app.services.working_memory import _wm_enabled
            ctx.wm_enabled = bool(_wm_enabled())
        except Exception:
            ctx.wm_enabled = True

    # Resolve mode inside Scorer context if not provided.
    if ctx.mode is None and ctx.store is not None:
        try:
            from app.services import attention_mode as _amode
            ctx.mode = _amode.current(store=ctx.store, now=ctx.now)
        except Exception as exc:
            print(f"[ranking.pipeline] attention mode skipped ({exc}).")
            ctx.mode = None

    scorer = scorer or get_scorer()
    # Work on copies so callers can reuse the input list.
    pool = [dict(n) for n in candidates]
    breakdowns: dict[str, ScoreBreakdown] = scorer.score(pool, ctx)

    ranked = sorted(
        pool,
        key=lambda n: (-int(bool(n.get("pinned"))),
                       -float(n.get("gravity") or 0)),
    )

    focus = _selector.select(ranked, ctx)
    focus = _admitter.admit(focus, ranked, breakdowns, ctx)

    # Sync breakdown admitted_by onto focus nodes.
    for n in focus:
        bd = breakdowns.get(n["id"])
        if bd:
            n["admitted_by"] = bd.admitted_by
        else:
            n.setdefault("admitted_by", "score")

    selection = {
        "path": "pipeline",
        "fallback": False,
        "reason": None,
        "scorer": scorer.name,
        "wm": bool(ctx.wm_enabled),
        "ts": time.time(),
    }
    # Surface selector fallbacks if WM marked one.
    try:
        from app.services import working_memory as _wm
        last = _wm.last_selection() or {}
        if last.get("fallback"):
            selection["fallback"] = True
            selection["reason"] = last.get("reason")
        elif ctx.wm_enabled:
            _wm.mark_selection(path="pipeline", fallback=False)
    except Exception:
        pass

    return PipelineResult(
        focus=focus,
        ranked=ranked,
        breakdowns=breakdowns,
        selection=selection,
        mode=ctx.mode,
    )
