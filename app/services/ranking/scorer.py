"""Scorers — GravityScorer and FieldV2Scorer emit ScoreBreakdown natively."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.services.ranking.config import BREAKDOWN_SUM_EPS
from app.services.ranking.types import (
    PipelineContext,
    ScoreBreakdown,
    ScoreComponent,
)


class Scorer(ABC):
    """Takes candidates + context → mutates gravity on nodes, returns breakdowns."""

    name: str = "scorer"

    @abstractmethod
    def score(
        self,
        candidates: list[dict],
        ctx: PipelineContext,
    ) -> dict[str, ScoreBreakdown]:
        """Score every candidate in place; return node_id → ScoreBreakdown."""


def _parse_id(nid: str) -> tuple[str, int] | None:
    if not nid or ":" not in nid:
        return None
    kind, _, rest = nid.partition(":")
    try:
        return kind, int(rest)
    except ValueError:
        return None


def _component_labels(n: dict, *, pinned: bool, pros: float, rel: float,
                      cent: float, temp: float, conf: float, act: float = 0.0,
                      mode_mult: float = 1.0, mode_label: str | None = None,
                      aging: float = 0.0
                      ) -> dict[str, str]:
    kind = n.get("kind") or ""
    is_open = kind in ("commitment", "task")
    labels: dict[str, str] = {}
    if pinned:
        labels["pin"] = "You pinned this"
    else:
        labels["pin"] = "Not pinned"
    if is_open:
        if pros >= 0.75:
            labels["due"] = "Overdue or due very soon"
        elif pros >= 0.55:
            labels["due"] = "Due within a week"
        elif pros >= 0.45:
            labels["due"] = "Open commitment without a due date"
        else:
            labels["due"] = "Open work, lower urgency"
    elif kind == "person" and pros >= 0.45:
        labels["due"] = "Multiple open promises tied to this person"
    else:
        labels["due"] = "No due / open-work pressure"
    if rel >= 0.45:
        labels["relationship"] = "Strong co-mention / relationship signal"
    elif rel > 0:
        labels["relationship"] = "Some relationship signal"
    else:
        labels["relationship"] = "No relationship signal"
    deg = int(round((cent * 5.0) ** 2)) if cent > 0 else 0
    if cent >= 0.5:
        labels["centrality"] = f"Highly connected ({deg} link-weight)"
    elif cent > 0:
        labels["centrality"] = f"Moderately connected ({deg} link-weight)"
    else:
        labels["centrality"] = "Low connectivity"
    age = float(n.get("_age") or 0)
    if age < 1.5:
        labels["recency"] = "Recently appeared"
    elif age < 7:
        labels["recency"] = "Seen this week"
    else:
        labels["recency"] = f"Last activity ~{int(age)} days ago"
    kind_labels = {
        "person": "Person (high kind weight)",
        "project": "Project",
        "org": "Org",
        "tool": "Tool",
        "task": "Open task",
        "commitment": "Open commitment",
        "place": "Place",
        "idea": "Idea",
    }
    labels["kind"] = kind_labels.get(kind, f"Kind: {kind}")
    if conf < 0.35:
        labels["confidence_gate"] = "Low extraction confidence (gated)"
    elif conf < 0.5:
        labels["confidence_gate"] = "Moderate confidence"
    else:
        labels["confidence_gate"] = "Trusted confidence"
    if act >= 0.15:
        labels["activation"] = "Lit by what you're doing right now"
    elif act > 0:
        labels["activation"] = "Weak context activation"
    else:
        labels["activation"] = "No context activation"
    if aging >= 0.5:
        labels["aging"] = f"Open for {int(age)} days — gaining attention"
    elif aging > 0:
        labels["aging"] = "Starting to age without follow-through"
    else:
        labels["aging"] = "Not aging"
    if mode_mult != 1.0 and mode_label:
        labels["context"] = f"Ranking for: {mode_label}"
    elif mode_mult != 1.0:
        labels["context"] = "Mode reweight applied"
    return labels


def _evidence_for(key: str, n: dict) -> tuple[list[str], str | None]:
    """Return (refs, evidence marker). Structural signals mark evidence=none."""
    meta = n.get("meta") or {}
    refs = list(meta.get("evidence_refs") or [])
    if key == "due":
        fid = n.get("id") if (n.get("kind") in ("task", "commitment")) else None
        due_refs = list(meta.get("due_evidence") or [])
        if fid:
            due_refs = due_refs or [fid]
        return due_refs, (None if due_refs else "none")
    if key == "relationship":
        rel_refs = list(meta.get("relationship_evidence") or [])
        return rel_refs, (None if rel_refs else "none")
    if key == "pin":
        return ([n["id"]] if n.get("pinned") else []), (
            None if n.get("pinned") else "none")
    if key in ("centrality", "recency", "kind", "confidence_gate", "context"):
        return refs if refs else [], "none"
    if key == "activation":
        act_refs = list(meta.get("activation_evidence") or [])
        return act_refs, (None if act_refs else "none")
    if key == "aging":
        fid = n.get("id") if (n.get("kind") in ("task", "commitment")) else None
        return ([fid] if fid else []), (None if fid else "none")
    return [], "none"


def _scale_components_to_total(
    contributions: list[tuple[str, float]],
    total: float,
    labels: dict[str, str],
    n: dict,
) -> list[ScoreComponent]:
    """Build components whose values sum to `total` (proportional to |contrib|)."""
    # Keep only nonzero (or pin when pinned) for a quiet UI; always keep
    # structure for golden tests by including all keys that had a contribution
    # slot defined — zeros are fine and sum cleanly.
    abs_sum = sum(abs(v) for _, v in contributions)
    comps: list[ScoreComponent] = []
    if abs_sum <= 1e-12:
        # Degenerate: put everything in kind.
        refs, ev = _evidence_for("kind", n)
        comps.append(ScoreComponent(
            key="kind", label=labels.get("kind", "Present"),
            value=float(total), evidence_refs=refs, evidence=ev,
        ))
        return comps
    scale = float(total) / abs_sum
    # Preserve sign of contribution while scaling magnitudes to sum to total.
    # Using signed values: sum(v * (total/sum(v))) only works if all same sign.
    # Use: value_i = sign(c_i) * |c_i| / abs_sum * total  → sum of values = total
    # only when all contributions have the same sign. For mixed signs:
    # value_i = c_i / sum(c) * total when sum(c) != 0.
    signed_sum = sum(v for _, v in contributions)
    if abs(signed_sum) > 1e-12:
        scale = float(total) / signed_sum
        for key, raw_v in contributions:
            val = raw_v * scale
            refs, ev = _evidence_for(key, n)
            comps.append(ScoreComponent(
                key=key,  # type: ignore[arg-type]
                label=labels.get(key, key),
                value=val,
                evidence_refs=refs,
                evidence=ev,
            ))
    else:
        # Cancelled contributions — distribute by absolute mass.
        for key, raw_v in contributions:
            val = (abs(raw_v) / abs_sum) * float(total)
            refs, ev = _evidence_for(key, n)
            comps.append(ScoreComponent(
                key=key,  # type: ignore[arg-type]
                label=labels.get(key, key),
                value=val,
                evidence_refs=refs,
                evidence=ev,
            ))
    return comps


def _apply_mode(n: dict, gravity: float, mode: dict | None
                ) -> tuple[float, float, str | None]:
    """Return (adjusted_gravity, multiplier, mode_label)."""
    if not mode:
        return gravity, 1.0, None
    mults = mode.get("kind_multipliers") or {}
    kind = n.get("kind") or ""
    m = float(mults.get(kind, 1.0))
    g = gravity * m
    if mode.get("quiet") and not n.get("pinned"):
        risk = float(n.get("prospective_risk") or 0.0)
        if risk < 0.75:
            g *= 0.25
            m *= 0.25
    return g, m, mode.get("label")


def _features_from_candidate(n: dict) -> dict[str, float]:
    """Read feature terms already attached by constellation (or fixtures)."""
    return {
        "pros": float(n.get("_feat_pros") if "_feat_pros" in n
                      else n.get("prospective_risk") or 0.0),
        "rel": float(n.get("_feat_rel") if "_feat_rel" in n
                     else n.get("relationship_strength") or 0.0),
        "fut": float(n.get("_feat_fut") or 0.0),
        "unres": float(n.get("_feat_unres") or 0.0),
        "cent": float(n.get("_feat_cent") or 0.0),
        "sem": float(n.get("_feat_sem") or 0.0),
        "rep": float(n.get("_feat_rep") or 0.0),
        "temp": float(n.get("_feat_temp") if "_feat_temp" in n
                      else n.get("recency") or 0.0),
        "act": float(n.get("_feat_act") or 0.0),
        "b": float(n.get("_feat_b") or 0.0),
        "v": float(n.get("_feat_v") or 0.0),
        "aging": float(n.get("_feat_aging") or 0.0),
    }


class GravityScorer(Scorer):
    """Shipped Memory Gravity heuristics — emits breakdown components."""

    name = "gravity"

    def score(
        self,
        candidates: list[dict],
        ctx: PipelineContext,
    ) -> dict[str, ScoreBreakdown]:
        from app.services.graph import (
            GRAVITY,
            score_gravity,
            temporal_salience,
        )

        w = GRAVITY["w"]
        out: dict[str, ScoreBreakdown] = {}
        mode = ctx.mode

        for n in candidates:
            pinned = bool(n.get("pinned"))
            conf = float(n.get("confidence") or 0.5)
            age = float(n.get("_age") if "_age" in n else 0.0)
            if "_age" not in n and n.get("ts") and ctx.now:
                age = max(0.0, (float(ctx.now) - float(n["ts"])) / 86400.0)
                n["_age"] = age
            feat = _features_from_candidate(n)
            # Prefer explicit feat temp; else recompute temporal salience.
            temp = feat["temp"] if "_feat_temp" in n else temporal_salience(age)
            pros, rel = feat["pros"], feat["rel"]
            fut, unres = feat["fut"], feat["unres"]
            cent, sem, rep = feat["cent"], feat["sem"], feat["rep"]
            if "_feat_aging" in n:
                aging = feat["aging"]
            else:
                from app.services.field_history import aging_signal
                aging = aging_signal(age, kind=n.get("kind") or "")
                n["_feat_aging"] = aging

            scored = score_gravity(
                kind=n.get("kind") or "idea",
                confidence=conf,
                age_days=age,
                pinned=pinned,
                prospective=pros,
                relationship=rel,
                future=fut,
                unresolved=unres,
                centrality=cent,
                semantic=sem if sem else (
                    0.55 if n.get("kind") == "person" else 0.35),
                repeats=rep,
            )
            # Fold aging into raw after score_gravity so g1 stays the replay
            # anchor without aging; live total includes it.
            g1_base = float(scored["gravity"])
            is_pin = 1.0 if pinned else 0.0
            unc = 1.0 - max(0.05, min(1.0, conf))
            sem_eff = sem if sem else (
                0.55 if n.get("kind") == "person" else 0.35)
            aging_w = float(w.get("aging", 0.95))
            # Recompute gravity with aging term in raw.
            # Open commitments resist decay as they age — follow-through
            # must not fade the longer a promise sits open.
            decay = float(scored["decay"])
            trust = float(scored["trust"])
            if aging > 0 and (n.get("kind") in ("task", "commitment")):
                decay = decay + (1.0 - decay) * min(1.0, aging * 1.15)
            raw_with_aging = float(scored["raw"]) + aging_w * aging
            from app.services.graph import _sigmoid, GRAVITY as _G
            g1 = (_sigmoid(raw_with_aging - _G["sigmoid_offset"])
                  * decay * trust)
            # Additive contributions to raw (pre-sigmoid) — used for explain.
            contributions: list[tuple[str, float]] = [
                ("pin", w["pin"] * is_pin),
                ("due", w["pros"] * pros + w["fut"] * fut + w["unres"] * unres),
                ("relationship", w["rel"] * rel),
                ("centrality", w["cent"] * cent + w["rep"] * rep),
                ("recency", w["temp"] * temp),
                ("kind", w["sem"] * sem_eff),
                ("confidence_gate", -w["unc"] * unc),
            ]
            if aging > 1e-9:
                contributions.append(("aging", aging_w * aging))
            # Drop near-zero for quieter breakdowns but keep pin if pinned.
            contributions = [
                (k, v) for k, v in contributions
                if abs(v) > 1e-9 or (k == "pin" and pinned)
            ]
            if not contributions:
                contributions = [("kind", 1.0)]

            g_adj, mode_mult, mode_label = _apply_mode(n, g1, mode)
            labels = _component_labels(
                n, pinned=pinned, pros=pros, rel=rel, cent=cent,
                temp=temp, conf=conf, mode_mult=mode_mult,
                mode_label=mode_label, aging=aging)
            if abs(mode_mult - 1.0) > 1e-9:
                comps = _scale_components_to_total(
                    contributions, g1, labels, n)
                ctx_refs, ctx_ev = _evidence_for("context", n)
                comps.append(ScoreComponent(
                    key="context",
                    label=labels.get("context", "Mode reweight"),
                    value=float(g_adj - g1),
                    evidence_refs=ctx_refs,
                    evidence=ctx_ev,
                ))
            else:
                comps = _scale_components_to_total(
                    contributions, g_adj, labels, n)

            total = round(g_adj, 4)
            # Re-normalize tiny float drift.
            s = sum(c.value for c in comps)
            if comps and abs(s - total) > BREAKDOWN_SUM_EPS:
                comps[-1].value += (total - s)

            n["gravity"] = total
            n["prominence"] = round(min(1.9, 0.4 + total * 1.5), 3)
            n["memory_strength"] = round(
                float(scored["decay"]) * conf, 3)
            n["relationship_strength"] = round(rel, 3)
            n["prospective_risk"] = round(pros, 3)
            n["aging"] = round(aging, 3)
            n["age_days"] = round(age, 2)

            # Shadow (logged, not ranked) for ledger / I-5 continuity.
            b_val = v_val = shadow = None
            act_val = float(feat["act"] or 0.0)
            try:
                from app.services import traces as _traces
                import json as _json
                parsed = _parse_id(n["id"]) or ("", 0)
                dyn = (ctx.dyn_map or {}).get(parsed) if ctx.dyn_map else None
                now = float(ctx.now or 0.0)
                if dyn:
                    recent = _json.loads(dyn.get("access_recent") or "[]")
                    n_old = int(dyn.get("access_n_older") or 0)
                    t_old = dyn.get("access_t_older")
                    v_val = float(dyn.get("V") or _traces.V_DEFAULT)
                else:
                    recent = [n["ts"]] if n.get("ts") else (
                        [now - age * 86400.0] if now else [])
                    n_old, t_old = 0, None
                    v_val = _traces.v_seed(
                        n.get("kind") or "idea", pinned=pinned)
                b_val = _traces.b_hat(
                    _traces.base_level(recent, n_old, t_old, now or 1.0))
                act_val = min(
                    1.0,
                    float((ctx.act_map or {}).get(parsed, act_val)))
                shadow = _traces.shadow_score(
                    kind=n.get("kind") or "idea",
                    confidence=conf, age_days=age, pinned=pinned,
                    prospective=pros, relationship=rel, future=fut,
                    unresolved=unres, centrality=cent, repeats=rep,
                    b=b_val, v=v_val)
            except Exception:
                pass

            n["_decomp"] = {
                "pin": round(is_pin, 3),
                "pros": round(pros, 4),
                "rel": round(rel, 4),
                "fut": round(fut, 4),
                "unres": round(unres, 4),
                "cent": round(cent, 4),
                "sem": round(sem_eff, 4),
                "rep": round(rep, 4),
                "temp": round(temp, 4),
                "unc": round(unc, 4),
                "decay": round(float(scored["decay"]), 4),
                "trust": round(float(scored["trust"]), 4),
                "raw": round(float(scored["raw"]), 4),
                "conf": round(conf, 4),
                "age_days": round(age, 2),
                "g1": round(g1, 4),
                "act": round(act_val, 4) if act_val else 0.0,
                "v2": None,
                "shadow": round(shadow, 4) if shadow is not None else None,
                "B": round(b_val, 4) if b_val is not None else None,
                "V": round(v_val, 4) if v_val is not None else None,
            }
            n["_mode_mult"] = mode_mult
            bd = ScoreBreakdown(
                node_id=n["id"], total=total, components=comps,
                admitted_by="pin" if pinned else "score",
            )
            n["_breakdown"] = bd
            out[n["id"]] = bd

        candidates.sort(
            key=lambda x: (-int(bool(x.get("pinned"))),
                           -float(x.get("gravity") or 0)))
        return out


class FieldV2Scorer(Scorer):
    """Traces + spreading activation — same pipeline, different score."""

    name = "field_v2"

    def score(
        self,
        candidates: list[dict],
        ctx: PipelineContext,
    ) -> dict[str, ScoreBreakdown]:
        from app.services import traces as _traces
        from app.services.graph import GRAVITY, score_gravity

        w = GRAVITY["w"]
        out: dict[str, ScoreBreakdown] = {}
        mode = ctx.mode
        act_map = ctx.act_map or {}
        dyn_map = ctx.dyn_map or {}
        learned_w = ctx.learned_w
        now = float(ctx.now or 0.0)

        for n in candidates:
            pinned = bool(n.get("pinned"))
            conf = float(n.get("confidence") or 0.5)
            age = float(n.get("_age") if "_age" in n else 0.0)
            feat = _features_from_candidate(n)
            pros, rel = feat["pros"], feat["rel"]
            fut, unres = feat["fut"], feat["unres"]
            cent, rep = feat["cent"], feat["rep"]
            sem = feat["sem"]

            # Always compute g1 for ledger / replay anchor.
            scored = score_gravity(
                kind=n.get("kind") or "idea",
                confidence=conf,
                age_days=age,
                pinned=pinned,
                prospective=pros,
                relationship=rel,
                future=fut,
                unresolved=unres,
                centrality=cent,
                semantic=sem if sem else 0.35,
                repeats=rep,
            )
            g1 = float(scored["gravity"])

            parsed = _parse_id(n["id"]) or ("", 0)
            dyn = dyn_map.get(parsed) if dyn_map else None
            import json as _json
            if dyn:
                recent = _json.loads(dyn.get("access_recent") or "[]")
                n_old = int(dyn.get("access_n_older") or 0)
                t_old = dyn.get("access_t_older")
                v_val = float(dyn.get("V") or _traces.V_DEFAULT)
            else:
                recent = [n["ts"]] if n.get("ts") else (
                    [now - age * 86400.0] if now else [])
                n_old, t_old = 0, None
                v_val = _traces.v_seed(n.get("kind") or "idea", pinned=pinned)
            b_val = _traces.b_hat(
                _traces.base_level(recent, n_old, t_old, now or 1.0))
            act_val = min(1.0, float(act_map.get(parsed, feat["act"])))

            shadow = _traces.shadow_score(
                kind=n.get("kind") or "idea",
                confidence=conf,
                age_days=age,
                pinned=pinned,
                prospective=pros,
                relationship=rel,
                future=fut,
                unresolved=unres,
                centrality=cent,
                repeats=rep,
                b=b_val,
                v=v_val,
            )
            v2 = _traces.shadow_score(
                kind=n.get("kind") or "idea",
                confidence=conf,
                age_days=age,
                pinned=pinned,
                prospective=pros,
                relationship=rel,
                future=fut,
                unresolved=unres,
                centrality=cent,
                repeats=rep,
                b=b_val,
                v=v_val,
                act=act_val,
                w=learned_w,
            )

            is_pin = 1.0 if pinned else 0.0
            unc = 1.0 - max(0.05, min(1.0, conf))
            if "_feat_aging" in n:
                aging = feat["aging"]
            else:
                from app.services.field_history import aging_signal
                aging = aging_signal(age, kind=n.get("kind") or "")
                n["_feat_aging"] = aging
            aging_w = float(w.get("aging", 0.95))
            # Open commitments resist decay + gain an aging boost (same as Gravity).
            decay = float(scored["decay"])
            trust = float(scored["trust"])
            if aging > 0 and (n.get("kind") in ("task", "commitment")):
                decay = decay + (1.0 - decay) * min(1.0, aging * 1.15)
            from app.services.graph import _sigmoid, GRAVITY as _G
            # Rebuild v2 with aging in the raw channel.
            ww = learned_w or w
            raw_v2 = (
                ww.get("pin", 1.35) * is_pin
                + ww.get("pros", 1.55) * pros
                + ww.get("rel", 1.15) * rel
                + ww.get("fut", 0.95) * fut
                + ww.get("unres", 0.85) * unres
                + ww.get("cent", 0.70) * cent
                + ww.get("sem", 0.55) * v_val
                + ww.get("rep", 0.45) * rep
                + ww.get("temp", 0.70) * b_val
                + ww.get("act", 0.90) * act_val
                + aging_w * aging
                - ww.get("unc", 0.80) * unc
            )
            v2_live = _sigmoid(raw_v2 - _G["sigmoid_offset"]) * decay * trust

            contributions: list[tuple[str, float]] = [
                ("pin", w["pin"] * is_pin),
                ("due", w["pros"] * pros + w["fut"] * fut + w["unres"] * unres),
                ("relationship", w["rel"] * rel),
                ("centrality", w["cent"] * cent + w["rep"] * rep),
                ("recency", w["temp"] * b_val),
                ("kind", w["sem"] * v_val),
                ("confidence_gate", -w["unc"] * unc),
                ("activation", w["act"] * act_val),
            ]
            if aging > 1e-9:
                contributions.append(("aging", aging_w * aging))
            contributions = [
                (k, v) for k, v in contributions
                if abs(v) > 1e-9 or (k == "pin" and pinned)
                or (k == "activation" and act_val > 0)
            ]
            if not contributions:
                contributions = [("kind", 1.0)]

            g_adj, mode_mult, mode_label = _apply_mode(n, v2_live, mode)
            labels = _component_labels(
                n, pinned=pinned, pros=pros, rel=rel, cent=cent,
                temp=b_val, conf=conf, act=act_val,
                mode_mult=mode_mult, mode_label=mode_label, aging=aging)

            if abs(mode_mult - 1.0) > 1e-9:
                comps = _scale_components_to_total(
                    contributions, v2_live, labels, n)
                ctx_refs, ctx_ev = _evidence_for("context", n)
                comps.append(ScoreComponent(
                    key="context",
                    label=labels.get("context", "Mode reweight"),
                    value=float(g_adj - v2_live),
                    evidence_refs=ctx_refs,
                    evidence=ctx_ev,
                ))
            else:
                comps = _scale_components_to_total(
                    contributions, g_adj, labels, n)

            total = round(g_adj, 4)
            s = sum(c.value for c in comps)
            if comps and abs(s - total) > BREAKDOWN_SUM_EPS:
                comps[-1].value += (total - s)

            n["gravity"] = total
            n["prominence"] = round(min(1.9, 0.4 + total * 1.5), 3)
            n["memory_strength"] = round(float(scored["decay"]) * conf, 3)
            n["relationship_strength"] = round(rel, 3)
            n["prospective_risk"] = round(pros, 3)
            n["aging"] = round(aging, 3)
            n["age_days"] = round(age, 2)
            if act_val >= 0.15:
                why = list(n.get("why") or [])
                n["why"] = (["Lit by what you're doing right now"] + why)[:3]
            n["_decomp"] = {
                "pin": round(is_pin, 3),
                "pros": round(pros, 4),
                "rel": round(rel, 4),
                "fut": round(fut, 4),
                "unres": round(unres, 4),
                "cent": round(cent, 4),
                "sem": round(sem, 4),
                "rep": round(rep, 4),
                "temp": round(float(scored["temporal"]), 4),
                "unc": round(unc, 4),
                "decay": round(float(scored["decay"]), 4),
                "trust": round(float(scored["trust"]), 4),
                "raw": round(float(scored["raw"]), 4),
                "conf": round(conf, 4),
                "age_days": round(age, 2),
                "B": round(b_val, 4),
                "V": round(v_val, 4),
                "shadow": round(shadow, 4),
                "act": round(act_val, 4),
                "v2": round(v2, 4),
                "g1": round(g1, 4),
            }
            n["_mode_mult"] = mode_mult
            bd = ScoreBreakdown(
                node_id=n["id"], total=total, components=comps,
                admitted_by="pin" if pinned else "score",
            )
            n["_breakdown"] = bd
            out[n["id"]] = bd

        candidates.sort(
            key=lambda x: (-int(bool(x.get("pinned"))),
                           -float(x.get("gravity") or 0)))
        return out


def get_scorer(*, field_v2: bool | None = None) -> Scorer:
    """Factory: QUILL_FIELD_V2 selects Scorer implementation only."""
    if field_v2 is None:
        try:
            from app.config import settings
            field_v2 = bool(settings.attention.field_v2)
        except Exception:
            field_v2 = False
    return FieldV2Scorer() if field_v2 else GravityScorer()
