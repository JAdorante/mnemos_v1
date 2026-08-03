"""Ranking pipeline constants — one home for behavioral knobs.

Every constant here has a stated meaning. Do not scatter magic numbers in
scorer / selector / admitter.
"""
from __future__ import annotations

# Max focus-set membership churn when re-running the pipeline after one
# low-impact event (hysteresis contract). Property tests assert ≤ this.
FOCUS_CHURN_K = 2

# Focus ring size bounds (matches working_memory capacity).
FOCUS_LO = 7
FOCUS_HI = 12

# Diversity quotas enforced by the Admitter (post-selection swaps).
# Behavioral meaning: under a flood of open work, keep people and projects/
# tools/places visible — not as an alternate selector, as a constraint.
MIN_PEOPLE_IN_FOCUS = 2
MIN_ENTITIES_IN_FOCUS = 3

# Constellation kinds that count as "entities" for quota admission.
ENTITY_FOCUS_KINDS = frozenset({"project", "org", "tool", "place", "idea"})

# Breakdown float tolerance: components must sum to total within this.
BREAKDOWN_SUM_EPS = 1e-3

# --- Time / aging (WS3) -------------------------------------------------------
# Open commitments *gain* gravity past this age (days since extracted_at).
# Inverts the usual recency bias exactly where follow-through matters.
AGING_OPEN_DAYS = 2.0
# Days after AGING_OPEN_DAYS to ramp the aging component from 0 → 1.
AGING_RAMP_DAYS = 12.0
# Margin /diff.aging threshold — "several open tasks are aging".
AGING_MARGIN_DAYS = 2.0
# Field snapshot ring buffer (diff source, not an archive).
FIELD_SNAPSHOT_RETAIN_DAYS = 30.0
FIELD_SNAPSHOT_MAX_N = 200

# Score component keys (WS2/WS3 schema).
COMPONENT_KEYS = frozenset({
    "pin",
    "due",
    "relationship",
    "centrality",
    "recency",
    "kind",
    "confidence_gate",
    "activation",  # Field v2 only
    "context",     # mode reweight (WS4 surface; emitted when mode ≠ 1)
    "aging",       # neglected open commitments (WS3)
})

# Admission provenance.
ADMITTED_BY = frozenset({"score", "quota", "pin"})
