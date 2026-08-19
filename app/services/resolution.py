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

import numpy as np

from app.storage import Store, get_store

# High by default: a false merge is worse than a missed one (you can merge later,
# un-merging is painful). Tunable for experiments.
PERSON_SIM_THRESHOLD = float(os.environ.get("QUILL_PERSON_SIM_THRESHOLD", "0.82"))
_MIN_PREFIX = 3


def _cos(a, b) -> float:
    # embedder returns L2-normalized vectors, so cosine == dot; guard anyway.
    if a is None or b is None:
        return -1.0
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / denom)


def _is_known_person_name(store: Store, name: str) -> bool:
    """True when `name` is already a person's canonical name or alias —
    hidden/merged rows count, since a merged-away alias ("justin") is still a
    person, not a mintable project."""
    key = (name or "").strip().lower()
    if not key:
        return False
    for p in store.all_people():
        if (p.get("name") or "").strip().lower() == key:
            return True
        if any((a or "").strip().lower() == key for a in p.get("aliases") or []):
            return True
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
        if not is_plausible_person(key):
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

    def resolve_entity(self, name: str, kind: str | None = None, *,
                       ts: float | None = None) -> int:
        """Return a stable entity id for a named org/project/place, routing the
        raw name through the SAME correction layer as people so a mis-heard
        entity ("Dell Capitol" -> "Dell Capital") collapses instead of forking.
        Falls back to the store's exact-match-or-create for a genuinely new name.

        Person-shaped names (e.g. 'Bill Clinton' mislabeled as project) are
        routed to people instead of minting a junk entity.
        """
        key = (name or "").strip()
        if not key:
            return 0
        from app.services.name_quality import (
            is_plausible_entity,
            is_person_shaped_entity_name,
            normalize_entity_kind,
            should_mint_as_entity,
        )
        if not is_plausible_entity(key):
            return 0
        kind_n = normalize_entity_kind(kind)
        if not should_mint_as_entity(key, kind_n):
            # Person-shaped project/idea — land in people, skip entity mint.
            if is_person_shaped_entity_name(key):
                try:
                    self.resolve_person(key, ts=ts)
                except Exception:
                    pass
            return 0
        with self._lock:
            store = self._s()
            # Exact/create is cheap and handles the common case + brand-new names.
            existing = store.all_entities()
            exact = next((e for e in existing
                          if (e["name"] or "").lower() == key.lower()), None)
            if exact is not None:
                store.touch_entity(exact["id"], ts=ts)
                return int(exact["id"])
            from app.services.entity_correction import corrector
            m = corrector.match(key, existing)
            if m is not None:
                store.touch_entity(int(m.ref), ts=ts, alias=key)
                return int(m.ref)
            # A name already living in the people table (canonical OR alias,
            # hidden/merged rows included) must not fork into a project/idea
            # node — that's how "Justin"[project] and "Marc"[project] happened.
            # Orgs/tools/places stay allowed: a company can share a founder's
            # name; a second project named "Marc" cannot.
            if kind_n in ("project", "idea") and _is_known_person_name(store, key):
                return 0
            return store.resolve_entity(key, kind_n, ts=ts)


resolver = Resolver()
