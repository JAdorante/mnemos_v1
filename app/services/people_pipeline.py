"""People Intelligence v2 — mention ledger, candidate resolution, contacts.

Feature-flagged via QUILL_PEOPLE_V2. When off, callers fall back to legacy
resolver.resolve_person. When on:

  * every owner/party string becomes a PersonMention
  * identity resolves to a candidate set (auto / leave_open / create_new)
  * new people start as promotion_state=candidate
  * contact points are written as evidence-linked rows

LLM is never the resolver — deterministic scoring only.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from app.services import name_quality as nq
from app.services import self_profile
from app.services import source_policy as sp
from app.services.person_details import (
    _EMAIL, _PHONE, _contact_belongs, _name_tokens, _phone_ok,
)
from app.services.resolution import _prefix_match, resolver

PIPELINE_VERSION = "people_v2.1"
EXTRACTOR_VERSION = "owner_party_v1"

# Thresholds (calibrate later on bench)
_AUTO_RESOLVE = 0.92
_AUTO_MARGIN = 0.15
_CREATE_NEW = 0.85
_CREATE_RELEVANCE = 0.55
_ATTR_MIN = 2.0


def enabled() -> bool:
    return os.getenv("QUILL_PEOPLE_V2", "0") not in ("0", "false", "False")


@dataclass
class ResolveResult:
    person_id: int | None
    decision: str  # auto_resolve|create_new|leave_open|reject|self|legacy
    mention_id: int | None = None
    confidence: float = 0.0


def resolve_person_mention(
    name: str,
    *,
    store,
    event_id: int | None = None,
    event_source: str = "",
    window: str = "",
    text: str = "",
    discourse_role: str = "unknown",
    grammatical_role: str = "unknown",
    now: float | None = None,
    relationship_boost: float = 0.6,
) -> ResolveResult:
    """Resolve a name mention under People v2 policy.

    Returns person_id or None (leave_open / reject / policy deny). Always
    persists a PersonMention when event_id is set and extract_mentions allows.
    """
    ts = now if now is not None else time.time()
    raw = (name or "").strip()
    if not raw:
        return ResolveResult(None, "reject", confidence=0.0)

    if self_profile.is_self_name(raw):
        pid = self_profile.self_person_id(store)
        mid = None
        if event_id is not None:
            mid = store.insert_person_mention(
                event_id=event_id, raw_text=raw,
                normalized_text=nq.normalize_person_name(raw) or raw,
                discourse_role=discourse_role,
                grammatical_role=grammatical_role,
                observed_at=ts,
                extractor_version=EXTRACTOR_VERSION,
                pipeline_version=PIPELINE_VERSION,
                person_probability=1.0,
                extraction_confidence=1.0,
                actor_types=[("user_self", 1.0)],
                resolution_status="resolved",
                resolved_person_id=pid,
                resolution_confidence=1.0,
                relationship_relevance=1.0,
            )
        return ResolveResult(pid, "self", mention_id=mid, confidence=1.0)

    if not enabled():
        pid = resolver.resolve_person(raw, ts=ts)
        return ResolveResult(pid, "legacy", confidence=0.5)

    policy = sp.policy_for_event(
        event_source=event_source, window=window, text=text)
    display = nq.normalize_person_name(raw) or raw

    if nq.is_os_account_name(display) or not nq.is_plausible_person(display):
        mid = None
        if event_id is not None and policy.extract_mentions:
            mid = store.insert_person_mention(
                event_id=event_id, raw_text=raw, normalized_text=display,
                discourse_role=discourse_role, grammatical_role=grammatical_role,
                observed_at=ts, extractor_version=EXTRACTOR_VERSION,
                pipeline_version=PIPELINE_VERSION,
                person_probability=0.1, extraction_confidence=0.2,
                actor_types=[("machine_user" if nq.is_os_account_name(display)
                              else "unknown_actor", 0.8)],
                resolution_status="rejected",
                relationship_relevance=0.0,
            )
        return ResolveResult(None, "reject", mention_id=mid, confidence=0.0)

    if not policy.extract_mentions:
        return ResolveResult(None, "reject", confidence=0.0)

    # --- candidate generation (also used for knowledge-only exact bind) ---
    people = store.list_people_embed()
    # Filter absorbed / hidden
    people = [p for p in people
              if not p.get("canonical_person_id")
              and not p.get("hide_from_people")]

    scored: list[tuple[dict | None, float, dict]] = []
    key = display.lower()
    for p in people:
        names = [p["name"], *(p.get("aliases") or [])]
        pos, neg, feats = 0.0, 0.0, {}
        if any(n.lower() == key for n in names):
            pos, feats["exact"] = 3.0, True
        elif any(_prefix_match(display, n) for n in names):
            pos, feats["prefix"] = 1.8, True
        else:
            # cheap token overlap
            mt = set(re.findall(r"\w{3,}", key))
            pt = set()
            for n in names:
                pt |= set(re.findall(r"\w{3,}", n.lower()))
            if mt and pt:
                j = len(mt & pt) / len(mt | pt)
                if j >= 0.5:
                    pos, feats["jaccard"] = 1.2 * j, j
        if pos <= 0:
            continue
        # Negative: different multi-token first names that only prefix-match wrongly
        # already blocked by _prefix_match.
        score = _sigmoid(pos - neg)
        scored.append((p, score, {"pos": pos, "neg": neg, **feats}))

    scored.sort(key=lambda x: -x[1])

    if not policy.create_person_candidates:
        # Knowledge-only surfaces (news / feeds): bind to EXISTING people on
        # exact match; never mint Bill-Clinton-from-TMZ as a contact.
        top = scored[0] if scored else None
        chosen: int | None = None
        decision = "reject"
        conf = 0.2
        if (top and top[0] is not None and top[2].get("exact")
                and top[1] >= _AUTO_RESOLVE):
            decision = "auto_resolve"
            chosen = int(top[0]["id"])
            store.touch_person(chosen, ts, alias=display)
            conf = float(top[1])
        mid = None
        if event_id is not None:
            mid = store.insert_person_mention(
                event_id=event_id, raw_text=raw, normalized_text=display,
                discourse_role=discourse_role, grammatical_role=grammatical_role,
                observed_at=ts, extractor_version=EXTRACTOR_VERSION,
                pipeline_version=PIPELINE_VERSION,
                person_probability=0.7, extraction_confidence=0.6,
                actor_types=([("human_person", 0.85)] if chosen
                             else [("public_figure", 0.5), ("human_person", 0.4)]),
                resolution_status=("resolved" if chosen else "rejected"),
                resolved_person_id=chosen,
                resolution_confidence=conf,
                relationship_relevance=0.15 if not chosen else float(relationship_boost),
            )
        return ResolveResult(chosen, decision, mention_id=mid, confidence=conf)

    # New-person prior: high when relationship-relevant and no strong match
    # (task owner / commitment party should mint a candidate, not leave_open).
    if not scored:
        new_score = 0.93 if relationship_boost >= _CREATE_RELEVANCE else 0.4
    else:
        new_score = max(0.15, 0.9 - (scored[0][1] * 0.7))
        # Only prefer create when existing matches are weak — not when
        # ambiguous strong candidates should leave_open for review.
        if (relationship_boost >= _CREATE_RELEVANCE
                and scored[0][1] < 0.70):
            new_score = max(new_score, 0.88)
    scored.append((None, new_score, {"new": True}))
    scored.sort(key=lambda x: -x[1])

    top = scored[0]
    second = scored[1] if len(scored) > 1 else (None, 0.0, {})
    top_score, second_score = top[1], second[1]
    margin = top_score - second_score

    decision = "leave_open"
    chosen: int | None = None
    conf = top_score
    relevance = float(relationship_boost)

    if (top[0] is not None and top_score >= _AUTO_RESOLVE
            and margin >= _AUTO_MARGIN):
        decision = "auto_resolve"
        chosen = int(top[0]["id"])
        store.touch_person(chosen, ts, alias=display)
        _bump_promotion(store, chosen, relevance, ts)
    elif (top[0] is None and top_score >= _CREATE_NEW
          and relevance >= _CREATE_RELEVANCE):
        decision = "create_new"
        chosen = store.insert_person(
            display, ts=ts,
            actor_type="human_person",
            promotion_state="candidate",
        )
    else:
        decision = "leave_open"
        chosen = None

    mid = None
    if event_id is not None:
        mid = store.insert_person_mention(
            event_id=event_id, raw_text=raw, normalized_text=display,
            discourse_role=discourse_role, grammatical_role=grammatical_role,
            observed_at=ts, extractor_version=EXTRACTOR_VERSION,
            pipeline_version=PIPELINE_VERSION,
            person_probability=0.85, extraction_confidence=0.75,
            actor_types=[("human_person", 0.85)],
            resolution_status=("resolved" if chosen else "unresolved"),
            resolved_person_id=chosen,
            resolution_confidence=conf,
            relationship_relevance=relevance,
        )
        # Persist candidates
        for rank, (p, score, feats) in enumerate(scored[:8]):
            store.insert_identity_candidate(
                mention_id=mid,
                person_id=(int(p["id"]) if p else None),
                is_new=(p is None),
                score=score,
                rank=rank,
                pos_evidence=feats,
                neg_evidence={},
                created_at=ts,
            )
        store.insert_resolution_decision(
            mention_id=mid, decision=decision,
            chosen_person_id=chosen, confidence=conf,
            threshold_policy=f"auto>={_AUTO_RESOLVE}/margin>={_AUTO_MARGIN}",
            resolver_version=PIPELINE_VERSION,
            decided_at=ts,
        )

    return ResolveResult(chosen, decision, mention_id=mid, confidence=conf)


def attribute_contacts_from_text(
    text: str,
    *,
    store,
    person_id: int | None,
    person_name: str,
    event_id: int | None,
    now: float | None = None,
    discourse_role: str = "unknown",
    event_source: str = "",
    window: str = "",
) -> list[int]:
    """Write PersonContactPoint rows when attribution is strong enough."""
    if not enabled() or not person_id or not (text or "").strip():
        return []
    # Gate only when we know the surface — callers without source (legacy /
    # tests) still attribute; news/social/terminal pass event_source+window.
    if event_source or window:
        policy = sp.policy_for_event(
            event_source=event_source, window=window, text=text)
        if not policy.extract_contacts:
            return []
    ts = now if now is not None else time.time()
    tokens = _name_tokens(person_name, [])
    created: list[int] = []

    for m in _EMAIL.finditer(text):
        email = m.group(0)
        if not _contact_belongs(
                text, kind="email", value=email,
                start=m.start(), end=m.end(), tokens=tokens):
            continue
        cid = store.upsert_contact_point(
            person_id=person_id, type_="email",
            value_display=email, value_normalized=email.lower(),
            confidence=0.85, attribution_method="possessive_or_reach_or_local",
            verification_status="attributed",
            source_event_id=event_id, evidence_quote=text[:240],
            discourse_role=discourse_role, ts=ts,
            created_by="system", pipeline_version=PIPELINE_VERSION,
        )
        if cid:
            created.append(cid)

    for m in _PHONE.finditer(text):
        phone = m.group(1)
        if not _phone_ok(phone, text):
            continue
        if not _contact_belongs(
                text, kind="phone", value=phone,
                start=m.start(), end=m.end(), tokens=tokens):
            continue
        norm = re.sub(r"[^\d+]", "", phone)
        cid = store.upsert_contact_point(
            person_id=person_id, type_="phone",
            value_display=phone.strip(), value_normalized=norm,
            confidence=0.8, attribution_method="possessive_or_reach",
            verification_status="attributed",
            source_event_id=event_id, evidence_quote=text[:240],
            discourse_role=discourse_role, ts=ts,
            created_by="system", pipeline_version=PIPELINE_VERSION,
        )
        if cid:
            created.append(cid)
    return created


def contacts_roster(store, *, limit: int = 40) -> list[dict]:
    """People the user actually knows — for 'who do I know?' grounding.

    Prefers promoted / evidence-backed rows; excludes self, absorbed, hidden,
    and flagged public figures. Pure read — never invents names.
    """
    from app.services import self_profile
    try:
        self_pid = self_profile.self_person_id(store)
    except Exception:
        self_pid = None

    # Open work parties — stronger than a news "mentioned_in" edge.
    open_party: set[int] = set()
    try:
        for kind in ("task", "commitment"):
            for f in store.list_facts(kind=kind, status="open", limit=2000):
                for key in ("owner_person_id", "from_person_id", "to_person_id"):
                    pid = f.get(key)
                    if pid is not None:
                        open_party.add(int(pid))
    except Exception:
        pass

    out: list[dict] = []
    for p in store.all_people():
        if not p or not (p.get("name") or "").strip():
            continue
        if self_pid is not None and p.get("id") == self_pid:
            continue
        if p.get("hide_from_people") or p.get("canonical_person_id"):
            continue
        if p.get("public_figure"):
            continue
        state = (p.get("promotion_state") or "candidate").lower()
        if state in ("archived",):
            continue
        pid = int(p["id"])
        evidence = state in ("active", "recognized") or pid in open_party
        if not evidence:
            try:
                attrs = store.person_attrs(pid) or {}
                evidence = bool(attrs)
            except Exception:
                pass
        if not evidence:
            try:
                cps = store.list_contact_points(pid) or []
                evidence = bool(cps)
            except Exception:
                pass
        if not evidence:
            continue
        out.append({
            "id": pid,
            "name": (p.get("name") or "").strip(),
            "promotion_state": state,
        })
        if len(out) >= limit * 3:  # gather then sort/trim
            break
    out.sort(key=lambda r: (
        0 if r["promotion_state"] in ("active", "recognized") else 1,
        r["name"].lower()))
    return out[:limit]


def agent_may_use_contact(store, person_id: int, contact_type: str) -> dict:
    """Fail-closed gate for approval-gated actions."""
    p = store.get_person(person_id)
    if not p:
        return {"allow": False, "reason": "unknown_person"}
    if p.get("hide_from_people") or p.get("canonical_person_id"):
        return {"allow": False, "reason": "hidden_or_merged"}
    state = p.get("promotion_state") or "candidate"
    if state not in ("active", "trusted"):
        return {"allow": False, "reason": f"promotion_state={state}"}
    pts = store.list_contact_points(person_id, type_=contact_type, active_only=True)
    ok = [c for c in pts
          if c.get("verification_status") in ("user_verified", "attributed")]
    if not ok:
        return {"allow": False, "reason": "no_verified_contact"}
    best = max(ok, key=lambda c: float(c.get("confidence") or 0))
    return {"allow": True, "contact": best, "person": p}


def _bump_promotion(store, person_id: int, relevance: float, ts: float) -> None:
    p = store.get_person(person_id)
    if not p:
        return
    state = p.get("promotion_state") or "candidate"
    if state in ("trusted", "archived", "rejected"):
        return
    # Commitment/task ownership or repeated resolve → active
    if state == "candidate" and relevance >= 0.7:
        store.set_person_promotion(person_id, "recognized", ts)
        state = "recognized"
    if state == "recognized" and relevance >= 0.85:
        store.set_person_promotion(person_id, "active", ts)


def _sigmoid(x: float) -> float:
    import math
    return 1.0 / (1.0 + math.exp(-x))
