"""Entity model, Phase 4: repair the rows that already exist.

Phases 0-3 protect new writes. The rows already in a store were made before
them: a name an assistant note mentioned once became a "project" for good, a
milestone id sits beside the project it belongs to, one thing is two rows.
This module plans one supervised pass over every visible entity row and
applies it, two ways:

- reviewed: `plan(store)` is a diff: per row keep, re-kind, re-parent, merge,
  hide or drop, each with its rule, a reason and evidence counts.
  `scripts/entity_repair.py` writes it as JSON for the user to read, and
  `apply(store, plan)` applies exactly that plan (it refuses a plan made for
  another database, by other rules, one the store has moved on from, or one
  with an action the rules do not make now: `check`).
- automatic, for a seat whose user never reads a diff: `auto_run(store)`
  applies only high-confidence, reversible changes (kind changes, hides,
  parent links; a drop is a hide on this path, never a delete) and leaves
  everything else (merges, deletions, anything below
  QUILL_ENTITY_REPAIR_AUTO_MIN_CONF, anything on a row the user touched) in
  the Inbox (`proposals`, `decide`; accepted there, a drop is a hide the
  user can undo, and a merge is made only after a backup). It runs once per
  `RULES_VERSION`, only after a backup of the database, only when
  QUILL_ENTITY_REPAIR_AUTO=1, never on a read-only store and never while
  another repair run is in flight (one run at a time across processes: the
  run row is inserted only when none is unfinished, in one statement,
  `Store.entity_repair_start_run`). Only the newest few backups are kept
  (`prune_backups`).

Every change either path makes is logged with the value before it
(`Store.entity_repair_log`); `undo(store, run_id=... | change_id=...)` puts
a field back, and refuses one the user (or anything else) changed since;
undoing a run withdraws the questions it left in the Inbox.

The rules, in order (the first that decides a row ends it, except that the
kind rules 5-8 and the scope rule 9 add up):

1. A row whose kind the user set is kept. A row the user touched (a detail
   or a note they wrote, an edge they drew (a pin or a graph hide
   included), a parent they chose, a rename, an alias they typed, any
   `entities.*` decision of theirs on it, a repair hide they undid) is kept
   as well; only the filing and org-evidence priors (5, 6) may still
   re-kind it (asked), and nothing destructive happens to it.
2. A value ("$49", "Friday") is hidden and becomes a `thing`.
3. A name `name_quality.is_plausible_entity` refuses, or a placeholder
   ("unknown"), is dropped and tombstoned (a company whose name is its
   domain, "Acme.io", is not; a dressed-up project name beside its plain
   twin, "Atlas (project)", is left to the merge of rule 4).
   2b. A topic, thing, label or model "project" named exactly like a known
   person, or by a given name only one known person of this seat has, is
   dropped (`person`: who it probably is). A given name is asked, and never
   tombstoned.
4. Duplicates merge: the same name key, the same name without spaces, a
   project name dressed up ("The Atlas project", "Atlas (project)"), the
   seat user's own possessive ("<user>'s Atlas") of a row that exists, and
   a shortening of a filed project used only inside it, and the short form
   of one org row's name kept as a topic. Survivor: filed,
   then the user's, then the plain spelling, then most facts, then oldest.
5. A filed name (the shared or filed registry tiers) is a `project` at
   filing rank, or an `org` when it is an org by evidence (a legal form, a
   known person's email domain).
6. Org evidence makes an org: a legal form or an email domain (rule rank),
   or people who work at a topic written as a proper name (asked, not
   applied on its own). On a row from before kind provenance only the
   email domain (a prior the kind vote applies too) is automatic; a legal
   form is asked.
   (Check B) A row every supporting event of which is a source that may not
   mint (an assistant note, screen text), and that the user never typed, is
   dropped without a tombstone, so
   a later real sighting can bring it back; with `note_only="label"` it is
   kept as a label under its filing project instead.
10. (before 7, since it drops) A lowercase common-noun phrase with at most
   one supporting event is dropped and tombstoned. What the user typed in
   their own chat counts (the ledger's typed sightings, or on a store from
   before the ledger the chat turns themselves): typed in as many sessions
   as the candidate gate asks, the row stays; typed at all, it is never
   tombstoned. An automatic hide by any rule but a value's is lifted when
   real sightings meet the gate's promotion rule (`lift_note_only_hide`).
7. A code-shaped name ("k9", "Phase 1", "4e7a1f0c") is a `label`. One the
   model called an org or a tool stays one only when two independent
   sightings agree (`entity_typing.code_named_kind`); otherwise the change
   is asked, not applied on its own.
8. A model "project" that no filing made is a `label` when the scope rule
   puts it inside one filed project, else a topic (`idea`). A row from
   before kind provenance (whose kind the user may have chosen) is changed
   only on positive evidence that it is no project, never for lacking a
   filing: the scope rule at a project's own bar (2 events inside one
   filed project) makes it a label, a lowercase description makes it a
   topic, and everything else keeps its kind (the shape, person, value and
   duplicate rules above have already had their say). Both are asked, not
   applied on its own (D4).
9. Scope: the parent `entity_scope` gives the row once the plan's own
   changes are made.

General use: no rule names a person, company, domain, product or term; the
rules read name shapes, the seat's own registry, people and sources.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from dataclasses import dataclass, field

from app.services import entity_typing

RULES_VERSION = "entity-repair-v1"
PLAN_FORMAT = 1
# An unfinished run younger than this is in flight (one run at a time across
# processes); an older one crashed and holds nothing.
ACTIVE_RUN_S = 3600.0
_OFF = ("0", "false", "False")
ACTIONS = ("keep", "rekind", "reparent", "merge", "hide", "drop")
_APPLY_ORDER = {"merge": 0, "rekind": 1, "reparent": 2, "hide": 3, "drop": 4}

# How sure each rule is: what the automatic path compares with its bar
# (QUILL_ENTITY_REPAIR_AUTO_MIN_CONF, default 0.8). Shape and registry
# rules are near certain; evidence rules a step below; a rule that would
# overturn what the model said about a named thing (an org or a tool) is
# below the bar and is always asked.
CONFIDENCE = {
    "value_literal": 0.95,
    "implausible": 0.9,
    "person_name": 0.9,
    # A given name only one known person has ("Ruby" beside "Ruby Chen") is
    # often a thing of its own (a language, a gem, a product): asked.
    "person_given_name": 0.75,
    "filed_project": 0.95,
    "filed_org": 0.9,
    "legal_form": 0.9,
    "email_domain": 0.9,
    "people_work_there": 0.7,
    "note_only": 0.85,
    "note_only_label": 0.85,
    "common_noun": 0.85,
    "code_like": 0.9,
    "code_like_named": 0.6,
    "project_scoped": 0.85,
    "project_unfiled": 0.8,
    "project_description": 0.8,
    "scope": 0.9,
    "scope_clear": 0.9,
    # Merges are never applied on their own (they delete a row); the number
    # says how sure the match is, for the reviewer.
    "merge_same_name": 0.9,
    "merge_same_name_spacing": 0.85,
    "merge_dressed_name": 0.85,
    "merge_self_possessive": 0.9,
    "merge_names_filed_project": 0.8,
    "merge_org_short_form": 0.75,
}
# Rules whose verdict depends on the evidence a merge pools: on the
# automatic path they wait for the merge's own decision.
_EVIDENCE_RULES = frozenset({"note_only", "note_only_label", "common_noun",
                             "project_scoped", "project_unfiled",
                             "project_description", "scope", "scope_clear",
                             "people_work_there"})
# The kind rules for a model "project" (rule 8). On a row from before kind
# provenance they are asked, never applied on their own (D4).
_PROJECT_RULES = frozenset({"project_scoped", "project_unfiled",
                            "project_description"})
# On a row from before kind provenance (D4) the automatic path applies only
# the deterministic priors the kind vote would (a filing, an email domain, a
# value) and the code-shape rule (D4 tightened: Phase 4's job). A legal
# form that is not filed is a vote prior for rows with provenance only, so
# on a legacy row it is asked, like rule 8.
_LEGACY_ASKED = _PROJECT_RULES | {"legal_form"}
# Kinds a project name can be dressed up as and still be the same thing.
_PROJECT_LIKE = frozenset({"project", "idea", "label", "thing"})
# The possessive of a name: "Ana's", "Ana’s", "James'".
_POSSESSIVE = re.compile(r"^(?P<who>.+?)(?:'s|’s|'|’)\s+(?P<what>\S.*)$")
# A file name is no domain, though some extensions are country codes
# ("notes.md", "build.sh"): the extensions `name_quality` refuses.
_FILE_EXTENSIONS = frozenset({
    "py", "md", "db", "json", "txt", "js", "ts", "tsx", "jsx", "csv", "log",
    "toml", "yaml", "yml", "ini", "cfg", "sh", "bat"})
_MIN_SQUASHED = 5
# Whole names that name nothing (the extractor's placeholders): the same
# generic set cleanup hides orphans by (`ambient_cleanup`).
from app.services.ambient_cleanup import _PLACEHOLDER_NAMES  # noqa: E402


def _domain_named(name: str) -> bool:
    """A company whose name is its domain ("Acme.io", "Monzo.co.uk",
    "Example.com.br"): a real name, though the mint gate refuses
    domain-shaped strings. Structural, by the vendored public suffix list:
    the name is one registrable domain (one label before a suffix the list
    has), and its last label is no file extension."""
    s = (name or "").strip()
    if not s or " " in s:
        return False
    from app.perception import psl
    host = psl.host_of(s)
    if not host or host != s.lower() or psl.registrable_domain(host) != host:
        return False
    if not psl.is_listed_suffix(psl.public_suffix(host) or ""):
        return False
    return host.rsplit(".", 1)[-1] not in _FILE_EXTENSIONS


def auto_enabled() -> bool:
    """QUILL_ENTITY_REPAIR_AUTO (default off): may the graph job run the
    automatic path (once per rules version, after a backup)?"""
    return os.environ.get("QUILL_ENTITY_REPAIR_AUTO", "0") not in _OFF


def auto_min_conf() -> float:
    """QUILL_ENTITY_REPAIR_AUTO_MIN_CONF (default 0.8): the automatic path
    applies an action only at or above this confidence."""
    try:
        v = float(os.environ.get("QUILL_ENTITY_REPAIR_AUTO_MIN_CONF", "0.8"))
    except (TypeError, ValueError):
        return 0.8
    return min(1.0, max(0.0, v))


def note_only_mode() -> str:
    """QUILL_ENTITY_REPAIR_NOTE_ONLY: `drop` (default) or `label`, what
    happens to a row only assistant notes or screen text named."""
    v = os.environ.get("QUILL_ENTITY_REPAIR_NOTE_ONLY", "drop").strip().lower()
    return v if v in ("drop", "label") else "drop"


# ------------------------------------------------------------- evidence --
@dataclass
class _Ev:
    """What stands behind one row: its facts, the distinct events they and
    its sightings came from, and what the user did to it."""
    facts: set = field(default_factory=set)
    events: dict = field(default_factory=dict)   # id -> {source, cls, ok, filed}
    unplaced: int = 0                            # facts with no event
    mentions: list = field(default_factory=list)
    created: set = field(default_factory=set)
    user: set = field(default_factory=set)

    def add(self, other: "_Ev") -> None:
        self.facts |= other.facts
        for k, v in other.events.items():
            self.events.setdefault(k, v)
        self.unplaced += other.unplaced
        self.mentions += other.mentions
        self.created |= other.created
        self.user |= other.user

    @property
    def support(self) -> int:
        return len(self.events) + self.unplaced

    def counts(self) -> dict:
        classes: dict[str, int] = {}
        for v in self.events.values():
            classes[v["cls"] or "unknown"] = classes.get(v["cls"] or "unknown",
                                                         0) + 1
        filed = sorted({v["filed"] for v in self.events.values()
                        if v["filed"]})
        return {"facts": len(self.facts), "events": len(self.events),
                "eligible_events": sum(1 for v in self.events.values()
                                       if v["ok"]),
                "note_or_screen_events": sum(1 for v in self.events.values()
                                             if not v["ok"]),
                "created": len(self.created), "mentions": len(self.mentions),
                "unplaced_facts": self.unplaced, "classes": classes,
                "filed_projects": filed, "user_touched": sorted(self.user)}


_CLASS_CACHE: dict[str, str] = {}


def _source_class(event_source: str | None) -> str:
    src = (event_source or "").strip()
    hit = _CLASS_CACHE.get(src)
    if hit is None:
        try:
            from app.services import source_policy
            hit = source_policy.classify_source(event_source=src) or "unknown"
        except Exception:
            hit = "unknown"
        _CLASS_CACHE[src] = hit
    return hit


def _eligible(event_source: str | None, cls: str | None) -> bool:
    from app.services import entity_candidates
    return entity_candidates.mint_eligible(
        {"event_source": event_source or "", "source_class": cls or "unknown"})


def _evidence(store, rows: list[dict]) -> tuple[dict[int, _Ev], list]:
    """{entity_id: _Ev} for every row, and the affiliated entity ids."""
    raw = store.entity_repair_evidence()
    out: dict[int, _Ev] = {int(r["id"]): _Ev() for r in rows}

    def ev(eid) -> _Ev | None:
        return out.get(int(eid)) if eid is not None else None

    def place(e: _Ev, event_id, source, cls, filed) -> None:
        if event_id is None:
            return
        cls = cls or _source_class(source)
        fp = filed.strip() if isinstance(filed, str) and filed.strip() \
            else None
        cur = e.events.get(int(event_id))
        if cur is None:
            e.events[int(event_id)] = {"source": source or "", "cls": cls,
                                       "ok": _eligible(source, cls),
                                       "filed": fp}
        elif fp and not cur["filed"]:
            cur["filed"] = fp

    for r in raw.get("facts") or ():
        e = ev(r.get("entity_id"))
        if e is None:
            continue
        e.facts.add(int(r["fact_id"]))
        if r.get("event_id") is None or not r.get("has_event"):
            e.unplaced += 1
        else:
            place(e, r["event_id"], r.get("event_source"), None,
                  r.get("filing_project"))
        if (r.get("review") or "") in ("approved", "edited") \
                and (r.get("origin") or "") in ("asserted", "user"):
            e.user.add("note")
    for r in raw.get("mentions") or ():
        e = ev(r.get("entity_id"))
        if e is None:
            continue
        e.mentions.append(r)
        place(e, r.get("event_id"), r.get("event_source"),
              r.get("source_class"), r.get("filing_project"))
    for r in raw.get("edges") or ():
        e = ev(r.get("entity_id"))
        if e is None or r.get("event_id") is None or not r.get("has_event"):
            continue
        place(e, r["event_id"], r.get("event_source"), None,
              r.get("filing_project"))
    for r in raw.get("created") or ():
        e = ev(r.get("entity_id"))
        if e is None or r.get("event_id") is None:
            continue
        e.created.add(int(r["event_id"]))
        if r.get("mixed"):
            # Minted in a batch whose events disagree: a sighting of
            # unknown source and project. It is evidence (the row is not
            # "only ever in notes", it is not a one-off on nothing), but it
            # places the name nowhere.
            e.events.setdefault(int(r["event_id"]), {
                "source": "", "cls": "batch", "ok": True, "filed": None,
                "mixed": True})
            continue
        place(e, r["event_id"], r.get("event_source"), None,
              r.get("filing_project"))
    for eid, n in (raw.get("user_edges") or {}).items():
        e = ev(eid)
        if e is not None and n:
            e.user.add("edge")
    for eid in raw.get("attrs") or ():
        e = ev(eid)
        if e is not None:
            e.user.add("detail")
    for r in rows:
        e = out[int(r["id"])]
        if (r.get("parent_source") or "") == "user":
            e.user.add("parent")
    # What else the user did to a row: a rename, an alias they typed, a
    # kind, a merge into it, an Inbox answer, a repair hide they undid
    # (`Store.entity_user_touches`). Pins and graph hides are user edges.
    try:
        touches = store.entity_user_touches()
    except Exception:
        touches = {}
    for eid, whys in touches.items():
        e = ev(eid)
        if e is not None:
            e.user |= set(whys)
    return out, list(raw.get("affiliated") or ())


def support(store, rows: list[dict] | None = None) -> dict[int, int]:
    """{entity id: distinct supporting events} for every visible row (or
    `rows`): the same count rule 10 reads ("at most one supporting event"),
    and what the seat's "nodes never seen twice" metric counts."""
    if rows is None:
        rows = [r for r in store.entity_repair_rows() if not r["hidden"]]
    ev, _ = _evidence(store, rows)
    return {int(eid): e.support for eid, e in ev.items()}


def _self_keys(store) -> set[str]:
    """Name keys the seat's user goes by (their name and given name), from
    the identity they stated, else their approved onboarding answer."""
    from app.services.kg_keys import entity_name_key
    names: list[str] = []
    try:
        from app.services.identity import user_identity
        n = (user_identity(store) or {}).get("name")
        if n:
            names.append(n)
    except Exception:
        pass
    if not names:
        try:
            claims = store.list_facts(kind="claim", review="approved",
                                      limit=1_000_000)
        except Exception:
            claims = []
        pfx = "the user's name is"
        for c in claims:
            t = (c.get("text") or "").strip()
            if t.lower().startswith(pfx):
                v = t[len(pfx):].strip().strip(".").strip()
                if v:
                    names.append(v)
                    break               # newest first: the latest answer
    keys: set[str] = set()
    for n in names:
        k = entity_name_key(n)
        if k:
            keys.add(k)
            if len(k.split()) >= 2:
                keys.add(k.split()[0])
    return keys


# A given name as people write it: letters, maybe a hyphen or an inner
# apostrophe ("Anne-Marie", "D'Arcy"), never a possessive ("Acme's CMO" is
# a description that landed in the people table, not a person called Acme).
_GIVEN = re.compile(r"^[^\W\d_]+(?:[-'’][^\W\d_]{2,})*$")


def _people(store) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """({full-name key: person}, {given-name key: [people]}) over the
    seat's visible people. A given name counts only from a name that reads
    as a person's (`is_plausible_person`) and whose first word is a given
    name (`_GIVEN`)."""
    from app.services.kg_keys import entity_name_key
    from app.services.name_quality import is_plausible_person
    try:
        people = store.all_people()
    except Exception:
        people = []
    full: dict[str, dict] = {}
    given: dict[str, list[dict]] = {}
    for p in people:
        if p.get("hide_from_people") or p.get("canonical_person_id"):
            continue
        name = p.get("name") or ""
        k = entity_name_key(name)
        if not k:
            continue
        ref = {"id": int(p["id"]), "name": name}
        full.setdefault(k, ref)
        words = name.split()
        if len(words) >= 2 and _GIVEN.match(words[0]) \
                and words[0][:1].isupper():
            try:
                ok = is_plausible_person(name, store=store)
            except Exception:
                ok = False
            if ok:
                given.setdefault(entity_name_key(words[0]), []).append(ref)
    return full, given


# ----------------------------------------------------------------- plan --
def _snapshot(r: dict) -> dict:
    """The row as the plan saw it: what `apply` checks before it writes."""
    return {"canonical_id": r.get("canonical_id"), "name": r.get("name"),
            "kind": r.get("kind"), "kind_source": r.get("kind_source"),
            "hidden": bool(r.get("hidden")), "parent_id": r.get("parent_id"),
            "parent_source": r.get("parent_source"),
            # Not a staleness field (a sighting moves it): the automatic
            # path reads it to skip a hide whose evidence moved since.
            "last_seen": r.get("last_seen")}


def _kind(k) -> str:
    from app.services.name_quality import normalize_entity_kind
    return normalize_entity_kind(k)


def _decorated(name: str) -> bool:
    """Is this a project name dressed up ("The Atlas project", "Project
    Atlas", "Atlas (project)")? `projects.norm` strips the dressing."""
    from app.services import projects
    from app.services.kg_keys import entity_name_key
    return entity_name_key(name).replace(" ", "") != \
        projects.norm(name).replace(" ", "")


class _UF:
    def __init__(self):
        self.p: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def plan(store, *, note_only: str | None = None) -> dict:
    """The repair diff for every visible entity row. Reads only.

    Returns {format, rules_version, created_at, db, options, self_known,
    actions, counts}. Each action: {entity_id, name, kind, action, rule,
    reason, confidence, route, evidence, state} plus, by action, `to_kind`
    and `kind_source` (rekind), `parent_id` / `parent_name` /
    `parent_state` (reparent; a None parent takes the row out),
    `into` / `into_name` / `into_state` (merge), `tombstone` (drop),
    `person` (a drop of a person's name: who it probably is). `route` is
    `auto` when the automatic path may apply it on its own, else `review`
    (the reviewed path applies every action)."""
    from app.services import entity_scope, projects
    from app.services.kg_keys import entity_name_key
    from app.services.name_quality import is_plausible_entity
    mode = (note_only or note_only_mode()).strip().lower()
    if mode not in ("drop", "label"):
        mode = "drop"
    rows = store.entity_repair_rows()
    by_id = {r["id"]: r for r in rows}
    evs, affiliated = _evidence(store, rows)
    affiliated = set(affiliated)
    live = {r["id"]: r for r in rows if not r["hidden"]}
    try:
        targets = projects.filed_target_ids(live.values())
    except Exception:
        targets = set()
    self_keys = _self_keys(store)
    full_people, given_people = _people(store)

    def names_of(r) -> list[str]:
        return [r["name"]] + list(r.get("confirmed") or [])

    def org_ev(r) -> str | None:
        try:
            return entity_typing.org_by_evidence(store, names_of(r))
        except Exception:
            return None

    org_words = entity_scope._org_words(live)
    acts: dict[int, list[dict]] = {}
    final: dict[int, str] = {}          # eid -> why no later rule runs
    # A project name dressed up ("Atlas (project)") fails the mint gate's
    # name check, but beside its plain twin it is a duplicate for rule 4 to
    # merge, not a name to forget: rule 3 leaves it to the merge. The
    # twins are counted as rule 4 groups them (project-like rows, filed
    # names as projects, hidden rows that are no value or rule hide).
    plain_rows: dict[str, set[int]] = {}
    for eid_, r_ in by_id.items():
        if r_["hidden"] and ((r_.get("kind_source") or "") == "rule"
                             or entity_typing.value_literal(r_["name"])):
            continue
        gk_ = "project" if eid_ in targets else _kind(r_["kind"])
        pk_ = projects.norm(r_["name"]).replace(" ", "")
        if gk_ in _PROJECT_LIKE and pk_:
            plain_rows.setdefault(pk_, set()).add(eid_)

    def dressed_twin(eid_: int, name_: str) -> bool:
        if not _decorated(name_):
            return False
        pk_ = projects.norm(name_).replace(" ", "")
        return eid_ in plain_rows.get(pk_, ()) and \
            len(plain_rows.get(pk_, ())) > 1

    def add(eid: int, action: str, rule: str, reason: str, **kw) -> dict:
        r = by_id[eid]
        a = {"entity_id": eid, "name": r["name"], "kind": r["kind"],
             "action": action, "rule": rule, "reason": reason,
             "confidence": CONFIDENCE.get(rule), "state": _snapshot(r)}
        a.update(kw)
        acts.setdefault(eid, []).append(a)
        return a

    def touched(eid: int) -> bool:
        return bool(evs[eid].user)

    # 1-3 and 2b: decisions on the row alone.
    for eid, r in live.items():
        name, kind = r["name"], _kind(r["kind"])
        if (r.get("kind_source") or "") == "user":
            final[eid] = "user"
            continue
        if touched(eid):
            continue                    # only rules 5 and 6 below
        if entity_typing.value_literal(name):
            add(eid, "hide", "value_literal",
                "a value, not a thing: it stays in the fact text")
            if kind != "thing":
                add(eid, "rekind", "value_literal",
                    "a value is a thing, as claim values are minted",
                    to_kind="thing", kind_source="rule")
            final[eid] = "value_literal"
            continue
        if not is_plausible_entity(name) and not (
                kind in ("org", "tool") and _domain_named(name)) \
                and not dressed_twin(eid, name):
            add(eid, "drop", "implausible",
                "the mint gate refuses this name (a fragment, a path, a "
                "product word or a run-on)", tombstone=True)
            final[eid] = "implausible"
            continue
        if " ".join(name.lower().split()) in _PLACEHOLDER_NAMES:
            add(eid, "drop", "implausible",
                "a placeholder the extractor filled in, not a name",
                tombstone=True)
            final[eid] = "implausible"
            continue
        key = entity_name_key(name)
        model_project = kind == "project" and \
            (r.get("kind_source") or "model") == "model"
        # An org by its evidence is no person's name, whatever the people
        # table holds: people work there, or it is the short form of an org
        # row's name ("Acme" beside "Acme Freight").
        orgish = eid in affiliated or _short_form_of_org(name, org_words)
        if (kind in ("idea", "thing", "label") or model_project) \
                and not orgish:
            who = full_people.get(key)
            rule = "person_name"
            if who is None and len(key.split()) == 1 \
                    and len(given_people.get(key) or ()) == 1:
                who, rule = given_people[key][0], "person_given_name"
            if who is not None and eid not in targets:
                # A given name is not forgotten for good (it may be the
                # name of something else): dropped, never tombstoned.
                add(eid, "drop", rule,
                    "a person's name stored as a topic; the person is a "
                    "node of their own",
                    tombstone=rule != "person_given_name", person=who)
                final[eid] = rule

    # 4: merges among the rows still standing (hidden rows only as the
    # plain form a dressed-up or possessive name points at).
    standing = {eid: r for eid, r in live.items() if eid not in final
                or final[eid] == "user"}
    hidden_ok = {eid: r for eid, r in by_id.items() if r["hidden"]
                 and (r.get("kind_source") or "") != "rule"
                 and not entity_typing.value_literal(r["name"])}
    uf, why, forced = _UF(), {}, set()

    def link(a: int, b: int, rule: str) -> None:
        uf.union(a, b)
        why.setdefault(a, rule)
        why.setdefault(b, rule)

    by_key: dict[str, list[int]] = {}
    by_squash: dict[str, list[int]] = {}
    by_plain: dict[str, list[int]] = {}
    for eid, r in standing.items():
        k = entity_name_key(r["name"])
        if not k:
            continue
        by_key.setdefault(k, []).append(eid)
        sq = k.replace(" ", "")
        if len(sq) >= _MIN_SQUASHED:
            by_squash.setdefault(sq, []).append(eid)
        # A filed name is grouped as the project rule 5 makes it.
        gk = "project" if eid in targets and not org_ev(r) \
            else _kind(r["kind"])
        if gk in _PROJECT_LIKE:
            pk = projects.norm(r["name"]).replace(" ", "")
            if pk:
                by_plain.setdefault(pk, []).append(eid)
    for group, rule in ((by_key, "same_name"),
                        (by_squash, "same_name_spacing")):
        for ids in group.values():
            for other in ids[1:]:
                link(ids[0], other, rule)
    hidden_plain: dict[str, int] = {}
    for eid, r in sorted(hidden_ok.items()):
        hidden_plain.setdefault(projects.norm(r["name"]).replace(" ", ""),
                                eid)
    for pk, ids in by_plain.items():
        dressed = [i for i in ids if _decorated(standing[i]["name"])]
        if not dressed:
            continue
        for other in ids:
            if other != ids[0]:
                link(ids[0], other, "dressed_name")
        h = hidden_plain.get(pk)
        if h is not None and h not in ids and \
                not _decorated(by_id[h]["name"]):
            for i in dressed:
                link(h, i, "dressed_name")
                forced.add(i)
    if self_keys:
        key_rows: dict[str, int] = {}
        for pool_ in (standing, hidden_ok):     # a visible row first
            for eid, r in sorted(pool_.items()):
                key_rows.setdefault(entity_name_key(r["name"]), eid)
        for eid, r in standing.items():
            m = _POSSESSIVE.match(r["name"].strip())
            if not m or entity_name_key(m.group("who")) not in self_keys:
                continue
            x = key_rows.get(entity_name_key(m.group("what")))
            if x is not None and x != eid:
                link(x, eid, "self_possessive")
                forced.add(eid)
    target_words = {t: tuple(projects.norm(standing[t]["name"]).split())
                    for t in targets if t in standing}
    if target_words:
        tg = entity_scope._Targets({**live})
        for eid, r in standing.items():
            if eid in targets or _kind(r["kind"]) not in _PROJECT_LIKE \
                    or org_ev(r):
                continue
            words = tuple(projects.norm(r["name"]).split())
            outside, inside = 0, set()
            for d in evs[eid].events.values():
                if d["source"].lower().startswith("desktop.screen"):
                    continue
                t = tg.of(d["filed"]) if d["filed"] else None
                if t is None:
                    outside += 1
                else:
                    inside.add(t)
            if outside or len(inside) != 1:
                continue
            t = next(iter(inside))
            tw = target_words.get(t)
            if tw and entity_scope._shortens(words, tw):
                link(t, eid, "names_filed_project")
                forced.add(eid)
    # The short form of one org row's name ("Acme" beside "Acme Freight
    # Lines") stored as a topic or a "project" is that org. One capitalised
    # word only: a longer prefix is often a piece of work named after the
    # group that does it ("Vendor Review" beside "Vendor Review working
    # group", measured on the worlds).
    org_rows = [(eid, tuple(entity_scope._key_words(r["name"])))
                for eid, r in standing.items() if _kind(r["kind"]) == "org"]
    for eid, r in standing.items():
        if eid in targets or _kind(r["kind"]) not in _PROJECT_LIKE:
            continue
        words = tuple(entity_scope._key_words(r["name"]))
        if len(words) != 1 or not r["name"].strip()[:1].isupper():
            continue
        hits = [o for o, ow in org_rows
                if len(ow) > len(words) and ow[:len(words)] == words]
        if len(hits) == 1:
            link(hits[0], eid, "org_short_form")
            forced.add(eid)
    groups: dict[int, list[int]] = {}
    for eid in set(uf.p):
        groups.setdefault(uf.find(eid), []).append(eid)
    absorbed: dict[int, int] = {}       # absorbed -> survivor
    for members in groups.values():
        if len(members) < 2:
            continue

        def rank(i):
            r = by_id[i]
            return (i in targets,
                    (r.get("kind_source") or "") == "user" or touched(i),
                    not _decorated(r["name"]), not r["hidden"],
                    len(evs[i].facts), -(r.get("first_seen") or 1e18), -i)
        pool = [i for i in members if i not in forced] or members
        surv = max(pool, key=rank)
        for i in members:
            if i == surv or (by_id[i].get("kind_source") or "") == "user" \
                    or touched(i) or by_id[i]["hidden"] or i in final:
                continue
            absorbed[i] = surv
    for a, s in absorbed.items():
        sr = by_id[s]
        add(a, "merge", "merge_" + why.get(a, "same_name"),
            _merge_reason(why.get(a, "same_name"), by_id[a]["name"],
                          sr["name"], hidden=bool(sr["hidden"])),
            into=s, into_name=sr["name"], into_state=_snapshot(sr),
            survivor_hidden=bool(sr["hidden"]),
            kind_conflict=_kind(by_id[a]["kind"]) != _kind(sr["kind"]))
        final[a] = "merged"
    # A survivor answers for its group's evidence.
    pooled: dict[int, _Ev] = {eid: evs[eid] for eid in by_id}
    for a, s in absorbed.items():
        p = _Ev()
        p.add(pooled[s])
        p.add(evs[a])
        pooled[s] = p
    group_of = set(absorbed) | set(absorbed.values())

    # What the user typed in their own chat (`chat.user`): a sighting the
    # candidate gate weighs most. The ledger marks it (`typed`); a store
    # from before the ledger has only the chat turns themselves. Read once,
    # and only for a row an evidence rule would otherwise drop.
    chat: list | None = None
    typed_seen: dict[int, dict] = {}

    def typed_events(eid_: int, name_: str) -> dict:
        """{event id: time} of the sightings the user typed."""
        nonlocal chat
        if eid_ in typed_seen:
            return typed_seen[eid_]
        from app.services.kg_keys import norm_name
        got: dict = {}
        for i, m in enumerate(pooled[eid_].mentions):
            if m.get("typed"):
                ev_ = m.get("event_id")
                got[int(ev_) if ev_ is not None else f"m{i}"] = \
                    float(m.get("observed_at") or 0.0)
        if chat is None:
            try:
                chat = [(int(c["id"]), float(c.get("time") or 0.0),
                         f" {norm_name(c.get('raw') or '')} ")
                        for c in store.typed_chat_events()]
            except Exception:
                chat = []
        key_ = norm_name(name_)
        if key_:
            for cid, t, text in chat:
                if f" {key_} " in text:
                    got.setdefault(cid, t)
        typed_seen[eid_] = got
        return got

    def typed_times(eid_: int, name_: str) -> list[float]:
        return sorted(typed_events(eid_, name_).values())

    def typed_sessions(eid_: int, name_: str) -> int:
        from app.services import entity_candidates as cand
        ts_ = typed_times(eid_, name_)
        return cand.distinct_sessions(store, ts_) if ts_ else 0

    # 5-10 on the rows still standing.
    planned_kind: dict[int, str] = {}
    planned_src: dict[int, str | None] = {}
    tentative: set[int] = set()
    for eid, r in standing.items():
        if eid in final:
            continue
        kind, src = _kind(r["kind"]), (r.get("kind_source") or None)
        held = src in ("rule", "filing", "user")
        e = pooled[eid]
        legacy = src is None
        why_org = org_ev(r)
        user_touch = touched(eid)
        if eid in targets:
            to = "org" if why_org else "project"
            rule = "filed_org" if why_org else "filed_project"
            if (kind != to or src != "filing") and \
                    store.kind_source_rank("filing") >= \
                    store.kind_source_rank(src):
                add(eid, "rekind", rule,
                    ("a name the user files under, and an org by its "
                     f"{'legal form' if why_org == 'company_name' else 'email domain'}")
                    if why_org else "a name the user files notes under",
                    to_kind=to, kind_source="filing")
            planned_kind[eid], planned_src[eid] = to, "filing"
            continue
        if not held and why_org and kind != "org":
            rule = "legal_form" if why_org == "company_name" \
                else "email_domain"
            add(eid, "rekind", rule,
                "a company's legal form in the name" if rule == "legal_form"
                else "a known person's email domain names it",
                to_kind="org", kind_source="rule")
            planned_kind[eid], planned_src[eid] = "org", "rule"
            continue
        if user_touch:
            final[eid] = "user_touched"
            continue
        if held:
            final[eid] = "held"
            continue
        # People who work at it or belong to it: a topic or a thing written
        # as a proper name is then an org. A model "project" or a phrase is
        # not changed by it (people are said to "work at" the work they do
        # and "in" a department; measured on the worlds).
        if not why_org and eid in affiliated and \
                kind in ("idea", "thing", "label") and _proper_name(r["name"]):
            add(eid, "rekind", "people_work_there",
                "people work at it or belong to it", to_kind="org",
                kind_source="rule")
            planned_kind[eid], planned_src[eid] = "org", "rule"
            continue
        # People who work at it or belong to it are evidence of their own:
        # such a row is neither note-only nor a one-off description.
        vouched = eid in affiliated
        if why_org != "email_domain" and not vouched and e.events \
                and not e.unplaced \
                and not any(v["ok"] for v in e.events.values()) \
                and not typed_times(eid, r["name"]):
            if mode == "label":
                # Kept: a topic or a working name as a label (the scope
                # rule puts it inside the project its notes were filed
                # under); an org, a tool or a place keeps its kind (it is
                # never a child, and a note naming it does not make it a
                # label).
                if kind in ("org", "tool", "place"):
                    final[eid] = "note_only_kept"
                    continue
                if kind != "label":
                    add(eid, "rekind", "note_only_label",
                        "only assistant notes or screen text named it; kept "
                        "as a label of the project it was filed under",
                        to_kind="label", kind_source="rule")
                planned_kind[eid], planned_src[eid] = "label", "rule"
                continue
            add(eid, "drop", "note_only",
                "only assistant notes or screen text ever named it (the "
                "facts keep the text; no tombstone, so a real sighting can "
                "bring it back)", tombstone=False)
            final[eid] = "note_only"
            continue
        mention_kinds = [m.get("proposed_kind") for m in e.mentions]
        if not vouched and e.support <= 1 and \
                entity_typing.weak_name(r["name"], [kind] + mention_kinds):
            # The user's own typing is evidence the ledger may not have (a
            # store from before it): typed in as many sessions as the gate
            # asks, the name stays; typed at all, it is never tombstoned.
            from app.services import entity_candidates as cand
            typed = typed_times(eid, r["name"])
            n_sup = len(set(e.events) | {k for k in typed_events(
                eid, r["name"]) if isinstance(k, int)}) + e.unplaced
            if not (typed and (typed_sessions(eid, r["name"])
                               >= cand.min_sessions() or n_sup > 1)):
                add(eid, "drop", "common_noun",
                    f"a lowercase description, not a name, with "
                    f"{n_sup} supporting event{'s' if n_sup != 1 else ''}"
                    + ("; the user typed it, so it is not forgotten for "
                       "good" if typed else ""),
                    tombstone=not typed)
                final[eid] = "common_noun"
                continue
        if entity_typing.code_like(r["name"]):
            if kind in ("org", "tool", "place"):
                try:
                    named = entity_typing.code_named_kind(
                        store, e.mentions,
                        store.entity_candidate_summary(
                            entity_name_key(r["name"])))
                except Exception:
                    named = None
                if named == kind:
                    continue
                add(eid, "rekind", "code_like_named",
                    f"code-shaped; nothing independent confirms it is "
                    f"{'an' if kind == 'org' else 'a'} {kind}",
                    to_kind="label", kind_source="rule")
            elif kind != "label":
                add(eid, "rekind", "code_like",
                    "an identifier (a milestone, row, version or hash id)",
                    to_kind="label", kind_source="rule")
            planned_kind[eid], planned_src[eid] = "label", "rule"
            continue
        if kind == "project":
            tentative.add(eid)
            if not legacy:
                # Phase 2's rule for a model "project" (the kind vote's):
                # a label when its evidence sits in one filed project, so
                # the scope rule reads it at a label's bar.
                planned_kind[eid], planned_src[eid] = "label", "rule"
            # A legacy row keeps its kind for the scope rule: inside one
            # filed project at a project's bar, or not at all.
            continue
    # 9: the scope rule on the store as the plan leaves it.
    removed = {eid for eid, why_ in final.items()
               if why_ not in ("user", "user_touched", "held",
                               "note_only_kept")}
    view: dict[int, dict] = {}
    for eid, r in live.items():
        if eid in removed:
            continue
        v = {"id": eid, "name": r["name"], "kind": r["kind"],
             "kind_source": r.get("kind_source"),
             "aliases": list(r.get("confirmed") or []),
             "parent_id": r.get("parent_id"),
             "parent_source": r.get("parent_source")}
        if eid in planned_kind:
            v["kind"], v["kind_source"] = planned_kind[eid], planned_src[eid]
        view[eid] = v
    scope_ev = store.entity_scope_evidence()

    def remap(rows_):
        out = []
        for x in rows_ or ():
            eid = x.get("entity_id")
            if eid is None:
                continue
            eid = absorbed.get(int(eid), int(eid))
            if eid in view:
                out.append({**x, "entity_id": eid})
        return out
    scope_ev = {"facts": remap(scope_ev.get("facts")),
                "mentions": remap(scope_ev.get("mentions")),
                "created": remap(scope_ev.get("created")),
                "affiliated": [absorbed.get(int(a), int(a))
                               for a in scope_ev.get("affiliated") or ()]}
    try:
        scoped = entity_scope.compute(store, entities=view,
                                      evidence=scope_ev)
    except Exception as exc:
        print(f"[entity_repair] scope skipped ({exc}).")
        scoped = []
    parent_of = {s["entity_id"]: s for s in scoped}
    for eid in sorted(tentative):
        s = parent_of.get(eid)
        legacy = by_id[eid].get("kind_source") is None
        if s is not None and s.get("parent_id") is not None:
            add(eid, "rekind", "project_scoped",
                "a model 'project' no filing made, used only inside "
                f"{s.get('parent_name')}", to_kind="label",
                kind_source="rule")
            planned_kind[eid], planned_src[eid] = "label", "rule"
        elif not legacy:
            add(eid, "rekind", "project_unfiled",
                "a model 'project' no filing made and the user never filed "
                "under: a topic", to_kind="idea", kind_source="rule")
            planned_kind[eid] = "idea"
        elif entity_typing.common_noun_like(by_id[eid]["name"]):
            add(eid, "rekind", "project_description",
                "a lowercase description, not a project's name: a topic",
                to_kind="idea", kind_source="rule")
            planned_kind[eid], planned_src[eid] = "idea", "rule"
        else:
            # No evidence that it is not a project: lacking a filing is
            # none (its kind may be the user's own, from before provenance).
            final[eid] = "project_legacy"
    # Every row the scope pass of the graph job would move, so the repair
    # and the next graph job agree (rows the user touched are asked).
    for s in scoped:
        eid = s["entity_id"]
        if s["action"] not in ("set", "clear") or eid in removed:
            continue
        pid = s.get("parent_id")
        pr = by_id.get(pid) if pid is not None else None
        add(eid, "reparent", "scope" if pid is not None else "scope_clear",
            (f"every event naming it sits in {s.get('parent_name')}"
             if pid is not None else
             "it now comes up outside the project it was put in"),
            parent_id=pid, parent_name=(pr or {}).get("name"),
            parent_state=_snapshot(pr) if pr else None)
    # Keep rows, so the diff accounts for every visible row.
    for eid, r in live.items():
        if eid in acts:
            continue
        add(eid, "keep", final.get(eid) or "no_change",
            {"user": "the user chose its kind",
             "user_touched": "the user worked on this row",
             "held": "a filing or a rule set its kind",
             "note_only_kept": "only notes or screen text named it; kept "
                               "(note_only=label)",
             "project_legacy": "a 'project' from before kind provenance; "
                               "nothing says it is not one (a filing under "
                               "it would confirm it)"}.get(
                 final.get(eid), "nothing to change"))
    actions = []
    bar = auto_min_conf()
    for eid in sorted(acts):
        for a in acts[eid]:
            a["evidence"] = pooled[eid].counts() if a["action"] != "merge" \
                else evs[eid].counts()
            if typed_seen.get(eid):
                a["evidence"]["typed_events"] = len(typed_seen[eid])
            a["route"] = _route(a, bar=bar, touched=touched(eid),
                                legacy=(by_id[eid].get("kind_source")
                                        is None),
                                grouped=eid in group_of)
            actions.append(a)
    # A parent link the kind change it needs waits with that change.
    review_rekind = {a["entity_id"] for a in actions
                     if a["action"] == "rekind" and a["route"] == "review"}
    for a in actions:
        if a["action"] == "reparent" and a["entity_id"] in review_rekind:
            a["route"] = "review"
    return {"format": PLAN_FORMAT, "rules_version": RULES_VERSION,
            "created_at": time.time(),
            "db": {"path": str(getattr(store, "db_path", "") or ""),
                   "entities": len(rows), "visible": len(live)},
            "options": {"note_only": mode}, "self_known": bool(self_keys),
            "actions": actions, "counts": counts(actions)}


def _proper_name(name: str) -> bool:
    """Every word with letters starts with a capital ("Novak Studio"), and
    the name is no identifier: a name, not a phrase ("support material",
    "Peak capacity planning")."""
    words = [w for w in (name or "").split() if any(c.isalpha() for c in w)]
    return bool(words) and all(w[:1].isupper() for w in words) \
        and not entity_typing.code_like(name)


def _short_form_of_org(name: str, org_words: list) -> bool:
    """Is `name` the first word(s) of a longer org row's name?"""
    from app.services import entity_scope
    words = entity_scope._key_words(name)
    return bool(words) and any(full[:len(words)] == words
                               for full in org_words
                               if len(full) > len(words))


def _merge_reason(rule: str, name: str, into: str, *,
                  hidden: bool = False) -> str:
    why = {
        "same_name": f"the same name as {into} (case, accents, punctuation)",
        "same_name_spacing": f"the same name as {into} but for spacing",
        "dressed_name": f"{into} with 'project' or 'the' around it",
        "self_possessive": f"the user's own {into}",
        "names_filed_project": f"a short form of {into}, used only inside it",
        "org_short_form": f"the short form of the org {into}",
    }.get(rule, f"the same thing as {into}")
    if hidden:
        # A hidden survivor is the only plain spelling there is (a visible
        # one always wins): the merge takes the name off the tab with it.
        why += (f"; {into} is hidden from Orgs & tools, so {name} would "
                "leave the tab too")
    return why


def _route(a: dict, *, bar: float, touched: bool, legacy: bool,
           grouped: bool) -> str:
    """May the automatic path apply this action on its own?"""
    if a["action"] == "keep":
        return "auto"
    if a["action"] == "merge" or touched:
        return "review"
    conf = a.get("confidence")
    if conf is None or conf < bar:
        return "review"
    if legacy and a["rule"] in _LEGACY_ASKED:
        return "review"             # D4: a pre-provenance kind, not a shape
    if grouped and a["rule"] in _EVIDENCE_RULES:
        return "review"             # waits for the merge it depends on
    return "auto"


def counts(actions: list[dict]) -> dict:
    """{action: n, "auto": {action: n}, "review": {action: n}}."""
    out: dict = {k: 0 for k in ACTIONS}
    out["auto"], out["review"] = {}, {}
    seen: dict[str, set] = {k: set() for k in ACTIONS}
    for a in actions:
        if a["entity_id"] in seen[a["action"]]:
            continue
        seen[a["action"]].add(a["entity_id"])
        out[a["action"]] += 1
        if a["action"] != "keep":
            side = out[a["route"]]
            side[a["action"]] = side.get(a["action"], 0) + 1
    return out


# ---------------------------------------------------------------- apply --
class PlanRefused(Exception):
    """The plan does not fit this store: made for another database, or the
    store has moved on since (`problems` says which rows)."""

    def __init__(self, reason: str, problems: list[dict]):
        super().__init__(reason)
        self.reason = reason
        self.problems = problems


def _signature(a: dict) -> tuple:
    """What an action changes, and on which rows: the fields a hand edit
    could alter (the kind it writes, the survivor or parent it names, the
    tombstone), never its prose, confidence or route."""
    act = a.get("action")
    into = (a.get("into_state") or {}).get("canonical_id") \
        if act == "merge" else None
    parent = (a.get("parent_state") or {}).get("canonical_id") \
        if act == "reparent" else None
    return ((a.get("state") or {}).get("canonical_id"), a.get("entity_id"),
            act, a.get("rule"), a.get("to_kind"), a.get("kind_source"),
            a.get("into") if act == "merge" else None, into,
            a.get("parent_id") if act == "reparent" else None, parent,
            bool(a.get("tombstone")) if act == "drop" else None)


def check(store, plan_data: dict, *, verify: bool = False) -> list[dict]:
    """Why `plan_data` cannot be applied to `store` as it is now ([] when it
    can). Stale means, precisely:

    - `missing`: a row an action names (the row, a merge's survivor, a
      parent) is gone;
    - `other_database`: that row's `canonical_id` differs (the plan was made
      for another database);
    - `stale`: the row's name, kind, kind source, hidden flag, parent or
      parent source moved since the plan was made;
    - `rules_changed`: the plan was made by another rules version;
    - `not_in_plan` (with `verify`, for a plan file): the action is not one
      the rules make on the store as it is now. A hand edit (another kind,
      survivor, parent or tombstone, an action added) and new evidence
      that changes a verdict (a real sighting of a note-only name) both
      land here. Deleting an action from the file (to keep that row as it
      is) is allowed: the file may be a subset of the plan."""
    rows = {r["id"]: r for r in store.entity_repair_rows()}
    problems: list[dict] = []
    for a in plan_data.get("actions") or ():
        if a.get("action") == "keep":
            continue
        for key, eid in (("state", a.get("entity_id")),
                         ("into_state", a.get("into")),
                         ("parent_state", a.get("parent_id"))):
            want = a.get(key)
            if not want or eid is None:
                continue
            cur = rows.get(int(eid))
            if cur is None:
                problems.append({"entity_id": eid, "problem": "missing"})
                continue
            if want.get("canonical_id") != cur.get("canonical_id"):
                problems.append({"entity_id": eid,
                                 "problem": "other_database"})
                continue
            if key != "state":
                continue
            now = _snapshot(cur)
            moved = [k for k in ("name", "kind", "kind_source", "hidden",
                                 "parent_id", "parent_source")
                     if now.get(k) != want.get(k)]
            if moved:
                problems.append({"entity_id": eid, "problem": "stale",
                                 "fields": moved})
    version = plan_data.get("rules_version")
    if version is not None and version != RULES_VERSION:
        problems.append({"entity_id": None, "problem": "rules_changed",
                         "fields": [version]})
    if problems or not verify:
        return problems
    opts = plan_data.get("options") or {}
    fresh = plan(store, note_only=opts.get("note_only"))
    now = {_signature(a) for a in fresh["actions"] if a["action"] != "keep"}
    for a in plan_data.get("actions") or ():
        if a.get("action") == "keep":
            continue
        if _signature(a) not in now:
            problems.append({"entity_id": a.get("entity_id"),
                             "problem": "not_in_plan",
                             "fields": [a.get("action"), a.get("rule")]})
    return problems


def refusal_reason(problems: list[dict]) -> str:
    kinds_ = {p["problem"] for p in problems}
    if "other_database" in kinds_:
        return "made for another database"
    if "rules_changed" in kinds_:
        return "made by another version of the repair rules"
    if kinds_ == {"not_in_plan"}:
        return ("an action is not what the rules decide now (edited by hand, "
                "or the evidence moved since)")
    return "the store changed since the plan was made"


def _new_run_id(mode: str) -> str:
    return f"{mode}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-" \
           f"{secrets.token_hex(3)}"


def _kind_fields(r: dict | None) -> dict:
    r = r or {}
    return {"kind": r.get("kind"), "kind_source": r.get("kind_source"),
            "kind_confidence": r.get("kind_confidence")}


def _parent_fields(r: dict | None) -> dict:
    r = r or {}
    return {"parent_id": r.get("parent_id"),
            "parent_source": r.get("parent_source")}


def _row(store, eid: int) -> dict | None:
    try:
        e = store.get_entity(int(eid))
    except Exception:
        return None
    if e is None:
        return None
    e["hidden"] = store.entity_is_hidden(int(eid))
    return e


def _seen_since(store, a: dict, before: dict, since: float | None) -> bool:
    """Has the row been sighted since the plan was made? (Its last_seen
    moved, or a ledger sighting bound to it was written after the plan.)"""
    st = a.get("state") or {}
    if "last_seen" in st and before.get("last_seen") != st.get("last_seen"):
        return True
    if since is None:
        return False
    try:
        rows = store.entity_mentions(entity_id=int(a["entity_id"]))
    except Exception:
        return False
    return any(float(r.get("created_at") or 0) >= float(since) for r in rows)


def _apply_one(store, a: dict, *, run_id: str, auto: bool,
               actor: str, since: float | None = None,
               hide_drops: bool | None = None) -> dict:
    """Make one action and log what it changed. {ok, ...}. With
    `hide_drops` (the automatic path and the Inbox) a drop is a hide, which
    the repair log can put back; only the reviewed path deletes."""
    if hide_drops is None:
        hide_drops = auto
    eid = int(a["entity_id"])
    before = _row(store, eid)
    if before is None:
        return {"ok": False, "why": "missing"}
    act, rule = a["action"], a.get("rule")
    name = before.get("name")
    # The automatic path plans and applies in one go, but the app keeps
    # extracting meanwhile: a hide that rests on what the row's evidence
    # was (only notes, one sighting) is skipped when a sighting came in
    # between. The reviewed path re-plans instead (`check(verify=True)`).
    if auto and act in ("hide", "drop") and rule in _EVIDENCE_RULES \
            and _seen_since(store, a, before, since):
        return {"ok": False, "why": "seen_since"}

    def log(field_, b, after, *, logged_name=None) -> int:
        return store.entity_repair_log_change(
            run_id, entity_id=eid, canonical_id=before.get("canonical_id"),
            name=name if logged_name is None else logged_name, action=act,
            rule=rule, field=field_, before=b, after=after)

    def undoable(field_, b, after) -> int:
        # The change and its log entry are two writes. A change the log
        # cannot record could never be undone: put the field back and let
        # the action fail (`apply` reports it skipped, with the error).
        try:
            return log(field_, b, after)
        except Exception:
            try:
                store.entity_repair_restore(eid, field_, before=b,
                                            expect=after)
            except Exception:
                pass
            raise

    def audited(field_, b, after, **kw) -> dict:
        # A merge or a deletion is done once its own write commits; the
        # log entry is the record of it, so a failure to write that is
        # said, not reported as a skip.
        try:
            log(field_, b, after, **kw)
            return {}
        except Exception as exc:
            print(f"[entity_repair] {act} of entity {eid} applied but not "
                  f"logged ({exc}).")
            return {"log_error": str(exc)}
    if act == "merge":
        res = store.merge_entities(int(a["into"]), eid,
                                   reason=f"repair:{rule}", actor=actor)
        if not res:
            return {"ok": False, "why": "merge_refused"}
        return {"ok": True, "merge_id": res.get("merge_id"),
                **audited("row", {"name": name, "kind": before.get("kind"),
                                  "kind_source": before.get("kind_source")},
                          {"into": int(a["into"]),
                           "merge_id": res.get("merge_id")})}
    if act == "rekind":
        ok = store.set_entity_kind(eid, a["to_kind"],
                                   source=a.get("kind_source") or "rule",
                                   confidence=a.get("confidence"))
        if not ok:
            return {"ok": False, "why": "kind_refused"}
        after = _row(store, eid)
        return {"ok": True, "change_id": undoable(
            "kind", _kind_fields(before), _kind_fields(after))}
    if act == "reparent":
        ok = store.set_entity_parent(eid, a.get("parent_id"), source="rule")
        if not ok:
            return {"ok": False, "why": "parent_refused"}
        after = _row(store, eid)
        if _parent_fields(after) == _parent_fields(before):
            return {"ok": True, "noop": True}
        return {"ok": True, "change_id": undoable(
            "parent", _parent_fields(before), _parent_fields(after))}
    if act == "hide" or (act == "drop" and hide_drops):
        if before.get("hidden"):
            return {"ok": True, "noop": True}
        # Hidden by the repair: its undo lifts only its own hide.
        store.set_entity_hidden(eid, hidden=True, source="repair")
        # On the automatic path and in the Inbox a drop is a hide; the rule
        # says what kind of drop it stood for (an automatic hide is lifted
        # by real sightings: `entity_candidates`; one the user accepted in
        # the Inbox stays until they undo it).
        return {"ok": True, "as_hide": act == "drop" or None,
                "change_id": undoable("hidden", {"hidden": False},
                                      {"hidden": True})}
    if act == "drop":
        tomb = bool(a.get("tombstone"))
        gone = store.delete_entity(eid, tombstone=tomb,
                                   reason=f"repair:{rule}", actor=actor)
        if not gone:
            return {"ok": False, "why": "missing"}
        # A forgotten name is not kept, here either: a tombstoned row is
        # logged by its name hash.
        return {"ok": True, **audited(
            "row", {"kind": before.get("kind"),
                    "kind_source": before.get("kind_source")},
            {"deleted": True, "tombstone": tomb},
            logged_name=store.entity_tombstone_hash(name) if tomb else name)}
    return {"ok": True, "noop": True}


def apply(store, plan_data: dict, *, mode: str = "reviewed",
          only: str | None = None, actor: str = "repair",
          backup: str | None = None, run_id: str | None = None) -> dict:
    """Apply a plan. `mode` "reviewed" applies every action as planned
    (merges and deletions included); "auto" applies the actions routed
    `auto` (a drop becomes a hide). Refuses (`PlanRefused`, nothing
    written) a plan that does not fit the store (`check`). Returns {run_id,
    applied, skipped}."""
    if getattr(store, "_readonly", False):
        raise PlanRefused("the store is read-only", [])
    # One run at a time, across processes: the script's reviewed apply and
    # the app's automatic pass (or an Inbox answer) never interleave.
    busy = store.entity_repair_active_run()
    if busy is not None:
        raise PlanRefused(f"another repair run is in progress "
                          f"({busy['run_id']})",
                          [{"entity_id": None, "problem": "busy",
                            "fields": [busy["run_id"]]}])
    # A reviewed plan (a file the user read, perhaps edited) must still be
    # what the rules decide now; the automatic path and the Inbox apply
    # what was just planned or what the user just answered.
    problems = check(store, plan_data, verify=(mode == "reviewed"))
    if problems:
        raise PlanRefused(refusal_reason(problems), problems)
    auto = mode == "auto"
    hide_drops = mode in ("auto", "inbox")
    run_id = run_id or _new_run_id(mode)
    # The busy check above is a read; this insert is the guard: it records
    # the run only when no other run is in flight, in one statement, so two
    # processes that both read "none" cannot both go on.
    if not store.entity_repair_start_run(
            run_id, mode=mode, rules_version=plan_data.get("rules_version"),
            backup=backup, exclusive_s=ACTIVE_RUN_S):
        busy = store.entity_repair_active_run(max_age_s=ACTIVE_RUN_S)
        raise PlanRefused(
            "another repair run is in progress"
            + (f" ({busy['run_id']})" if busy else ""),
            [{"entity_id": None, "problem": "busy",
              "fields": [busy["run_id"]] if busy else []}])
    todo = [a for a in plan_data.get("actions") or ()
            if a.get("action") != "keep"
            and (not auto or a.get("route") == "auto")
            and (only is None or a.get("action") == only)]
    todo.sort(key=lambda a: (_APPLY_ORDER.get(a["action"], 9),
                             int(a["entity_id"])))
    applied, skipped = [], []
    merged_away: set[int] = set()
    by_action: dict[str, int] = {}
    summary: dict = {}
    try:
        for a in todo:
            if int(a["entity_id"]) in merged_away:
                skipped.append({**_brief(a), "why": "merged"})
                continue
            try:
                res = _apply_one(store, a, run_id=run_id, auto=auto,
                                 actor=actor,
                                 since=plan_data.get("created_at"),
                                 hide_drops=hide_drops)
            except Exception as exc:
                res = {"ok": False, "why": f"error: {exc}"}
            if res.get("ok"):
                brief = _brief(a)
                if a["action"] == "drop" and a.get("tombstone") \
                        and not hide_drops:
                    # Forgotten now: the report keeps its hash, not its name.
                    brief["name"] = store.entity_tombstone_hash(a.get("name"))
                applied.append({**brief, **{k: v for k, v in res.items()
                                            if k != "ok" and v is not None}})
                if a["action"] == "merge":
                    merged_away.add(int(a["entity_id"]))
            else:
                skipped.append({**_brief(a), "why": res.get("why")})
        for x in applied:
            if not x.get("noop"):
                by_action[x["action"]] = by_action.get(x["action"], 0) + 1
    finally:
        # Always closed, so a failure never leaves a run "in progress"
        # that blocks the next one.
        summary = {"applied": len(applied), "skipped": len(skipped),
                   "by_action": by_action}
        store.entity_repair_finish_run(run_id, summary)
    return {"run_id": run_id, "mode": mode, "applied": applied,
            "skipped": skipped, "summary": summary}


def _brief(a: dict) -> dict:
    out = {k: a.get(k) for k in ("entity_id", "name", "action", "rule")}
    for k in ("to_kind", "parent_id", "parent_name", "into", "into_name",
              "tombstone"):
        if a.get(k) is not None:
            out[k] = a[k]
    return out


def report_safe(plan_data: dict, store=None) -> dict:
    """The plan as a report file keeps it: no more personal text than the
    review needs. A drop that tombstones keeps its name (the reviewer must
    see what goes) but a B2 person's suggestion keeps only the person's id
    (`person_id`): the person's own row says who it is."""
    out = dict(plan_data)
    acts = []
    for a in plan_data.get("actions") or ():
        if a.get("person"):
            a = {**a, "person": {"id": (a["person"] or {}).get("id")}}
        acts.append(a)
    out["actions"] = acts
    return out


# ----------------------------------------------------- automatic path --
def backup_path(store, run_id: str):
    db = getattr(store, "db_path", None)
    if db is None:
        return None
    from pathlib import Path
    db = Path(db)
    return db.with_name(f"{db.name}.bak-entity-repair-{run_id}")


def last_auto_run(store) -> dict | None:
    """The automatic run that finished for this rules version, if any (an
    attempt the plan check refused is no run: `_refused_recently`)."""
    for r in store.entity_repair_runs(mode="auto", limit=200):
        if r.get("rules_version") == RULES_VERSION and r.get("finished_at") \
                and not (r.get("summary") or {}).get("refused"):
            return r
    return None


def _refused_recently(store) -> dict | None:
    """The latest automatic attempt, when the plan check refused it less
    than `backup_retry_s` ago (the store moved while it ran): the graph job
    waits that long before it copies the database for another try."""
    for r in store.entity_repair_runs(mode="auto", limit=20):
        if r.get("rules_version") != RULES_VERSION:
            continue
        if (r.get("summary") or {}).get("refused") and \
                time.time() - float(r.get("started_at") or 0) \
                < backup_retry_s():
            return r
        return None
    return None


def backups_keep() -> int:
    """QUILL_ENTITY_REPAIR_BACKUPS_KEEP (default 3): how many backups of
    each kind (`.bak-entity-repair-*`, `.bak-seat-vocab-*`) are kept next to
    the database; older ones are deleted after each new one."""
    try:
        return max(1, int(os.environ.get("QUILL_ENTITY_REPAIR_BACKUPS_KEEP",
                                         "3")))
    except (TypeError, ValueError):
        return 3


_BACKUP_KINDS = ("bak-entity-repair-", "bak-seat-vocab-")


def prune_backups(db, *, keep: int | None = None) -> list[str]:
    """Keep the newest `keep` (default `backups_keep()`) backups of each
    kind next to `db` and delete the rest. A backup holds every name the
    store ever had, forgotten ones too, so their number (and with it how
    long a forgotten name survives in one) is bounded. Returns the files
    deleted; never raises."""
    from pathlib import Path
    keep = backups_keep() if keep is None else max(1, int(keep))
    try:
        db = Path(db)
        files = [p for p in db.parent.iterdir() if p.is_file()]
    except OSError:
        return []
    removed: list[str] = []
    for tag in _BACKUP_KINDS:
        mine = [p for p in files if p.name.startswith(f"{db.name}.{tag}")
                and not p.name.endswith(("-wal", "-shm", "-journal"))]
        try:
            mine.sort(key=lambda p: (p.stat().st_mtime, p.name),
                      reverse=True)
        except OSError:
            continue
        for p in mine[keep:]:
            try:
                p.unlink()
                removed.append(str(p))
            except OSError:
                pass
    return removed


# A failed backup is not retried at every graph job (each attempt copies
# the whole database): {db path: time of the last failure}.
_BACKUP_FAILED: dict[str, float] = {}


def backup_retry_s() -> float:
    """QUILL_ENTITY_REPAIR_BACKUP_RETRY_S (default 3600): how long the
    automatic path waits after a failed backup before it tries again."""
    try:
        return max(0.0, float(os.environ.get(
            "QUILL_ENTITY_REPAIR_BACKUP_RETRY_S", "3600")))
    except (TypeError, ValueError):
        return 3600.0


def auto_run(store, *, force: bool = False, note_only: str | None = None,
             backup: str | None = None) -> dict:
    """The automatic path. Once per RULES_VERSION (unless `force`), only
    when QUILL_ENTITY_REPAIR_AUTO is on (or `force`), never on a read-only
    store, never while another repair run is in flight (the script's
    reviewed apply in another process included), and never without a
    backup: the database is copied first with SQLite's backup API (safe
    while the app writes), and a failed copy means no run (tried again
    after `backup_retry_s`). `backup` is a copy the caller already made
    (the script makes it before it opens the store for writing). Applies
    the `auto` actions, leaves the rest in the Inbox. Returns what it did."""
    if not force and not auto_enabled():
        return {"ran": False, "why": "off"}
    if getattr(store, "_readonly", False):
        return {"ran": False, "why": "read_only"}
    if not force:
        done = last_auto_run(store)
        if done is not None:
            return {"ran": False, "why": "done", "run_id": done["run_id"]}
        refused = _refused_recently(store)
        if refused is not None:
            return {"ran": False, "why": "refused_recently",
                    "run_id": refused["run_id"],
                    "error": (refused.get("summary") or {}).get("refused")}
    busy = store.entity_repair_active_run()
    if busy is not None:
        return {"ran": False, "why": "busy", "run_id": busy["run_id"]}
    run_id = _new_run_id("auto")
    if backup is not None:
        dest = backup
    else:
        dest = backup_path(store, run_id)
        if dest is None:
            return {"ran": False, "why": "no_backup"}
        key = str(dest.parent)
        failed = _BACKUP_FAILED.get(key)
        if not force and failed is not None \
                and time.time() - failed < backup_retry_s():
            return {"ran": False, "why": "no_backup",
                    "error": "the last backup failed; waiting to retry"}
        try:
            store.backup_to(dest)
        except Exception as exc:
            _BACKUP_FAILED[key] = time.time()
            return {"ran": False, "why": "no_backup", "error": str(exc)}
        _BACKUP_FAILED.pop(key, None)
        prune_backups(getattr(store, "db_path", dest))
    plan_data = plan(store, note_only=note_only)
    try:
        res = apply(store, plan_data, mode="auto", backup=str(dest),
                    run_id=run_id)
    except PlanRefused as exc:
        # Nothing was written. The attempt is recorded (a finished run with
        # nothing applied), so the next graph job does not copy the database
        # again before `backup_retry_s`; the copy made for it is of no use.
        if backup is None:
            try:
                from pathlib import Path
                Path(dest).unlink()
            except OSError:
                pass
        if store.entity_repair_start_run(
                run_id + "-refused", mode="auto",
                rules_version=RULES_VERSION):
            store.entity_repair_finish_run(
                run_id + "-refused", {"applied": 0, "skipped": 0,
                                      "by_action": {},
                                      "refused": exc.reason})
        return {"ran": False, "why": "refused", "error": exc.reason,
                "backup": str(dest) if backup is not None else None}
    proposed = 0
    for a in plan_data["actions"]:
        if a["action"] == "keep" or a["route"] != "review":
            continue
        if propose(store, a, run_id=run_id):
            proposed += 1
    res["proposed"] = proposed
    res["backup"] = str(dest)
    res["ran"] = True
    return res


def _dedupe_key(a: dict) -> str:
    """The same change to the same row, whichever run proposes it."""
    st = a.get("state") or {}
    if a["action"] == "merge":
        target = (a.get("into_state") or {}).get("canonical_id") \
            or a.get("into")
    elif a["action"] == "reparent":
        target = (a.get("parent_state") or {}).get("canonical_id") or "none"
    else:
        target = a.get("to_kind") or ""
    raw = "|".join(str(x) for x in (st.get("canonical_id") or a["entity_id"],
                                    a["action"], a.get("rule"), target))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def propose(store, a: dict, *, run_id: str) -> int:
    """Leave one action for the user to decide in the Inbox."""
    return store.entity_repair_propose(
        run_id=run_id, entity_id=int(a["entity_id"]),
        canonical_id=(a.get("state") or {}).get("canonical_id"),
        action=a["action"], rule=a.get("rule"), dedupe_key=_dedupe_key(a),
        payload=a)


def proposals(store) -> list[dict]:
    """Open proposals, each {id, action, rule, payload}."""
    return store.entity_repair_proposals(status="open")


# ----------------------------------------------------- the decision trail --
# The user's answers (an Inbox proposal accepted or dismissed, a run or a
# change undone, a reviewed plan applied by the operator) go to the shared
# trail (`decisions.record`, surface `entities.repair`). A proposal to drop
# and forget a name keeps the name's hash, not the name, once accepted.
DECISION_SURFACE = "entities.repair"


def _proposal_shown(store, a: dict, *, forgotten: bool) -> dict:
    out = {k: a.get(k) for k in ("action", "rule", "kind", "to_kind",
                                 "into_name", "parent_name", "confidence")
           if a.get(k) is not None}
    if forgotten:
        out["name_hash"] = store.entity_tombstone_hash(a.get("name"))
    else:
        out["name"] = a.get("name")
    return out


def _record(store, subject: str, verdict: str, *, shown=None, options=None,
            meta=None, actor: str = "user", via: str | None = None) -> None:
    from app.services import decisions
    decisions.record(surface=DECISION_SURFACE, subject=subject,
                     verdict=verdict, shown=shown, options=options,
                     meta=meta, actor=actor, via=via, store=store)


def record_undo(store, *, run_id: str | None = None,
                change_id: int | None = None, result: dict | None = None,
                actor: str = "user", via: str | None = None) -> None:
    """Trail one undo of a run or a change (`undo`'s result as counts).
    Nothing is trailed when every change was refused: nothing was undone."""
    from app.services import decisions
    res = result or {}
    if not res.get("undone") and not res.get("withdrawn"):
        return
    subject = (f"repair_run:{run_id}" if run_id
               else f"repair_change:{int(change_id)}")
    decisions.undo(surface=DECISION_SURFACE, subject=subject, actor=actor,
                   via=via, store=store,
                   meta={"undone": len(res.get("undone") or ()),
                         "refused": len(res.get("refused") or ()),
                         "withdrawn": int(res.get("withdrawn") or 0)})


def record_apply(store, result: dict, *, actor: str = "operator",
                 via: str | None = None, plan_data: dict | None = None) -> None:
    """Trail a reviewed plan (or a forced automatic run) the operator
    applied: one approval for the whole run, with its counts."""
    _record(store, f"repair_run:{result.get('run_id')}", "approved",
            actor=actor, via=via,
            meta={"mode": result.get("mode"),
                  "summary": result.get("summary"),
                  "rules_version": (plan_data or {}).get("rules_version")})


def decide(store, proposal_id: int, verdict: str, *,
           decided_by: str = "user", via: str | None = None) -> dict:
    """The user's answer to one proposal: `accept` makes the change (a drop
    as a hide the user can undo, never a deletion; a merge only after a
    backup of the database, since a merge has no undo), `dismiss` closes
    it, and it is never proposed again. A proposal the store moved on from is closed as
    stale and nothing is written. An answer that changed something is
    trailed (`decisions`): accepted `approved`, dismissed `rejected`; a
    stale one, which wrote nothing, is not."""
    rows = store.entity_repair_proposals(status=None,
                                         proposal_id=int(proposal_id))
    if not rows:
        return {"ok": False, "error": "no such proposal"}
    p = rows[0]
    if p["status"] != "open":
        return {"ok": False, "error": f"already {p['status']}"}
    subject = f"repair_proposal:{int(p['id'])}"
    options = ["accept", "dismiss"]
    if verdict == "dismiss":
        store.entity_repair_decide_proposal(p["id"], "dismissed",
                                            decided_by=decided_by)
        _record(store, subject, "rejected", options=options, via=via,
                shown=_proposal_shown(store, p["payload"], forgotten=False))
        return {"ok": True, "status": "dismissed"}
    if verdict != "accept":
        return {"ok": False, "error": f"unknown verdict {verdict!r}"}
    a = p["payload"]
    # An Inbox "remove" is a hide (`apply` mode inbox), which the user can
    # undo: nothing is deleted or forgotten from here.
    forgets = False
    if check(store, {"actions": [a]}):
        store.entity_repair_decide_proposal(p["id"], "stale",
                                            decided_by=decided_by)
        return {"ok": False, "error": "it changed since; nothing written",
                "status": "stale"}
    run_id = _new_run_id("inbox")
    backup = None
    if a.get("action") == "merge":
        # A merge has no undo: the database is copied first (kept with the
        # repair's other backups), and with no copy nothing is merged.
        dest = backup_path(store, run_id)
        try:
            if dest is None:
                raise OSError("no database path")
            backup = store.backup_to(dest)
        except Exception as exc:
            return {"ok": False, "status": "open",
                    "error": f"could not back the database up first "
                             f"({exc}); nothing merged"}
        prune_backups(getattr(store, "db_path", dest))
    try:
        res = apply(store, {"actions": [a], "rules_version": RULES_VERSION},
                    mode="inbox", actor=decided_by, backup=backup,
                    run_id=run_id)
    except PlanRefused as exc:
        # Another run is in flight (or the store is read-only): nothing
        # written, and the question stays open to answer again.
        return {"ok": False, "error": exc.reason, "status": "open"}
    ok = bool(res["applied"])
    store.entity_repair_decide_proposal(p["id"], "accepted" if ok else "stale",
                                        decided_by=decided_by)
    if ok:
        _record(store, subject, "approved", options=options, via=via,
                shown=_proposal_shown(store, a, forgotten=forgets),
                meta={"status": "accepted", "run_id": res["run_id"]})
    return {"ok": ok, "status": "accepted" if ok else "stale",
            "run_id": res["run_id"], "applied": res["applied"],
            "skipped": res["skipped"], "backup": backup}


# ----------------------------------------------------------------- undo --
def undo(store, *, run_id: str | None = None,
         change_id: int | None = None) -> dict:
    """Put back what a run (newest change first) or one change wrote.
    A field the user, or anything else, changed since is refused, and so
    is a merge or a deletion (restore those from the run's backup).
    Returns {undone: [...], refused: [...]}."""
    if run_id is None and change_id is None:
        return {"undone": [], "refused": [], "error": "nothing to undo"}
    busy = store.entity_repair_active_run()
    if busy is not None:
        return {"undone": [], "refused": [], "busy": busy["run_id"],
                "error": "another repair run is in progress"}
    rows = store.entity_repair_log(run_id=run_id, change_id=change_id)
    backup = None
    if run_id is not None:
        for r in store.entity_repair_runs(limit=500):
            if r["run_id"] == run_id:
                backup = r.get("backup")
    undone, refused = [], []
    for r in reversed(rows):
        if r.get("undone_at"):
            continue
        brief = {"change_id": r["id"], "entity_id": r["entity_id"],
                 "name": r.get("name"), "field": r["field"],
                 "action": r["action"], "rule": r.get("rule")}
        if r["field"] not in store.REPAIR_FIELDS:
            from pathlib import Path
            why = "a merge or a deletion is not undone here" + (
                (f"; restore from {backup}" if Path(backup).exists()
                 else f"; its backup ({backup}) is no longer kept")
                if backup else "")
            refused.append({**brief, "why": why})
            continue
        ok, why = store.entity_repair_restore(
            int(r["entity_id"]), r["field"], before=r["before"] or {},
            expect=r["after"] or {})
        if ok:
            store.entity_repair_mark_undone(r["id"], "undone")
            undone.append(brief)
        else:
            store.entity_repair_note(r["id"], f"undo refused: {why}")
            refused.append({**brief, "why": why})
    withdrawn = 0
    if run_id is not None:
        # The run is taken back: the questions it left in the Inbox go with
        # it (a later run may ask them again; an answered one stays).
        withdrawn = store.entity_repair_withdraw_proposals(run_id)
        # "Undone" only when nothing of it stands: a merge or a deletion
        # (never undone here) keeps the run in force.
        left = [r for r in store.entity_repair_log(run_id=run_id)
                if not r["undone_at"]]
        if not left:
            store.entity_repair_mark_run_undone(run_id)
    return {"undone": undone, "refused": refused, "withdrawn": withdrawn}


def lift_note_only_hide(store, entity_id: int, *,
                        rows: list[dict] | None = None) -> bool:
    """A row the automatic path hid (only notes or screen text named it, a
    one-off lowercase description, ...: every rule but a value's) comes
    back when real sightings would have promoted it (the candidate gate's
    rule over the sightings bound since the hide: the user typing it in two
    sessions brings back a lowercase name). True when it was shown
    again."""
    hold = store.entity_repair_hold(int(entity_id))
    if hold is None or not store.entity_is_hidden(int(entity_id)):
        return False
    from app.services import entity_candidates as cand
    if rows is None:
        rows = store.entity_mentions(entity_id=int(entity_id))
    since = [r for r in rows if float(r.get("created_at") or 0) > hold["at"]
             and cand.mint_eligible(r)]
    if not since:
        return False
    e = store.get_entity(int(entity_id)) or {}
    name = e.get("name") or ""
    kinds = [r.get("proposed_kind") for r in since]
    typed = [r for r in since if r.get("typed")]
    sessions = cand.distinct_sessions(
        store, [float(r.get("observed_at") or 0) for r in since])
    classes = len({r.get("source_class") for r in since
                   if r.get("source_class")})
    need = cand.min_sessions()
    if entity_typing.weak_name(name, kinds):
        promote = bool(typed) and cand.distinct_sessions(
            store, [float(r.get("observed_at") or 0) for r in typed]) >= need
    elif typed and not entity_typing.code_like(name):
        promote = True
    else:
        promote = sessions >= need or classes >= 2
    if not promote:
        return False
    ok, _why = store.entity_repair_restore(
        int(entity_id), "hidden", before={"hidden": False},
        expect={"hidden": True})
    if ok:
        store.entity_repair_mark_undone(hold["change_id"],
                                        "lifted: real sightings")
    return ok
