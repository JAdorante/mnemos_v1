"""Mint-time person adjudication — the model decides human vs org/tool/junk.

The write-time lexical gate (name_quality.is_plausible_person) can only refuse
structural junk: pronouns, paths, env vars, digits. A Title-Case product name
("PortCo Blogs", "OpenAI Codex") is lexically indistinguishable from a human
name, so when the extractor misfiles one into a person slot, a bogus candidate
person row gets minted. Minting a brand-new person is RARE (a handful a week),
so each new candidate can afford one semantic look from the model — with the
row's accumulated evidence as context — via the ModelRouter (local-first,
schema-enforced). Non-humans are rerouted to the entity graph with the right
kind; debris is soft-hidden. This keeps semantic vocabulary in the model, not
in ever-growing hardcoded token lists (general-code invariant).

Conservative by design: candidate rows only (recognized/active/trusted rows
carry human evidence), protected rows (contact points, user-edited attrs, open
work, self) are never sent to the model, nothing is hard-deleted, and any
model failure or hesitation leaves the row in place — 'unsure' verdicts are
retried once the row accumulates new evidence. QUILL_PERSON_ADJUDICATE=0
turns the pass off.
"""
from __future__ import annotations

import os
import time
from typing import Any

_MAX_FACTS = 6           # evidence lines per prompt — enough context, no bloat

_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["human", "org", "tool", "place", "junk", "unsure"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                       "description": "0.0-1.0"},
        "reason": {"type": "string"},
    },
    "required": ["kind", "confidence"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You judge whether a name in a personal memory graph denotes a human "
    "being or something else. The name was extracted from ambient speech, "
    "documents, or screen text and was filed as a PERSON — your job is to "
    "catch misfiles.\n"
    "- human: any plausible human name, from any culture, unusual names "
    "included.\n"
    "- org: a company, team, fund, or institution.\n"
    "- tool: a product, software, app, model, blog, or publication.\n"
    "- place: a location or venue.\n"
    "- junk: a fragment or debris that names nothing real.\n"
    "- unsure: you genuinely cannot tell.\n"
    "Choose org/tool/place/junk ONLY when the name clearly denotes that "
    "non-human thing. Wrongly reclassifying a real person loses a contact; "
    "keeping a stray product name is cheap — when in doubt, answer unsure."
)


def enabled() -> bool:
    return (os.environ.get("QUILL_PERSON_ADJUDICATE", "1")
            not in ("0", "false", "False"))


def _min_conf() -> float:
    try:
        return float(os.environ.get("QUILL_PERSON_ADJUDICATE_MIN_CONF", "0.6"))
    except ValueError:
        return 0.6


def _gather_context(store, pid: int) -> dict[str, Any]:
    """Evidence for the prompt: linked fact texts, associated entities, and
    the source classes the mentions came from. Best-effort — an orphan row
    just gets its name."""
    facts: list[str] = []
    ents: list[str] = []
    sources: set[str] = set()
    try:
        rel = store.relations_of("person", pid)
        edges = list(rel.get("out") or []) + list(rel.get("in") or [])
    except Exception:
        edges = []
    for e in edges:
        try:
            if (e.get("subj_type") == "person"
                    and int(e.get("subj_id") or 0) == pid):
                ot, oid = e.get("obj_type"), e.get("obj_id")
            else:
                ot, oid = e.get("subj_type"), e.get("subj_id")
            if ot == "fact" and len(facts) < _MAX_FACTS:
                f = store.get_fact(int(oid))
                if f and f.get("text"):
                    facts.append(str(f["text"])[:200])
            elif ot == "entity" and len(ents) < _MAX_FACTS:
                ent = store.get_entity(int(oid))
                if ent and ent.get("name"):
                    ents.append(f"{ent['name']} ({ent.get('kind') or '?'})")
            ev_id = e.get("source_event_id")
            if ev_id:
                ev = store.get_event(int(ev_id))
                if ev and ev.get("source"):
                    sources.add(str(ev["source"]))
        except Exception:
            continue
    return {"facts": facts, "entities": ents, "sources": sorted(sources)}


def _build_prompt(person: dict, ctx: dict[str, Any]) -> str:
    lines = [f"Name filed as a person: {person.get('name') or ''}"]
    aliases = [a for a in (person.get("aliases") or [])
               if a and a != person.get("name")]
    if aliases:
        lines.append("Also seen as: " + ", ".join(aliases[:5]))
    if ctx["sources"]:
        lines.append("Captured from: " + ", ".join(ctx["sources"]))
    if ctx["entities"]:
        lines.append("Linked to: " + "; ".join(ctx["entities"]))
    for t in ctx["facts"]:
        lines.append(f"Mentioned in: {t}")
    if not (ctx["facts"] or ctx["entities"] or ctx["sources"]):
        lines.append("No linked evidence yet — judge the name itself.")
    return "\n".join(lines)


def adjudicate(store, person: dict) -> dict[str, Any] | None:
    """One model verdict for one candidate row, or None when the model call
    failed outright (leave the row unmarked so a later pass retries)."""
    from app.services.model_router import router
    ctx = _gather_context(store, int(person["id"]))
    try:
        res = router.complete_json(
            "person_adjudicate",
            system=_SYSTEM,
            messages=[{"role": "user", "content": _build_prompt(person, ctx)}],
            schema=_SCHEMA, max_tokens=128)
    except Exception as exc:
        print(f"[person_adjudicate] model call failed ({exc}); "
              f"leaving '{person.get('name')}' for a later pass.")
        return None
    kind = str((res or {}).get("kind") or "").strip().lower()
    if kind not in ("human", "org", "tool", "place", "junk", "unsure"):
        return None
    try:
        conf = float((res or {}).get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf > 1.0:      # local models sometimes answer in percent (live: 95.0)
        conf = conf / 100.0
    conf = max(0.0, min(1.0, conf))
    return {"kind": kind, "confidence": conf,
            "reason": str((res or {}).get("reason") or "")[:200],
            "ts": time.time()}


def _apply(store, person: dict, verdict: dict[str, Any]) -> str:
    """Act on a verdict; returns the action taken. Low-confidence non-human
    verdicts are downgraded to 'unsure' (kept)."""
    pid = int(person["id"])
    kind, conf = verdict["kind"], verdict["confidence"]
    if kind in ("org", "tool", "place", "junk") and conf < _min_conf():
        verdict = {**verdict, "kind": "unsure", "demoted_from": kind}
        kind = "unsure"
    store.set_person_adjudication(pid, verdict)
    if kind in ("human", "unsure"):
        return "kept"
    # Reroute the name to the entity graph before hiding the person row —
    # the thing is real, it was just filed on the wrong side of the graph.
    if kind in ("org", "tool", "place"):
        try:
            store.resolve_entity(person.get("name") or "", kind,
                                 ts=time.time())
        except Exception as exc:
            print(f"[person_adjudicate] entity reroute failed for "
                  f"'{person.get('name')}' ({exc}); hiding anyway.")
    store.set_person_hidden(pid, hidden=True, public_figure=False)
    return f"hidden_{kind}"


def _eligible(person: dict, *, self_pid: int | None,
              open_work: set[int]) -> bool:
    pid = int(person["id"])
    if person.get("hide_from_people") or person.get("canonical_person_id"):
        return False
    if (person.get("promotion_state") or "candidate") != "candidate":
        return False
    if self_pid is not None and pid == self_pid:
        return False
    if pid in open_work:
        return False
    adj = person.get("adjudication")
    if adj:
        # Re-judge only an 'unsure' row that has seen new evidence since.
        if (adj.get("kind") == "unsure"
                and (person.get("last_seen") or 0) > (adj.get("ts") or 0)):
            return True
        return False
    return True


def run_once(store=None, *, limit: int = 8) -> dict[str, Any]:
    """Adjudicate up to `limit` unjudged candidate people. Returns a summary
    {checked, kept, hidden, failed}. Safe to call repeatedly — rows are only
    ever judged once per evidence state."""
    if not enabled():
        return {"checked": 0, "kept": 0, "hidden": 0, "failed": 0,
                "disabled": True}
    if store is None:
        from app.storage import get_store
        store = get_store()
    from app.services.ambient_cleanup import (_person_open_work,
                                              _person_protected)
    try:
        from app.services import self_profile
        self_pid = self_profile.self_person_id(store)
    except Exception:
        self_pid = None
    open_work = _person_open_work(store)
    checked = kept = hidden = failed = 0
    for p in store.all_people():
        if checked >= limit:
            break
        if not _eligible(p, self_pid=self_pid, open_work=open_work):
            continue
        if _person_protected(store, int(p["id"])):
            continue
        checked += 1
        verdict = adjudicate(store, p)
        if verdict is None:
            failed += 1
            continue
        action = _apply(store, p, verdict)
        if action == "kept":
            kept += 1
        else:
            hidden += 1
            print(f"[person_adjudicate] '{p.get('name')}' -> "
                  f"{verdict['kind']} ({verdict['confidence']:.2f}); "
                  "rerouted off the people graph.")
    return {"checked": checked, "kept": kept, "hidden": hidden,
            "failed": failed}


def run_job(_payload=None) -> None:
    """Worker entrypoint — chained after graph rebuilds (mint-adjacent)."""
    try:
        run_once()
    except Exception as exc:
        print(f"[person_adjudicate] pass skipped ({exc}).")
