"""Knowledge graph v1 — turn the nodes we already extract into a graph.

The facts pipeline gives us nodes (people, facts, events) but no edges, so
nothing can traverse "who is this, and what's open with them, and where did each
piece come from". This builds the connective tissue *deterministically* — no LLM
calls — from signal already in the store:

  * typed person↔fact edges from resolved owners/parties (responsible_for / committed / owed)
  * mention edges: a known person's name appearing in a fact's text (mentioned_in)
  * provenance edges: fact → its source event (evidenced_by)
  * co-occurrence edges: people named together in a fact or a turn (co_occurs, weighted)

`context_for_person` then walks those edges to answer a relational question by
traversal, not by flat text search. Org/project nodes are still missing (the
`entities` table is empty until the extractor is enriched to populate it) — this
layer is ready for them the moment they exist.
"""
from __future__ import annotations

import re
import time
from itertools import combinations

from app.storage import Store, get_store

# Pronouns / fillers that land in the people table but aren't real entities;
# scanning for these as "names" would create noise edges.
_STOP_NAMES = {"she", "he", "me", "i", "we", "they", "you", "it", "them"}

# Change 6: person↔org affiliation predicates — for people/network queries
# these retrieve status IN ('active','superseded') by default (implicit past
# tense: "who do I know at Figma" includes people who USED to work there).
# Non-people predicates (uses/depends_on/…) keep the strict-current default.
AFFILIATION_PREDS = frozenset(
    {"works_at", "part_of", "member_of", "affiliated_with", "founded"})

# Org reporting-line predicates (asserted via onboarding / org-network UI).
REPORTING_PREDS = frozenset({"reports_to", "manages"})


def _affiliations_from_kg(store: Store, person_id: int,
                          emap: dict | None = None) -> dict[int, dict]:
    """Plan 2.6 — affiliations primary-read from kg_predicates (+ past)."""
    if emap is None:
        emap = {e["id"]: e for e in store.all_entities()}
    try:
        kg_rows = store.list_kg_predicates(
            subj_type="person", subj_id=int(person_id),
            statuses=("active", "superseded"))
    except Exception:
        return {}
    affil: dict[int, dict] = {}
    for r in kg_rows:
        if r.get("obj_type") != "entity" or r["predicate"] not in AFFILIATION_PREDS:
            continue
        eid = int(r["obj_id"])
        ent = emap.get(eid)
        if not ent:
            continue
        cur = affil.get(eid)
        if cur is not None and cur.get("belief_status") == "active" \
                and r["status"] != "active":
            continue
        try:
            from app.services import kg_beliefs
            conf = float(kg_beliefs.posterior(store, int(r["id"])))
        except Exception:
            conf = float(r.get("confidence") or 0.5)
        affil[eid] = {
            "id": eid, "name": ent["name"], "kind": ent["kind"],
            "predicate": r["predicate"], "asserted": True,
            "weight": conf, "confidence": conf,
            "belief_status": r["status"],
            "former": r["status"] == "superseded",
            "valid_from": r.get("valid_from"),
            "valid_to": r.get("valid_to"),
            "superseded_by": r.get("superseded_by"),
            "predicate_id": int(r["id"]),
            "source": "kg_beliefs",
        }
    return affil


def _name_pattern(name: str):
    return re.compile(r"\b" + re.escape(name) + r"\b", re.I)


def _entity_patterns(e: dict) -> list:
    """Canonical name + aliases for an entity, same rationale as people —
    speech says "the studio", facts say "FL Studio". Aliases get a stricter
    junk guard (>=3 chars) so a stray 2-letter alias can't match everywhere."""
    pats, seen = [], set()
    names = [(e.get("name") or "", 2)] + [(a, 3) for a in (e.get("aliases") or [])]
    for n, min_len in names:
        n = (n or "").strip()
        low = n.lower()
        if len(n) < min_len or low in _STOP_NAMES or low in seen:
            continue
        seen.add(low)
        pats.append(_name_pattern(n))
    return pats


def _person_patterns(p: dict) -> list:
    """Match patterns for a person: canonical name PLUS aliases — facts say
    "Hugh", not "Hugh Salva", so scanning the full name alone missed most
    mentions (observed live: a cofounder scored 0.0). Junk-guard each alias
    like a name (length + stop words)."""
    pats, seen = [], set()
    for n in [p.get("name") or ""] + list(p.get("aliases") or []):
        n = (n or "").strip()
        low = n.lower()
        if len(n) < 3 or low in _STOP_NAMES or low in seen:
            continue
        seen.add(low)
        pats.append(_name_pattern(n))
    return pats


def _real_people(store: Store) -> list[dict]:
    return [p for p in store.all_people()
            if len(p["name"]) >= 3 and p["name"].lower() not in _STOP_NAMES]


def _real_entities(store: Store) -> list[dict]:
    return [e for e in store.all_entities() if len(e["name"]) >= 2]


def rebuild(store: Store | None = None, *, scope: str = "full") -> dict:
    """Recompute DERIVED edges. Asserted/user edges are left intact.

    scope:
      - "full": wipe all derived edges and re-derive from the whole corpus
        (nightly consistency backstop).
      - "dirty": re-derive only edges incident to dirty nodes ∪ their 1-hop
        neighborhood (WS5 incremental). Equivalence: dirty* + eventual full
        converges to the same derived-edge set as full-from-scratch.
    """
    import time as _time
    t0 = _time.perf_counter()
    store = store or get_store()
    scope = (scope or "full").strip().lower()
    if scope not in ("full", "dirty"):
        scope = "full"

    dirty: set[tuple[str, int]] = set()
    neighborhood: set[tuple[str, int]] | None = None
    if scope == "dirty":
        dirty = store.graph_dirty_nodes()
        if not dirty:
            return {
                "scope": "dirty", "dirty": 0, "neighborhood": 0,
                "skipped": True, "duration_ms": 0,
            }
        neighborhood = set(dirty)
        # 1-hop via ANY current edges (for co-mention symmetry).
        for nt, nid in list(dirty):
            try:
                rel = store.relations_of(nt, nid)
            except Exception:
                continue
            for e in (rel.get("out") or []):
                neighborhood.add((e["obj_type"], int(e["obj_id"])))
            for e in (rel.get("in") or []):
                neighborhood.add((e["subj_type"], int(e["subj_id"])))
        cleared = store.clear_relations(
            origin="derived", incident_to=neighborhood)
    else:
        store.clear_relations(origin="derived")   # keep asserted (extractor) edges
        cleared = None

    now = time.time()
    people = _real_people(store)
    ppat = [(p, pats) for p in people if (pats := _person_patterns(p))]
    entities = _real_entities(store)
    epat = [(e, pats) for e in entities if (pats := _entity_patterns(e))]
    counts = {"typed": 0, "mentioned_in": 0, "evidenced_by": 0, "co_occurs": 0,
              "about_entity": 0, "associated_with": 0,
              "scope": scope, "dirty": len(dirty),
              "neighborhood": len(neighborhood or ()),
              "cleared_derived": cleared}

    def _in_nb(nt: str, nid: int) -> bool:
        if neighborhood is None:
            return True
        return (nt, int(nid)) in neighborhood

    # 1) typed person↔fact edges from resolved owners/parties.
    for fact_id, person_id, role in store.fact_person_links():
        if not (_in_nb("fact", fact_id) or _in_nb("person", person_id)):
            continue
        store.add_relation("person", person_id, role, "fact", fact_id, ts=now)
        counts["typed"] += 1

    # 2) mention + provenance + co-occurrence + entity links, scanning fact text.
    facts = store.list_facts(limit=100000)
    for f in facts:
        fid = f["fact_id"]
        text = f"{f.get('text') or ''} {f.get('source_span') or ''}"
        sev = f.get("source_event_id")
        phits = [p for p, pats in ppat if any(pat.search(text) for pat in pats)]
        ehits = [e for e, pats in epat if any(pat.search(text) for pat in pats)]
        touches = (
            _in_nb("fact", fid)
            or any(_in_nb("person", p["id"]) for p in phits)
            or any(_in_nb("entity", e["id"]) for e in ehits)
        )
        if neighborhood is not None and not touches:
            continue
        if sev:
            store.add_relation("fact", fid, "evidenced_by", "event", sev, ts=now)
            counts["evidenced_by"] += 1
        for p in phits:
            store.add_relation("person", p["id"], "mentioned_in", "fact", fid,
                               source_event_id=sev, ts=now)
            counts["mentioned_in"] += 1
        for e in ehits:
            store.add_relation("fact", fid, "about", "entity", e["id"],
                               source_event_id=sev, ts=now)
            counts["about_entity"] += 1
        for p in phits:
            for e in ehits:
                store.add_relation("person", p["id"], "associated_with", "entity",
                                   e["id"], source_event_id=sev, ts=now)
                counts["associated_with"] += 1
        for a, b in combinations({p["id"] for p in phits}, 2):
            if neighborhood is not None and not (
                    _in_nb("person", a) or _in_nb("person", b)):
                continue
            store.add_relation("person", a, "co_occurs", "person", b,
                               source_event_id=sev, ts=now)
            store.add_relation("person", b, "co_occurs", "person", a,
                               source_event_id=sev, ts=now)
            counts["co_occurs"] += 2

    # 3) co-occurrence across conversational turns (people named together).
    for t in store.recent_turns(100000):
        ttext = t.get("text") or ""
        hits = [p for p, pats in ppat if any(pat.search(ttext) for pat in pats)]
        for a, b in combinations({p["id"] for p in hits}, 2):
            if neighborhood is not None and not (
                    _in_nb("person", a) or _in_nb("person", b)):
                continue
            store.add_relation("person", a, "co_occurs", "person", b, ts=now)
            store.add_relation("person", b, "co_occurs", "person", a, ts=now)
            counts["co_occurs"] += 2

    counts["total"] = store.relation_count()

    # Edge dynamics sidecar (A2)
    try:
        from app.services.activation import compute_edge_dynamics, activation_field
        counts["edge_dynamics"] = store.replace_edge_dynamics(
            compute_edge_dynamics(store.all_relations()))
        activation_field.invalidate()
    except Exception as exc:
        print(f"[graph] edge dynamics skipped ({exc}).")

    if scope == "dirty" and dirty:
        store.clear_graph_dirty(dirty)

    counts["duration_ms"] = round((_time.perf_counter() - t0) * 1000, 1)
    print(f"[graph] rebuild scope={scope} dirty={counts['dirty']} "
          f"nb={counts['neighborhood']} duration_ms={counts['duration_ms']}")
    return counts


def _resolve_person(store: Store, name: str,
                    cache: dict[str, dict | None] | None = None) -> dict | None:
    key = (name or "").strip().lower()
    if not key:
        return None
    if cache is not None and key in cache:
        return cache[key]
    people = store.all_people()
    found: dict | None = None
    for p in people:                       # exact (case-insensitive)
        if p["name"].lower() == key:
            found = p
            break
    if found is None:
        for p in people:                   # prefix either way (Chris/Christopher)
            n = p["name"].lower()
            if len(key) >= 3 and (n.startswith(key) or key.startswith(n)):
                found = p
                break
    if cache is not None:
        cache[key] = found
    return found


def context_for_person(name: str, store: Store | None = None) -> dict:
    """Traverse the graph around a person: their open items (by edge type) and
    who they come up with — the relational view flat search can't give."""
    store = store or get_store()
    person = _resolve_person(store, name)
    if person is None:
        return {"found": False, "query": name}

    edges = store.relations_of("person", person["id"])
    # Fact edges → hydrate the facts they point to.
    fact_ids, fact_pred = [], {}
    for e in edges["out"]:
        if e["obj_type"] == "fact":
            fact_ids.append(e["obj_id"])
            # prefer a typed role over a bare mention
            cur = fact_pred.get(e["obj_id"])
            if cur is None or cur == "mentioned_in":
                fact_pred[e["obj_id"]] = e["predicate"]
    fmap = store.facts_by_ids(list(set(fact_ids))) if fact_ids else {}
    items = []
    for fid in dict.fromkeys(fact_ids):
        fr = fmap.get(fid)
        if not fr:
            continue
        if (fr.get("state") or "active") != "active":
            continue  # superseded facts: the replacement row carries the truth
        items.append({
            "fact_id": fid, "predicate": fact_pred.get(fid, "mentioned_in"),
            "kind": fr.get("kind"), "text": fr.get("text") or fr.get("source_span"),
            "status": fr.get("status"), "source_event_id": fr.get("source_event_id"),
            "updated_at": fr.get("updated_at") or fr.get("extracted_at") or 0,
        })
    # sort: open first, typed roles before mere mentions, then freshest first
    _role_rank = {"responsible_for": 0, "committed": 0, "owed": 0, "mentioned_in": 1}
    items.sort(key=lambda i: (i.get("status") != "open",
                              _role_rank.get(i["predicate"], 2),
                              -(i.get("updated_at") or 0)))

    # co_occurs neighbours, aggregated by weight.
    pmap = {p["id"]: p["name"] for p in store.all_people()}
    neigh: dict[int, float] = {}
    for e in edges["out"]:
        if e["predicate"] == "co_occurs" and e["obj_type"] == "person":
            neigh[e["obj_id"]] = neigh.get(e["obj_id"], 0) + e["weight"]
    discussed = [{"name": pmap.get(pid, "?"), "weight": w}
                 for pid, w in sorted(neigh.items(), key=lambda kv: -kv[1])]

    # entity affiliations: asserted (works_at/part_of/…) first, then associated_with.
    # Plan 2.6: when read_v2 is on, affiliations primary-read kg_beliefs.
    emap = {e["id"]: e for e in store.all_entities()}
    try:
        from app.services import kg_parity
        read_v2 = kg_parity.read_v2_enabled(store)
    except Exception:
        read_v2 = False

    if read_v2:
        affil = _affiliations_from_kg(store, int(person["id"]), emap)
    else:
        affil = {}
        for e in edges["out"]:
            if e["obj_type"] != "entity":
                continue
            ent = emap.get(e["obj_id"])
            if not ent:
                continue
            cur = affil.get(e["obj_id"])
            asserted = e["origin"] == "asserted"
            # keep the strongest edge per entity (asserted beats derived)
            if cur is None or (asserted and cur["predicate"] == "associated_with"):
                affil[e["obj_id"]] = {"id": e["obj_id"], "name": ent["name"],
                                      "kind": ent["kind"],
                                      "predicate": e["predicate"],
                                      "asserted": asserted,
                                      "weight": e["weight"]}
        # Change 6: annotate with KG belief status/interval (hybrid path).
        try:
            kg_rows = store.list_kg_predicates(
                subj_type="person", subj_id=person["id"],
                statuses=("active", "superseded"))
        except Exception:
            kg_rows = []
        kg_by_obj = {}
        for r in kg_rows:
            if r.get("obj_type") == "entity" and r["predicate"] in AFFILIATION_PREDS:
                cur = kg_by_obj.get(int(r["obj_id"]))
                if cur is None or (cur["status"] != "active"
                                   and r["status"] == "active"):
                    kg_by_obj[int(r["obj_id"])] = r
            if r.get("obj_type") == "entity" and r["predicate"] in AFFILIATION_PREDS \
                    and int(r["obj_id"]) not in affil:
                ent = emap.get(int(r["obj_id"]))
                if ent:
                    affil[int(r["obj_id"])] = {
                        "id": int(r["obj_id"]), "name": ent["name"],
                        "kind": ent["kind"], "predicate": r["predicate"],
                        "asserted": True, "weight": 0.0}
        for a in affil.values():
            kg = kg_by_obj.get(a["id"])
            if kg:
                a["belief_status"] = kg["status"]
                a["former"] = kg["status"] == "superseded"
                a["valid_from"] = kg.get("valid_from")
                a["valid_to"] = kg.get("valid_to")
                a["superseded_by"] = kg.get("superseded_by")
            else:
                a["former"] = False
    affiliations = sorted(affil.values(),
                          key=lambda a: (a.get("former", False),
                                         not a.get("asserted", True),
                                         -(a.get("weight") or 0)))

    return {
        "found": True, "person": person,
        "items": items, "affiliations": affiliations, "discussed_with": discussed,
        "edge_count": len(edges["out"]) + len(edges["in"]),
    }


def people_for_entity(store: Store, entity_name: str) -> dict:
    """'Who do I know at X' — current AND former affiliates, labeled with
    their validity interval (Change 6). Explicit time filters in the caller's
    question still win over this default (§16 rule 3)."""
    eid = store.find_entity_exact(entity_name)
    if not eid:
        return {"found": False, "query": entity_name}
    rows = store.list_kg_predicates(
        obj_type="entity", obj_id=int(eid),
        statuses=("active", "superseded"))
    out = []
    for r in rows:
        if r["predicate"] not in AFFILIATION_PREDS or r["subj_type"] != "person":
            continue
        p = store.get_person(int(r["subj_id"]))
        if not p or p.get("hide_from_people"):
            continue

        def _year(v):
            return time.strftime("%Y", time.localtime(float(v))) if v else "?"
        former = r["status"] == "superseded"
        out.append({
            "person_id": int(r["subj_id"]), "name": p["name"],
            "predicate": r["predicate"], "former": former,
            "status": r["status"], "confidence": r.get("confidence"),
            "valid_from": r.get("valid_from"), "valid_to": r.get("valid_to"),
            "superseded_by": r.get("superseded_by"),
            "label": (f"{p['name']} (at {entity_name} "
                      f"{_year(r.get('valid_from'))}–{_year(r.get('valid_to'))})"
                      if former else p["name"]),
            "predicate_id": int(r["id"]),  # -> GET /kg/predicates/{id}/explain
        })
    out.sort(key=lambda a: (a["former"], -(a.get("confidence") or 0)))
    return {"found": True, "entity_id": int(eid), "entity": entity_name,
            "people": out}


def _parse_constellation_id(nid: str) -> tuple[str, int] | None:
    try:
        kind, rest = (nid or "").split(":", 1)
        if kind not in ("person", "entity", "fact"):
            return None
        return kind, int(rest)
    except (TypeError, ValueError):
        return None


def _pair_key(a: str, b: str) -> str:
    return "\0".join(sorted((a, b)))


def unlink_constellation_edge(store: Store, source: str, target: str) -> dict:
    """Remove visible link and remember a user hide so rebuild won't restore it."""
    pa, pb = _parse_constellation_id(source), _parse_constellation_id(target)
    if not pa or not pb or pa == pb:
        return {"ok": False, "error": "need two distinct constellation nodes"}
    (ta, ia), (tb, ib) = pa, pb
    removed = store.delete_edges_between(ta, ia, tb, ib)
    # Durable hide (survives clear_relations(origin='derived')).
    store.delete_edges_between(ta, ia, tb, ib, predicates=["hides"])
    store.add_relation(ta, ia, "hides", tb, ib, origin="user", weight=1.0,
                       ts=time.time())
    return {"ok": True, "removed": removed, "source": source, "target": target}


def link_constellation_edge(store: Store, source: str, target: str) -> dict:
    """Assert a manual link and clear any prior user hide between the pair."""
    pa, pb = _parse_constellation_id(source), _parse_constellation_id(target)
    if not pa or not pb or pa == pb:
        return {"ok": False, "error": "need two distinct constellation nodes"}
    (ta, ia), (tb, ib) = pa, pb
    store.delete_edges_between(ta, ia, tb, ib, predicates=["hides"])
    store.add_relation(ta, ia, "linked", tb, ib, origin="user", weight=2.0,
                       ts=time.time())
    return {"ok": True, "source": source, "target": target}


def _short_constellation_label(text: str, *, kind: str) -> str:
    """Keep constellation labels short — long ASR/log lines ruin the sky."""
    t = " ".join((text or "").split())
    if not t:
        return "…"
    for sep in (" — ", " - ", ": ", " | "):
        if sep in t and len(t.split(sep, 1)[0]) >= 3:
            t = t.split(sep, 1)[0]
            break
    cap = 18 if kind == "person" else 22
    return t if len(t) <= cap else (t[: cap - 1] + "…")


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = pow(2.718281828, -x)
        return 1.0 / (1.0 + z)
    z = pow(2.718281828, x)
    return z / (1.0 + z)


_TWO_PI = 6.283185307179586


def _anchor_angle(nid: str) -> float:
    """Stable polar home angle (0..2π) for a node id — the renderer keeps people
    on this angle across rebuilds (spatial memory).

    FNV-1a followed by an avalanche finalizer, so a one-character id change (the
    difference between 'person:1' and 'person:2') diffuses across the whole
    circle. A plain sum-of-char-codes — what this used to be — put sequential ids
    within ~2° of each other, collapsing every person onto one radial spoke and
    defeating the anchor's purpose.
    """
    h = 2166136261
    for ch in nid:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2C1B3C6D) & 0xFFFFFFFF
    h ^= h >> 12
    h = (h * 0x297A2D39) & 0xFFFFFFFF
    h ^= h >> 15
    return round((h / 4294967296.0) * _TWO_PI, 5)


def _age_days(ts: float | None, now: float) -> float:
    if not ts:
        return 21.0
    return max(0.0, (now - float(ts)) / 86400.0)


def _field_v2_enabled() -> bool:
    """QUILL_FIELD_V2 — rank by the trace/activation score instead of shipped
    gravity. Isolated for testability (mock this, not the frozen settings)."""
    try:
        from app.config import settings
        return bool(settings.attention.field_v2)
    except Exception:
        return False


def _due_days(due, now: float) -> float | None:
    if due is None:
        return None
    try:
        if isinstance(due, (int, float)):
            return (float(due) - now) / 86400.0
        # ISO-ish date string
        from datetime import datetime
        s = str(due).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return (dt.timestamp() - now) / 86400.0
    except Exception:
        return None


# Memory Gravity — named knobs so golden tests can catch regressions.
# Structural signals are separate from a single temporal salience term.
GRAVITY = {
    "w": {
        "pin": 1.35,
        "pros": 1.55,
        "rel": 1.15,
        "fut": 0.95,
        "unres": 0.85,
        "cent": 0.70,
        "sem": 0.55,
        "rep": 0.45,
        "temp": 0.70,   # single temporal channel (replaces freq+long+nov pile-up)
        "unc": 0.80,
        "act": 0.90,    # context activation (field v2 only; v1 ignores it)
        # Neglected open commitments gain gravity (WS3) — resists decay bias.
        "aging": 0.95,
        # Meeting Layer P2 — pin-like boost when a notepad jot co-times the turn.
        "note_adjacent": 1.20,
    },
    "recency_horizon_days": 45.0,
    "decay_half_life_days": {
        "idea": 14.0,
        "tool": 60.0,
        "place": 75.0,
        "open_work": 40.0,
        "default": 90.0,
    },
    "trust_lo": 0.20,
    "trust_hi": 0.35,
    "sigmoid_offset": 1.1,
    "min_people_in_focus": 2,
    # Hold focus slots for entities (projects/tools/places/products) too, so a
    # flood of open tasks/commitments can't crowd every project and tool out of
    # the field — the failure the constellation showed with 26 open items.
    "min_entities_in_focus": 3,
}

# Constellation kinds that are "entities" (things you work on/with), as opposed to
# people or open work (task/commitment) — used to reserve focus slots for them.
_ENTITY_FOCUS_KINDS = frozenset({"project", "org", "tool", "place", "idea"})

# Back-compat alias used by older call sites / notebooks.
_W = GRAVITY["w"]


def _select_focus_by_quota(ranked: list[dict], focus_k: int) -> list[dict]:
    """Deprecated shim — quotas now live in ranking.admitter only.

    Kept so older tests that patch this symbol still resolve. New code must
    call `ranking.pipeline.run` (Selector → Admitter), never this function.
    """
    from app.services.ranking.admitter import admit
    from app.services.ranking.types import PipelineContext

    # Seed focus as pure top-k, then let Admitter enforce quotas.
    seed = []
    for n in ranked:
        out = dict(n)
        out["layer"] = "focus"
        seed.append(out)
        if len(seed) >= focus_k:
            break
    return admit(seed, ranked, {}, PipelineContext(focus_k=focus_k))


def _apply_softmax_prominence(nodes: list[dict], *, temperature: float = 0.45
                              ) -> None:
    """Zero-sum attention budget over focus (Field §8.2).

    Prominence is a temperature softmax over focus gravity so Σ prominence
    stays ≈ focus_count (mean 1.0). Periphery stays dimmer and uncapped by
    the budget — it is not competing for the same attention pool.
    """
    import math
    focus = [n for n in nodes if n.get("layer") == "focus"]
    if not focus:
        return
    scores = [float(n.get("gravity") or 0.0) for n in focus]
    peak = max(scores) if scores else 0.0
    t = max(0.05, float(temperature))
    exps = [math.exp((s - peak) / t) for s in scores]
    z = sum(exps) or 1.0
    budget = float(len(focus))
    for n, e in zip(focus, exps):
        n["prominence"] = round(budget * (e / z), 3)
    for n in nodes:
        if n.get("layer") == "periphery":
            g = float(n.get("gravity") or 0.0)
            n["prominence"] = round(min(0.55, 0.22 + g * 0.28), 3)


_ORG_ENTITY_KINDS = frozenset({"org", "company", "organization"})
_PROJECT_ENTITY_KINDS = frozenset({"project"})
_TOOL_ENTITY_KINDS = frozenset(
    {"tool", "software", "app", "service", "product", "platform"})
_PLACE_ENTITY_KINDS = frozenset({"place", "location", "venue"})


def entity_constellation_kind(entity_kind: str | None) -> str:
    """Map store entity.kind → constellation visual kind.

    Orgs stay visually distinct from projects (companies are not diamonds).
    """
    ek = (entity_kind or "").strip().lower()
    if ek in _ORG_ENTITY_KINDS:
        return "org"
    if ek in _PROJECT_ENTITY_KINDS:
        return "project"
    if ek in _TOOL_ENTITY_KINDS:
        return "tool"
    if ek in _PLACE_ENTITY_KINDS:
        return "place"
    return "idea"


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 <= edge0:
        return 1.0 if x >= edge1 else 0.0
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def trust_gate(confidence: float, *, pinned: bool = False) -> float:
    """Soft trust ramp — pinned always fully trusted."""
    if pinned:
        return 1.0
    return _smoothstep(GRAVITY["trust_lo"], GRAVITY["trust_hi"], confidence)


def temporal_salience(age_days: float) -> float:
    """One temporal channel: recency with a brief novelty bump."""
    horizon = GRAVITY["recency_horizon_days"]
    base = max(0.12, min(1.0, 1.0 - (age_days / horizon)))
    if age_days < 1.5:
        base = min(1.0, base + 0.25)
    elif age_days < 4.0:
        base = min(1.0, base + 0.10)
    return base


def decay_for_kind(kind: str, age_days: float) -> float:
    hl = GRAVITY["decay_half_life_days"]
    if kind == "idea":
        half = hl["idea"]
        floor = 0.15
    elif kind == "tool":
        half = hl["tool"]
        floor = 0.30
    elif kind == "place":
        half = hl["place"]
        floor = 0.35
    elif kind in ("commitment", "task"):
        half = hl["open_work"]
        floor = 0.35
    else:
        half = hl["default"]
        floor = 0.45
    return max(floor, pow(0.5, age_days / half))


def score_gravity(*, kind: str, confidence: float, age_days: float,
                  pinned: bool = False, prospective: float = 0.0,
                  relationship: float = 0.0, future: float = 0.0,
                  unresolved: float = 0.0, centrality: float = 0.0,
                  semantic: float = 0.0, repeats: float = 0.0) -> dict:
    """Pure Memory Gravity scorer — used by constellation + golden tests."""
    w = GRAVITY["w"]
    is_pin = 1.0 if pinned else 0.0
    unc = 1.0 - max(0.05, min(1.0, confidence))
    temp = temporal_salience(age_days)
    decay = decay_for_kind(kind, age_days)
    trust = trust_gate(confidence, pinned=pinned)
    raw = (
        w["pin"] * is_pin
        + w["pros"] * prospective
        + w["rel"] * relationship
        + w["fut"] * future
        + w["unres"] * unresolved
        + w["cent"] * centrality
        + w["sem"] * semantic
        + w["rep"] * repeats
        + w["temp"] * temp
        - w["unc"] * unc
    )
    gravity = _sigmoid(raw - GRAVITY["sigmoid_offset"]) * decay * trust
    return {
        "gravity": gravity,
        "raw": raw,
        "decay": decay,
        "trust": trust,
        "temporal": temp,
    }


def pin_constellation_node(store: Store, node_id: str, pinned: bool = True) -> dict:
    parsed = _parse_constellation_id(node_id)
    if not parsed:
        return {"ok": False, "error": "invalid node id"}
    kind, iid = parsed
    store.set_constellation_pin(kind, iid, bool(pinned))
    return {"ok": True, "id": node_id, "pinned": bool(pinned)}


_ALLOWED = {
    "person": ["person", "project", "org", "idea", "tool"],
    "fact": ["commitment", "task"],
    "entity": ["project", "org", "idea", "tool", "place"],
}


def reclassify_constellation_node(store: Store, node_id: str, new_kind: str) -> dict:
    """User correction of constellation category — mutates the underlying store."""
    parsed = _parse_constellation_id(node_id)
    if not parsed:
        return {"ok": False, "error": "invalid node id"}
    ntype, iid = parsed
    new_kind = (new_kind or "").strip().lower()
    if new_kind in ("company", "organization"):
        new_kind = "org"
    allowed = _ALLOWED.get(ntype) or []
    if new_kind not in allowed:
        return {"ok": False, "error": f"kind must be one of {allowed}"}

    if ntype == "fact":
        if not store.reclassify_fact_kind(iid, new_kind):
            return {"ok": False, "error": "could not reclassify fact"}
        return {"ok": True, "id": f"fact:{iid}", "kind": new_kind}

    if ntype == "entity":
        if not store.set_entity_kind(iid, new_kind):
            return {"ok": False, "error": "could not update entity"}
        return {"ok": True, "id": f"entity:{iid}",
                "kind": entity_constellation_kind(new_kind)}

    if ntype == "person":
        if new_kind == "person":
            store.set_constellation_hidden("person", iid, False)
            return {"ok": True, "id": node_id, "kind": "person"}
        res = store.convert_person_to_entity(iid, new_kind)
        if not res:
            return {"ok": False, "error": "could not convert person"}
        return {
            "ok": True,
            "id": f"entity:{res['entity_id']}",
            "kind": entity_constellation_kind(new_kind),
            "replaced": node_id,
        }
    return {"ok": False, "error": "unsupported node"}


def constellation_allowed_kinds(node_id: str) -> list[str]:
    parsed = _parse_constellation_id(node_id)
    if not parsed:
        return []
    return list(_ALLOWED.get(parsed[0]) or [])


def constellation_evidence(store: Store | None, node_id: str) -> dict:
    """Provenance + gravity explanation for a constellation node."""
    store = store or get_store()
    parsed = _parse_constellation_id(node_id)
    if not parsed:
        return {"ok": False, "error": "invalid node id"}
    kind, iid = parsed
    # explain=True so rank breakdown rides with provenance (same interaction).
    field = constellation(store, limit=36, explain=True)
    node = next((n for n in field["nodes"] if n["id"] == node_id), None)
    breakdown = (field.get("breakdowns") or {}).get(node_id)
    sources: list[dict] = []
    detail: dict = {"kind": kind, "id": iid}

    if kind == "person":
        ctx = context_for_person(
            next((p["name"] for p in store.all_people() if p["id"] == iid), ""),
            store,
        )
        detail["person"] = ctx.get("person")
        detail["items"] = (ctx.get("items") or [])[:12]
        detail["discussed_with"] = (ctx.get("discussed_with") or [])[:8]
        for it in detail["items"]:
            if it.get("source_event_id"):
                sources.append({
                    "channel": "memory",
                    "event_id": it["source_event_id"],
                    "text": it.get("text"),
                    "kind": it.get("kind"),
                    "confidence": None,
                })
    elif kind == "fact":
        fact = store.get_fact(iid)
        if fact:
            detail["fact"] = {
                "text": fact.get("text") or fact.get("source_span"),
                "kind": fact.get("kind"),
                "status": fact.get("status"),
                "due": fact.get("due"),
                "confidence": fact.get("confidence"),
                "owner": fact.get("owner"),
                "from_person": fact.get("from_person"),
                "to_person": fact.get("to_person"),
                "source_span": fact.get("source_span") or "",
                "source_event_id": fact.get("source_event_id"),
            }
            sources.append({
                "channel": fact.get("source_modality") or "event",
                "event_id": fact.get("source_event_id"),
                "time": fact.get("source_time"),
                "text": fact.get("text") or fact.get("source_span"),
                "source_span": fact.get("source_span") or "",
                "confidence": fact.get("confidence"),
                "kind": fact.get("kind"),
            })
    elif kind == "entity":
        ents = {e["id"]: e for e in store.all_entities()}
        ent = ents.get(iid)
        if ent:
            detail["entity"] = ent
        rel = store.relations_of("entity", iid)
        for e in (rel.get("in") or [])[:10]:
            if e.get("source_event_id"):
                sources.append({
                    "channel": "relation",
                    "event_id": e.get("source_event_id"),
                    "text": e.get("predicate"),
                    "confidence": e.get("confidence"),
                })

    # Hydrate event snippets + playback clip (plan 3.4).
    from app.services.evidence_playback import hydrate_source
    eids = [int(s["event_id"]) for s in sources if s.get("event_id")]
    emap = store.by_ids_map(eids) if eids else {}
    # Person-linked items may carry their own source_span via fact text.
    fact_span = ""
    if kind == "fact" and detail.get("fact"):
        fact_span = detail["fact"].get("source_span") or ""
    for s in sources:
        ev = emap.get(int(s["event_id"])) if s.get("event_id") else None
        if not ev:
            s["playable"] = False
            continue
        raw = (getattr(ev, "raw", None) or "")[:220]
        s.setdefault("text", raw)
        span = s.get("source_span") or fact_span or ""
        # For person graph items, try matching the item text as the span.
        if not span and kind == "person":
            span = (s.get("text") or "")[:160]
        hydrate_source(s, ev, source_span=span or None)

    # Surface current editable kind for the drawer.
    current_kind = (node or {}).get("kind")
    if kind == "fact" and detail.get("fact"):
        current_kind = detail["fact"].get("kind") or current_kind
    elif kind == "entity" and detail.get("entity"):
        current_kind = entity_constellation_kind(detail["entity"].get("kind"))

    return {
        "ok": True,
        "id": node_id,
        "node": node,
        "detail": detail,
        "sources": sources[:16],
        "why": (node or {}).get("why") or [],
        "gravity": (node or {}).get("gravity"),
        "breakdown": breakdown,
        "allowed_kinds": constellation_allowed_kinds(node_id),
        "current_kind": current_kind,
    }


def constellation(store: Store | None = None, limit: int = 28,
                  record_impressions: bool = False,
                  explain: bool = False) -> dict:
    """Constellation as Memory Field: gravity-ranked focus + periphery.

    Substrate remains a deterministic typed graph. Rendering selects ~focus_k
    high-gravity objects and a soft periphery — not a DB diagram of everything.

    `record_impressions` logs the surfaced nodes (with their score
    decomposition) to the attention ledger — set only by the UI-facing route,
    never by internal reuse (evidence), so one render = one impression set.

    `explain=True` adds a `breakdowns` map (node_id → ScoreBreakdown) for
    surfaced nodes so rank is as auditable as edges. Default omits it to keep
    the payload small; `/field/state?explain=true` opts in.
    """
    store = store or get_store()
    if store.relation_count() == 0:
        try:
            rebuild(store)
        except Exception:
            pass
    now = time.time()
    total = max(12, min(int(limit or 28), 40))
    focus_k = min(12, max(7, total // 3 + 4))
    periphery_m = max(0, total - focus_k)

    pinned = store.user_pinned_nodes()
    hidden = store.user_hidden_pairs()
    node_hidden = store.constellation_hidden_nodes()
    people = [p for p in _real_people(store)
              if ("person", p["id"]) not in node_hidden]
    entities = [e for e in _real_entities(store)
                if ("entity", e["id"]) not in node_hidden]
    open_facts = [
        f for f in store.list_facts(status="open", limit=120, actionable=True)
        if f.get("kind") in ("task", "commitment")
    ]

    # Degree / co-occurrence strength for crude centrality.
    degree: dict[str, float] = {}
    rel_strength: dict[str, float] = {}
    for p in people:
        rel = store.relations_of("person", p["id"])
        src = f"person:{p['id']}"
        for e in (rel.get("out") or []) + (rel.get("in") or []):
            w = float(e.get("weight") or 1)
            degree[src] = degree.get(src, 0) + w
            if e["predicate"] == "co_occurs" and e.get("obj_type") == "person":
                rel_strength[src] = rel_strength.get(src, 0) + w
            elif e.get("subj_type") == "person" and e["predicate"] == "co_occurs":
                rel_strength[src] = rel_strength.get(src, 0) + w

    resolve_cache: dict[str, dict | None] = {}
    unresolved: dict[str, int] = {}
    for f in open_facts:
        for role_key in ("owner", "from_person", "to_person"):
            name = (f.get(role_key) or "").strip()
            person = _resolve_person(store, name, resolve_cache) if name else None
            if person:
                pid = f"person:{person['id']}"
                unresolved[pid] = unresolved.get(pid, 0) + 1

    candidates: dict[str, dict] = {}

    def _base(nid: str, label: str, kind: str, *, ts: float | None,
              confidence: float = 0.7, due=None, meta: dict | None = None) -> dict:
        age = _age_days(ts, now)
        recency = temporal_salience(age)
        return {
            "id": nid,
            "label": _short_constellation_label(label, kind=kind),
            "kind": kind,
            "recency": recency,
            "ts": ts,
            "confidence": max(0.05, min(1.0, float(confidence or 0.5))),
            "due": due,
            "meta": meta or {},
            "_age": age,
        }

    for p in people:
        ts = p.get("last_seen")
        candidates[f"person:{p['id']}"] = _base(
            f"person:{p['id']}", p["name"], "person", ts=ts, confidence=0.9,
            meta={"name": p["name"]},
        )

    for e in entities:
        kind = entity_constellation_kind(e.get("kind"))
        candidates[f"entity:{e['id']}"] = _base(
            f"entity:{e['id']}", e["name"], kind, ts=e.get("last_seen"),
            confidence=0.75, meta={"entity_kind": e.get("kind")},
        )

    for f in open_facts:
        if ("fact", int(f["fact_id"])) in node_hidden:
            continue
        fid = f"fact:{f['fact_id']}"
        ts = f.get("extracted_at") or f.get("source_time")
        fkind = "task" if f.get("kind") == "task" else "commitment"
        candidates[fid] = _base(
            fid, f.get("text") or f.get("source_span") or "item", fkind,
            ts=ts, confidence=float(f.get("confidence") or 0.5), due=f.get("due"),
            meta={"fact_kind": f.get("kind"), "status": f.get("status"),
                  "source_time": f.get("source_time"),
                  "source_event_id": f.get("source_event_id")},
        )

    # Meeting Layer P2: precompute jot timestamps once for note_adjacent.
    note_times: list[float] = []
    try:
        from app.services import meeting_notes as _mnotes
        note_times = _mnotes.jot_times(store, since=now - 14 * 86400, limit=800)
    except Exception:
        note_times = []

    # Memory traces + activation + learned β — fed to FieldV2Scorer via
    # PipelineContext. Always computed so the ledger keeps g1/shadow/v2
    # continuity; only the Scorer choice (QUILL_FIELD_V2) changes ranking.
    dyn_keys = [p for p in (_parse_constellation_id(nid) for nid in candidates)
                if p]
    try:
        dyn_map = store.node_dynamics_map(dyn_keys)
    except Exception:
        dyn_map = {}

    v2_on = _field_v2_enabled()
    try:
        from app.services.activation import activation_field
        act_map = activation_field.activation_map(store)
    except Exception:
        act_map = {}

    learned_w = None
    try:
        from app.services import ranking_learn
        if ranking_learn._learn_enabled():
            ranking_learn.refresh_thompson(store)
            learned_w = ranking_learn.current_beta(store)
    except Exception:
        learned_w = None

    # Feature assembly for the unified ranking pipeline (Scorer → Selector →
    # Admitter). Gravity / Field-v2 scoring and mode reweight live in the
    # Scorer; quotas live only in the Admitter — never as an alternate path.
    for nid, n in candidates.items():
        parsed = _parse_constellation_id(nid)
        is_pin = bool(parsed and parsed in pinned)
        age = n["_age"]
        conf = n["confidence"]
        is_open_work = n["kind"] in ("commitment", "task")

        pros = 0.0
        if is_open_work:
            dd = _due_days(n.get("due"), now)
            if dd is None:
                pros = 0.45 + (1.0 - conf) * 0.15
            elif dd < 0:
                pros = min(1.0, 0.75 + min(14.0, -dd) * 0.04)
            elif dd < 2:
                pros = 0.85
            elif dd < 7:
                pros = 0.55
            else:
                pros = 0.25
        elif n["kind"] == "person":
            pros = min(1.0, unresolved.get(nid, 0) * 0.28)

        rel = min(1.0, (rel_strength.get(nid, 0) ** 0.5) / 4.0)
        fut = 0.0
        if is_open_work:
            dd = _due_days(n.get("due"), now)
            if dd is not None and 0 <= dd <= 14:
                fut = max(fut, 1.0 - dd / 14.0)
        if n["kind"] == "person" and unresolved.get(nid, 0):
            fut = max(fut, min(0.7, unresolved[nid] * 0.2))

        unres = min(1.0, unresolved.get(nid, 0) / 4.0) if n["kind"] == "person" else (
            0.7 if is_open_work else 0.0)
        cent = min(1.0, (degree.get(nid, 0) ** 0.5) / 5.0)
        if n["kind"] == "person":
            sem = 0.55
        elif n["kind"] == "org":
            sem = 0.48
        elif n["kind"] == "project":
            sem = 0.40
        elif n["kind"] == "tool":
            sem = 0.32
        elif is_open_work:
            sem = 0.35
        else:
            sem = 0.20
        rep = min(1.0, degree.get(nid, 0) / 12.0)
        temp = temporal_salience(age)
        parsed_key = parsed or ("", 0)
        act_val = min(1.0, float(act_map.get(parsed_key, 0.0)))

        why: list[str] = []
        if is_pin:
            why.append("Pinned by you")
        if pros >= 0.7:
            why.append("Open promise at risk" if is_open_work
                       else "Multiple open promises")
        elif pros >= 0.45 and is_open_work:
            why.append("Unresolved commitment" if n["kind"] == "commitment"
                       else "Open task")
        if fut >= 0.5:
            why.append("Relevant in the coming days")
        if rel >= 0.45:
            why.append("Strong relationship signal")
        if n["kind"] in ("person", "project", "tool") and age > 30 and temp >= 0.3:
            why.append("Long-term significance")
        if age < 1.5:
            why.append("Recently appeared")
        if conf < GRAVITY["trust_hi"]:
            why.append("Lower extraction confidence")
        if not why:
            why.append("Present in your memory graph")

        n["pinned"] = is_pin
        n["layer"] = "archive"
        n["why"] = why[:3]
        n["anchor"] = _anchor_angle(nid)
        n["prospective_risk"] = round(pros, 3)
        n["relationship_strength"] = round(rel, 3)
        n["recency"] = temp
        # Feature bundle consumed by ranking.Scorer (do not score here).
        n["_feat_pros"] = pros
        n["_feat_rel"] = rel
        n["_feat_fut"] = fut
        n["_feat_unres"] = unres
        n["_feat_cent"] = cent
        n["_feat_sem"] = sem
        n["_feat_rep"] = rep
        n["_feat_temp"] = temp
        n["_feat_act"] = act_val
        from app.services.field_history import aging_signal as _aging_signal
        aging = _aging_signal(age, kind=n["kind"])
        n["_feat_aging"] = aging
        n["aging"] = round(aging, 3)
        n["age_days"] = round(age, 2)
        # Note adjacency: open work whose source turn co-timed a jot.
        note_adj = 0.0
        if is_open_work and note_times:
            try:
                from app.services import meeting_notes as _mnotes
                src_ts = (n.get("meta") or {}).get("source_time") or n.get("ts")
                note_adj = _mnotes.note_adjacent_score(src_ts, note_times)
            except Exception:
                note_adj = 0.0
        n["_feat_note_adjacent"] = note_adj
        if note_adj >= 1.0:
            why = list(n.get("why") or [])
            n["why"] = (["Highlighted in your live notes"] + why)[:3]

    from app.services.ranking.pipeline import run as _rank_run
    from app.services.ranking.types import PipelineContext as _PCtx
    from app.services.ranking.scorer import get_scorer as _get_scorer

    mode_info: dict | None = None
    try:
        from app.services import attention_mode as _amode
        mode_info = _amode.current(store=store, now=now)
    except Exception as exc:
        print(f"[graph] attention mode skipped ({exc}).")

    pipe_ctx = _PCtx(
        store=store,
        now=now,
        focus_k=focus_k,
        mode=mode_info,
        act_map=act_map,
        dyn_map=dyn_map,
        learned_w=learned_w,
        persist_wm=True,
    )
    # QUILL_FIELD_V2 selects Scorer only — pipeline structure is fixed.
    scorer = _get_scorer(field_v2=v2_on)
    result = _rank_run(
        list(candidates.values()),
        ctx=pipe_ctx,
        scorer=scorer,
    )
    focus = result.focus
    ranked = result.ranked
    selection = result.selection
    if result.mode:
        mode_info = result.mode
    # Sync scored fields back onto the candidates map for periphery / ledger.
    for n in ranked:
        candidates[n["id"]] = n

    focus_ids = {n["id"] for n in focus}
    periphery: list[dict] = []
    for n in ranked:
        if n["id"] in focus_ids:
            continue
        # Cluster members absorbed into a focus head stay out of the ring
        absorbed = False
        for f in focus:
            if n["id"] in (f.get("cluster_members") or []):
                absorbed = True
                break
        if absorbed:
            continue
        if len(periphery) >= periphery_m:
            break
        n["layer"] = "periphery"
        periphery.append(n)

    node_list = focus + periphery
    _apply_softmax_prominence(node_list)
    keep = {n["id"] for n in node_list}

    edges: list[dict] = []
    edge_keys: set[str] = set()

    def _hidden(a: str, b: str) -> bool:
        pa, pb = _parse_constellation_id(a), _parse_constellation_id(b)
        if not pa or not pb:
            return False
        pair = (*pa, *pb) if pa <= pb else (*pb, *pa)
        return pair in hidden

    def _add_edge(a: str, b: str, *, weight: float, rel: str,
                  confidence: float = 0.7, manual: bool = False) -> None:
        if a == b or a not in keep or b not in keep or _hidden(a, b):
            return
        key = _pair_key(a, b)
        if key in edge_keys:
            return
        edge_keys.add(key)
        style = "solid" if manual or confidence >= 0.75 else (
            "dashed" if confidence >= 0.45 else "dotted")
        edges.append({
            "source": a, "target": b, "weight": weight,
            "rel": rel, "confidence": round(confidence, 3),
            "manual": bool(manual), "style": style,
        })

    for f in open_facts:
        fid = f"fact:{f['fact_id']}"
        if fid not in keep:
            continue
        conf = float(f.get("confidence") or 0.5)
        for role_key, pred in (("owner", "responsible_for"),
                               ("from_person", "promise"),
                               ("to_person", "promise")):
            name = (f.get(role_key) or "").strip()
            person = _resolve_person(store, name, resolve_cache) if name else None
            if person:
                _add_edge(f"person:{person['id']}", fid, weight=1.4,
                          rel=pred, confidence=conf)

    try:
        from app.services import kg_parity
        read_v2 = kg_parity.read_v2_enabled(store)
    except Exception:
        read_v2 = False

    for p in people:
        src = f"person:{p['id']}"
        if src not in keep:
            continue
        rel = store.relations_of("person", p["id"])
        for e in rel.get("out") or []:
            # Derived co_occurs always stay on relations (never in belief store).
            if e["predicate"] == "co_occurs" and e["obj_type"] == "person":
                _add_edge(src, f"person:{e['obj_id']}",
                          weight=float(e.get("weight") or 1),
                          rel="mentioned_together",
                          confidence=float(e.get("confidence") or 0.55))
            elif e["obj_type"] == "fact":
                _add_edge(src, f"fact:{e['obj_id']}",
                          weight=float(e.get("weight") or 1),
                          rel=e["predicate"] or "related",
                          confidence=float(e.get("confidence") or 0.55))
            elif e["obj_type"] == "entity" and not read_v2:
                pred = (e["predicate"] if e["predicate"] != "associated_with"
                        else "affiliated")
                _add_edge(src, f"entity:{e['obj_id']}",
                          weight=float(e.get("weight") or 1),
                          rel=pred,
                          confidence=float(e.get("confidence") or 0.6))
        # Plan 2.6: person↔entity affiliation edges from kg_beliefs when cutover.
        if read_v2:
            try:
                from app.services import kg_beliefs
                kg_rows = store.list_kg_predicates(
                    subj_type="person", subj_id=int(p["id"]),
                    statuses=("active",))
            except Exception:
                kg_rows = []
            for r in kg_rows:
                if (r.get("obj_type") != "entity"
                        or r["predicate"] not in AFFILIATION_PREDS):
                    continue
                try:
                    conf = float(kg_beliefs.posterior(store, int(r["id"])))
                except Exception:
                    conf = float(r.get("confidence") or 0.6)
                pred = (r["predicate"] if r["predicate"] != "associated_with"
                        else "affiliated")
                _add_edge(src, f"entity:{int(r['obj_id'])}",
                          weight=max(0.5, conf),
                          rel=pred, confidence=conf)

    for ta, ia, tb, ib, w in store.user_linked_pairs():
        _add_edge(f"{ta}:{ia}", f"{tb}:{ib}", weight=max(1.5, w),
                  rel="manual", confidence=1.0, manual=True)

    # Strip internal scoring fields from payload.
    clean_nodes = []
    for n in node_list:
        row = {
            "id": n["id"],
            "label": n["label"],
            "kind": n["kind"],
            "layer": n["layer"],
            "gravity": n["gravity"],
            "prominence": n["prominence"],
            "recency": n["recency"],
            "memory_strength": n.get("memory_strength", 0),
            "relationship_strength": n.get("relationship_strength", 0),
            "prospective_risk": n.get("prospective_risk", 0),
            "confidence": n.get("confidence", 0.5),
            "pinned": bool(n.get("pinned")),
            "anchor": n.get("anchor", 0),
            "why": n.get("why") or [],
        }
        if n.get("age_days") is not None:
            row["age_days"] = n["age_days"]
        if float(n.get("aging") or 0) > 0:
            row["aging"] = n["aging"]
        cn = int(n.get("cluster_n") or 1)
        if cn > 1:
            row["cluster_n"] = cn
        clean_nodes.append(row)

    edge_list = sorted(edges, key=lambda e: -e["weight"])[: max(60, total * 2)]
    insights = _constellation_insights(
        clean_nodes, open_facts, store, cache=resolve_cache)

    if record_impressions:
        try:
            from app.services.attention_ledger import attention_ledger
            attention_ledger.record_field(node_list, store)
        except Exception as exc:  # instrumentation never breaks the field
            print(f"[graph] attention ledger skipped ({exc}).")

    out = {
        "nodes": clean_nodes,
        "edges": edge_list,
        "count": {
            "nodes": len(clean_nodes),
            "edges": len(edge_list),
            "focus": sum(1 for n in clean_nodes if n["layer"] == "focus"),
            "periphery": sum(1 for n in clean_nodes if n["layer"] == "periphery"),
        },
        "editable": True,
        "field": True,
        "insights": insights,
        "selection": selection,
        "mode": ({
            "id": mode_info.get("id"),
            "label": mode_info.get("label"),
            "source": mode_info.get("source"),
            "confidence": mode_info.get("confidence"),
            "quiet": mode_info.get("quiet"),
        } if mode_info else None),
    }
    if explain:
        # Focus + periphery only — keep explain payload bounded.
        out["breakdowns"] = {
            nid: bd.to_dict()
            for nid, bd in (result.breakdowns or {}).items()
            if nid in keep
        }
    return out


def _constellation_insights(nodes: list[dict], open_facts: list[dict],
                            store: Store,
                            cache: dict[str, dict | None] | None = None) -> list[dict]:
    """Sparse active intelligence — high bar, one or two cards max."""
    out: list[dict] = []
    cache = cache if cache is not None else {}
    # Promise concentration on a single person.
    burden: dict[str, list[str]] = {}
    for f in open_facts:
        for role_key in ("owner", "from_person", "to_person"):
            name = (f.get(role_key) or "").strip()
            if not name:
                continue
            person = _resolve_person(store, name, cache)
            if not person:
                continue
            pid = f"person:{person['id']}"
            burden.setdefault(pid, []).append(
                (f.get("text") or "")[:80])
    for pid, items in burden.items():
        if len(items) < 3:
            continue
        label = next((n["label"] for n in nodes if n["id"] == pid), None)
        if not label:
            continue
        out.append({
            "kind": "promise_cluster",
            "confidence": 0.8,
            "text": f"You've promised {label} {len(items)} open things.",
            "node_id": pid,
        })
        break

    rising = [n for n in nodes
              if n["kind"] in ("project", "person") and n["gravity"] >= 0.62
              and n["layer"] == "focus"]
    if rising and len(out) < 2:
        n = rising[0]
        out.append({
            "kind": "centrality",
            "confidence": 0.65,
            "text": f"{n['label']} is quietly central in your field right now.",
            "node_id": n["id"],
        })

    risk = [n for n in nodes if n.get("prospective_risk", 0) >= 0.75
            and n["kind"] in ("commitment", "task")]
    if risk and len(out) < 2:
        n = risk[0]
        out.append({
            "kind": "promise_risk",
            "confidence": 0.78,
            "text": f"At risk: {n['label']}",
            "node_id": n["id"],
        })

    return out[:2]
