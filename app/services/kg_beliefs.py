"""Knowledge Graph v2 — KG-A belief + evidence layer.

Dual-writes asserted/user relations into `kg_predicates` + `kg_evidence` so
every affiliation can answer *why* with an evidence bag. Derived rebuild
edges stay out of this store (Attention Field / constellation still use
`relations` for projection).

Also enforces `source_policy.knowledge_entities` on entity minting so news
tabs cannot freely create org/tool nodes (bind-to-existing still allowed).
"""
from __future__ import annotations

import math
import time
from typing import Any

# Source-class → evidence weight (architecture §5). User assertions dominate.
# DEFAULTS ONLY — the live table is the versioned kg_config row
# 'source_weights' (Change 4), seeded from this dict on first read so fitted
# weights can ship later without a code change.
_SOURCE_W = {
    "user": 10.0,
    "private_conversation": 2.0,
    "meeting_transcript": 2.0,
    "direct_message": 2.0,
    "email": 3.0,
    "calendar": 2.5,
    "user_authored_document": 1.8,
    "peer_answer": 1.5,   # a teammate's Mnemos said so — sturdier than a shared
                          # doc, weaker than hearing the teammate directly
    "shared_document": 1.2,
    "unknown": 1.0,
    "news_page": 0.2,
    "social_feed": 0.1,
    "browser_article": 0.3,
    "terminal": 0.0,
    "advertisement": 0.0,
}

_LAYER_FROM_ORIGIN = {
    "user": "user_verified",
    "asserted": "asserted",
    "derived": "derived",
}


# Change 3 — temporal-split-first conflict resolution knobs.
SEQ_GAP_DAYS = 14        # evidence gap that reads as "life moved on"
SPLIT_MIN_CONF = 0.6     # new belief must stand on its own before splitting
LAMBDA_CONFLICT = 0.8    # logit penalty, applied ONLY to simultaneous conflicts
# Functional predicates: at most one open object per subject at a time.
# Plan 2.5: money/date claim predicates join works_at so simultaneous
# $49 vs $55 (or two due dates) raise conflict_flag, never silent overwrite.
_FUNCTIONAL_PREDS = {"works_at", "costs", "priced_at", "due_on"}
_CLAIM_PREDICATES = frozenset({"costs", "priced_at", "due_on"})


def source_weights(store=None) -> tuple[int, dict[str, float]]:
    """(version, weights) from kg_config, seeding from _SOURCE_W once."""
    if store is not None:
        try:
            got = store.get_kg_config("source_weights")
            if got is None:
                v = store.set_kg_config("source_weights", dict(_SOURCE_W))
                return v, dict(_SOURCE_W)
            v, val = got
            if isinstance(val, dict) and val:
                return v, {k: float(x) for k, x in val.items()}
        except Exception:
            pass
    return 0, dict(_SOURCE_W)


def source_weight(source_class: str | None, *, origin: str = "asserted",
                  store=None) -> float:
    _, w = source_weights(store)
    if origin == "user":
        return float(w.get("user", 10.0))
    return float(w.get((source_class or "unknown"), 1.0))


def layer_for_origin(origin: str) -> str:
    return _LAYER_FROM_ORIGIN.get(origin, "asserted")


def record_from_relation(
    store,
    *,
    subj_type: str,
    subj_id: int,
    predicate: str,
    obj_type: str,
    obj_id: int,
    origin: str = "asserted",
    source_event_id: int | None = None,
    confidence: float | None = None,
    ts: float | None = None,
    quote: str | None = None,
    source_class: str | None = None,
    modality: str | None = None,
    fact_id: int | None = None,
    meta: dict | None = None,
) -> dict[str, Any]:
    """Dual-write one asserted/user edge into the belief store."""
    now = float(ts if ts is not None else time.time())
    layer = layer_for_origin(origin)
    # Infer source_class from event when not provided.
    if source_class is None and source_event_id:
        try:
            ev = store.get_event(int(source_event_id))
            if ev:
                src = (ev.get("source") or "") if isinstance(ev, dict) else ""
                meta_ev = ev.get("meta") if isinstance(ev, dict) else {}
                if not isinstance(meta_ev, dict):
                    meta_ev = {}
                from app.services import source_policy as sp
                pol = sp.policy_for_event(
                    event_source=src,
                    window=str(meta_ev.get("window") or ""),
                    text=(ev.get("raw") or ev.get("summary") or "")[:800]
                    if isinstance(ev, dict) else "")
                source_class = pol.source_class
                if modality is None:
                    modality = (ev.get("modality") if isinstance(ev, dict)
                                else None) or None
                if quote is None and isinstance(ev, dict):
                    quote = (ev.get("summary") or ev.get("raw") or "")[:400]
        except Exception:
            pass

    w = source_weight(source_class, origin=origin, store=store)
    if w <= 0:
        return {"ok": False, "reason": "zero_weight_source"}

    pred_id = store.upsert_kg_predicate(
        subj_type=subj_type, subj_id=subj_id, predicate=predicate,
        obj_type=obj_type, obj_id=obj_id, layer=layer,
        confidence=confidence if confidence is not None else min(0.95, 0.4 + 0.1 * w),
        ts=now)
    ev_id = store.add_kg_evidence(
        pred_id, event_id=source_event_id, fact_id=fact_id,
        modality=modality, source_class=source_class, quote=quote, weight=w,
        extractor_conf=confidence, observed_at=now,
        created_by=("user" if origin == "user" else "system"),
        meta=meta)
    # Change 5: NO posterior math on the intake path — the evidence insert
    # flipped posterior_stale; reads and the recal sweep do the math.
    conflict = resolve_conflicts(store, pred_id, now=now)
    cur = store.get_kg_predicate(pred_id) or {}
    return {"ok": True, "predicate_id": pred_id, "evidence_id": ev_id,
            "confidence": float(cur.get("confidence") or 0),
            "conflict": conflict}


def record_from_claim(
    store,
    *,
    subj_type: str,
    subj_id: int,
    predicate: str,
    obj_type: str,
    obj_id: int,
    fact_id: int | None = None,
    source_event_id: int | None = None,
    confidence: float | None = None,
    ts: float | None = None,
    quote: str | None = None,
    source_class: str | None = None,
    speaker: str | None = None,
    speaker_is_source: bool | None = None,
    modality: str | None = None,
) -> dict[str, Any]:
    """Plan 2.5 — dual-write a parseable claim into kg_beliefs.

    Evidence meta carries speaker attribution so "David said $49" is
    queryable via beliefs_by_speaker / evidence meta. Unparseable claims
    never call this (they stay flat facts only).
    """
    pred = (predicate or "").strip()
    if pred not in _CLAIM_PREDICATES:
        return {"ok": False, "reason": f"unsupported_claim_predicate:{pred}"}
    meta = {
        "speaker": (speaker or "").strip() or None,
        "speaker_is_source": bool(speaker_is_source)
        if speaker_is_source is not None else None,
        "from_claim": True,
    }
    return record_from_relation(
        store, subj_type=subj_type, subj_id=subj_id, predicate=pred,
        obj_type=obj_type, obj_id=obj_id, origin="asserted",
        source_event_id=source_event_id, confidence=confidence, ts=ts,
        quote=quote, source_class=source_class, modality=modality,
        fact_id=fact_id, meta=meta)


def beliefs_by_speaker(store, speaker: str, *, limit: int = 50) -> list[dict]:
    """Beliefs whose evidence bag attributes `speaker` (plan 2.5 AC)."""
    want = (speaker or "").strip().lower()
    if not want:
        return []
    out: list[dict] = []
    try:
        preds = store.list_kg_predicates(limit=500)
    except Exception:
        return []
    for pred in preds:
        try:
            evs = store.list_kg_evidence(int(pred["id"]), limit=50)
        except Exception:
            continue
        matched = []
        for e in evs:
            meta = e.get("meta") if isinstance(e.get("meta"), dict) else None
            if meta is None:
                raw = e.get("meta_json")
                if isinstance(raw, str) and raw.strip():
                    try:
                        import json
                        meta = json.loads(raw)
                    except Exception:
                        meta = {}
                elif isinstance(raw, dict):
                    meta = raw
                else:
                    meta = {}
            if not isinstance(meta, dict):
                continue
            spk = (meta.get("speaker") or "").strip().lower()
            if spk == want:
                matched.append(e)
        if matched:
            out.append({
                "predicate": pred,
                "evidence": matched,
                "conflict": bool(pred.get("conflict")),
            })
            if len(out) >= limit:
                break
    return out


def _evidence_features(rows: list[dict], *, now: float) -> list[dict]:
    """Per-evidence feature snapshot for kg_adjudications.features_json."""
    out = []
    for r in rows[:40]:
        out.append({
            "source_class": r.get("source_class"),
            "w": float(r.get("weight") or 0),
            "extractor_conf": r.get("extractor_conf"),
            "faithfulness": r.get("faithfulness"),
            "recency_days": round(
                max(0.0, (now - float(r.get("observed_at") or now)) / 86400.0), 2),
            "modality": r.get("modality"),
        })
    return out


def resolve_conflicts(store, new_pred_id: int,
                      *, now: float | None = None) -> dict[str, Any] | None:
    """Temporal-split-first conflict handling (Change 3).

    For functional predicates, a competing open belief is classified before
    any penalty is applied:
    - sequential (new evidence starts > SEQ_GAP_DAYS after the old belief's
      last sighting, and the new belief stands at >= SPLIT_MIN_CONF on its
      own): auto temporal split — old.valid_to = new.valid_from, superseded.
      Neither belief is penalized. Protected/user-verified old beliefs get a
      pre-filled split proposal (decision=defer) instead of the auto-apply.
    - simultaneous (overlapping evidence windows): both flagged conflict=1
      (the symmetric penalty is conditional on that flag) and adjudication is
      enqueued; "both true" clears the flags and restores posteriors.
    """
    now = float(now if now is not None else time.time())
    pred = store.get_kg_predicate(new_pred_id)
    if not pred or pred["predicate"] not in _FUNCTIONAL_PREDS:
        return None
    competitors = store.list_kg_competitors(
        subj_type=pred["subj_type"], subj_id=int(pred["subj_id"]),
        predicate=pred["predicate"], exclude_id=new_pred_id)
    if not competitors:
        return None
    # Bag scans happen only on this (rare) conflict path, never plain intake.
    new_evs = store.list_kg_evidence(new_pred_id, limit=200)
    new_first = min((float(e["observed_at"]) for e in new_evs), default=now)
    new_conf = posterior(store, new_pred_id, now=now)  # unpenalized (flag not set)
    result: dict[str, Any] = {"classified": []}
    for old in competitors:
        old_evs = store.list_kg_evidence(int(old["id"]), limit=200)
        old_last = max((float(e["observed_at"]) for e in old_evs),
                       default=float(old.get("last_seen") or 0))
        gap_days = (new_first - old_last) / 86400.0
        sequential = gap_days > SEQ_GAP_DAYS and new_conf >= SPLIT_MIN_CONF
        features = {
            "conflict_class": "sequential" if sequential else "simultaneous",
            "gap_days": round(gap_days, 2),
            "new_posterior": new_conf,
            "old_posterior": float(old.get("confidence") or 0),
            "new_evidence": _evidence_features(new_evs, now=now),
            "old_evidence": _evidence_features(old_evs, now=now),
            "old_predicate_id": int(old["id"]),
            "new_predicate_id": int(new_pred_id),
        }
        if sequential:
            trusted = bool(old.get("protected")) or \
                old.get("layer") == "user_verified"
            split_at = float(pred.get("valid_from") or new_first)
            if not trusted:
                store.supersede_kg_predicate(
                    int(old["id"]), int(new_pred_id), valid_to=split_at, ts=now)
                store.log_adjudication(
                    kind="split_accept", decision="accept", decided_by="auto",
                    features=features, predicate_id=int(old["id"]),
                    model_score=new_conf, ts=now)
                result["classified"].append(
                    {"old_id": int(old["id"]), "class": "sequential",
                     "action": "auto_split"})
            else:
                # Review-first for trusted beliefs: proposal pre-filled, no
                # penalty on either side while it waits.
                store.log_adjudication(
                    kind="split_accept", decision="defer", decided_by="auto",
                    features={**features, "proposal": {
                        "valid_to": split_at,
                        "superseded_by": int(new_pred_id)}},
                    predicate_id=int(old["id"]), model_score=new_conf, ts=now)
                result["classified"].append(
                    {"old_id": int(old["id"]), "class": "sequential",
                     "action": "split_proposed"})
        else:
            store.set_kg_predicate_conflict(int(old["id"]), True, ts=now)
            store.set_kg_predicate_conflict(int(new_pred_id), True, ts=now)
            recompute_confidence(store, int(old["id"]), now=now)
            recompute_confidence(store, int(new_pred_id), now=now)
            store.log_adjudication(
                kind="conflict_flag", decision="defer", decided_by="auto",
                features=features, predicate_id=int(new_pred_id),
                model_score=new_conf, ts=now)
            result["classified"].append(
                {"old_id": int(old["id"]), "class": "simultaneous",
                 "action": "penalized_pending_adjudication"})
    return result


def resolve_conflict_both_true(store, pred_a: int, pred_b: int, *,
                               decided_by: str = "user",
                               now: float | None = None) -> dict[str, Any]:
    """Adjudication outcome: both beliefs are genuinely true (two roles).
    Clears the conflict flags and restores unpenalized posteriors."""
    now = float(now if now is not None else time.time())
    for pid in (pred_a, pred_b):
        store.set_kg_predicate_conflict(int(pid), False, ts=now)
    confs = {int(p): recompute_confidence(store, int(p), now=now)
             for p in (pred_a, pred_b)}
    store.log_adjudication(
        kind="conflict_both_true", decision="both_true", decided_by=decided_by,
        features={"predicates": [int(pred_a), int(pred_b)],
                  "posteriors_after": confs}, predicate_id=int(pred_a), ts=now)
    return {"ok": True, "confidence": confs}


def _decay(times: list[float], now: float) -> float:
    return sum(0.002 * min(max(0.0, (now - t) / 86400.0), 365.0)
               for t in times)


def _finish(logit: float) -> float:
    conf = 1.0 / (1.0 + math.exp(-logit))
    return max(0.02, min(0.99, conf))


def recompute_confidence(store, predicate_id: int,
                         *, now: float | None = None) -> float:
    """FULL posterior recompute: scan the bag, cache the time-invariant
    Σ evidence terms (`logit_sum`) so later reads only re-apply decay
    (Change 5). Log-odds accumulate over evidence weights; clamp (0.02, 0.99)."""
    rows = store.list_kg_evidence(predicate_id, limit=200)
    pred = store.get_kg_predicate(predicate_id)
    if pred and pred.get("protected"):
        return float(pred.get("confidence") or 0.99)
    now = float(now if now is not None else time.time())
    if rows:
        # Age relative to latest observation so historical fixtures don't collapse.
        now = max(now, max(float(r.get("observed_at") or 0) for r in rows))
    wv, weights = source_weights(store)
    logit_sum = 0.0
    times: list[float] = []
    for r in rows:
        # weight==0 means user-rejected evidence (rows are never deleted).
        if float(r.get("weight") or 0) <= 0:
            continue
        # Live config weight by source class (Change 4) — the frozen row
        # weight is only the fallback for classes the table no longer names.
        sc = r.get("source_class")
        if r.get("created_by") == "user":
            w = float(weights.get("user", 10.0))
        elif sc is not None and sc in weights:
            w = float(weights[sc])
        else:
            w = float(r.get("weight") or 1.0)
        # Each evidence contributes tanh-scaled weight so piles don't saturate
        # instantly. 0.9 calibrates one strong source (email sig, w=3) to a
        # posterior ≥ SPLIT_MIN_CONF so a fresh well-evidenced belief can win
        # a temporal split on its own (Change 3).
        logit_sum += 0.9 * math.tanh(w / 3.0)
        times.append(float(r.get("observed_at") or now))
    logit = logit_sum - _decay(times, now)
    # Change 3: symmetric conflict penalty ONLY while a simultaneous conflict
    # is pending adjudication (λ_conflict · 1[simultaneous]).
    if pred and pred.get("conflict"):
        logit -= LAMBDA_CONFLICT
    conf = _finish(logit)
    try:
        store.set_kg_posterior_cache(
            predicate_id, confidence=conf, logit_sum=logit_sum,
            weights_version=wv, ts=now)
    except Exception:
        pass
    return conf


def posterior(store, predicate_id: int, *, now: float | None = None) -> float:
    """Recompute-if-stale read path (Change 5). A fresh cache only needs the
    decay term re-applied (observed_at scan via the covering index); a stale
    flag, a missing cache, or a source-weight version bump forces the full
    bag scan."""
    pred = store.get_kg_predicate(predicate_id)
    if not pred:
        return 0.0
    if pred.get("protected"):
        return float(pred.get("confidence") or 0.99)
    now = float(now if now is not None else time.time())
    wv, _ = source_weights(store)
    if pred.get("posterior_stale") or pred.get("logit_sum") is None \
            or int(pred.get("weights_version") or -1) != wv:
        return recompute_confidence(store, predicate_id, now=now)
    times = store.list_kg_evidence_times(predicate_id)
    if times:
        now = max(now, max(times))
    logit = float(pred["logit_sum"]) - _decay(times, now)
    if pred.get("conflict"):
        logit -= LAMBDA_CONFLICT
    conf = _finish(logit)
    try:
        store.set_kg_predicate_confidence(predicate_id, conf, ts=now)
    except Exception:
        pass
    return conf


def recal_sweep(store, *, limit: int = 500,
                now: float | None = None) -> dict[str, Any]:
    """Batch half of Change 5: clear posterior_stale flags in capped batches
    (the kg_confidence_recal worker job — calm pattern, boot + on demand)."""
    now = float(now if now is not None else time.time())
    ids = store.list_stale_kg_predicates(limit=limit)
    for pid in ids:
        recompute_confidence(store, pid, now=now)
    remaining = len(store.list_stale_kg_predicates(limit=1))
    return {"recomputed": len(ids), "remaining": remaining}


def evidence_verdict(store, evidence_id: int, verdict: str, *,
                     decided_by: str = "user",
                     now: float | None = None) -> dict[str, Any]:
    """Evidence-drawer confirm/reject (Change 4). Reject zeroes the weight
    (append-only rows stay); both log a frozen feature snapshot."""
    now = float(now if now is not None else time.time())
    ev = store.get_kg_evidence(int(evidence_id))
    if not ev:
        return {"ok": False, "error": "not_found"}
    pred_id = int(ev["predicate_id"])
    pred = store.get_kg_predicate(pred_id) or {}
    features = {
        "evidence": _evidence_features([ev], now=now)[0],
        "posterior_before": float(pred.get("confidence") or 0),
        "predicate": pred.get("predicate"),
        "layer": pred.get("layer"),
    }
    if verdict == "reject":
        store.set_kg_evidence_weight(int(evidence_id), 0.0)
        kind, decision = "evidence_reject", "reject"
    elif verdict == "confirm":
        kind, decision = "evidence_confirm", "accept"
    else:
        return {"ok": False, "error": "bad_verdict"}
    conf = recompute_confidence(store, pred_id, now=now)
    store.log_adjudication(
        kind=kind, decision=decision, decided_by=decided_by,
        features=features, predicate_id=pred_id,
        evidence_id=int(evidence_id),
        model_score=features["posterior_before"], ts=now)
    return {"ok": True, "confidence": conf}


def manual_split(store, *, node_type: str, node_id: int, new_name: str,
                 predicate_ids: list[int], decided_by: str = "user",
                 now: float | None = None) -> dict[str, Any]:
    """Memory Console "Split node" (Change 7). Automated split detection is
    deferred post-PMF; because evidence is append-only and merges are soft,
    a contaminated node is fully recoverable by hand: mint a fresh node
    (opaque id per Change 1), reassign the chosen beliefs (each carries its
    evidence bag), recompute both sides, log the adjudication."""
    now = float(now if now is not None else time.time())
    new_name = (new_name or "").strip()
    if not new_name or node_type not in ("person", "entity"):
        return {"ok": False, "error": "bad_request"}
    if node_type == "person":
        new_id = store.resolve_person(new_name, ts=now)
    else:
        old = store.get_entity(int(node_id)) or {}
        new_id = store.resolve_entity(new_name, old.get("kind"), ts=now)
    if not new_id or int(new_id) == int(node_id):
        return {"ok": False, "error": "target_is_source"}
    moved: list[int] = []
    for pid in predicate_ids:
        if store.reassign_kg_predicate_node(int(pid), node_type,
                                            int(node_id), int(new_id), ts=now):
            moved.append(int(pid))
    confs = {pid: recompute_confidence(store, pid, now=now) for pid in moved}
    store.log_adjudication(
        kind="split_accept", decision="accept", decided_by=decided_by,
        features={"node_type": node_type, "from_node": int(node_id),
                  "to_node": int(new_id), "new_name": new_name,
                  "moved_predicates": moved,
                  "requested": [int(p) for p in predicate_ids]},
        node_a=int(node_id), node_b=int(new_id), ts=now)
    return {"ok": True, "new_node_id": int(new_id), "moved": moved,
            "confidence": confs}


def explain_predicate(store, predicate_id: int) -> dict[str, Any]:
    """Human-readable provenance for one belief."""
    pred = store.get_kg_predicate(predicate_id)
    if not pred:
        return {"ok": False, "error": "not_found"}
    # Change 5: explain is a read trigger — recompute-if-stale first.
    posterior(store, predicate_id)
    pred = store.get_kg_predicate(predicate_id)
    evidence = store.list_kg_evidence(predicate_id, limit=40)
    subj = _label(store, pred["subj_type"], int(pred["subj_id"]))
    obj = _label(store, pred["obj_type"], int(pred["obj_id"]))
    conf = float(pred.get("confidence") or 0)
    lines = [
        f"{subj} {pred['predicate']} {obj}",
        f"Confidence {int(round(conf * 100))}% · {pred.get('layer') or 'asserted'} "
        f"· status {pred.get('status') or 'active'}",
    ]
    # Change 6: superseded beliefs cite their validity interval + successor.
    if (pred.get("status") == "superseded") or pred.get("valid_to"):
        def _d(v):
            return (time.strftime("%Y-%m-%d", time.localtime(float(v)))
                    if v else "?")
        lines.append(
            f"Valid {_d(pred.get('valid_from'))} → {_d(pred.get('valid_to'))}"
            + (f" · superseded by belief #{pred['superseded_by']}"
               if pred.get("superseded_by") else ""))
    lines.append("Why we believe this:")
    for i, e in enumerate(evidence, 1):
        when = e.get("observed_at")
        when_s = time.strftime("%Y-%m-%d", time.localtime(float(when))) if when else "?"
        src = e.get("source_class") or e.get("modality") or "observation"
        q = (e.get("quote") or "").strip().replace("\n", " ")
        bit = f"  {i}. {src} ({when_s}) — weight {float(e.get('weight') or 0):.1f}"
        if q:
            bit += f" — “{q[:160]}”"
        lines.append(bit)
    if not evidence:
        lines.append("  (no evidence rows yet — legacy dual-write pending)")
    return {
        "ok": True,
        "predicate": dict(pred),
        "subject": subj,
        "object": obj,
        "confidence": conf,
        "evidence": evidence,
        "explanation": "\n".join(lines),
    }


def explain_edge(store, *, subj_type: str, subj_id: int, predicate: str,
                 obj_type: str, obj_id: int) -> dict[str, Any]:
    row = store.find_kg_predicate(
        subj_type=subj_type, subj_id=subj_id, predicate=predicate,
        obj_type=obj_type, obj_id=obj_id)
    if not row:
        return {"ok": False, "error": "no_belief",
                "hint": "asserted/user edges dual-write on new observations"}
    return explain_predicate(store, int(row["id"]))


def _label(store, typ: str, nid: int) -> str:
    try:
        if typ == "person":
            p = store.get_person(nid)
            return (p or {}).get("name") or f"person:{nid}"
        if typ == "entity":
            e = store.get_entity(nid)
            return (e or {}).get("name") or f"entity:{nid}"
    except Exception:
        pass
    return f"{typ}:{nid}"


def allow_entity_mint(*, event_source: str = "", window: str = "",
                      text: str = "") -> tuple[bool, str]:
    """Whether extraction may CREATE new entity nodes (orgs/tools/…)."""
    from app.services import source_policy as sp
    pol = sp.policy_for_event(
        event_source=event_source, window=window, text=text)
    return bool(pol.knowledge_entities), pol.source_class
