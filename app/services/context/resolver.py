"""CAL Stage 3 — the project resolver.

Stages 0–2 answer "what identifiers does this event carry?" and "what stretch of
work is this?". This answers the harder one: when an event carries several
plausible anchors, or none that binds on its own, which node is it about?

The scorer is log-linear and hand-weighted. The design calls for a trained
logistic model once labels accumulate — the same pattern as `router_train` and
`fast_heads` — but weights are a lie until there is a labelled corpus to fit
them to, and there isn't one yet. `WEIGHTS` is therefore an explicit, readable
prior, `train()` is where the fitted version will land, and `weights_version`
records which was used so a decision can be reproduced.

Two rules do almost all the work of keeping this honest:

  supporting evidence may REORDER, never PROPOSE
      Without it a system that watched you code for an hour starts attributing
      your bank statement to whatever was open at 10:47. `candidates()` admits
      a node only on strong or medium evidence; supporting features attach to
      candidates already proposed and can never create one.

  belief is absolute, margin is relative
      `p` is a softmax share, so with ONE candidate it is always 1.0 no matter
      how thin the evidence. Bands therefore read `strength` = σ(Σ w·f) for "am
      I sure", and use `p` only for "is anything competing". Conflating them
      binds a lone supporting-only guess at confidence 1.00.

  the 0.30 clamp
      Enforced at scoring time, not training time. Uncapped, the model learns
      to predict "whatever they were doing five minutes ago", which is right
      about 80% of the time and catastrophic the other 20%.

`unbound` is a first-class outcome, not a failure. The failure mode of these
systems is confident wrongness; an honest "I don't know what this was about" is
a better product than a wrong label, and it is the input to new-project
detection in Stage 4.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

WEIGHTS_VERSION = 1

# Which tier each feature belongs to. The clamp is computed over this mapping,
# so adding a feature without classifying it is a loud KeyError rather than a
# silent hole in the cap.
STRONG, MEDIUM, SUPPORTING = "strong", "medium", "supporting"
FEATURE_TIER = {
    "f_key":    STRONG,      # max strength over strong keys bound to this node
    "f_key_n":  STRONG,      # log(1 + distinct strong keys) — corroboration
    "f_alias":  MEDIUM,      # best alias / surface match
    "f_scope":  MEDIUM,      # a medium key resolved under a strong scope
    "f_frame":  SUPPORTING,  # decayed working-context weight
    "f_graph":  SUPPORTING,  # damped 1-hop propagation (Stage 4)
    "f_embed":  SUPPORTING,  # cosine to the node's centroid
    "f_person": SUPPORTING,  # share of co-present people bound to the node
    "f_prior":  SUPPORTING,  # base rate given app / weekday / hour bucket
}

# Hand-weighted prior, not a fit. Strong features dominate by construction;
# the supporting block is additionally capped below.
WEIGHTS = {
    "f_key":    2.60,
    "f_key_n":  0.90,
    "f_alias":  1.10,
    "f_scope":  0.85,
    "f_frame":  0.55,
    "f_graph":  0.40,
    "f_embed":  0.35,
    "f_person": 0.45,
    "f_prior":  0.25,
}
BIAS = -1.30                 # so a lone weak feature does not clear 0.5

SUPPORTING_CAP = 0.30        # share of total score supporting may contribute
TEMPERATURE = 0.7
BIND_P, BIND_MARGIN = 0.75, 0.25
ESCALATE_P = 0.45
MAX_ESCALATION_OPTIONS = 3


@dataclass(frozen=True)
class Candidate:
    """A node that strong-or-medium evidence has PROPOSED for this event."""
    node_type: str
    node_id: object
    name: str = ""
    features: dict = field(default_factory=dict)
    # Which distinct strong keys carried it here. Disjointness (§5.3) is
    # decided on this set, so it has to travel with the candidate.
    strong_keys: frozenset = frozenset()

    @property
    def key(self) -> tuple:
        return (self.node_type, self.node_id)


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    raw: float                # Σ w·f after the clamp, before the squash
    strength: float           # σ(raw) — ABSOLUTE belief this candidate is right
    p: float                  # softmax share — RELATIVE, only meaningful vs rivals
    features: dict            # the clamped feature vector actually used
    clamped: bool = False


@dataclass(frozen=True)
class Decision:
    band: str                 # deterministic|scored|scored_multi|pending|provisional|unbound
    chosen: tuple             # Candidates bound (0, 1, or 2)
    confidence: float
    margin: float
    scored: tuple             # every Scored, best first — the "why"
    method: str = ""
    escalate: tuple = ()      # options handed to the model, when band=pending

    @property
    def is_bound(self) -> bool:
        return bool(self.chosen)


def clamp_supporting(features: dict, *, cap: float = SUPPORTING_CAP,
                     weights: dict | None = None) -> tuple[dict, bool]:
    """Scale supporting features so they contribute at most `cap` of the total.

    Returns the adjusted vector and whether anything was cut. Applied per
    candidate at scoring time: a candidate carried by real keys is unaffected,
    while one floating on frame + embedding + base rate is held to a third of
    its own score no matter how many supporting signals agree. They agree
    because they are correlated, not because they are independent.
    """
    w = weights or WEIGHTS
    sup = sum(w[k] * v for k, v in features.items()
              if FEATURE_TIER[k] == SUPPORTING)
    hard = sum(w[k] * v for k, v in features.items()
               if FEATURE_TIER[k] != SUPPORTING)
    total = sup + hard
    if total <= 0 or sup <= cap * total:
        return dict(features), False
    # Largest sup' with sup' <= cap*(sup' + hard)  =>  sup' = cap*hard/(1-cap)
    allowed = (cap * hard / (1.0 - cap)) if cap < 1.0 else sup
    scale = (allowed / sup) if sup > 0 else 0.0
    out = {k: (v * scale if FEATURE_TIER[k] == SUPPORTING else v)
           for k, v in features.items()}
    return out, True


def _sigmoid(x: float) -> float:
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def score(candidates, *, weights: dict | None = None,
          temperature: float = TEMPERATURE) -> list[Scored]:
    """Score and softmax-normalize. Best first; ties broken by node key."""
    w = weights or WEIGHTS
    out: list[Scored] = []
    for c in candidates:
        feats, cut = clamp_supporting(c.features, weights=w)
        raw = BIAS + sum(w[k] * v for k, v in feats.items())
        out.append(Scored(c, raw, _sigmoid(raw), 0.0, feats, cut))
    if not out:
        return []
    t = max(1e-3, float(temperature))
    top = max(s.raw for s in out)
    exps = [math.exp((s.raw - top) / t) for s in out]
    z = sum(exps) or 1.0
    out = [replace(s, p=e / z) for s, e in zip(out, exps)]
    out.sort(key=lambda s: (-s.p, -s.strength, str(s.candidate.key)))
    return out


def disjoint_evidence(a: Scored, b: Scored) -> bool:
    """True when two candidates rest on INDEPENDENT strong keys.

    This is the test that makes multi-project binding safe. Two git remotes in
    one terminal session is multiplicity — bind both. The same ambiguous alias
    pointing two ways is ambiguity — escalate. Without the distinction the
    resolver either loses real monorepo/meeting cases or invents them.
    """
    ka, kb = a.candidate.strong_keys, b.candidate.strong_keys
    return bool(ka) and bool(kb) and not (ka & kb)


def decide(scored) -> Decision:
    """Apply the decision bands. Never raises; an empty list is `unbound`."""
    scored = list(scored)
    if not scored:
        return Decision("unbound", (), 0.0, 0.0, (), "no_candidates")
    top = scored[0]
    second = scored[1] if len(scored) > 1 else None
    margin = top.p - (second.p if second else 0.0)

    if top.strength >= BIND_P and margin >= BIND_MARGIN:
        band = "deterministic" if top.candidate.strong_keys else "scored"
        return Decision(band, (top.candidate,), top.strength, margin, tuple(scored),
                        "key" if top.candidate.strong_keys else "scored")

    if (second is not None and second.strength >= ESCALATE_P
            and disjoint_evidence(top, second)):
        # Real and common: a monorepo, a meeting covering two initiatives.
        return Decision("scored_multi", (top.candidate, second.candidate),
                        top.strength, margin, tuple(scored), "disjoint_evidence")

    if top.strength >= ESCALATE_P:
        return Decision("pending", (), top.strength, margin, tuple(scored),
                        "ambiguous",
                        tuple(s.candidate for s in scored[:MAX_ESCALATION_OPTIONS]))

    # Candidates exist but none is credible. Record it, surface nothing.
    return Decision("provisional", (), top.strength, margin, tuple(scored),
                    "weak")


def explain(decision: Decision) -> str:
    """Render the decision as the trace a user can read and correct.

    A user who can read this will forgive a wrong answer, because they can see
    why it was wrong and fix the binding. A user who cannot will distrust a
    right one.
    """
    lines = []
    if decision.chosen:
        names = " + ".join(c.name or str(c.key) for c in decision.chosen)
        lines.append(f"{names} ({decision.confidence:.2f}, {decision.band})")
    else:
        lines.append(f"unbound ({decision.band})")
    for s in decision.scored:
        c = s.candidate
        bits = ", ".join(f"{k}={v:.2f}" for k, v in sorted(s.features.items())
                         if v)
        mark = "chosen " if c in decision.chosen else "rejected"
        lines.append(f"  {mark} {c.name or c.key} "
                     f"conf={s.strength:.2f} share={s.p:.2f}"
                     + (f" [{bits}]" if bits else "")
                     + ("  (supporting clamped)" if s.clamped else ""))
    if decision.band == "pending":
        lines.append(f"  escalating among {len(decision.escalate)} options")
    return "\n".join(lines)


def train(*_a, **_k):          # pragma: no cover - Stage 3 tail
    """Where the fitted logistic model lands once labels exist.

    Deliberately unimplemented. Fitting weights against a corpus the system
    labelled itself would learn its own prior back; this needs the hand-labelled
    day the design asks for, and until then `WEIGHTS` is an honest prior rather
    than a fake fit.
    """
    raise NotImplementedError(
        "resolver.train needs a hand-labelled corpus; see docs/cal.md")


__all__ = ["Candidate", "Scored", "Decision", "WEIGHTS", "WEIGHTS_VERSION",
           "FEATURE_TIER", "SUPPORTING_CAP", "clamp_supporting", "score",
           "decide", "disjoint_evidence", "explain", "train"]
