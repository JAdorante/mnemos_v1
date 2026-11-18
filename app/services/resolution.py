"""Step 3 — person/entity resolution: collapse mentions to one identity.

The extractor pulls names out of speech as raw strings ("Chris", "Christopher",
"chris"). Without resolution, "my open tasks owned by Chris" fragments across
three rows. This service maps a raw name to a stable person id using a cheap
cascade, most-confident first:

  1. exact  — same name (case-insensitive). The common case.
  2. prefix — short name is exactly the FIRST TOKEN of a longer name
     ("Chris"/"Chris Falloon"). Not string-startswith ("Chris"/"Christina").
  3. embedding — cosine over the local MiniLM name embedding >= threshold. Catches
     spelling/spacing variants the first two miss.
  else     — a new person, stored with its embedding for future matches.

Deliberately conservative: the embedding threshold is high (name strings are
short, so false merges — "Marc"/"Mike" — are the real risk). Every fuzzy merge is
recorded as an alias on the person row, so a wrong merge is visible and fixable.
`me`/'' resolve to None (the speaker is linked to the enrolled voice later).
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import numpy as np

from app.storage import Store, get_store

# High by default: a false merge is worse than a missed one (you can merge later,
# un-merging is painful). Tunable for experiments.
PERSON_SIM_THRESHOLD = float(os.environ.get("QUILL_PERSON_SIM_THRESHOLD", "0.82"))
_MIN_PREFIX = 3


@dataclass(frozen=True)
class EntityMatch:
    """What `Resolver.match_entity` found for a name (see there)."""
    status: str                     # existing | rejected | new
    name: str = ""                  # the name as given, stripped
    entity_id: int = 0              # the row, when status is `existing`
    via: str = ""                   # how it matched, or why it was rejected
    forgotten: bool = False         # the user forgot this name
    person_shaped: bool = False     # rejected, and belongs in people
    canonical: str = ""             # exact match: the row's stored name
    kind_source: str | None = None  # exact match: who set the row's kind


def _cos(a, b) -> float:
    # embedder returns L2-normalized vectors, so cosine == dot; guard anyway.
    if a is None or b is None:
        return -1.0
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / denom)


# What the person adjudicator writes on a row it judged not a human.
_NOT_HUMAN_VERDICTS = frozenset({"org", "tool", "place", "junk", "entity"})


def _is_known_person_name(store: Store, name: str) -> bool:
    """True when `name` is already a person's canonical name or alias —
    hidden/merged rows count, since a merged-away alias ("justin") is still a
    person, not a mintable project. A row the person adjudicator judged not
    human ("a place", "an org") is no person: it was hidden for that.

    This is also the evidence a person-shaped entity sighting needs before
    it goes to people (`Resolver.match_entity`): two Title Case words alone
    are not. Measured on the 24 practice worlds (fix pass B): the shape
    alone sent 89 sightings of answer-key orgs and projects, places and
    other non-people to people and caught 8 of answer-key people; a person
    the seat already knew caught 7 of those 8 and none of the rest."""
    key = (name or "").strip().lower()
    if not key:
        return False
    for p in store.all_people():
        verdict = p.get("adjudication") or {}
        if isinstance(verdict, dict) and verdict.get("kind") in \
                _NOT_HUMAN_VERDICTS:
            continue
        if (p.get("name") or "").strip().lower() == key:
            return True
        if any((a or "").strip().lower() == key for a in p.get("aliases") or []):
            return True
    return False


def _is_filed_name(name: str) -> bool:
    """A name in the shared or filed tier of the project registry
    (`projects.filed_keys`, never the circular `known` tier)."""
    try:
        from app.services import projects
        return projects.norm(name) in projects.filed_keys()
    except Exception:
        return False


def _prefix_match(a: str, b: str) -> bool:
    """Nickname ↔ full-name only — never string-prefix merges.

    "Chris" matches "Chris Falloon" (first token equal). It does NOT match
    "Christina" or "Christopher" via startswith (those go through phonetic /
    embedding). Arbitrary startswith was collapsing unrelated people and
    dumping their emails onto one node.
    """
    a, b = a.lower().strip(), b.lower().strip()
    if a == b:
        return True
    aw, bw = a.split(), b.split()
    if len(aw) == 1 and len(bw) >= 2:
        return len(aw[0]) >= _MIN_PREFIX and bw[0] == aw[0]
    if len(bw) == 1 and len(aw) >= 2:
        return len(bw[0]) >= _MIN_PREFIX and aw[0] == bw[0]
    return False


class Resolver:
    def __init__(self, store: Store | None = None,
                 threshold: float = PERSON_SIM_THRESHOLD) -> None:
        self._store = store
        self._threshold = threshold
        self._embedder = None
        self._lock = threading.Lock()

    def _s(self) -> Store:
        if self._store is None:
            self._store = get_store()
        return self._store

    def _embed(self, text: str):
        if self._embedder is None:
            from app.services.embeddings import embedder
            self._embedder = embedder
        try:
            return self._embedder.encode(text)
        except Exception:
            return None   # embeddings unavailable -> degrade to exact+prefix only

    def resolve_person(self, name: str, *, ts: float | None = None) -> int | None:
        """Return a stable person id for `name`, creating one if needed. Returns
        None for the speaker ('me'), an empty name, or a string that isn't a
        plausible person name (a fragment / system token / path) — the gate that
        keeps extractor noise out of the people graph."""
        key = (name or "").strip()
        if not key or key.lower() in ("me", "myself", "i"):
            return None
        from app.services.name_quality import is_plausible_person, normalize_person_name
        key = normalize_person_name(key)
        # The seat's own vocabulary comes with its store; a Resolver with no
        # store yet uses the shared one only if it is already open.
        if not is_plausible_person(key, store=self._store):
            return None
        with self._lock:
            store = self._s()
            # 1) exact (follows soft-merge redirects inside find_person_exact)
            pid = store.find_person_exact(key)
            if pid is not None:
                store.touch_person(pid, ts)
                return pid

            people = store.list_people_embed()
            # Soft-merged / hidden nodes stay out of fuzzy matching — same filter
            # as people_pipeline so legacy resolve cannot re-touch absorbed ids.
            people = [p for p in people
                      if not p.get("canonical_person_id")
                      and not p.get("hide_from_people")]
            # 2) prefix / nickname
            for p in people:
                names = [p["name"], *p.get("aliases", [])]
                if any(_prefix_match(key, n) for n in names):
                    store.touch_person(p["id"], ts, alias=key)
                    return p["id"]

            # 3) phonetic / edit correction (catch ASR mis-hearings of a name
            # that exact+prefix miss and embeddings rate too low: "Abby Nangle"
            # -> "Abby Nengel"). Shared EntityCorrectionService owns the rule.
            from app.services.entity_correction import corrector
            m = corrector.match(key, people)
            if m is not None:
                store.touch_person(int(m.ref), ts, alias=key)
                return int(m.ref)

            # 4) embedding
            emb = self._embed(key)
            if emb is not None and people:
                best, best_sim = None, -1.0
                for p in people:
                    sim = _cos(emb, p.get("embedding"))
                    if sim > best_sim:
                        best, best_sim = p, sim
                if best is not None and best_sim >= self._threshold:
                    store.touch_person(best["id"], ts, alias=key)
                    return best["id"]

            # else: a new person, remembered with its embedding
            return store.insert_person(key, embedding=emb, ts=ts)

    # Entity resolution is two steps. `match_entity` asks what a name is
    # against what is already known and writes nothing; `mint_entity` acts
    # on that answer (touch, re-kind, or create the row). `resolve_entity`
    # is the two run back to back under one lock. The candidate gate
    # (services/entity_candidates.py) runs them apart, so a new name can
    # wait in the ledger between the match and the mint.
    def match_entity(self, name: str, kind: str | None = None) -> EntityMatch:
        """The match step: what is `name`, against what is already known?

        - `existing`: an entity row answers to it (`entity_id`). `via` says
          how: `exact` (a visible row with that name), `hidden` (a hidden
          row carries exactly this name, as its name or a confirmed alias),
          `alias` (a visible row's confirmed alias), `fuzzy` (the correction
          layer folds it into a neighbour). An exact name always wins over
          a fuzzy neighbour.
        - `rejected`: it can never be an entity. `via` is `empty`,
          `implausible`, `not_entity` (a person-shaped name under a
          project/idea label that this seat already knows as a person;
          `person_shaped` set, so the mint step touches that person) or
          `known_person`.
        - `new`: nothing answers to it. `forgotten` is set when the user
          forgot this name: only a user act may mint it then.

        The lookups come before the person-shape gate: a name a row
        already carries (visible or hidden, canonical or confirmed alias)
        is that row, and a forgotten name (of any kind) stays forgotten,
        whatever the name looks like. A person-shaped name goes to people
        only on person evidence (`_is_known_person_name`) and never when it
        is a filed project's name; otherwise it is a new name like any
        other, and the candidate gate and the kind vote decide what it
        becomes.

        Read-only: no row is touched and nothing is created."""
        pre, kind_n, shaped = self._entity_precheck(name, kind,
                                                    store=self._store)
        if pre is not None:
            return pre
        with self._lock:
            return self._match_entity_unlocked((name or "").strip(), kind_n,
                                               person_shaped=shaped)

    @staticmethod
    def _entity_precheck(name: str, kind: str | None, *, store=None):
        """(rejection | None, normalized kind, person-shaped): the
        name-shape gates, which read no rows (`store` only lends its seat
        vocabulary). Shared by the match step and the composition.
        Person-shaped: the shape alone would not mint the name under this
        kind (`should_mint_as_entity`); the store-backed match decides
        what that means."""
        key = (name or "").strip()
        if not key:
            return EntityMatch("rejected", via="empty"), None, False
        from app.services.name_quality import (
            is_plausible_entity,
            normalize_entity_kind,
            should_mint_as_entity,
        )
        if not is_plausible_entity(key):
            return (EntityMatch("rejected", name=key, via="implausible"),
                    None, False)
        kind_n = normalize_entity_kind(kind)
        return None, kind_n, not should_mint_as_entity(key, kind_n,
                                                       store=store)

    def _match_entity_unlocked(self, key: str, kind_n: str, *,
                               person_shaped: bool = False) -> EntityMatch:
        """Store-backed half of the match. Caller holds `_lock`."""
        store = self._s()
        # Exact is cheap and handles the common case.
        existing = store.all_entities()
        exact = next((e for e in existing
                      if (e["name"] or "").lower() == key.lower()), None)
        if exact is not None:
            return EntityMatch("existing", name=key,
                               entity_id=int(exact["id"]), via="exact",
                               canonical=exact["name"],
                               kind_source=exact.get("kind_source"))
        # Forgotten by the user, and no live row carries the name (a
        # hidden row is not in `existing`, so ask the store directly).
        # A forgotten name is not sent to people either, whatever its kind.
        forgotten = store.is_entity_tombstoned(key)
        exact_id = store.find_entity_exact(key)
        if forgotten and exact_id is None:
            return EntityMatch("new", name=key, forgotten=True)
        # A name already living in the people table (canonical OR alias,
        # hidden/merged rows included) must not fork into a project/idea
        # node — that's how "Justin"[project] and "Marc"[project] happened.
        # Orgs/tools/places stay allowed: a company can share a founder's
        # name; a second project named "Marc" cannot.
        # Audit 2026-11-11: OCR of an inbox re-minted five known people as
        # kind=tool, so the guard now covers every non-org kind; only an
        # org may legitimately share a person's name (a founder's firm).
        def known_person() -> bool:
            return kind_n != "org" and _is_known_person_name(store, key)
        if exact_id is not None:
            # A row that carries exactly this name (a hidden row's canonical
            # name, or any row's confirmed alias) is what the sighting names,
            # and it wins over the corrector's fuzzy neighbour: a sighting of
            # a name the user or the repair hid must not fold into another
            # entity (and the repair's "hidden until a real sighting" must
            # see it). The row stays hidden; only the store's own rules (a
            # filing or the user) show it again. A forgotten name that a
            # hidden row still carries binds here too, as before. The
            # known-person guard still comes first for a hidden row and for
            # a forgotten name, as it did when this match came last.
            hidden = store.entity_is_hidden(int(exact_id))
            if (hidden or forgotten) and known_person():
                return EntityMatch("rejected", name=key, via="known_person")
            return EntityMatch("existing", name=key,
                               entity_id=int(exact_id),
                               via="hidden" if hidden else "alias",
                               forgotten=forgotten)
        # A project the user files under is that project, even before it
        # has a row (a team's shared project) and even if a person was once
        # minted under its name.
        filed = _is_filed_name(key)
        if person_shaped and not filed and _is_known_person_name(store, key):
            # Two Title Case words under a project/topic label, and this
            # seat knows a person by that name: it is that person.
            return EntityMatch("rejected", name=key, via="not_entity",
                               person_shaped=True)
        from app.services.entity_correction import corrector
        # The corrector folds ASR slips ("Dell Capitol" -> "Dell Capital").
        # An all-caps acronym from a document is not an ASR slip, and
        # Soundex makes "ABB" a twin of "Abby" (audit 2026-11-11).
        is_acronym = key.isupper() and key.isalpha() and len(key) <= 5
        m = None if is_acronym else corrector.match(key, existing)
        if m is not None:
            return EntityMatch("existing", name=key, entity_id=int(m.ref),
                               via="fuzzy")
        if not filed and known_person():
            return EntityMatch("rejected", name=key, via="known_person")
        return EntityMatch("new", name=key)

    def mint_entity(self, match: EntityMatch, kind: str | None = None, *,
                    ts: float | None = None,
                    kind_source: str | None = None,
                    kind_confidence: float | None = None) -> int:
        """The mint step: act on a match. Touches (and, for a higher-rank
        caller, re-kinds) an existing row, creates the row for a new name,
        and sends a person-shaped rejection to people. Returns the entity
        id, or 0 for "no entity"."""
        if match.status == "rejected":
            self._reroute_person_shaped(match, ts)
            return 0
        with self._lock:
            return self._mint_entity_unlocked(
                match, kind, ts=ts, kind_source=kind_source,
                kind_confidence=kind_confidence)

    def _reroute_person_shaped(self, match: EntityMatch,
                               ts: float | None) -> None:
        # Person-shaped project/idea that names a person this seat knows:
        # the sighting is that person's, skip the entity mint. It touches
        # the person and never creates one (an entity sighting is no
        # evidence that a new person exists). Outside `_lock`: the store
        # takes its own.
        if match.person_shaped:
            try:
                store = self._s()
                pid = store.find_person_exact(match.name)
                if pid is not None:
                    store.touch_person(pid, ts)
            except Exception:
                pass

    def _mint_entity_unlocked(self, match: EntityMatch, kind: str | None, *,
                              ts: float | None, kind_source: str | None,
                              kind_confidence: float | None,
                              retry: bool = True) -> int:
        store = self._s()
        if match.status == "existing" and match.via == "exact":
            if (kind or "").strip() and Store.kind_source_rank(kind_source) \
                    > Store.kind_source_rank(match.kind_source):
                # A higher-rank caller re-kinds the row; the store
                # applies the rank rule and bumps last_seen itself.
                # The raw label goes down, not the normalized kind: the
                # store ignores a label that is not a kind ("client" would
                # arrive as "idea" and overwrite the row's kind). It is
                # exact-match-or-create: a row gone since is not revived.
                return store.resolve_entity(
                    match.canonical or match.name, kind, ts=ts,
                    kind_source=kind_source,
                    kind_confidence=kind_confidence)
            if store.touch_entity(match.entity_id, ts=ts) is not False:
                return int(match.entity_id)
            return self._rematch_unlocked(match, kind, ts=ts,
                                          kind_source=kind_source,
                                          kind_confidence=kind_confidence,
                                          retry=retry)
        if match.status == "existing" and match.via == "fuzzy":
            if store.touch_entity(match.entity_id, ts=ts,
                                  alias=match.name) is not False:
                return int(match.entity_id)
            return self._rematch_unlocked(match, kind, ts=ts,
                                          kind_source=kind_source,
                                          kind_confidence=kind_confidence,
                                          retry=retry)
        if match.status == "new" and match.forgotten \
                and (kind_source or "").strip().lower() != "user":
            return 0
        # A new name (for a forgotten one, the user is adding it back: mint
        # under the name they gave, never as an alias of a fuzzy neighbour),
        # or a hidden row with exactly this name: the store's
        # exact-match-or-create mints the one and binds the other.
        return store.resolve_entity(match.name, kind, ts=ts,
                                    kind_source=kind_source,
                                    kind_confidence=kind_confidence)

    def _rematch_unlocked(self, match: EntityMatch, kind: str | None, *,
                          ts: float | None, kind_source: str | None,
                          kind_confidence: float | None,
                          retry: bool) -> int:
        """The row a match named was merged or deleted between the match and
        the mint step (its touch found no row: a request thread forgot or
        merged it while the extractor ran). Match the name again, once, and
        act on what answers now: a merge's survivor through its confirmed
        alias, a new row, or nothing for a forgotten name; never the dead
        id."""
        if not retry:
            return 0
        _pre, kind_n, shaped = self._entity_precheck(match.name, kind,
                                                     store=self._store)
        again = self._match_entity_unlocked(match.name, kind_n,
                                            person_shaped=shaped)
        if again.status == "rejected" or (
                again.status == "existing"
                and again.entity_id == match.entity_id):
            return 0
        return self._mint_entity_unlocked(
            again, kind, ts=ts, kind_source=kind_source,
            kind_confidence=kind_confidence, retry=False)

    def resolve_entity(self, name: str, kind: str | None = None, *,
                       ts: float | None = None,
                       kind_source: str | None = None,
                       kind_confidence: float | None = None) -> int:
        """Return a stable entity id for a named org/project/place, routing the
        raw name through the SAME correction layer as people so a mis-heard
        entity ("Dell Capitol" -> "Dell Capital") collapses instead of forking.
        Falls back to the store's exact-match-or-create for a genuinely new name.

        A person-shaped name (e.g. 'Bill Clinton' mislabeled as project)
        that names a person this seat knows goes to that person instead of
        minting a junk entity (see `match_entity`).

        `kind_source` / `kind_confidence` say who vouches for `kind` and are
        handed to the store, which owns the rank rule. Returns 0 — "no
        entity", never an id — for an implausible name, a person, or a name
        the user has forgotten: a forgotten name is neither minted nor folded
        into a similar-sounding neighbour (a forgotten "k9" must not come
        back as an alias of "k8"). Only a `user` source brings one back.

        This is `match_entity` then `mint_entity` under one lock: a name
        that matches nothing is minted on the spot. Callers that must not
        mint at first sight (the extractor) run the two steps apart.
        """
        pre, kind_n, shaped = self._entity_precheck(name, kind,
                                                    store=self._store)
        if pre is not None:
            return 0
        with self._lock:
            match = self._match_entity_unlocked((name or "").strip(), kind_n,
                                                person_shaped=shaped)
            if match.status != "rejected":
                return self._mint_entity_unlocked(
                    match, kind, ts=ts, kind_source=kind_source,
                    kind_confidence=kind_confidence)
        self._reroute_person_shaped(match, ts)
        return 0


resolver = Resolver()
