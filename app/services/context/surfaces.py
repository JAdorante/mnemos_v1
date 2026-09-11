"""CAL — surface resolution: window-title segments to nodes already in the graph.

Keys answer "what identifier does this event carry?". This module answers the
question that matters when there is no identifier at all, which on a
browser-centric surface is most of the time.

A measured example. On a real 494-event corpus, 44% of events carried a window
title and 0.2% yielded a binding key — the key grammar is repo/path/branch
shaped and fits a Cursor-and-terminal workflow, not someone living in Outlook,
Drive and search. But the titles were not empty of meaning:

    "Andrew Andy Karos Boostrun - Google Search - Chromium"
    "Mail - Justin Adorante - Outlook — Mozilla Firefox"

`Boostrun` was already an org in the graph. `Andy Karos` and `Justin Adorante`
were already people. Nothing resolved, for two fixable reasons: resolution
matched the WHOLE segment (so an org inside a search query missed), and it only
ever consulted entities (so the single most frequent segment in the corpus, a
person, matched nothing). Fixing both took segment coverage from 4.1% to 36.4%
with no new capture, no new producer and no model.

Everything here is BIND-ONLY, the same contract `entity_alias.resolve` keeps: a
surface string can never mint a node. An unrecognized title is an honest
unknown, and unknowns are what new-entity detection is built from later.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

# Window-title segments that are the application's own furniture. Every one of
# these appeared in the measured corpus; none of them names anything the user
# is working ON, and resolving them would attach whatever entity happens to be
# named "Home" to a third of the stream.
CHROME = frozenset({
    "mail", "inbox", "sent items", "drafts", "archive", "junk", "spam",
    "calendar", "contacts", "tasks", "notes", "home", "recent", "starred",
    "shared with me", "my drive", "settings", "preferences", "profile",
    "sign in", "sign out", "log in", "log out", "login page", "logout",
    "new tab", "untitled", "about:blank", "blank page", "loading",
    "problem loading page", "page not found", "error", "not found",
    "google search", "search", "search results", "results", "dashboard",
    "notifications", "help", "support", "open files", "save as", "print",
    "downloads", "history", "bookmarks", "extensions", "downloads manager",
    "account", "accounts", "google accounts", "my account", "security",
    "privacy", "billing", "subscription", "upgrade", "welcome", "overview",
})

# Names too generic to match INSIDE a longer string. An exact whole-segment
# match against them is still fine — if a segment is literally "MVP" then the
# entity called MVP is what it names. Containment is the dangerous direction:
# this graph holds entities called `unknown`, `company`, `CEO` and `Boston`,
# and `graph._STOP_NAMES` covers pronouns only.
_NO_CONTAINMENT = frozenset({
    "unknown", "company", "ceo", "cto", "cfo", "mvp", "stage", "the pen",
    "team", "project", "product", "design", "research", "meeting", "notes",
    "draft", "report", "review", "plan", "spec", "doc", "docs", "test",
    "demo", "app", "api", "data", "code", "work", "home", "main", "user",
    "admin", "client", "customer", "vendor", "partner", "board", "console",
})
_MIN_CONTAINMENT_LEN = 5      # a 4-char name inside prose is a coincidence

# How much a surface match is worth. These are NOT key strengths — a title
# segment is not an identifier — but the design's flat "medium, always" grade
# was reasoned from a Cursor example and is wrong for this surface. A segment
# that IS a known name is strong evidence; a name found inside a longer segment
# is weaker, because the rest of the segment is unexplained.
STRENGTH = {"exact": 0.80, "alias": 0.72, "contains": 0.60}

# Which entity kinds may NAME a stretch of work. A tool in a window title is
# app identity — real evidence, never sufficient evidence. "Claude" for ninety
# minutes is accurate and useless: it names what you were using, not what you
# were using it for. Unnameable anchors still compete in the evidence field, so
# they can lose a frame to a project, but they cannot take one.
_UNNAMEABLE_KINDS = frozenset({"tool", "place"})
_KIND_WEIGHT = {"tool": 0.45, "place": 0.6}


@dataclass(frozen=True)
class SurfaceHit:
    node_type: str        # entity | person
    node_id: int
    name: str
    surface: str          # the segment this came from
    method: str           # exact | alias | contains
    strength: float
    kind: str = ""        # entity kind, "" for people
    nameable: bool = True  # may this anchor NAME a frame? see _UNNAMEABLE_KINDS


def is_chrome(segment: str) -> bool:
    """True for application furniture — Mail, Sign in, Google Search."""
    s = re.sub(r"\s+", " ", (segment or "")).strip().lower()
    return not s or s in CHROME


class SurfaceIndex:
    """Compiled name patterns for the whole graph, built once and reused.

    `all_entities()` + `all_people()` + pattern compilation per event would
    blow the deterministic-path latency budget on its own, so callers hold one
    of these. `ttl_s` bounds how stale a hot loop's view of the graph can get.
    """

    def __init__(self, store, *, ttl_s: float = 120.0,
                 exclude_self: bool = True):
        self._store = store
        self._ttl = float(ttl_s)
        # The user's own name is in half their window titles — "Mail - Justin
        # Adorante - Outlook" — and it identifies nothing they are working ON.
        # Left in, it wins the evidence field by sheer frequency and names a
        # third of the timeline after the person reading it.
        self._exclude_self = bool(exclude_self)
        self._built_at = 0.0
        self._entities: list[tuple[dict, list]] = []
        self._people: list[tuple[dict, list]] = []

    def _identity_name(self) -> str:
        from app.services.identity import user_identity
        return (user_identity(self._store) or {}).get("name") or ""

    def _git_name(self) -> str:
        from app.services.onboarding_scan import git_identity
        return (git_identity() or {}).get("name") or ""

    def _build(self) -> None:
        from app.services.graph import _entity_patterns, _person_patterns
        try:
            ents = [e for e in self._store.all_entities() if not e.get("hidden")]
        except Exception:
            ents = []
        try:
            ppl = self._store.all_people()
        except Exception:
            ppl = []
        # Who is sitting here, by NAME and scoped to this store.
        # `self_profile.self_person_id` is deliberately not used: it memoizes a
        # person id in a process-global cache, so in any process that opens more
        # than one store — the test suite, a multi-seat host — the first store's
        # answer silently becomes every later store's answer, and this index
        # would then exclude an unrelated person from the graph.
        # Two witnesses, because one is routinely absent: a machine whose
        # onboarding profile was never filled in reports a placeholder name
        # ("User 2"), which leaves the REAL human sitting in the graph as an
        # ordinary contact, free to name a third of the timeline after the
        # person reading it.
        self_names = set()
        if self._exclude_self:
            for get in (self._identity_name, self._git_name):
                try:
                    nm = (get() or "").strip().lower()
                except Exception:
                    nm = ""
                if nm:
                    self_names.add(nm)

        def _is_self(p) -> bool:
            names = [p.get("name") or p.get("canonical_name") or ""]
            names += list(p.get("aliases") or [])
            return any((n or "").strip().lower() in self_names for n in names)
        self._entities = [(e, _entity_patterns(e)) for e in ents]
        # An absorbed person is not a node any more; its survivor is.
        self._people = [(p, _person_patterns(p)) for p in ppl
                        if not p.get("hide_from_people")
                        and not (self._exclude_self and _is_self(p))]
        self._built_at = time.time()

    def _fresh(self) -> None:
        if time.time() - self._built_at > self._ttl:
            self._build()

    @staticmethod
    def _name_of(node: dict) -> str:
        return str(node.get("name") or node.get("canonical_name") or "")

    @staticmethod
    def _may_contain(name: str) -> bool:
        n = (name or "").strip().lower()
        return len(n) >= _MIN_CONTAINMENT_LEN and n not in _NO_CONTAINMENT

    @staticmethod
    def _graded(node_type, nid, name, seg, method, node) -> SurfaceHit:
        kind = str((node or {}).get("kind") or "") if node_type == "entity" else ""
        return SurfaceHit(node_type, int(nid), name, seg, method,
                          STRENGTH[method] * _KIND_WEIGHT.get(kind, 1.0),
                          kind, kind not in _UNNAMEABLE_KINDS)

    def _contains(self, seg: str, rows, node_type: str) -> SurfaceHit | None:
        for node, pats in rows:
            name = self._name_of(node)
            if not self._may_contain(name) or not pats:
                continue
            if any(p.search(seg) for p in pats):
                nid = node.get("canonical_person_id") or node.get("id")
                return self._graded(node_type, nid, name, seg, "contains", node)
        return None

    def resolve(self, segment: str) -> list[SurfaceHit]:
        """Nodes named by one title segment. Never mints, never guesses.

        A segment may legitimately yield TWO hits — "Andrew Andy Karos
        Boostrun" names a person and an org — so this returns a list. That is
        multiplicity, not ambiguity: the evidence for each is independent.
        """
        seg = re.sub(r"\s+", " ", (segment or "")).strip()
        if not seg or is_chrome(seg):
            return []
        self._fresh()
        out: list[SurfaceHit] = []

        # 1) the whole segment IS a known entity name or alias.
        try:
            from app.services import entity_alias
            eid = entity_alias.resolve(seg, store=self._store, record=False)
        except Exception:
            eid = None
        if eid:
            node = next((e for e, _ in self._entities
                         if int(e.get("id") or 0) == int(eid)), None)
            name = self._name_of(node) if node else seg
            method = "exact" if name.lower() == seg.lower() else "alias"
            out.append(self._graded("entity", eid, name, seg, method, node))
        else:
            hit = self._contains(seg, self._entities, "entity")
            if hit is not None:
                out.append(hit)

        # 2) people, which entity resolution never consults. On the measured
        #    corpus this alone was 94 of the 134 resolving occurrences.
        pers = None
        for node, pats in self._people:
            name = self._name_of(node)
            if name and name.lower() == seg.lower():
                nid = node.get("canonical_person_id") or node.get("id")
                pers = self._graded("person", nid, name, seg, "exact", None)
                break
        if pers is None:
            pers = self._contains(seg, self._people, "person")
        if pers is not None:
            out.append(pers)
        return out

    def from_title(self, window_title: str) -> list[SurfaceHit]:
        """Every node named by a window title, via context_anchor's segmenter."""
        from app.services import context_anchor
        out: list[SurfaceHit] = []
        seen: set[tuple[str, int]] = set()
        for seg in context_anchor.title_candidates(window_title or ""):
            for hit in self.resolve(seg):
                key = (hit.node_type, hit.node_id)
                if key not in seen:
                    seen.add(key)
                    out.append(hit)
        return out


def from_title(window_title: str, *, store) -> list[SurfaceHit]:
    """One-shot convenience. Hot paths should hold a `SurfaceIndex` instead."""
    return SurfaceIndex(store).from_title(window_title)


__all__ = ["SurfaceHit", "SurfaceIndex", "from_title", "is_chrome",
           "CHROME", "STRENGTH"]
