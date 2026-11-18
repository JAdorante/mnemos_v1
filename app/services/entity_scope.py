"""Entity model, Phase 3: scope. A name that lives inside one project.

Some names only mean something inside one piece of work: a milestone id, a
codename, an option letter, a working title the model called a project. The
Orgs & tools tab lists them beside the work they belong to unless something
says where they belong. This module says it, from evidence only:

For each visible entity of kind `label`, `idea` or `thing`, or of kind
`project` that is not a filed project, collect its evidence events:

- the events its ledger sightings came from (`entity_mentions`, bound to
  it), with the project each event was filed under; a sighting whose event
  was filed nowhere counts for the project the model named as its `parent`,
  but only when that names a filed project;
- the source events of the facts `about` it, with the project each was
  filed under (`events.meta["project"]`);
- the event whose extraction minted the row (`Store.creation_events`), the
  one sighting a row from before the ledger keeps.

If every evidence event carries the same filing project P, P is a filed
project (a filing target: `projects.filed_target_ids`) other than the
entity itself, and there are at least `QUILL_ENTITY_SCOPE_MIN_EVENTS`
(default 2) evidence events (`QUILL_ENTITY_SCOPE_MIN_EVENTS_LABEL`, default
1, for a `label`), the entity's parent is P (`Store.set_entity_parent`,
source `rule`). An event filed nowhere, or under another project, is
evidence outside P: it keeps a parent from being set, and removes a parent
the rule set before. Screen text and facts with no source event are
neutral: they echo what is on the user's screen (often the project's own
notes) and say nothing about where a name is used.

Never children: orgs, tools and places (they keep the rollup's home
project); a filed project; a name that is an org by evidence whatever its
kind says (a legal form, a known person's email domain, a person who works
at it or belongs to it, or the short form of an org row's name: "Acme" next
to "Acme Freight"); a shortening of the project's own name ("Atlas" inside
"Atlas Rollout" is the project, a duplicate for repair to merge, not a part
of it); and a value ("$49", a date). A parent the user set, or removed, is
never touched, and nothing goes inside a project the user unlinked it from
in the graph (a `hides` edge between the two).

Runs in the `graph` worker job after `project_rollup.run()`. Bind-only: it
links existing entities and never mints one. Kill switch:
QUILL_ENTITY_SCOPE=0 (existing parents stay; nothing new is written).
"""
from __future__ import annotations

import os

from app.services import entity_typing

_OFF = ("0", "false", "False")
_DEFAULT_MIN_EVENTS = 2
_DEFAULT_MIN_EVENTS_LABEL = 1
# The kinds that may live inside a project (`project` only when not filed).
SCOPED_KINDS = frozenset({"label", "idea", "thing", "project"})
# Event sources whose evidence is neutral (screen OCR echoes the screen).
_NEUTRAL_SOURCE_PREFIXES = ("desktop.screen",)


def enabled() -> bool:
    return os.environ.get("QUILL_ENTITY_SCOPE", "1") not in _OFF


def _count(var: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(var, default)))
    except (TypeError, ValueError):
        return default


def min_events() -> int:
    """QUILL_ENTITY_SCOPE_MIN_EVENTS: distinct evidence events a topic, a
    thing or an unfiled project needs, all inside one filed project."""
    return _count("QUILL_ENTITY_SCOPE_MIN_EVENTS", _DEFAULT_MIN_EVENTS)


def min_events_label() -> int:
    """QUILL_ENTITY_SCOPE_MIN_EVENTS_LABEL: the same for a `label`, which
    by its kind already means something inside one project."""
    return _count("QUILL_ENTITY_SCOPE_MIN_EVENTS_LABEL",
                  _DEFAULT_MIN_EVENTS_LABEL)


def _neutral_source(source: str | None) -> bool:
    return (source or "").lower().startswith(_NEUTRAL_SOURCE_PREFIXES)


def _exact(name: str | None) -> str:
    return " ".join((name or "").casefold().split())


class _Targets:
    """Filing targets, and which one a filed project name means: the
    target whose canonical name it is (case and spacing aside: the row a
    filing made carries the filed name itself), else the one target whose
    name or alias (a renamed project keeps its old name as one) has its
    `projects.norm` key. "Heron" and "Project Heron" share a key, so the
    key alone names neither; a note filed under "Project Heron" still goes
    to that row."""

    def __init__(self, entities: dict[int, dict]):
        from app.services import projects
        try:
            self.ids = projects.filed_target_ids(entities.values())
        except Exception as exc:
            print(f"[entity_scope] filing targets skipped ({exc}).")
            self.ids = set()
        self._exact: dict[str, int] = {}
        self._key: dict[str, int] = {}
        ambiguous: set[str] = set()
        for tid in sorted(self.ids):
            e = entities.get(tid) or {}
            if _exact(e.get("name")):
                self._exact.setdefault(_exact(e.get("name")), tid)
            for n in [e.get("name")] + list(e.get("aliases") or []):
                k = projects.norm(n)
                if not k:
                    continue
                if k in self._key and self._key[k] != tid:
                    ambiguous.add(k)
                self._key.setdefault(k, tid)
        for k in ambiguous:
            self._key.pop(k, None)

    def of(self, name: str | None) -> int | None:
        from app.services import projects
        hit = self._exact.get(_exact(name))
        return hit if hit is not None else self._key.get(projects.norm(name))


def _events(evidence: dict[str, list[dict]]) -> dict[int, dict[int, dict]]:
    """{entity_id: {event_id: {"source", "filed", "parents"}}}: one entry
    per distinct evidence event, the facts' and the ledger's merged. A fact
    with no source event, or whose event was purged, cannot be placed and
    is left out (neutral)."""
    out: dict[int, dict[int, dict]] = {}

    def slot(eid, ev, source):
        per = out.setdefault(int(eid), {})
        d = per.get(int(ev))
        if d is None:
            d = per[int(ev)] = {"source": source or "", "filed": None,
                                "parents": set()}
        elif source and not d["source"]:
            d["source"] = source
        return d

    # The facts' events, and the event whose extraction minted the row (the
    # one sighting a row from before the ledger has; a batch extracted at
    # one instant counts once, or not at all when its events disagree:
    # `Store.creation_events`).
    for r in list(evidence.get("facts") or ()) + list(
            evidence.get("created") or ()):
        if r.get("entity_id") is None or r.get("event_id") is None \
                or not r.get("has_event", True) or r.get("mixed"):
            continue                # no source event to place: neutral
        d = slot(r["entity_id"], r["event_id"], r.get("event_source"))
        fp = r.get("filing_project")
        if isinstance(fp, str) and fp.strip():
            d["filed"] = fp.strip()
    for r in evidence.get("mentions") or ():
        if r.get("entity_id") is None or r.get("event_id") is None:
            continue
        d = slot(r["entity_id"], r["event_id"], r.get("event_source"))
        fp = r.get("filing_project")
        if isinstance(fp, str) and fp.strip() and not d["filed"]:
            d["filed"] = fp.strip()
        if isinstance(r.get("parent"), str) and r["parent"].strip():
            d["parents"].add(r["parent"].strip())
    return out


def _key_words(name: str | None) -> tuple[str, ...]:
    """A name as words to compare (`projects.norm`: case, punctuation and
    a leading "the" or "project" do not count)."""
    from app.services import projects
    return tuple(projects.norm(name).split())


def _shortens(short: tuple, full: tuple) -> bool:
    """`short` is the first or last words of `full`, and not all of it."""
    n = len(short)
    return 0 < n < len(full) and (full[:n] == short or full[-n:] == short)


def _org_words(entities: dict[int, dict]) -> list[tuple[str, ...]]:
    from app.services.name_quality import normalize_entity_kind
    return [w for w in (_key_words(e.get("name")) for e in entities.values()
                        if normalize_entity_kind(e.get("kind")) == "org")
            if len(w) > 1]


def _eligible(store, e: dict, targets: set[int], *,
              affiliated: set[int], org_words: list) -> str | None:
    """Why this row can never be a child, or None when it may be one."""
    from app.services.name_quality import normalize_entity_kind
    kind = normalize_entity_kind(e.get("kind"))
    if kind not in SCOPED_KINDS:
        return "kind"
    if int(e["id"]) in targets:
        return "filed"
    name = e.get("name") or ""
    if entity_typing.value_literal(name):
        return "value"
    if int(e["id"]) in affiliated:
        return "people_work_there"
    words = _key_words(name)
    if words and any(full[:len(words)] == words for full in org_words
                     if len(full) > len(words)):
        return "org_short_form"
    return None


def _org_by_evidence(store, e: dict) -> bool:
    """A legal form or a known person's email domain in the canonical name
    or a confirmed alias (`entity_typing.org_by_evidence`). Asked only of a
    row that would otherwise get a parent: it reads the alias table."""
    name = e.get("name") or ""
    try:
        names = [name] + [r["alias"] for r in store.list_entity_aliases(
            entity_id=int(e["id"]), confirmed=True, limit=50)]
    except Exception:
        names = [name]
    try:
        return entity_typing.org_by_evidence(store, names) is not None
    except Exception:
        return False


def compute(store, *, entities: dict[int, dict] | None = None,
            evidence: dict | None = None,
            kinds: dict[int, str] | None = None) -> list[dict]:
    """The parent every visible entity should have by the rule, against
    what it has. One row per entity with evidence or a parent (the rest
    have nothing to decide):

    {entity_id, name, kind, current_parent_id, current_source,
     parent_id, parent_name, action, reason, inside, outside, neutral}

    `action` is `set` (give or change a rule parent), `clear` (take a rule
    parent away), `keep` (nothing to do) or `held` (a parent the user or a
    filing set, or a removal the user made, is left alone). Reads only.

    `entities` (visible rows by id), `evidence` (as
    `Store.entity_scope_evidence` returns it) and `kinds` ({id: kind})
    stand in for the store's own: the repair plan asks what the rule decides
    on the store as its changes will leave it (rows merged away left out, a
    survivor's evidence pooled, the kinds it plans)."""
    if entities is None:
        entities = {int(e["id"]): e for e in store.all_entities()}
    if kinds:
        entities = {eid: ({**e, "kind": kinds[eid]} if eid in kinds else e)
                    for eid, e in entities.items()}
    if not entities:
        return []
    tg = _Targets(entities)
    targets = tg.ids
    if evidence is None:
        evidence = store.entity_scope_evidence()
    events = _events(evidence)
    affiliated = set(evidence.get("affiliated") or ())
    org_words = _org_words(entities)
    need, need_label = min_events(), min_events_label()
    # Pairs the user unlinked in the graph: the rule never puts one inside
    # the other (that would draw the link back).
    try:
        unlinked = {(a, b) for ta, a, tb, b in store.user_hidden_pairs()
                    if ta == "entity" and tb == "entity"}
    except Exception:
        unlinked = set()
    out: list[dict] = []
    for eid, e in entities.items():
        cur = e.get("parent_id")
        cur = int(cur) if cur is not None else None
        cur_src = (e.get("parent_source") or "").strip().lower() or None
        evs = events.get(eid) or {}
        if not evs and cur is None:
            continue                # no evidence, no parent: nothing to do
        row = {"entity_id": eid, "name": e.get("name") or "",
               "kind": e.get("kind") or "", "current_parent_id": cur,
               "current_source": cur_src, "parent_id": None,
               "parent_name": None, "action": "keep", "reason": "",
               "inside": 0, "outside": 0, "neutral": 0}
        why_not = _eligible(store, e, targets, affiliated=affiliated,
                            org_words=org_words)
        inside: dict[int, int] = {}
        for d in evs.values():
            if _neutral_source(d["source"]):
                row["neutral"] += 1
                continue
            filed = tg.of(d["filed"]) if d["filed"] else None
            if filed is None and not d["filed"]:
                # Filed nowhere: the model's `parent` counts when it names
                # a filed project (and only one).
                named = {tg.of(p) for p in d["parents"]}
                named.discard(None)
                if len(named) == 1:
                    filed = next(iter(named))
            if filed is None:
                row["outside"] += 1
            else:
                inside[filed] = inside.get(filed, 0) + 1
        row["inside"] = sum(inside.values())
        want = None
        if why_not is not None:
            row["reason"] = why_not
        elif not inside:
            row["reason"] = "no_filed_evidence"
        elif row["outside"] or len(inside) > 1:
            row["reason"] = "outside_project"
        else:
            pid, n = next(iter(inside.items()))
            floor = need_label if (e.get("kind") or "") == "label" else need
            if pid == eid:
                row["reason"] = "self"
            elif _shortens(_key_words(e.get("name")),
                           _key_words((entities.get(pid) or {}).get("name"))):
                row["reason"] = "names_the_project"
            elif n < floor:
                row["reason"] = "too_few_events"
            elif (min(eid, pid), max(eid, pid)) in unlinked:
                row["reason"] = "user_unlinked"
            elif _org_by_evidence(store, e):
                row["reason"] = "org_by_evidence"
            else:
                want = pid
                row["reason"] = "one_filed_project"
        row["parent_id"] = want
        row["parent_name"] = (entities.get(want) or {}).get("name") \
            if want is not None else None
        if cur_src in ("user", "filing"):
            row["action"] = "held"
        elif want is not None and want != cur:
            row["action"] = "set"
        elif want is None and cur is not None:
            row["action"] = "clear"
        out.append(row)
    out.sort(key=lambda r: (r["action"], r["name"].lower()))
    return out


def run(store=None) -> dict:
    """Apply `compute`: set and clear rule parents. Same inputs, same
    parents; a second run writes nothing."""
    if store is None:
        from app.storage import get_store
        store = get_store()
    if not enabled():
        return {"enabled": False, "set": 0, "cleared": 0, "held": 0,
                "refused": 0}
    plan = compute(store)
    done = {"enabled": True, "set": 0, "cleared": 0, "held": 0,
            "refused": 0, "changes": []}
    for r in plan:
        if r["action"] == "held":
            done["held"] += 1
            continue
        if r["action"] not in ("set", "clear"):
            continue
        ok = store.set_entity_parent(r["entity_id"], r["parent_id"],
                                     source="rule")
        if not ok:
            done["refused"] += 1
            continue
        done["set" if r["action"] == "set" else "cleared"] += 1
        done["changes"].append({k: r[k] for k in (
            "entity_id", "name", "kind", "action", "parent_id",
            "parent_name", "current_parent_id", "reason")})
    return done
