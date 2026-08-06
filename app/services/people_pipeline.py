"""People Intelligence v2 — mention ledger, candidate resolution, contacts.

Feature-flagged via QUILL_PEOPLE_V2 (code default ON after plan 2.3 golden
gate). When off (`QUILL_PEOPLE_V2=0`), callers fall back to the legacy
`resolution.resolver.resolve_person` path — kept for one release as the
kill-switch / rollback. When on:

  * every owner/party string becomes a PersonMention
  * identity resolves to a candidate set (auto / leave_open / create_new)
  * new people start as promotion_state=candidate
  * contact points are written as evidence-linked rows

LLM is never the resolver — deterministic scoring only.

Thresholds are calibrated by `scripts/eval_entity_resolution.py` against
`tests/fixtures/goldens/entity_resolution.jsonl` (merge-error ≈ 0). Override
with QUILL_PEOPLE_AUTO_RESOLVE / QUILL_PEOPLE_AUTO_MARGIN /
QUILL_PEOPLE_CREATE_NEW for experiments; do not loosen without re-running
the golden gate.
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
    ATTR_MIN, _EMAIL, _PHONE, _name_tokens, _phone_ok,
    contact_attribution_score,
)
from app.services.resolution import _prefix_match, resolver

PIPELINE_VERSION = "people_v2.1"
EXTRACTOR_VERSION = "owner_party_v1"

# Thresholds — calibrated on entity_resolution golden (plan 2.3).
# Env overrides: QUILL_PEOPLE_AUTO_RESOLVE / _AUTO_MARGIN / _CREATE_NEW.
_AUTO_RESOLVE = 0.92
_AUTO_MARGIN = 0.15
_CREATE_NEW = 0.85
_CREATE_RELEVANCE = 0.55
_ATTR_MIN = ATTR_MIN  # plan 2.4 — auto-write floor; below → review


def _thr_auto_resolve() -> float:
    return float(os.getenv("QUILL_PEOPLE_AUTO_RESOLVE", str(_AUTO_RESOLVE)))


def _thr_auto_margin() -> float:
    return float(os.getenv("QUILL_PEOPLE_AUTO_MARGIN", str(_AUTO_MARGIN)))


def _thr_create_new() -> float:
    return float(os.getenv("QUILL_PEOPLE_CREATE_NEW", str(_CREATE_NEW)))


def enabled() -> bool:
    # Plan 2.3: code default ON after merge-error ≈ 0 on entity-resolution golden.
    # Kill-switch: QUILL_PEOPLE_V2=0 → legacy resolver.resolve_person.
    return os.getenv("QUILL_PEOPLE_V2", "1") not in ("0", "false", "False")


def score_person_candidates(display: str, people: list[dict]) -> list[tuple[dict | None, float, dict]]:
    """Deterministic candidate scores for a mention against a people roster.

    Pure scoring — no DB writes. Used by resolve_person_mention and the
    plan 2.3 threshold-sweep eval.
    """
    scored: list[tuple[dict | None, float, dict]] = []
    key = (display or "").lower()
    for p in people:
        names = [p["name"], *(p.get("aliases") or [])]
        pos, neg, feats = 0.0, 0.0, {}
        if any(n.lower() == key for n in names):
            pos, feats["exact"] = 3.0, True
        elif any(_prefix_match(display, n) for n in names):
            pos, feats["prefix"] = 1.8, True
        else:
            mt = set(re.findall(r"\w{3,}", key))
            pt: set[str] = set()
            for n in names:
                pt |= set(re.findall(r"\w{3,}", n.lower()))
            if mt and pt:
                j = len(mt & pt) / len(mt | pt)
                if j >= 0.5:
                    pos, feats["jaccard"] = 1.2 * j, j
        if pos <= 0:
            continue
        score = _sigmoid(pos - neg)
        scored.append((p, score, {"pos": pos, "neg": neg, **feats}))
    scored.sort(key=lambda x: -x[1])
    return scored


def _attendee_name_match(mention: str, attendee_name: str) -> bool:
    """First-name or full-name match between a spoken mention and invite CN."""
    m = (mention or "").strip().lower()
    a = (attendee_name or "").strip().lower()
    if not m or not a:
        return False
    if m == a or _prefix_match(mention, attendee_name) or _prefix_match(attendee_name, mention):
        return True
    m_toks = re.findall(r"\w+", m)
    a_toks = re.findall(r"\w+", a)
    if not m_toks or not a_toks:
        return False
    # "Sarah" ↔ "Sarah Chen"
    if len(m_toks) == 1 and m_toks[0] == a_toks[0]:
        return True
    return m_toks[0] == a_toks[0] and (
        len(m_toks) == 1 or m_toks[-1] == a_toks[-1])


def matching_attendees(mention: str, attendees: list[dict] | None) -> list[dict]:
    """Attendees whose CN/email local-part matches the spoken mention."""
    out: list[dict] = []
    for a in attendees or []:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or ""
        email = (a.get("email") or "").strip().lower()
        local = email.split("@", 1)[0] if email else ""
        if _attendee_name_match(mention, name):
            out.append(a)
        elif local and _attendee_name_match(mention, local.replace(".", " ")):
            out.append(a)
    return out


def apply_attendee_boosts(
    display: str,
    scored: list[tuple[dict | None, float, dict]],
    people: list[dict],
    attendees: list[dict] | None,
    store,
) -> list[tuple[dict | None, float, dict]]:
    """Boost / inject roster candidates that match calendar attendees.

    An in-attendee-list first name with a known email is near-conclusive
    identity evidence (Meeting Layer P1 resolution prior).
    """
    matched = matching_attendees(display, attendees)
    if not matched:
        return scored

    by_id = {int(p["id"]): p for p in people if p.get("id") is not None}
    score_map: dict[int, tuple[dict | None, float, dict]] = {}
    for p, sc, feats in scored:
        if p is not None and p.get("id") is not None:
            score_map[int(p["id"])] = (p, sc, dict(feats))

    for att in matched:
        email = (att.get("email") or "").strip().lower()
        pid: int | None = None
        if email:
            try:
                pid = store.find_person_by_contact("email", email)
            except Exception:
                pid = None
        # Fall back to roster name match against the invite CN.
        if pid is None and att.get("name"):
            an = (att.get("name") or "").strip().lower()
            for p in people:
                names = [p["name"], *(p.get("aliases") or [])]
                if any(n.lower() == an for n in names) or any(
                        _prefix_match(att.get("name") or "", n) for n in names):
                    pid = int(p["id"])
                    break
        if pid is None or pid not in by_id:
            continue
        p = by_id[pid]
        prev = score_map.get(pid)
        # Strong prior: treat as near-exact when email-backed.
        pos = 4.5 if email else 3.5
        feats = {"attendee": True, "attendee_email": bool(email),
                 "pos": pos, "neg": 0.0}
        if prev:
            feats = {**prev[2], **feats, "pos": max(prev[2].get("pos", 0), pos)}
        score_map[pid] = (p, _sigmoid(pos), feats)

    out = list(score_map.values())
    # Keep any unscored non-person rows (shouldn't happen) + resorted.
    for p, sc, feats in scored:
        if p is None or p.get("id") is None:
            out.append((p, sc, feats))
        elif int(p["id"]) not in score_map:
            out.append((p, sc, feats))
    # Dedup by person id
    seen: set[int] = set()
    deduped: list[tuple[dict | None, float, dict]] = []
    for p, sc, feats in sorted(out, key=lambda x: -x[1]):
        if p is None:
            deduped.append((p, sc, feats))
            continue
        pid = int(p["id"])
        if pid in seen:
            continue
        seen.add(pid)
        deduped.append((p, sc, feats))
    return deduped


def decide_from_scores(
    scored: list[tuple[dict | None, float, dict]],
    *,
    relationship_boost: float,
    create_person_candidates: bool,
    auto_resolve: float | None = None,
    auto_margin: float | None = None,
    create_new: float | None = None,
) -> tuple[str, dict | None, float]:
    """Apply threshold policy to precomputed scores.

    Returns (decision, chosen_person_row_or_None, confidence).
    `chosen_person_row` is None for create_new / leave_open / reject.
    """
    auto_t = _thr_auto_resolve() if auto_resolve is None else float(auto_resolve)
    margin_t = _thr_auto_margin() if auto_margin is None else float(auto_margin)
    create_t = _thr_create_new() if create_new is None else float(create_new)
    relevance = float(relationship_boost)

    if not create_person_candidates:
        top = scored[0] if scored else None
        if (top and top[0] is not None and top[2].get("exact")
                and top[1] >= auto_t):
            return "auto_resolve", top[0], float(top[1])
        return "reject", None, 0.2

    work = list(scored)
    if not work:
        new_score = 0.93 if relevance >= _CREATE_RELEVANCE else 0.4
    else:
        new_score = max(0.15, 0.9 - (work[0][1] * 0.7))
        if relevance >= _CREATE_RELEVANCE and work[0][1] < 0.70:
            new_score = max(new_score, 0.88)
    work.append((None, new_score, {"new": True}))
    work.sort(key=lambda x: -x[1])

    top = work[0]
    second = work[1] if len(work) > 1 else (None, 0.0, {})
    top_score, second_score = top[1], second[1]
    margin = top_score - second_score
    conf = top_score

    if (top[0] is not None and top_score >= auto_t and margin >= margin_t):
        return "auto_resolve", top[0], conf
    if (top[0] is None and top_score >= create_t
            and relevance >= _CREATE_RELEVANCE):
        return "create_new", None, conf
    return "leave_open", None, conf


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
    attendee_priors: list[dict] | None = None,
) -> ResolveResult:
    """Resolve a name mention under People v2 policy.

    Returns person_id or None (leave_open / reject / policy deny). Always
    persists a PersonMention when event_id is set and extract_mentions allows.

    `attendee_priors`: optional calendar invitees [{name, email}, ...] from a
    calendar-linked session (Meeting Layer P1). Matching attendees boost
    existing people (email-backed ≈ conclusive) and suppress create_new.
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

    scored = score_person_candidates(display, people)
    if attendee_priors:
        scored = apply_attendee_boosts(
            display, scored, people, attendee_priors, store)
        # Invite match raises relevance so create_new is less attractive when
        # an existing attendee-linked person is in the candidate set.
        if matching_attendees(display, attendee_priors):
            relationship_boost = max(float(relationship_boost), 0.9)
    auto_t = _thr_auto_resolve()
    margin_t = _thr_auto_margin()
    # When an attendee boost produced a strong hit, slightly loosen margin so
    # a first-name mention of the invitee wins over create_new.
    if any((feats or {}).get("attendee") for _, _, feats in scored):
        margin_t = min(margin_t, 0.08)
        auto_t = min(auto_t, 0.90)
    decision, chosen_row, conf = decide_from_scores(
        scored,
        relationship_boost=relationship_boost,
        create_person_candidates=policy.create_person_candidates,
        auto_resolve=auto_t,
        auto_margin=margin_t,
        create_new=_thr_create_new(),
    )

    chosen: int | None = None
    relevance = float(relationship_boost)

    if decision == "auto_resolve" and chosen_row is not None:
        chosen = int(chosen_row["id"])
        store.touch_person(chosen, ts, alias=display)
        if policy.create_person_candidates:
            _bump_promotion(store, chosen, relevance, ts)
    elif decision == "create_new":
        # Prefer the invite CN over a bare first-name mention when minting.
        mint_name = display
        mint_email = ""
        matched = matching_attendees(display, attendee_priors)
        if matched:
            cn = (matched[0].get("name") or "").strip()
            if cn:
                mint_name = cn
            mint_email = (matched[0].get("email") or "").strip().lower()
        chosen = store.insert_person(
            mint_name, ts=ts,
            actor_type="human_person",
            promotion_state="candidate",
        )
        if mint_name.lower() != display.lower():
            store.touch_person(chosen, ts, alias=display)
        if mint_email:
            try:
                store.upsert_contact_point(
                    person_id=chosen, type_="email",
                    value_display=mint_email, value_normalized=mint_email,
                    confidence=0.95, attribution_method="calendar_attendee",
                    verification_status="unverified",
                    source_event_id=event_id, evidence_quote=None,
                    discourse_role="attendee", ts=ts,
                    created_by="meeting_join",
                    pipeline_version=PIPELINE_VERSION,
                )
            except Exception:
                pass

    # Rebuild the ranked list with the new-person prior for decision logging
    # (mirrors decide_from_scores when create is allowed).
    log_scored = list(scored)
    if policy.create_person_candidates:
        if not log_scored:
            new_score = 0.93 if relevance >= _CREATE_RELEVANCE else 0.4
        else:
            new_score = max(0.15, 0.9 - (log_scored[0][1] * 0.7))
            if relevance >= _CREATE_RELEVANCE and log_scored[0][1] < 0.70:
                new_score = max(new_score, 0.88)
        log_scored.append((None, new_score, {"new": True}))
        log_scored.sort(key=lambda x: -x[1])

    mid = None
    if event_id is not None:
        if not policy.create_person_candidates:
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
        for rank, (p, score, feats) in enumerate(log_scored[:8]):
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
            threshold_policy=f"auto>={auto_t}/margin>={margin_t}",
            resolver_version=PIPELINE_VERSION,
            decided_at=ts,
        )

    return ResolveResult(chosen, decision, mention_id=mid, confidence=conf)


@dataclass
class AttrWriteResult:
    """Plan 2.4 write-path decision for one contact value."""
    action: str  # write|review|deny_policy|skip
    kind: str
    value: str
    score: float
    contact_point_id: int | None = None
    reason: str = ""


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
    """Write PersonContactPoint rows when attribution clears ATTR_MIN.

    Weaker positive scores are routed to review (kg_adjudications
    kind='contact_review') — never auto-written. Policy-denied surfaces
    (news / article-mentioned / social / terminal) write nothing.
    """
    details = attribute_contacts_detailed(
        text, store=store, person_id=person_id, person_name=person_name,
        event_id=event_id, now=now, discourse_role=discourse_role,
        event_source=event_source, window=window)
    return [d.contact_point_id for d in details
            if d.action == "write" and d.contact_point_id]


def attribute_contacts_detailed(
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
) -> list[AttrWriteResult]:
    """Full write/review/deny decisions for contact values in `text`."""
    if not enabled() or not person_id or not (text or "").strip():
        return []
    if event_source or window:
        policy = sp.policy_for_event(
            event_source=event_source, window=window, text=text)
        if not policy.extract_contacts:
            return [AttrWriteResult(
                action="deny_policy", kind="*", value="", score=0.0,
                reason=f"source_class={policy.source_class}")]
    ts = now if now is not None else time.time()
    tokens = _name_tokens(person_name, [])
    out: list[AttrWriteResult] = []

    def _decide(kind: str, value: str, start: int, end: int,
                *, conf: float, method: str, norm: str) -> None:
        score = contact_attribution_score(
            text, kind=kind, value=value, start=start, end=end, tokens=tokens)
        if score <= 0:
            out.append(AttrWriteResult(
                action="skip", kind=kind, value=value, score=score,
                reason="no_link"))
            return
        if score < _ATTR_MIN:
            # Ambiguous / weak — review, do not auto-write (plan 2.4).
            try:
                store.log_adjudication(
                    kind="contact_review", decision="review",
                    decided_by="auto",
                    node_a=int(person_id),
                    model_score=float(score),
                    features={
                        "person_name": person_name,
                        "kind": kind,
                        "value": value,
                        "score": score,
                        "event_id": event_id,
                        "quote": (text or "")[:240],
                        "discourse_role": discourse_role,
                        "reason": f"score<{_ATTR_MIN}",
                    },
                )
            except Exception:
                pass
            out.append(AttrWriteResult(
                action="review", kind=kind, value=value, score=score,
                reason=f"score<{_ATTR_MIN}"))
            return
        cid = store.upsert_contact_point(
            person_id=person_id, type_=kind,
            value_display=value, value_normalized=norm,
            confidence=conf, attribution_method=method,
            verification_status="attributed",
            source_event_id=event_id, evidence_quote=text[:240],
            discourse_role=discourse_role, ts=ts,
            created_by="system", pipeline_version=PIPELINE_VERSION,
        )
        out.append(AttrWriteResult(
            action="write", kind=kind, value=value, score=score,
            contact_point_id=cid, reason="score>=ATTR_MIN"))

    for m in _EMAIL.finditer(text):
        email = m.group(0).rstrip(".,;:)>\"'")
        if "@" not in email:
            continue
        # Re-bound end so score patterns see the cleaned value.
        end = m.start() + len(email)
        _decide(
            "email", email, m.start(), end,
            conf=0.85, method="possessive_or_reach_or_local",
            norm=email.lower())

    for m in _PHONE.finditer(text):
        phone = m.group(1).strip().rstrip(".,;:)")
        if not _phone_ok(phone, text):
            continue
        norm = re.sub(r"[^\d+]", "", phone)
        _decide(
            "phone", phone, m.start(), m.start() + len(phone),
            conf=0.8, method="possessive_or_reach", norm=norm)

    return out


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


# --- desktop email capture → contacts + org affiliation -------------------
_FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "mail.com", "gmx.com",
})
_FROM_TO_RE = re.compile(
    r"(?im)^\s*(?P<field>from|to|cc|bcc)\s*:\s*(?P<body>.+)$")
_ANGLE_EMAIL = re.compile(
    r"(?:(?P<name>[A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,3})\s*)?"
    r"[<\[]?(?P<email>[\w.+-]+@[\w.-]+\.\w{2,})[>\]]?",
)
_SIG_BLOCK_RE = re.compile(
    r"(?:^|\n)--\s*\n(?P<sig>.{10,400})$|"
    r"(?:^|\n)(?:best|regards|thanks|thank you|cheers)[,!]?\s*\n(?P<sig2>.{10,400})$",
    re.I | re.S)
_ORG_HINT = re.compile(
    r"\b(?:inc|llc|ltd|corp|corporation|company|co\.|labs?|robotics|"
    r"systems|technologies|group|partners|ventures|studio|agency)\b",
    re.I)


def parse_email_parties(text: str) -> list[dict]:
    """Pull From/To/Cc parties + signature hint from OCR/transcript text."""
    parties: list[dict] = []
    seen: set[str] = set()
    for m in _FROM_TO_RE.finditer(text or ""):
        field = m.group("field").lower()
        body = m.group("body") or ""
        for em in _ANGLE_EMAIL.finditer(body):
            email = (em.group("email") or "").strip().lower()
            if not email or email in seen:
                continue
            seen.add(email)
            name = (em.group("name") or "").strip()
            if not name:
                local = email.split("@", 1)[0]
                name = re.sub(r"[._]+", " ", local).title()
            parties.append({
                "role": field, "name": name, "email": email,
                "quote": body.strip()[:200],
            })
    # Signature org: prefer a line with company cues or matching the From domain.
    sig_org = None
    sm = _SIG_BLOCK_RE.search(text or "")
    from_domain = ""
    for p in parties:
        if p["role"] == "from":
            from_domain = (p["email"].split("@", 1) + [""])[1]
            break
    brand = from_domain.split(".")[0] if from_domain else ""
    if sm:
        sig = sm.group("sig") or sm.group("sig2") or ""
        lines = [ln.strip() for ln in sig.splitlines() if ln.strip()]
        for line in lines:
            if "@" in line or _EMAIL.search(line):
                continue
            if re.search(r"\b(?:phone|tel|mobile|www\.|http)\b", line, re.I):
                continue
            if not (2 <= len(line) <= 48):
                continue
            low = line.casefold().replace(" ", "")
            if brand and brand in low:
                sig_org = line
                break
            if _ORG_HINT.search(line):
                sig_org = line
                break
    if sig_org:
        for p in parties:
            if p["role"] == "from" and not p.get("org"):
                p["org"] = sig_org
    return parties


def org_from_email_domain(email: str) -> str | None:
    """Corporate domain → org display name; free-mail domains → None."""
    try:
        domain = (email or "").split("@", 1)[1].lower().strip()
    except IndexError:
        return None
    if not domain or domain in _FREE_MAIL or domain.endswith(".example.com") \
            or domain == "example.com":
        return None
    # strip common second-level consumer-ish hosts
    if domain.startswith("mail.") or domain.startswith("email."):
        domain = domain.split(".", 1)[-1]
    label = domain.split(".")[0]
    if not label or len(label) < 2:
        return None
    return label.replace("-", " ").title()


def ingest_email_network(
    text: str,
    *,
    store,
    event_id: int | None = None,
    event_source: str = "desktop.screen",
    window: str = "",
    now: float | None = None,
) -> dict:
    """When capture is email-class: mint contacts + works_at from headers.

    Capture-first CRM slice — no OAuth/IMAP. User-asserted quality gates still
    apply via name_quality / People v2 resolve.
    """
    if not enabled() or not (text or "").strip():
        return {"ok": False, "reason": "disabled_or_empty", "parties": 0}
    policy = sp.policy_for_event(
        event_source=event_source, window=window, text=text)
    if policy.source_class != "email":
        return {"ok": False, "reason": f"source_class={policy.source_class}",
                "parties": 0}
    if not policy.extract_contacts and not policy.update_people:
        return {"ok": False, "reason": "policy_deny", "parties": 0}
    ts = now if now is not None else time.time()
    parties = parse_email_parties(text)
    written = 0
    affiliations = 0
    for party in parties:
        name = party.get("name") or ""
        email = party.get("email") or ""
        if not email:
            continue
        pid = None
        try:
            pid = store.find_person_by_contact("email", email)
        except Exception:
            pid = None
        if not pid and name and nq.is_plausible_person(name):
            res = resolve_person_mention(
                name, store=store, event_id=event_id,
                event_source=event_source, window=window, text=text,
                grammatical_role="email_" + party.get("role", "party"),
                now=ts, relationship_boost=0.75)
            pid = res.person_id
        if not pid:
            continue
        if policy.extract_contacts:
            try:
                store.upsert_contact_point(
                    person_id=int(pid), type_="email",
                    value_display=email, value_normalized=email,
                    confidence=0.9, attribution_method="email_header",
                    verification_status="attributed",
                    source_event_id=event_id,
                    evidence_quote=(party.get("quote") or text)[:240],
                    discourse_role="email_" + party.get("role", "party"),
                    ts=ts, created_by="system",
                    pipeline_version=PIPELINE_VERSION)
                written += 1
            except Exception:
                pass
        org_name = party.get("org") or org_from_email_domain(email)
        if org_name and policy.update_people and policy.relationship_evidence:
            try:
                eid = store.resolve_entity(org_name, "org", ts=ts)
                store.add_relation(
                    "person", int(pid), "works_at", "entity", int(eid),
                    origin="asserted", ts=ts,
                    quote=f"{name} <{email}>", source_class="email")
                from app.services import kg_beliefs
                kg_beliefs.record_from_relation(
                    store, subj_type="person", subj_id=int(pid),
                    predicate="works_at", obj_type="entity", obj_id=int(eid),
                    origin="asserted", source_event_id=event_id,
                    confidence=0.7, ts=ts,
                    quote=f"{name} <{email}>", source_class="email")
                affiliations += 1
            except Exception:
                pass
    return {"ok": True, "parties": len(parties), "contacts": written,
            "affiliations": affiliations}
