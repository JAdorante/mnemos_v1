"""Selector — Working Memory (MMR + hysteresis + cluster collapse).

When QUILL_WM=0, Selector is pure top-k by score (no MMR/hysteresis).
Admitter still runs afterward — quotas are never an alternate selector.
"""
from __future__ import annotations

from typing import Any

from app.services.ranking.types import PipelineContext


def select_topk(ranked: list[dict], focus_k: int) -> list[dict]:
    """Pure score order — kill-switch path when WM hysteresis is off."""
    focus: list[dict] = []
    for n in ranked:
        out = dict(n)
        out["layer"] = "focus"
        out["cluster_n"] = int(out.get("cluster_n") or 1)
        focus.append(out)
        if len(focus) >= focus_k:
            break
    return focus


def select(ranked: list[dict], ctx: PipelineContext) -> list[dict]:
    """Select focus nodes from already-scored ranked candidates."""
    focus_k = int(ctx.focus_k)
    if not ctx.wm_enabled:
        return select_topk(ranked, focus_k)

    from app.services import working_memory as _wm

    try:
        focus = _wm.select_focus(
            ranked,
            focus_k,
            store=ctx.store,
            now=ctx.now,
            persist=ctx.persist_wm,
        )
    except Exception as exc:
        print(f"[ranking.selector] WM failed — top-k ({exc}).")
        focus = select_topk(ranked, focus_k)
        try:
            _wm.mark_selection(
                path="pipeline", fallback=True, reason=str(exc))
        except Exception:
            pass
        return focus

    if not focus and ranked:
        print("[ranking.selector] WM empty — top-k fill.")
        focus = select_topk(ranked, focus_k)
        try:
            _wm.mark_selection(
                path="pipeline", fallback=True, reason="wm_empty")
        except Exception:
            pass
    return focus
