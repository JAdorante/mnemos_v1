"""Unified constellation ranking pipeline.

candidates → Scorer → Selector → Admitter → FocusSet

`QUILL_FIELD_V2` selects only the Scorer implementation.
Quotas are an Admitter constraint, never an alternate selection path.
"""
from __future__ import annotations

from app.services.ranking.config import (
    BREAKDOWN_SUM_EPS,
    ENTITY_FOCUS_KINDS,
    FOCUS_CHURN_K,
    FOCUS_HI,
    FOCUS_LO,
    MIN_ENTITIES_IN_FOCUS,
    MIN_PEOPLE_IN_FOCUS,
)
from app.services.ranking.pipeline import run
from app.services.ranking.scorer import (
    FieldV2Scorer,
    GravityScorer,
    Scorer,
    get_scorer,
)
from app.services.ranking.types import (
    PipelineContext,
    PipelineResult,
    ScoreBreakdown,
    ScoreComponent,
)

__all__ = [
    "BREAKDOWN_SUM_EPS",
    "ENTITY_FOCUS_KINDS",
    "FOCUS_CHURN_K",
    "FOCUS_HI",
    "FOCUS_LO",
    "MIN_ENTITIES_IN_FOCUS",
    "MIN_PEOPLE_IN_FOCUS",
    "FieldV2Scorer",
    "GravityScorer",
    "PipelineContext",
    "PipelineResult",
    "ScoreBreakdown",
    "ScoreComponent",
    "Scorer",
    "get_scorer",
    "run",
]
