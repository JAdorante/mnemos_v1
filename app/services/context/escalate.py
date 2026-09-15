"""CAL Stage 3 — escalation, and the ratchet.

When the resolver cannot separate its top candidates, a model breaks the tie.
Two things make that affordable.

**Constrained choice, not free generation.** The model is handed a numbered list
and must return an INDEX or null. It never invents a project, never renames one,
never returns prose. That is what makes a 7B local model sufficient and the
output trivially validatable — the difference between a 3% and a 30%
hallucination rate is mostly the shape of the answer you allow.

**The ratchet.** Every resolved escalation mints a binding, so the same
ambiguity is never paid for twice. If a model resolves `~/dev/ravenry` → Ravenry
once, that path is a key forever and the next ten thousand events touching it
exit at the binding lookup with no model in sight. This is the whole economic
thesis of the layer: cost tracks NOVEL identifiers, not events.

A minted binding is `origin='inferred'` and deliberately weaker than one a user
confirmed. It is also, by the layer rule, evidence that must never feed another
inference — the system's own guesses must not become its own evidence, or it
drifts into a confidently self-consistent fiction.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

TASK = "context_project"
MINT_STRENGTH = 0.80          # below a user confirmation (1.0) on purpose
MAX_OPTIONS = 3

# The model returns an index into the list it was given, or null. Nothing else
# is representable, so nothing else has to be validated away.
CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": ["integer", "null"],
                   "description": "0-based index of the best option, or null"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["choice"],
    "additionalProperties": False,
}

SYSTEM = (
    "You attribute one captured moment of a person's work to a project they "
    "already have. You are given a short description and a numbered list of "
    "candidate projects. Reply with JSON: the 0-based index of the single best "
    "candidate, or null if none of them is clearly right. Never invent a "
    "project, never return a name — only an index or null. Prefer null over a "
    "guess: an honest unknown is cheap and a wrong attribution is not."
)


@dataclass(frozen=True)
class Escalation:
    chosen: object | None         # a resolver.Candidate, or None
    index: int | None
    tier: str                     # local | shadow_priority | escalate
    confidence: float = 0.0
    minted: tuple = ()            # keys bound as a result — the ratchet
    error: str = ""


def prompt_for(summary: str, options, *, max_chars: int = 600) -> list:
    """The user turn: the moment, then the numbered options.

    `max_chars` is 600 for one event; an episode-level caller passes more,
    because a stretch of work is described by evidence lines, not a summary.
    """
    lines = [f"Moment: {(summary or '').strip()[:max_chars]}", "", "Candidates:"]
    for i, c in enumerate(options):
        label = getattr(c, "name", "") or str(getattr(c, "key", c))
        lines.append(f"  {i}. {label}")
    lines.append("")
    lines.append("Reply with {\"choice\": <index or null>, \"confidence\": <0-1>}.")
    return [{"role": "user", "content": "\n".join(lines)}]


def _tier_for(summary: str, confidence: float) -> str:
    """Which model tier should answer, per the existing escalation router."""
    try:
        from app.services.escalation_router import escalation_router as er
        p_fail = er.predict(TASK, summary or "", confidence=confidence)
        band = er.band(p_fail)
        # `no_model` means the router has no trained predictor for this task
        # yet — not that a model should be skipped. With no prediction, take
        # the cheap tier: an untrained router must not authorize cloud spend.
        return "local" if band in ("", "no_model", None) else band
    except Exception as exc:
        print(f"[cal.escalate] router unavailable ({exc}); staying local.")
        return "local"


def _ask(system: str, messages: list, tier: str) -> dict:
    from app.services.model_router import router
    raw = router.complete_json(
        TASK, system=system, messages=messages, schema=CHOICE_SCHEMA,
        max_tokens=64, model=("cloud" if tier == "escalate" else None))
    return raw if isinstance(raw, dict) else json.loads(raw)


def choose(summary: str, options, *, confidence: float = 0.0,
           ask=None, max_chars: int = 600) -> Escalation:
    """Pick one option, or none. `ask` is injectable so this is testable dry.

    Anything the model returns that is not a valid index into the list it was
    shown is treated as "none" rather than repaired. A repaired answer to a
    constrained question is a guess wearing a schema.
    """
    options = list(options)[:MAX_OPTIONS]
    if not options:
        return Escalation(None, None, "local", 0.0, (), "no_options")
    tier = _tier_for(summary, confidence)
    try:
        out = (ask or _ask)(SYSTEM, prompt_for(summary, options,
                                               max_chars=max_chars), tier)
    except Exception as exc:
        return Escalation(None, None, tier, 0.0, (), f"model_error: {exc}")
    idx = out.get("choice") if isinstance(out, dict) else None
    conf = float((out or {}).get("confidence") or 0.0)
    if idx is None or not isinstance(idx, int) or not 0 <= idx < len(options):
        return Escalation(None, None, tier, conf, (), "" if idx is None
                          else f"bad_index: {idx!r}")
    return Escalation(options[idx], idx, tier, conf)


def mint(store, node_type: str, node_id, keys, *, ts: float | None = None,
         strength: float = MINT_STRENGTH) -> tuple:
    """THE RATCHET — bind the keys that forced this escalation.

    Only identity-shaped keys are minted. A convention key (a bare path) that
    happened to be present is not evidence that it MEANS this node, and minting
    it would let one model call teach the graph a habit it never observed.
    """
    from app.services.context import keys as ckeys
    now = float(ts if ts is not None else time.time())
    out = []
    for sk in keys or []:
        if sk is None or sk.tier == ckeys.SUPPORTING or not sk.nameable:
            continue
        try:
            store.bind_node_key(node_type, int(node_id), sk.key_type,
                                sk.key_value, strength=min(strength, sk.strength),
                                key_class=sk.key_class, origin="inferred", ts=now)
        except Exception as exc:
            print(f"[cal.escalate] mint skipped {sk.key} ({exc}).")
            continue
        out.append(sk.key)
    return tuple(out)


def resolve(store, decision, *, summary: str, signal_keys=(), ask=None,
            ts: float | None = None) -> Escalation:
    """Escalate one ambiguous decision and ratchet the result into bindings.

    Returns an Escalation whose `minted` names the keys that will never need a
    model again. Callers run this OFF the capture path: the event is written
    `pending` and upgraded when this completes, because capture must never wait
    on inference.
    """
    if getattr(decision, "band", "") != "pending" or not decision.escalate:
        return Escalation(None, None, "local", 0.0, (), "not_escalatable")
    got = choose(summary, decision.escalate,
                 confidence=getattr(decision, "confidence", 0.0), ask=ask)
    if got.chosen is None:
        return got
    c = got.chosen
    minted = mint(store, c.node_type, c.node_id, signal_keys, ts=ts)
    return Escalation(c, got.index, got.tier, got.confidence, minted, got.error)


__all__ = ["Escalation", "CHOICE_SCHEMA", "SYSTEM", "TASK", "prompt_for",
           "choose", "mint", "resolve", "MINT_STRENGTH"]
