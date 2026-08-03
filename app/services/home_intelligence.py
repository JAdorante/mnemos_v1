"""Aggregate Today's Intelligence for the Home surface."""
from __future__ import annotations

import time
from typing import Any


def build(store, *, agent_state: dict | None = None,
          recent_events: list | None = None) -> dict[str, Any]:
    """Commitments, follow-ups, reflection, people, activity, ambient notes."""
    now = time.time()
    open_facts = store.list_facts(status="open", limit=80, actionable=True)
    commitments = [f for f in open_facts if f.get("kind") in ("task", "commitment")]
    commitments.sort(key=lambda f: (-float(f.get("confidence") or 0), f.get("fact_id") or 0))

    readiness = {"bands": {}, "items": []}
    try:
        from app.services.readiness import for_fact
        bands = {"auto": 0, "offer": 0, "review": 0, "hold": 0}
        items = []
        for f in [x for x in open_facts if x.get("kind") == "task"][:60]:
            v = for_fact(f)
            bands[v.band] = bands.get(v.band, 0) + 1
            items.append({
                "fact_id": f.get("fact_id"), "text": f.get("text"),
                "score": v.score, "band": v.band, "risk": v.risk,
                "confidence": f.get("confidence"),
            })
        items.sort(key=lambda x: x["score"], reverse=True)
        readiness = {"bands": bands, "items": items[:12]}
    except Exception:
        pass

    reflection = None
    try:
        header = store.latest_reflection("daily")
        if header:
            items = store.reflection_items(header["id"]) if hasattr(store, "reflection_items") else []
            if not items and hasattr(store, "get_reflection"):
                full = store.get_reflection(header["id"])
                items = (full or {}).get("items") or []
            reflection = {
                "id": header.get("id"),
                "summary": header.get("summary"),
                "confidence": header.get("confidence"),
                "items": [
                    {"id": i.get("id"), "kind": i.get("kind"), "text": i.get("text"),
                     "detail": i.get("detail"), "review": i.get("review")}
                    for i in (items or [])[:8]
                ],
            }
    except Exception:
        reflection = None

    try:
        people = rank_people(store, now=now)
    except Exception:
        people = [{"id": p["id"], "name": p["name"], "weight": 0}
                  for p in store.all_people()[:10]]

    activity = []
    try:
        activity = [
            {
                "id": a.get("id"),
                "app": a.get("app"),
                "summary": a.get("summary"),
                "started_at": a.get("started_at"),
                "ended_at": a.get("ended_at"),
            }
            for a in store.recent_activities(8)
        ]
    except Exception:
        activity = []

    highlights = []
    for ev in (recent_events or [])[-12:]:
        text = (ev.get("text") or ev.get("description") or "").strip()
        if not text:
            continue
        highlights.append({
            "id": ev.get("id"),
            "modality": (ev.get("modality") or "")[:24],
            "text": text[:180],
            "ts": ev.get("time") or ev.get("ts"),
        })

    aging_items: list = []
    try:
        from app.services import field_history as _fh
        aging_items = _fh.aging_open_work(store, now=now)
    except Exception:
        aging_items = []

    ambient = _ambient_notes(
        commitments, readiness, reflection, people, agent_state,
        aging=aging_items)
    awaiting = bool((agent_state or {}).get("awaiting")
                    or (agent_state or {}).get("todo_pending"))

    return {
        "generated_at": now,
        "date_label": time.strftime("%A, %B %d"),
        "commitments": [_fact_brief(f) for f in commitments[:10]],
        "follow_ups": readiness.get("items") or [],
        "readiness_bands": readiness.get("bands") or {},
        "reflection": reflection,
        "people": people,
        "activity": activity[:8] if isinstance(activity, list) else [],
        "highlights": highlights[:8],
        "ambient": ambient,
        "awaiting_approval": awaiting,
        "waiting_on": (agent_state or {}).get("waiting_on"),
    }


# "Important people" scoring. The old version summed raw edge weights over the
# FIRST 24 people rows (insertion order!), so late-added people could never
# appear, ASR-noise "people" with a few junk mentions outranked real contacts,
# and the user topped their own list. This ranks EVERY person by evidence
# quality instead: a typed relationship (owes/committed/works_at) is worth 3x a
# bare mention, co-occurrence is dampened, and a 30-day recency half-life keeps
# the list about who matters NOW (old contacts fade to 35%, never to zero).
_PEOPLE_SHOWN = 12
_SCORE_FLOOR = 1.0          # one stale mention never makes the board
_RECENCY_HALF_LIFE_D = 30.0


def person_score(out_edges: list[dict], last_seen: float | None,
                 now: float) -> float:
    fact_pred: dict = {}
    co = 0.0
    asserted_ent = 0
    for e in out_edges or []:
        if e.get("obj_type") == "fact":
            cur = fact_pred.get(e["obj_id"])
            if cur is None or cur == "mentioned_in":
                fact_pred[e["obj_id"]] = e.get("predicate")
        elif (e.get("obj_type") == "person"
              and e.get("predicate") == "co_occurs"):
            co += float(e.get("weight") or 1)
        elif e.get("obj_type") == "entity" and e.get("origin") == "asserted":
            asserted_ent += 1
    typed = sum(1 for p in fact_pred.values() if p != "mentioned_in")
    mentions = len(fact_pred) - typed
    base = 3.0 * typed + 1.0 * mentions + 0.5 * min(co, 10.0) + 2.0 * asserted_ent
    age_d = (now - last_seen) / 86400.0 if last_seen else 90.0
    rec = 0.5 ** (max(age_d, 0.0) / _RECENCY_HALF_LIFE_D)
    return base * (0.35 + 0.65 * rec)


def entity_score(in_edges: list[dict], last_seen: float | None,
                 now: float) -> float:
    """Evidence score for an org/project/tool/place — same recency shape as
    person_score, evidence counted from IN-edges (facts are `about` it, people
    are `associated_with` it)."""
    facts, ppl = set(), set()
    asserted = 0
    for e in in_edges or []:
        if e.get("subj_type") == "fact":
            facts.add(e["subj_id"])
        elif e.get("subj_type") == "person":
            ppl.add(e["subj_id"])
            if e.get("origin") == "asserted":
                asserted += 1
    base = 1.0 * len(facts) + 1.5 * len(ppl) + 1.0 * asserted
    age_d = (now - last_seen) / 86400.0 if last_seen else 90.0
    rec = 0.5 ** (max(age_d, 0.0) / _RECENCY_HALF_LIFE_D)
    return base * (0.35 + 0.65 * rec)


def rank_people(store, *, now: float, limit: int = _PEOPLE_SHOWN) -> list[dict]:
    """All real people ranked by relationship evidence — self node excluded
    (you are not one of your own important people)."""
    from app.services.graph import _real_people
    self_pid = None
    try:
        from app.services.self_profile import self_person_id
        self_pid = self_person_id(store)
    except Exception:
        pass
    out = []
    for p in _real_people(store):
        if p["id"] == self_pid:
            continue
        rel = store.relations_of("person", p["id"])
        score = person_score(rel.get("out") or [], p.get("last_seen"), now)
        if score >= _SCORE_FLOOR:
            out.append({"id": p["id"], "name": p["name"],
                        "weight": round(score, 1)})
    out.sort(key=lambda x: -x["weight"])
    return out[:limit]


def _fact_brief(f: dict) -> dict:
    return {
        "fact_id": f.get("fact_id"),
        "kind": f.get("kind"),
        "text": f.get("text"),
        "status": f.get("status"),
        "confidence": f.get("confidence"),
        "owner": f.get("owner"),
        "due": f.get("due"),
        "source_event_id": f.get("source_event_id"),
        "source_span": f.get("source_span"),
    }


def _ambient_notes(commitments, readiness, reflection, people, agent_state,
                   *, aging: list | None = None) -> list[dict]:
    from app.services.margin import action_command, action_route, note

    notes: list[dict] = []
    n_open = len(commitments)
    open_refs = [f"fact:{c['fact_id']}" for c in commitments[:16]
                 if c.get("fact_id")]
    if n_open:
        notes.append(note(
            f"You have {n_open} open commitment{'s' if n_open != 1 else ''} on the board.",
            kind="stat",
            attention=n_open >= 3,
            refs=open_refs,
            action=action_command(
                "Show in the sky", "constellation.emphasize"),
        ))
    aging = aging or []
    if aging:
        n_age = len(aging)
        days = int(round(float(aging[0].get("age_days") or 0)))
        notes.append(note(
            (
                f"Several open tasks are aging ({days} days)"
                if n_age > 1
                else f"An open task is aging ({days} days)"
            ),
            kind="nudge",
            attention=True,
            refs=[a["id"] for a in aging[:8] if a.get("id")],
            action=action_command(
                "Emphasize aging tasks", "constellation.emphasize"),
            source="field_diff.aging",
        ))
    review = [i for i in (readiness.get("items") or [])
              if i.get("band") in ("review", "hold")]
    if review:
        notes.append(note(
            f"{len(review)} follow-up{'s' if len(review) != 1 else ''} "
            f"need your judgment before acting.",
            kind="stat",
            attention=True,
            refs=[f"fact:{i['fact_id']}" for i in review[:8]
                  if i.get("fact_id")],
            action=action_route(
                "Open readiness queue", "/memory#readiness"),
        ))
    if reflection and reflection.get("items"):
        open_loops = [i for i in reflection["items"]
                      if i.get("kind") == "open_loop"]
        if open_loops:
            notes.append(note(
                open_loops[0].get("text")
                or "An open loop from today's reflection.",
                kind="observation",
                attention=True,
            ))
        elif reflection.get("summary"):
            notes.append(note(
                (reflection["summary"] or "")[:140],
                kind="observation",
                attention=False,
            ))
    if people and len(people) >= 2:
        quiet = people[-1]
        top = people[0]
        refs = []
        if top.get("id"):
            refs.append(f"person:{top['id']}")
        if quiet.get("id"):
            refs.append(f"person:{quiet['id']}")
        notes.append(note(
            f"{quiet['name']} is in your graph — quieter than {top['name']} lately.",
            kind="observation",
            attention=False,
            refs=refs,
            action=action_command(
                "Compare in the sky", "constellation.compare"),
        ))
    if agent_state and agent_state.get("awaiting"):
        notes.append(note(
            "An approval is waiting — open Chat to seal it.",
            kind="nudge",
            attention=True,
            action=action_route("Open approval queue", "/chat"),
        ))
    if not notes:
        notes.append(note("Quiet for now — listening.", kind="observation"))
    return notes[:6]
