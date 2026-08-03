"""Suggested triggers — mine repeated behavior into adopt-me trigger rows.

Passive by design (onboarding hard rule: zero labeling required): the miner
reads what the facts timeline already recorded and only ever creates rows with
status='suggested', which the engine surfaces as one calm "want me to watch
for this?" card. Dismissing retires the row — its pattern_key stays behind as
a durable negative example, so nothing is ever re-suggested.

v1 pattern — progress→outreach (the flagship "we saw you made progress on X,
email person Z?" loop): completing work tied to entity E is repeatedly
followed, within a couple of days, by an outreach task naming person P. That
pair becomes a suggested `progress_on(E) → draft update to P` trigger with the
recipient bound AT CREATION (mined from the user's own history, then frozen —
matched content can never redirect it).
"""
from __future__ import annotations

import os
import time

_PAIR_WINDOW_S = 2 * 86400.0     # outreach must follow progress within 2 days
_OUTREACH = ("email", "send", "message", "text", "update", "tell", "share",
             "ping", "call", "loop in", "follow up with")


def _min_count() -> int:
    return int(os.environ.get("QUILL_TRIGGER_MINE_MIN", "2") or "2")


def _outreach_person(fact: dict, people: list[str]) -> str | None:
    """Who does this fact reach out to? Typed columns first, then a known
    person's name in the text — never an arbitrary capitalized word."""
    for k in ("to_person", "owner", "from_person"):
        v = (fact.get(k) or "").strip()
        if v:
            return v
    text = (fact.get("text") or "").lower()
    for name in people:
        if len(name) >= 3 and name.lower() in text:
            return name
    return None


def _is_outreach(fact: dict) -> bool:
    text = (fact.get("text") or "").lower()
    return any(v in text for v in _OUTREACH)


def mine(store, *, now: float | None = None,
         min_count: int | None = None) -> list[int]:
    """One mining pass. Returns ids of newly created suggested rows."""
    now = float(now if now is not None else time.time())
    need = int(min_count if min_count is not None else _min_count())
    try:
        facts = store.list_facts(limit=2000)
    except Exception:
        return []
    tasks = [f for f in facts if f.get("kind") in ("task", "commitment")]
    done = [f for f in tasks if (f.get("status") or "") == "done"]
    if not done:
        return []
    ent_map = store.fact_entities([f.get("fact_id") for f in done])
    try:
        people = [p.get("name") or p.get("canonical_name") or ""
                  for p in store.all_people()]
    except Exception:
        people = []

    # (entity, person) -> distinct progress moments answered with outreach.
    pairs: dict[tuple[str, str], int] = {}
    outreach = [(f, float(f.get("extracted_at") or 0)) for f in tasks
                if _is_outreach(f)]
    for f in done:
        fid = f.get("fact_id")
        ents = ent_map.get(int(fid)) if fid else None
        if not ents:
            continue
        done_ts = float(f.get("updated_at") or f.get("extracted_at") or 0)
        for of, ots in outreach:
            if of.get("fact_id") == fid:
                continue
            if not (done_ts <= ots <= done_ts + _PAIR_WINDOW_S):
                continue
            person = _outreach_person(of, people)
            if not person:
                continue
            for ent in ents:
                if len((ent or "").strip()) < 3:
                    continue
                key = (ent.strip(), person.strip())
                pairs[key] = pairs.get(key, 0) + 1

    created: list[int] = []
    for (ent, person), n in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if n < need:
            continue
        pattern_key = f"progress_outreach|{ent.lower()}|{person.lower()}"
        if store.trigger_pattern_exists(pattern_key):
            continue
        tid = store.add_trigger(
            f"Update {person} when {ent} moves"[:80],
            "progress_on",
            condition={"entity": ent},
            action={"verb": "run_goal",
                    "goal": (f"Draft a short update to {person} about the "
                             f"latest progress on {ent}")},
            provenance={"source": "miner", "pattern_key": pattern_key,
                        "evidence_pairs": n},
            origin="suggested", status="suggested", created_at=now)
        created.append(tid)
        print(f"[triggers] mined suggestion #{tid}: "
              f"progress_on({ent}) -> update {person} ({n} pairs).")
    return created
