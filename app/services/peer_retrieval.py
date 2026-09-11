"""Typed retrieval for peer egress (Phase 1.1).

Peer answers used to be composed from `grounding.compose()`'s rendered prose —
`{label, items: [str]}` — which had already thrown away every fact id, event
id, speaker and timestamp. Three consequences followed from that one fact:

  * nothing could cite a source, so answers crossed the wire undated and
    unattributable;
  * there was no type to whitelist on, so egress hygiene had to be a growing
    blocklist of substrings ("ABOUT YOU", "DRAFTING RULE", assistant hedges),
    which leaks by construction — it can only filter shapes it recognises;
  * the answerer's raw grounding blobs (identity blocks, profile instructions,
    prior chat hedges) were egress *candidates* at all.

`facts_for_topic` replaces that with rows carrying their own provenance. Only
typed facts are eligible, so raw grounding text can no longer reach the wire —
the whitelist is structural, not a list to maintain.

Retrieval quality is deliberately NOT this module's job yet: Phase 2 adds
alias/graph query expansion and recency-weighted ranking for status questions.
This is the shape change only.
"""
from __future__ import annotations

import re
import time

# Only claims cross. Tasks and commitments are the sender's own work state:
# "[commitment] Send Andy an update by Friday" is private, and a teammate
# asking a related question must not pull it across (peer golden
# `no-self-reminder-leak`).
EGRESS_KINDS = ("claim",)

# Sources whose text the user merely SAW rather than said or wrote. Weak
# attribution is tolerable for a local board that a human prunes; it is not
# tolerable for something asserted to another person as our knowledge.
_WEAK_SOURCES = ("desktop.screen",)

# Prefix length for the near-miss's looser matching. Four characters keeps
# "migration"/"migrations" and "compute"/"computing" together without
# collapsing genuinely different words.
_STEM = 4

_STOP = {"the", "and", "for", "about", "what", "know", "said", "tell", "from",
         "with", "your", "this", "our", "any", "update", "latest", "where",
         "did", "how", "who", "are", "was", "were", "have", "has", "you",
         "they", "their", "there", "that", "been", "being", "get", "got"}


def topic_tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]{3,}", (text or "").casefold())
            if t not in _STOP]


def _speaker_for(row: dict, store) -> str:
    """Who asserted this. Typed person joins first; the event's people list is
    the fallback. Never guessed from the text."""
    for key in ("from_person", "owner", "originator"):
        val = (row.get(key) or "").strip()
        if val and val != "?":
            return val
    eid = row.get("source_event_id")
    if not eid:
        return ""
    try:
        ev = store.get_event(int(eid))          # a dict row, not an Event
    except Exception:
        return ""
    people = ev.get("people") if isinstance(ev, dict) else None
    if isinstance(people, str):
        try:
            import json
            people = json.loads(people)
        except Exception:
            people = None
    if isinstance(people, list) and people:
        first = people[0]
        if isinstance(first, dict):
            return str(first.get("name") or "").strip()
        return str(first).strip()
    return ""


def _eligible(row: dict) -> bool:
    """Structural egress whitelist — the whole point of Phase 1.

    A row crosses only if it is a typed fact of an egress kind, currently
    active (not superseded, not escrowed, not dismissed), and carries the
    provenance an asker needs to judge it.
    """
    if row.get("kind") not in EGRESS_KINDS:
        return False
    if (row.get("state") or "active") != "active":
        return False
    if row.get("superseded_by"):
        return False
    if (row.get("review") or "") == "dismissed":
        return False
    if (row.get("event_source") or "") in _WEAK_SOURCES:
        return False
    if not (row.get("text") or "").strip():
        return False
    # No timestamp means no freshness stamp, and an undated claim is exactly
    # what this phase exists to stop shipping.
    return bool(row.get("source_time") or row.get("extracted_at"))


def _normalize(row: dict, store) -> dict:
    ts = row.get("source_time") or row.get("extracted_at") or 0.0
    return {
        "fact_id": int(row["fact_id"]),
        "text": (row.get("text") or "").strip(),
        "source_event_id": row.get("source_event_id"),
        "source_span": (row.get("source_span") or "").strip(),
        "speaker": _speaker_for(row, store),
        "ts": float(ts),
        "kind": row.get("kind"),
        "confidence": row.get("confidence"),
    }


def _squash(text: str) -> str:
    """Match key ignoring spaces and punctuation: 'Boost Run' == 'Boostrun'.

    Deliberately looser than `entity_alias.normalize`, which keeps the space.
    This key only decides WHAT WE SEARCH FOR — it never asserts that two names
    are the same entity, and every fact it surfaces still carries its own
    provenance for the asker to judge. A retrieval hint can afford to be
    generous where an identity claim cannot.
    """
    return re.sub(r"[^a-z0-9]+", "", (text or "").casefold())


def _phrases(topic: str, max_words: int = 3) -> list[str]:
    """Candidate name-shaped spans from the question: every 1..3 word window.

    The asker writes "any update from Boostrun?" — the name we need to match
    is a window inside their sentence, not the sentence.
    """
    words = re.findall(r"[A-Za-z0-9][\w'&.-]*", topic or "")
    out: list[str] = []
    for n in range(1, max_words + 1):
        for i in range(len(words) - n + 1):
            span = " ".join(words[i:i + n])
            if len(_squash(span)) >= 3:
                out.append(span)
    return out


def expand_topic(topic: str, store) -> dict:
    """Phase 2.1 — re-express the asker's question in the ANSWERER's vocabulary.

    The asker names the company; our memory names the founder. The asker types
    "Boostrun"; our entity is "Boost Run". Retrieving on their surface string
    alone is why differently-phrased memory returned nothing.

    Returns {"terms": [...], "entity_ids": [...], "person_ids": [...]} — extra
    search terms plus the graph anchors Phase 2.2 walks.
    """
    terms: list[str] = []
    entity_ids: list[int] = []
    person_ids: list[int] = []
    if not topic:
        return {"terms": [], "entity_ids": [], "person_ids": []}

    try:
        entities = store.all_entities()
        people = store.all_people()
    except Exception as exc:
        print(f"[peer_retrieval] expansion skipped ({exc}).")
        return {"terms": [], "entity_ids": [], "person_ids": []}

    # Squashed index over canonical names AND stored aliases.
    ent_index: dict[str, dict] = {}
    for e in entities:
        names = [e.get("name") or e.get("canonical_name") or ""]
        raw_aliases = e.get("aliases")
        if isinstance(raw_aliases, str):
            try:
                import json
                raw_aliases = json.loads(raw_aliases)
            except Exception:
                raw_aliases = None
        if isinstance(raw_aliases, list):
            names.extend(str(a) for a in raw_aliases)
        for nm in names:
            key = _squash(nm)
            if key:
                ent_index.setdefault(key, e)
    ppl_index = {_squash(p.get("name") or ""): p for p in people
                 if _squash(p.get("name") or "")}

    seen_terms: set[str] = set()
    for span in _phrases(topic):
        key = _squash(span)
        ent = ent_index.get(key)
        if ent is not None:
            eid = ent.get("id")
            if eid is not None and int(eid) not in entity_ids:
                entity_ids.append(int(eid))
            name = ent.get("name") or ent.get("canonical_name") or ""
            if name and name.casefold() not in seen_terms:
                seen_terms.add(name.casefold())
                terms.append(name)
        person = ppl_index.get(key)
        if person is not None:
            pid = person.get("id")
            if pid is not None and int(pid) not in person_ids:
                person_ids.append(int(pid))
            name = person.get("name") or ""
            if name and name.casefold() not in seen_terms:
                seen_terms.add(name.casefold())
                terms.append(name)

    # An entity the question names pulls in the people affiliated with it, so
    # "where did we land with Boost Run?" can reach a memory that only ever
    # says "Andy agreed to the terms".
    for eid in list(entity_ids):
        for pid, pname in _people_for_entity(eid, store):
            if pid not in person_ids:
                person_ids.append(pid)
            if pname and pname.casefold() not in seen_terms:
                seen_terms.add(pname.casefold())
                terms.append(pname)

    return {"terms": terms, "entity_ids": entity_ids, "person_ids": person_ids}


def _people_for_entity(entity_id: int, store) -> list[tuple[int, str]]:
    """People linked to an entity (works_at / part_of / associated_with)."""
    try:
        edges = store.relations_of("entity", int(entity_id))
    except Exception:
        return []
    ids: list[int] = []
    for e in edges.get("in", []) + edges.get("out", []):
        if e.get("subj_type") == "person":
            ids.append(int(e["subj_id"]))
        elif e.get("obj_type") == "person":
            ids.append(int(e["obj_id"]))
    if not ids:
        return []
    try:
        names = {int(p["id"]): p.get("name") or "" for p in store.all_people()}
    except Exception:
        return []
    out: list[tuple[int, str]] = []
    for pid in dict.fromkeys(ids):
        if pid in names:
            out.append((pid, names[pid]))
    return out


def _graph_fact_rows(expansion: dict, store) -> list[dict]:
    """Phase 2.2 — candidates from graph EDGES, not embeddings.

    A fact linked to a person or entity the question names is relevant even
    when it shares no vocabulary with the question at all. This is the tier
    that finds "Andy agreed to the terms on Tuesday" for a question about
    Boost Run, which no amount of semantic similarity would reach.
    """
    fact_ids: list[int] = []
    for pid in expansion.get("person_ids") or []:
        fact_ids.extend(_linked_fact_ids("person", pid, store))
    for eid in expansion.get("entity_ids") or []:
        fact_ids.extend(_linked_fact_ids("entity", eid, store))
    if not fact_ids:
        return []
    try:
        fmap = store.facts_by_ids(list(dict.fromkeys(fact_ids)))
    except Exception:
        return []
    rows = []
    for fid in dict.fromkeys(fact_ids):
        row = fmap.get(fid)
        if row:
            rows.append(dict(row))
    return rows


def _linked_fact_ids(node_type: str, node_id: int, store) -> list[int]:
    try:
        edges = store.relations_of(node_type, int(node_id))
    except Exception:
        return []
    out: list[int] = []
    for e in edges.get("out", []) + edges.get("in", []):
        if e.get("obj_type") == "fact":
            out.append(int(e["obj_id"]))
        elif e.get("subj_type") == "fact":
            out.append(int(e["subj_id"]))
    return out


def _semantic_fact_rows(topic: str, limit: int, store) -> list[dict]:
    """Semantic hits, resolved to typed facts.

    `memory.search` returns two payload shapes. Indexed FACTS come back as
    fact views carrying `fact_id` — those resolve directly. Episode hits come
    back as `Event.to_dict()`, which carries no row id at all, so they are
    matched back by (time, text); a hit that never became a fact resolves to
    nothing, which is exactly the raw-blob class we do not want as an egress
    candidate.

    The id-less episode payload is why an earlier version of this function
    silently returned nothing: it looked for `h["id"]`, which no payload has.
    """
    try:
        from app.services.memory import memory
        hits = memory.search(topic, limit=limit)
    except Exception as exc:
        print(f"[peer_retrieval] semantic tier skipped ({exc}).")
        return []
    hits = _above_floor(hits)
    rows: list[dict] = []
    for h in hits or []:
        if not isinstance(h, dict):
            continue
        fid = h.get("fact_id")
        if fid is not None:
            try:
                row = store.get_fact(int(fid))
            except Exception:
                row = None
            if row:
                rows.append(row)
            continue
        rows.extend(_facts_for_episode(h, store))
    return rows


# Semantic relevance floors for EGRESS. Two of them, because one is not enough.
#
# `_SEM_FLOOR` matches grounding.compose's own 0.15: below it, a hit is not
# about the question at all (the embedder still returns a nearest neighbour
# when nothing is close, so top-k with no floor answers "what did legal say
# about the Trillium contract?" with "the office coffee machine is broken").
#
# `_SEM_RELATIVE` is the one local grounding does not need. A hit that scores a
# quarter of the best hit is an ADJACENT memory, not an answer — and adjacent
# memories are where private material lives. On the peer goldens, a question
# about open engineering roles scored the roles at 0.67 and "Dave's salary is
# 220k and he asked us to keep it quiet" at 0.17: above the absolute floor,
# and a colleague's salary sent to another colleague.
#
# The asymmetry justifies being stricter here than in local grounding: a false
# negative costs one round trip, a false positive discloses something to
# another person and cannot be taken back.
_SEM_FLOOR = 0.15
_SEM_RELATIVE = 0.35


def _above_floor(hits: list | None) -> list:
    """Drop semantic hits too weak, in absolute or relative terms, to assert."""
    scored = [h for h in (hits or [])
              if isinstance(h, dict) and h.get("score") is not None]
    if not scored:
        # No scores (a stubbed or substring-only tier) — nothing to filter on.
        return list(hits or [])
    best = max(float(h["score"]) for h in scored)
    cut = max(_SEM_FLOOR, best * _SEM_RELATIVE)
    return [h for h in (hits or [])
            if not isinstance(h, dict) or h.get("score") is None
            or float(h["score"]) >= cut]


def _facts_for_episode(hit: dict, store) -> list[dict]:
    """Map an episode payload back to the facts extracted from it.

    `Event.to_dict()` drops the row id, so the join is on the event's exact
    timestamp — unique in practice, and cheap to look up by index.
    """
    ts = hit.get("time")
    if ts is None:
        return []
    try:
        for eid in store.event_ids_at(float(ts)):
            found = store.fact_spans_for_event(int(eid))
            if found:
                return found
    except Exception:
        return []
    return []


def _literal_fact_rows(topic: str, limit: int, store) -> list[dict]:
    """Exact-substring tier. Keeps identifiers (codenames, surnames) that the
    embedder loses, and covers facts whose event is not in the index."""
    rows: list[dict] = []
    seen_q: set[str] = set()
    for tok in topic_tokens(topic)[:6]:
        if tok in seen_q:
            continue
        seen_q.add(tok)
        try:
            rows.extend(store.search_facts_like(tok, limit=limit))
        except Exception:
            continue
    return rows


def facts_for_topic(topic: str, *, limit: int = 8, store=None,
                    now: float | None = None) -> list[dict]:
    """Typed, provenance-carrying candidates for one topic, best first.

    Returns rows of {fact_id, text, source_event_id, source_span, speaker, ts,
    kind, confidence} — never rendered strings. Empty when nothing typed
    matches, which the composer reports as a dated near-miss rather than a
    flat refusal.

    Three retrieval tiers, because no one of them is sufficient: semantic hits
    (walked back to facts), literal substring (keeps identifiers the embedder
    loses), and graph edges (finds facts that share no vocabulary with the
    question at all). The question is first re-expressed in our own vocabulary
    (2.1) so all three search for what WE call the thing.
    """
    topic = (topic or "").strip()
    if not topic:
        return []
    if store is None:
        from app.storage import get_store
        store = get_store()

    expansion = expand_topic(topic, store)
    # Search our names alongside theirs, never instead of theirs.
    expanded = " ".join([topic, *expansion["terms"]])

    raw = (_semantic_fact_rows(expanded, limit * 2, store)
           + _literal_fact_rows(expanded, limit, store)
           + _graph_fact_rows(expansion, store))

    # Overlap is scored against the expanded topic, so a fact phrased in our
    # vocabulary is not penalised for missing the asker's words.
    toks = set(topic_tokens(expanded))
    by_id: dict[int, dict] = {}
    for row in raw:
        if not _eligible(row):
            continue
        fid = int(row["fact_id"])
        if fid in by_id:
            continue
        by_id[fid] = _normalize(row, store)

    out = list(by_id.values())
    now = time.time() if now is None else now

    if is_status_question(topic):
        return _rank_by_recency(out, limit)

    def _score(c: dict) -> tuple:
        overlap = len(toks & set(topic_tokens(c["text"])))
        return (-overlap, -(c["ts"] or 0.0))

    out.sort(key=_score)
    return out[:limit]


# "What's the latest on X" is a different question from "what is X". The first
# is answered by the newest thing we know; the second by the most relevant.
_STATUS_RE = re.compile(
    r"\b(?:what'?s?\s+(?:the\s+)?(?:latest|status|new)\b"
    r"|any\s+(?:update|news|progress)\b"
    r"|where\s+(?:are\s+we|do\s+we\s+stand|did\s+we\s+land)\b"
    r"|how\s+(?:is|are|'s)\s+.{0,40}\b(?:going|coming|progressing|tracking)\b"
    r"|latest\s+on\b|update\s+on\b|current\s+(?:status|state)\b"
    r"|still\s+on\s+track\b)",
    re.I)


def is_status_question(topic: str) -> bool:
    """Phase 2.3 — does this ask for the CURRENT state of something?"""
    return bool(_STATUS_RE.search(topic or ""))


def _rank_by_recency(claims: list[dict], limit: int) -> list[dict]:
    """Newest first, and an older claim about the same thing is dropped.

    Ranking by similarity answers "what's the latest on the launch?" with the
    September plan and the October slip side by side, and the asker cannot
    tell which holds — worse than vague prose, because citations make it look
    authoritative. Where the store recorded a supersession `_eligible` already
    dropped the old row; this is the read-time equivalent for the (common)
    case where it did not.
    """
    ordered = sorted(claims, key=lambda c: -(c["ts"] or 0.0))
    kept: list[dict] = []
    for c in ordered:
        toks = set(topic_tokens(c["text"]))
        if not toks:
            continue
        superseded = False
        for k in kept:                       # only newer claims are in `kept`
            ktoks = set(topic_tokens(k["text"]))
            if not ktoks:
                continue
            shared = toks & ktoks
            # Same subject, said later: the newer line carries the truth.
            # Two shared words, not one — "the launch slipped to October" and
            # "the launch is planned for the last week of September" share
            # {launch, week} and are two versions of one fact, while "launch
            # date is Oct 12" and "launch is blocked on legal" share only
            # {launch} and are two different facts that must both survive.
            if len(shared) >= 2 or len(shared) / len(toks) >= _SAME_SUBJECT:
                superseded = True
                break
        if not superseded:
            kept.append(c)
        if len(kept) >= limit:
            break
    return kept


# Share this fraction of your distinctive words with something newer and you
# are treated as the older version of it. Tuned on the peer goldens: high
# enough that two genuinely different facts about one project both survive.
_SAME_SUBJECT = 0.5


def as_of(claims: list[dict]) -> float | None:
    """Freshness stamp: the newest supporting claim's timestamp."""
    stamps = [float(c.get("ts") or 0) for c in claims or [] if c.get("ts")]
    return max(stamps) if stamps else None


def near_miss(topic: str, *, store=None, limit: int = 1) -> list[dict]:
    """The most recent RELATED thing we hold, when nothing matched the topic.

    A flat "I don't have enough in my memory" is a wasted round trip: it does
    not tell the asker whether to go find the human. "Nothing since June 3;
    the last thing I have is X" does.

    The near-miss must still be about roughly the right thing. Returning the
    globally newest claim would answer "what did legal say about the Trillium
    contract?" with "the office coffee machine is broken" — noise dressed as
    context, which is worse than admitting we have nothing. So a candidate
    needs to share a subject with the topic; sharing nothing means we say
    nothing.

    The match is deliberately LOOSER than `facts_for_topic`'s: prefixes rather
    than whole tokens, so "how are the migrations going?" can still surface a
    memory about "the Helio migration". An exact-match fallback behind an
    exact-match search would never fire.
    """
    stems = {t[:_STEM] for t in topic_tokens(topic)}
    if not stems:
        return []
    if store is None:
        from app.storage import get_store
        store = get_store()
    try:
        rows = store.list_facts(kind="claim", limit=200)
    except Exception:
        return []
    out = []
    for r in rows:
        if not _eligible(r):
            continue
        c = _normalize(r, store)
        if stems & {t[:_STEM] for t in topic_tokens(c["text"])}:
            out.append(c)
    out.sort(key=lambda c: -(c["ts"] or 0.0))
    return out[:limit]


def describe_age(ts: float | None, now: float | None = None) -> str:
    """'Aug 14' / 'today' — what the asker needs to judge staleness."""
    if not ts:
        return ""
    now = time.time() if now is None else now
    days = (now - float(ts)) / 86400.0
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    return time.strftime("%b %-d", time.localtime(float(ts)))
