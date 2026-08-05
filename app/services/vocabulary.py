"""VocabularyProvider (#11) + SessionContext for session-aware ASR biasing (#3).

Whisper transcribes each utterance in isolation, so it can't know your contact is
"Abby Nengel" (not "Abby Nagle"), or that "Venture Pulse" is a project. This turns
the personal knowledge graph into an ASR bias: a compact `initial_prompt` of known
names / projects plus the last few things said, nudging Whisper's spelling and
conversational continuity — fixing the "Abby Nagle" class of error at the SOURCE,
not just later in phone-goal correction.

The same provider is meant to bias every downstream stage that benefits from the
user's vocabulary (#11): the extractor prompt, task parser, and approval display —
`get_bias_terms()` returns the structured set for them; `whisper_prompt()` renders
the compact string for the ASR front-end.

CAUTION: an `initial_prompt` can INDUCE hallucination of the biased terms (worst on
silence). Mitigations: keep it short + relevant, the ingest filter drops silence,
and #8's eval harness gates changes (entity-recall gains must not cost WER/drops).
"""
from __future__ import annotations

import collections
import os
import re
import threading
import time

from app.config import settings

# Terms that are not useful vocabulary bias (self-reference, pronouns, fillers).
_STOP = {"me", "i", "you", "we", "us", "he", "she", "they", "it", "them", "my",
         "mine", "myself", "someone", "somebody", "everyone", "people", "guy",
         "guys", "thing", "stuff", "today", "tomorrow", "yesterday"}
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def _ok_term(t: str) -> bool:
    t = (t or "").strip()
    if len(t) < 2:
        return False
    low = t.lower()
    if low in _STOP:
        return False
    # must contain at least one real word (drop pure numbers / punctuation)
    return bool(_WORD.search(t))


def _norm_name(t: str) -> str:
    """Lowercased, punctuation-stripped name for matching ('Abby!' -> 'abby')."""
    return " ".join(_WORD.findall((t or "").lower()))


def _name_match(a: str, b: str) -> float:
    """Cheap 0..1 name similarity: exact=1.0, token-subset (first-name of a full
    name) = 0.8, shared-token = 0.65, else 0.0. Deliberately conservative so
    `recognize` only claims a match it can defend to a human reviewer."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    if ta and (ta <= tb or tb <= ta):     # "abby" ⊆ "abby nengel"
        return 0.8
    if ta & tb:                            # share at least one name token
        return 0.65
    return 0.0


def _dedup(items) -> list[str]:
    """Case-insensitive de-dup, order preserved."""
    seen, out = set(), []
    for x in items:
        x = (x or "").strip()
        k = x.lower()
        if x and k not in seen:
            seen.add(k)
            out.append(x)
    return out


class VocabularyProvider:
    """Serves the user's vocabulary (people, projects, orgs, aliases) from the KG,
    cached with a short TTL so it isn't rebuilt from SQLite on every utterance."""

    def __init__(self, ttl: float = 60.0) -> None:
        self._ttl = ttl
        self._cache: dict | None = None
        self._cache_ts = 0.0
        self._lock = threading.Lock()

    def get_bias_terms(self, force: bool = False) -> dict:
        """{people, projects, orgs, aliases} — deduped, filtered, recency-ranked."""
        with self._lock:
            now = time.time()
            if not force and self._cache and now - self._cache_ts < self._ttl:
                return self._cache
            terms = self._build()
            self._cache, self._cache_ts = terms, now
            return terms

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def _build(self) -> dict:
        cfg = settings.asr_bias
        people, aliases, projects, orgs = [], [], [], []
        try:
            from app.storage import get_store

            store = get_store()
            for p in store.recent_people(cfg.max_names * 2):
                people.append(p["name"])
                aliases += p.get("aliases", []) or []
            for e in store.recent_entities(cfg.max_projects * 2):
                kind = (e.get("kind") or "").lower()
                if kind == "org":
                    orgs.append(e["name"])
                else:                         # project | product | thing | unknown
                    projects.append(e["name"])
        except Exception as exc:
            print(f"[vocab] graph read failed: {exc}")
        try:
            from app.services.speakers import speakers as spk

            people += spk.enrolled_names()
        except Exception:
            pass
        return {
            "people": _dedup(p for p in people if _ok_term(p)),
            "projects": _dedup(p for p in projects if _ok_term(p)),
            "orgs": _dedup(o for o in orgs if _ok_term(o)),
            "aliases": _dedup(a for a in aliases if _ok_term(a)),
        }

    def all_terms(self) -> list[str]:
        """Flat, deduped term list — for post-ASR correction / extractor prompts."""
        t = self.get_bias_terms()
        return _dedup(t["people"] + t["aliases"] + t["projects"] + t["orgs"])

    # -- #11: downstream consumers (task parser / approval / extractor) -------
    def known_recipients(self, limit: int = 40) -> list[str]:
        """Known PEOPLE names (KG contacts + enrolled speakers), recency-ranked.

        Supplements recipient grounding: when the Phone Link contact scrape is
        thin or returns the notifications feed instead of real contacts, the
        people Mnemos has *heard* the user talk about are a second candidate
        source — so "text Abby" can snap to "Abby Nengel" even if the phone
        scrape missed her. Names only (not projects/orgs) — a recipient is a
        person."""
        t = self.get_bias_terms()
        return _dedup(list(t["people"]) + list(t["aliases"]))[: max(1, limit)]

    def recognize(self, name: str) -> dict:
        """Is `name` a known person/entity in the user's vocabulary?

        Returns {known, canonical, kind, score}. Used by the approval-packet
        display so a human reviewer sees "✓ known contact" vs "⚠ not recognized"
        before a text is sent — a cheap mis-address guard. Case-insensitive,
        with a light contains/startswith match so "Abby" recognizes "Abby
        Nengel". Never raises."""
        out = {"known": False, "canonical": (name or "").strip(),
               "kind": "", "score": 0.0}
        q = _norm_name(name)
        if not q:
            return out
        try:
            t = self.get_bias_terms()
        except Exception:
            return out
        best_score, best_name, best_kind = 0.0, "", ""
        for kind, key in (("person", "people"), ("person", "aliases"),
                          ("project", "projects"), ("org", "orgs")):
            for cand in t.get(key, []):
                s = _name_match(q, _norm_name(cand))
                if s > best_score:
                    best_score, best_name, best_kind = s, cand, kind
        if best_score >= 0.6:
            out.update(known=True, canonical=best_name, kind=best_kind,
                       score=round(best_score, 3))
        return out

    def spelling_hint(self, *, kinds=("people", "projects", "orgs"),
                      max_terms: int = 24) -> str:
        """A compact, SAFE prompt block naming the user's known people/projects so
        a downstream model spells them right — framed spelling-only so it does NOT
        invite inventing facts about them.

        The bias guardrail from this module's header applies with FULL force here:
        injecting real names into an *extractor* prompt (unlike the ASR front-end)
        can induce the model to hallucinate a task/claim about a named person. So
        the block is worded defensively and its use in the extractor is opt-in +
        eval-gated (QUILL_EXTRACT_VOCAB — see extractor.py). Returns "" when there
        is nothing to hint."""
        t = self.get_bias_terms()
        terms: list[str] = []
        for k in kinds:
            terms += list(t.get(k, []))
        terms = _dedup(terms)[: max(1, max_terms)]
        if not terms:
            return ""
        return ("KNOWN NAMES the speaker uses (for correct SPELLING only — do NOT "
                "invent, infer, or add any task, claim, or fact about these names "
                "that is not explicitly stated in the passage): "
                + ", ".join(terms) + ".")

    def whisper_prompt(self, recent_texts=None, extra_terms=None) -> str:
        """Render the compact Whisper `initial_prompt`. Returns "" when disabled or
        empty. `extra_terms` lets a caller (or the eval harness) inject known names."""
        cfg = settings.asr_bias
        if not cfg.enabled:
            return ""
        t = self.get_bias_terms()
        names = _dedup(list(t["people"]) + list(t["aliases"])
                       + list(extra_terms or []))[: cfg.max_names]
        proj = _dedup(list(t["projects"]) + list(t["orgs"]))[: cfg.max_projects]
        parts = []
        if names:
            parts.append("Names: " + ", ".join(names) + ".")
        if proj:
            parts.append("Projects: " + ", ".join(proj) + ".")
        if cfg.include_recent and recent_texts:
            ctx = " ".join(x for x in recent_texts if x).strip()
            if ctx:
                parts.append("Context: " + ctx[-cfg.recent_chars:])
        return " ".join(parts).strip()[: cfg.max_chars]


class SessionContext:
    """Rolling buffer of the last few accepted transcripts (per capture session) —
    the conversational continuity half of session-aware ASR."""

    def __init__(self, maxlen: int = 5, label: str = "") -> None:
        self._recent: collections.deque = collections.deque(maxlen=max(1, maxlen))
        self.label = label
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        t = (text or "").strip()
        if t:
            with self._lock:
                self._recent.append(t)

    def recent(self, n: int | None = None) -> list[str]:
        with self._lock:
            items = list(self._recent)
        return items[-n:] if n else items

    def clear(self) -> None:
        with self._lock:
            self._recent.clear()


vocabulary = VocabularyProvider()


# ---------------------------------------------------------------------------
# Few-shot prompt EXAMPLES (A2) — general code, no hardcoded personal names.
# ---------------------------------------------------------------------------
# Downstream prompts (the router, the phone-goal parser, the approval-packet
# schema, the extractor schema) used to bake in this developer's own contacts as
# illustrative examples ("text Justin", "Send email to Marc", "Dell Capital").
# That is user-specificity living in .py logic — exactly what the invariant bans.
# These placeholders replace them so the *code* is neutral; the *values* can come
# from the user's own vocabulary at runtime (opt-in, see below).
#
# Neutral, eval-safe defaults. `<name>`/`<org>`/`<project>` read as slots so no
# model treats them as real entities to emit; `Acme` is a conventional stand-in
# company. Rendering these preserves each prompt's instructive verb+role->label
# shape without naming anyone real.
_NEUTRAL_EXAMPLES = {
    "person": "<name>",
    "teammate": "<name>",
    "company": "Acme",
    "org": "<org>",
    "project": "<project>",
}


def _examples_from_data_enabled() -> bool:
    """Data-driven prompt examples are OPT-IN (default OFF).

    ⚠ Bias guardrail (the repo documents this exact failure mode in AsrBiasConfig
    and this module's own header): injecting the user's REAL names into a
    router/extractor/drafting prompt can INDUCE the model to hallucinate those
    names (invented tasks, mis-attribution). So real names are used as examples
    ONLY after an eval run (eval_intent / eval_extraction / eval_planner_routing)
    shows no routing/extraction precision regression vs. the neutral placeholders.
    Flip on with QUILL_PROMPT_EXAMPLES_FROM_DATA=1 once that gate passes for your
    data; if it regresses, leave it off (still fully honors the invariant — the
    code holds only neutral placeholders, never a real name)."""
    return os.environ.get("QUILL_PROMPT_EXAMPLES_FROM_DATA", "0") not in (
        "0", "false", "False")


_example_cache: dict | None = None


def example_terms() -> dict:
    """{person, teammate, company, org, project} for few-shot prompt EXAMPLES.

    Neutral placeholders by default (no user data enters the prompt). When
    QUILL_PROMPT_EXAMPLES_FROM_DATA=1, each slot is drawn from the user's OWN
    knowledge graph (capped at 1-2, deduped, recency-ranked via VocabularyProvider),
    with the neutral placeholder kept per-slot when the graph lacks that kind — so
    a fresh data dir still renders sensible, name-free prompts.

    Memoized: computed once per process so prompts built at import time stay cheap
    and stable (NOT rebuilt per call). Call reset_example_terms() after enrolling
    new vocabulary if you want the change to take effect without a restart."""
    global _example_cache
    if _example_cache is not None:
        return _example_cache
    out = dict(_NEUTRAL_EXAMPLES)
    if _examples_from_data_enabled():
        try:
            t = vocabulary.get_bias_terms()
            people = list(t.get("people") or [])
            orgs = list(t.get("orgs") or [])
            projects = list(t.get("projects") or [])
            if people:
                out["person"] = people[0]
                out["teammate"] = people[1] if len(people) > 1 else people[0]
            if orgs:
                out["company"] = orgs[0]
                out["org"] = orgs[0]
            if projects:
                out["project"] = projects[0]
        except Exception as exc:
            print(f"[vocab] example_terms fell back to placeholders ({exc}).")
    _example_cache = out
    return out


def reset_example_terms() -> None:
    """Drop the memoized examples (tests / after enrolling new vocabulary)."""
    global _example_cache
    _example_cache = None
