"""Ranking pipeline types — ScoreBreakdown and pipeline I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


AdmittedBy = Literal["score", "quota", "pin"]
ComponentKey = Literal[
    "pin", "due", "relationship", "centrality", "recency",
    "kind", "confidence_gate", "activation", "context", "aging",
]


@dataclass
class ScoreComponent:
    """One labeled contribution to a node's rank."""
    key: ComponentKey
    label: str
    value: float
    evidence_refs: list[str] = field(default_factory=list)
    # Explicit marker when the signal is structural / has no event provenance.
    evidence: str | None = None  # "none" when intentionally empty

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "value": round(float(self.value), 6),
            "evidence_refs": list(self.evidence_refs),
        }
        if self.evidence is not None:
            d["evidence"] = self.evidence
        elif not self.evidence_refs:
            d["evidence"] = "none"
        return d


@dataclass
class ScoreBreakdown:
    """Auditable rank for one node — components sum to total (within eps)."""
    node_id: str
    total: float
    components: list[ScoreComponent] = field(default_factory=list)
    admitted_by: AdmittedBy = "score"

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "total": round(float(self.total), 6),
            "components": [c.to_dict() for c in self.components],
            "admitted_by": self.admitted_by,
        }


@dataclass
class PipelineContext:
    """Shared context fed to Scorer → Selector → Admitter."""
    store: Any = None
    now: float | None = None
    focus_k: int = 10
    # Attention mode dict from attention_mode.current(); Scorer applies it.
    mode: dict[str, Any] | None = None
    # When False, Selector is pure top-k (no MMR/hysteresis). Admitter still runs.
    wm_enabled: bool = True
    # Field features already attached on candidates; Scorer may enrich.
    # Optional maps for FieldV2Scorer (activation, dynamics, learned weights).
    act_map: dict | None = None
    dyn_map: dict | None = None
    learned_w: dict | None = None
    persist_wm: bool = True


@dataclass
class PipelineResult:
    """Focus set + ranked candidates + breakdowns + selection metadata."""
    focus: list[dict]
    ranked: list[dict]
    breakdowns: dict[str, ScoreBreakdown]
    selection: dict[str, Any]
    mode: dict[str, Any] | None = None
