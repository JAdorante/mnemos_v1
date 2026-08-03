"""Detect and soft-hide ambient (news/social) people & entities.

Used by scripts/ambient_cleanup.py. Conservative on purpose: never hide
promoted contacts, open-work parties, or clean onboarding tools.

Also plans kind remaps (product→tool) and person-shaped project cleanup
(hide entity + mint person) — reversible, no hard deletes.
"""
from __future__ import annotations

import re
import time
from typing import Any

from app.services.name_quality import (
    is_person_shaped_entity_name,
    is_plausible_entity,
    is_plausible_person,
    normalize_entity_kind,
)

# Only these source_policy classes count as "ambient media" for cleanup.
_STRICT_AMBIENT = {
    "news_page", "social_feed", "browser_article", "advertisement",
}

# Lowercase-start camelCase only (extractEntities). Brands like OpenAI stay.
_CODE_JUNK = re.compile(
    r"(?i:[\\/(){}<>;=]|::|_|"
    r"^(?:stack_|test-|user-)|"
    r"\.(?:py|md|json|html|gs|exe)\b|"
    r"\b(?:api key|worktree|env var|localhost)\b)|"
    r"^[a-z]+[A-Z]"
)

# Legacy extractor kinds that should be remapped to the store canonical set.
_REMAP_FROM = frozenset({
    "product", "software", "app", "service", "platform",
    "company", "organization", "other", "location", "venue",
})


def _name(row: dict) -> str:
    return (row.get("name") or row.get("canonical_name") or "").strip()


def _classify_event(store, event_id: int | None) -> str | None:
    if not event_id:
        return None
    try:
        ev = store.get_event(int(event_id))
    except Exception:
        return None
    if not ev:
        return None
    src = (ev.get("source") or "")
    meta = ev.get("meta") if isinstance(ev.get("meta"), dict) else {}
    try:
        from app.services import source_policy as sp
        return sp.policy_for_event(
            event_source=src,
            window=str(meta.get("window") or ""),
            text=(ev.get("raw") or ev.get("summary") or "")[:800],
        ).source_class
    except Exception:
        return None


def _person_open_work(store) -> set[int]:
    out: set[int] = set()
    try:
        for kind in ("task", "commitment"):
            for f in store.list_facts(kind=kind, status="open", limit=5000):
                for key in ("owner_person_id", "from_person_id", "to_person_id"):
                    if f.get(key) is not None:
                        out.add(int(f[key]))
    except Exception:
        pass
    return out


def _person_protected(store, pid: int) -> bool:
    try:
        if store.list_contact_points(pid):
            return True
    except Exception:
        pass
    try:
        if store.person_attrs(pid):
            return True
    except Exception:
        pass
    return False


def _edge_classes(store, edges: list[dict]) -> list[str]:
    classes = []
    for e in edges:
        cls = _classify_event(store, e.get("source_event_id"))
        if cls:
            classes.append(cls)
    return classes


def plan_people(store, *, limit: int = 500) -> list[dict]:
    from app.services import self_profile
    try:
        self_pid = self_profile.self_person_id(store)
    except Exception:
        self_pid = None
    open_work = _person_open_work(store)
    out: list[dict] = []
    for p in store.all_people():
        if len(out) >= limit:
            break
        if not p or p.get("hide_from_people") or p.get("canonical_person_id"):
            continue
        pid = int(p["id"])
        if self_pid is not None and pid == self_pid:
            continue
        if pid in open_work or _person_protected(store, pid):
            continue
        state = (p.get("promotion_state") or "candidate").lower()
        if state in ("active", "recognized", "trusted"):
            continue
        name = _name(p)
        if not is_plausible_person(name):
            out.append({"id": pid, "name": name, "promotion_state": state,
                        "reason": "implausible_name"})
            continue

        rel = store.relations_of("person", pid)
        edges = list(rel.get("out") or []) + list(rel.get("in") or [])
        if not edges:
            # Orphan people: only hide if also flagged public_figure already,
            # or name is a known single-token celebrity pattern — skip orphans;
            # too many false positives (real contacts with thin graphs).
            continue

        classes = _edge_classes(store, edges)
        if not classes:
            continue
        # ALL classifiable support must be strict ambient media.
        if all(c in _STRICT_AMBIENT for c in classes):
            out.append({
                "id": pid, "name": name, "promotion_state": state,
                "reason": "news_social_only",
                "classes": sorted(set(classes)),
            })
    return out


def _is_seed_tool(name: str, kind: str | None) -> bool:
    """Onboarding / real software — never auto-hide just for lacking edges."""
    k = (kind or "").lower()
    if k not in ("tool", "product", "software", "app", "service", "platform"):
        return False
    n = (name or "").strip()
    words = n.split()
    if len(words) > 3:
        return False
    if not n[:1].isupper():
        return False
    if _CODE_JUNK.search(n):
        return False
    return True


_DEBRIS_TAIL = {
    "project", "agenda", "campaign", "event", "pipeline", "page", "docs",
    "key", "test", "utils", "suite",
}


def _looks_like_entity_junk(name: str, kind: str | None) -> bool:
    n = (name or "").strip()
    if not n or not is_plausible_entity(n):
        return True
    if _CODE_JUNK.search(n):
        return True
    words = n.split()
    clean = [w.strip(".,") for w in words]
    if len(words) >= 4:
        # Corporate suffixes don't make a real org into junk.
        if clean and clean[-1].lower() not in {
            "llc", "inc", "ltd", "corp", "co", "plc", "corporation",
        }:
            return True
    low = n.lower()
    if low in {"unknown", "not specified", "contacts", "mom", "justin",
               "environment", "readable", "login", "messages", "flight",
               "renewal", "beachhead", "shadowing", "unknown organization",
               "extraction", "consolidation", "capture", "prompt", "crm"}:
        return True
    k = (kind or "").lower()
    # Keep places unless already failed plausible/code checks.
    if k == "place":
        return False
    # Person-shaped names misfiled as projects/other (shared with write-time gate).
    if k in ("other", "project", "idea", "") and is_person_shaped_entity_name(n):
        return True
    # Multi-word debris phrases (campaigns, pages, pipelines…).
    if len(clean) >= 3 and (
        n == n.lower() or clean[-1].lower() in _DEBRIS_TAIL
    ):
        return True
    # Unknown-kind long phrases only if clearly non-Title-Case debris.
    if k in ("", "?", "other") and len(clean) >= 3:
        if not all(w[:1].isupper() for w in clean if w[:1].isalpha()):
            return True
    # Title-Case brands / orgs / tools with ≤4 words (incl. LLC) stay.
    if n[:1].isupper() and len(clean) <= 4 and not any(c.isdigit() for c in n):
        return False
    return False


def plan_entities(store, *, limit: int = 500) -> list[dict]:
    """Plan entity hygiene actions: reclassify, hide+person, or soft-hide.

    Each row includes `action`:
      - reclassify — set kind to `to_kind` (product→tool, …)
      - hide_person — soft-hide entity and mint/resolve a person with same name
      - hide — soft-hide only (orphan junk / news-social-only)
    """
    out: list[dict] = []
    for e in store.all_entities(include_hidden=False):
        if len(out) >= limit:
            break
        eid = int(e["id"])
        name = _name(e)
        kind = e.get("kind")
        kind_l = (kind or "").strip().lower()

        # 0) Person-shaped FIRST — even when mislabeled as product/other.
        #    (Otherwise "Russell Fry" product→tool and "Abby Nengel" other→idea.)
        if is_person_shaped_entity_name(name):
            out.append({
                "id": eid, "name": name, "kind": kind,
                "action": "hide_person",
                "reason": "person_shaped",
            })
            continue

        # 1) Clear kind remaps (product→tool, company→org, other→idea, …)
        if kind_l in _REMAP_FROM:
            to_kind = normalize_entity_kind(kind_l)
            if to_kind != kind_l:
                out.append({
                    "id": eid, "name": name, "kind": kind,
                    "action": "reclassify", "to_kind": to_kind,
                    "reason": f"remap_{kind_l}_to_{to_kind}",
                })
                continue

        # (person_shaped already handled above)

        rel = store.relations_of("entity", eid)
        edges = list(rel.get("out") or []) + list(rel.get("in") or [])

        if not edges:
            if _is_seed_tool(name, kind):
                continue
            # Orphan single-token orgs/products stay; only clear junk debris.
            if _looks_like_entity_junk(name, kind):
                out.append({
                    "id": eid, "name": name, "kind": kind,
                    "action": "hide", "reason": "orphan_junk",
                })
            continue

        classes = _edge_classes(store, edges)
        if classes and all(c in _STRICT_AMBIENT for c in classes):
            protected = False
            for edge in edges:
                pred = edge.get("predicate") or ""
                if pred not in ("works_at", "uses", "member_of", "founded"):
                    continue
                if edge.get("subj_type") == "person" or edge.get("obj_type") == "person":
                    protected = True
                    break
            if protected:
                continue
            out.append({
                "id": eid, "name": name, "kind": kind,
                "action": "hide",
                "reason": "news_social_only",
                "classes": sorted(set(classes)),
            })
    return out


def plan(store, *, limit: int = 500) -> dict[str, list]:
    return {
        "people": plan_people(store, limit=limit),
        "entities": plan_entities(store, limit=limit),
        "ts": time.time(),
    }


def apply(store, plan_data: dict) -> dict[str, list]:
    """Apply hygiene plan: hide people; reclassify / hide(+person) entities."""
    applied_p, applied_e = [], []
    for p in plan_data.get("people") or []:
        store.set_person_hidden(int(p["id"]), hidden=True, public_figure=True)
        applied_p.append(p)
    for e in plan_data.get("entities") or []:
        eid = int(e["id"])
        action = (e.get("action") or "hide").strip().lower()
        row = dict(e)
        if action == "reclassify":
            to_kind = e.get("to_kind") or normalize_entity_kind(e.get("kind"))
            if store.set_entity_kind(eid, to_kind):
                row["applied_kind"] = to_kind
                applied_e.append(row)
            continue
        if action == "hide_person":
            name = _name(e)
            if name and is_plausible_person(name):
                try:
                    store.resolve_person(name, ts=time.time())
                    row["minted_person"] = True
                except Exception:
                    row["minted_person"] = False
            store.set_entity_hidden(eid, hidden=True)
            applied_e.append(row)
            continue
        # Default: soft-hide
        store.set_entity_hidden(eid, hidden=True)
        applied_e.append(row)
    return {"people": applied_p, "entities": applied_e}


def backfill_kg(store, *, limit: int = 50_000) -> dict[str, int]:
    from app.services import kg_beliefs
    with store._lock:
        rows = store._conn.execute(
            "SELECT subj_type, subj_id, predicate, obj_type, obj_id, "
            "origin, source_event_id, confidence, created_at "
            "FROM relations WHERE origin IN ('asserted', 'user') "
            "LIMIT ?", (int(limit),)
        ).fetchall()
    n_pred = n_ev = skipped = 0
    for r in rows:
        try:
            out = kg_beliefs.record_from_relation(
                store,
                subj_type=r["subj_type"], subj_id=int(r["subj_id"]),
                predicate=r["predicate"], obj_type=r["obj_type"],
                obj_id=int(r["obj_id"]), origin=r["origin"] or "asserted",
                source_event_id=r["source_event_id"],
                confidence=r["confidence"], ts=r["created_at"])
            if out.get("ok"):
                n_pred += 1
                if out.get("evidence_id"):
                    n_ev += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    return {"predicates": n_pred, "evidence": n_ev, "skipped": skipped,
            "scanned": len(rows)}
