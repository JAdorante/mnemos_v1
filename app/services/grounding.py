"""Structured grounding for chat — look in the right drawer before fuzzy search.

Chat previously grounded every question the same way: flat semantic search over
the RAW event timeline (noisy ASR fragments, screen captions) — while the
curated layers the pipeline builds (approved facts, the knowledge graph,
activity rollups) went unused. Result: "what do you know about <person>?" got
garbled transcript shards instead of the person's graph neighborhood, and
"what tasks are open?" matched a screenshot caption instead of the facts table.

`compose(question)` builds the context block from the RIGHT stores, strongest
first:

  1. PEOPLE  — names detected in the question -> knowledge-graph traversal
     (context_for_person): their linked facts (open first, typed roles first),
     who they co-occur with, affiliations. Graph beats flat search.
  2. TASKS   — task/commitment-flavored questions -> the facts table directly
     (kind + status query, human-reviewed rows), not embedding roulette.
  3. MEMORIES — semantic timeline search as the FALLBACK layer it should be,
     preferring extracted summaries over raw ASR.
  4. ACTIVITY — recent desktop rollups ("what was I doing?"), as before.

Every layer is best-effort: any store/graph failure just drops that section,
so grounding can never break an answer. Used by BOTH chat paths (the agent's
memory provider and llm.answer), so local, Claude, and self-quiz answers all
see the same upgraded context. Generic code: every name, task, and memory
comes from this install's own store at call time.
"""
from __future__ import annotations

import re
from typing import Any

# Words that signal a task/commitment question — cheap gate for the facts query.
_TASKY = re.compile(
    r"\b(task|tasks|to-?dos?|open items?|follow[- ]?ups?|commitments?|"
    r"remind(er)?s?|priorit(y|ies)|due|deadline|owe[sd]?|promised?)\b", re.I)

# "Who do I know / list my contacts" — contacts roster, not ambient WM names.
_PEOPLE_LISTY = re.compile(
    r"\b((who|what|which)\s+(people|persons|contacts|names)\s+(do\s+i|i)\s+know|"
    r"people\s+i\s+know|who\s+do\s+i\s+know|list\s+(of\s+)?(my\s+)?"
    r"(people|contacts|names)|my\s+(people|contacts)|everyone\s+i\s+know|"
    r"all\s+the\s+people|contacts?\s+list|address\s+book)\b",
    re.I)

# Words that signal a "what was on my screen / what was I watching" question —
# gate for a vision-modality search (camera + desktop.screen captions), which
# generic semantic search loses under audio fragments ("thanks for watching").
_SCREENY = re.compile(
    r"\b(watch(ing|ed)?|youtube|video|screen|browser|tab|website|"
    r"looking at|reading|open(ed)? app|on my (computer|laptop|monitor))\b", re.I)

# Pronouns/fillers that land in the people table but aren't real entities.
_STOP_NAMES = {"she", "he", "me", "i", "we", "they", "you", "it", "them"}

# Keep the block small enough for a 3B context window to actually use.
_MAX_BLOCK_CHARS = 2600

# Loose-promise contamination guard for email-ish goals (drafting must not
# paste unrelated commitments) — moved verbatim from the agent provider,
# including its exception: a promise that OVERLAPS the goal's topic stays.
_LOOSE_PROMISE = re.compile(
    r"\b(i'?ll|i will|call you|after my call|remind me|don'?t forget|"
    r"open commitment|put some|shoot you|text you later)\b",
    re.I,
)


def _overlaps_goal(text: str, goal: str) -> bool:
    """True when a memory shares meaningful content words with the goal."""
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "my",
        "me", "i", "you", "we", "is", "are", "be", "with", "at", "this",
        "that", "it", "as", "from", "about", "summary", "email", "send",
    }
    g = {w for w in re.findall(r"[a-z0-9]{3,}", (goal or "").lower()) if w not in stop}
    t = {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in stop}
    if not g or not t:
        return False
    return len(g & t) >= 1


def _people_in(question: str, store) -> list[str]:
    """Known people the question mentions (cap 2). Matches the full stored
    name OR its first-name token ("Justin" finds "Justin Adorante") — the
    matched STRING is returned so graph._resolve_person does the final
    exact/prefix resolution with its own tie-breaking."""
    hits: list[str] = []
    q = question or ""
    for p in store.all_people():
        name = (p.get("name") or "").strip()
        if len(name) < 3 or name.lower() in _STOP_NAMES:
            continue
        if re.search(r"\b" + re.escape(name) + r"\b", q, re.I):
            hits.append(name)
        else:
            first = name.split()[0]
            if (len(first) >= 3 and first.lower() not in _STOP_NAMES
                    and re.search(r"\b" + re.escape(first) + r"\b", q, re.I)):
                hits.append(name)
        if len(hits) >= 2:
            break
    return list(dict.fromkeys(hits))


def _contacts_section(store) -> tuple[list[str], list[int]]:
    """Roster of real contacts for people-list questions."""
    from app.services.people_pipeline import contacts_roster
    rows = contacts_roster(store, limit=40)
    if not rows:
        return (
            ["PEOPLE YOU KNOW (contacts — empty; none promoted or evidenced yet):",
             "- (none yet)"],
            [],
        )
    lines = [
        "PEOPLE YOU KNOW (contacts — answer the people-list question from THIS "
        "list only; names that appear later in working set / screen / timeline "
        "are ambient media or activity, NOT contacts unless also listed here):",
    ]
    ids: list[int] = []
    for r in rows:
        lines.append(f"- {r['name']}")
        ids.append(int(r["id"]))
    return lines, ids


def _person_section(name: str, store) -> tuple[list[str], int | None, list[int]]:
    """Render one person's graph neighborhood (facts, people, affiliations).
    Also returns the resolved person id AND the fact ids actually included,
    so the attention ledger logs what grounding pulled in and the Now-Context
    seeds the whole neighborhood in play — not just the person."""
    from app.services import graph
    ctx = graph.context_for_person(name, store)
    if not ctx.get("found"):
        return [], None, []
    person = ctx.get("person") or {}
    lines = [f"KNOWN PERSON: {person.get('name', name)}"]
    used_fact_ids: list[int] = []
    for it in (ctx.get("items") or [])[:5]:
        status = f" [{it['status']}]" if it.get("status") else ""
        lines.append(f"- {it.get('predicate', 'mentioned_in')}: "
                     f"{it.get('text') or ''}{status}")
        if it.get("fact_id"):
            used_fact_ids.append(int(it["fact_id"]))
    discussed = [d["name"] for d in (ctx.get("discussed_with") or [])[:3]
                 if d.get("name") and d["name"] != "?"]
    if discussed:
        lines.append(f"- often comes up with: {', '.join(discussed)}")
    affil = ctx.get("affiliations") or []
    if affil:
        names = [a.get("name") for a in affil[:3] if a.get("name")]
        if names:
            lines.append(f"- affiliated with: {', '.join(names)}")
    pid = person.get("id")
    # even a bare hit names the person
    return lines, (int(pid) if pid else None), used_fact_ids


def _tasks_section(store) -> tuple[list[str], list[int]]:
    """Open tasks/commitments from the reviewed facts table, newest first.
    Also returns the fact ids actually included, for the attention ledger."""
    rows = (store.list_facts(kind="task", status="open", limit=8,
                             actionable=True)
            + store.list_facts(kind="commitment", status="open", limit=8,
                               actionable=True))
    rows.sort(key=lambda r: r.get("extracted_at") or 0, reverse=True)
    if not rows:
        return [], []
    try:
        from app.services.clock import format_due_for_prompt, now_local
        n = now_local()
    except Exception:
        n = None
        format_due_for_prompt = None  # type: ignore
    lines = ["OPEN TASKS & COMMITMENTS (from your reviewed memory, newest first):"]
    fact_ids: list[int] = []
    for r in rows[:8]:
        review = f" (review: {r['review']})" if r.get("review") else ""
        due_txt = ""
        if r.get("due") and format_due_for_prompt is not None:
            pretty = format_due_for_prompt(r.get("due"), n)
            if pretty:
                due_txt = f" · due {pretty}"
        elif r.get("due"):
            due_txt = f" · due {r.get('due')}"
        lines.append(f"- [{r.get('kind')}] {r.get('text') or r.get('source_span') or ''}"
                     f"{due_txt}{review}")
        if r.get("fact_id"):
            fact_ids.append(int(r["fact_id"]))
    return lines, fact_ids


def _dedup_hit_lines(kept: list[dict]) -> list[str]:
    """Render hits one per line, summaries over raw, exact repeats dropped."""
    lines: list[str] = []
    seen: set[str] = set()
    for h in kept:
        text = h.get("summary") or h.get("raw", "")
        key = " ".join(text.lower().split())
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- [{h.get('modality', '?')}] {text}")
    return lines


def _screen_section(question: str, *, limit: int = 6) -> list[str]:
    """Vision-modality search (webcam + desktop.screen captions) for
    what-was-I-watching/looking-at questions."""
    from app.services.memory import memory
    hits = memory.search(question, limit=limit, modality="vision")
    body = _dedup_hit_lines(hits)
    if not body:
        return []
    return (["SCREEN & CAMERA OBSERVATIONS (things visibly on screen or seen, "
             "newest matches first):"] + body)


def _semantic_section(question: str, *, limit: int, min_score: float,
                      email_guard: bool) -> tuple[list[str], list[dict]]:
    """Timeline search as fallback context; summaries preferred over raw ASR."""
    from app.services.memory import memory
    hits = memory.search(question, limit=limit)
    kept = [h for h in hits
            if h.get("score") is None or h["score"] >= min_score]
    if email_guard:
        kept = [h for h in kept
                if not (_LOOSE_PROMISE.search(h.get("summary") or h.get("raw") or "")
                        and not _overlaps_goal(
                            h.get("summary") or h.get("raw") or "", question))]
    body = _dedup_hit_lines(kept)
    if not body:
        return [], hits
    return (["RELEVANT MEMORIES (raw timeline — may contain transcription "
             "errors; ignore any that aren't relevant):"] + body), hits


def _activity_section() -> list[str]:
    from app.services.activity import describe_recent
    recent = describe_recent(limit=6)
    if not recent:
        return []
    return (["RECENT DESKTOP ACTIVITY (newest first; ignore if not relevant):"]
            + [f"- {r}" for r in recent])


def compose(question: str, *, semantic_limit: int = 5, min_score: float = 0.15,
            email_guard: bool = False, store=None,
            record_attention: bool = True) -> dict[str, Any]:
    """Build the grounding block for one question.

    Returns {"block": str, "hits": [...], "sources": [...]} — `hits` is the
    semantic layer's raw result for callers that surface it (llm.answer's
    `retrieved`); `sources` is one entry per section actually used
    ({label, n, items}) so the UI can show WHERE an answer looked (the
    "show sources" line under each chat bubble). Structured layers run first
    and are individually best-effort."""
    sections: list[list[str]] = []
    sources: list[dict] = []

    def _add(label: str, sec: list[str]) -> None:
        sections.append(sec)
        body = [ln[2:] if ln.startswith("- ") else ln for ln in sec[1:]]
        sources.append({"label": label, "n": max(1, len(sec) - 1),
                        "items": [b[:160] for b in body[:6]]})

    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            print(f"[grounding] store unavailable ({exc}); semantic only.")
            store = None

    # Identity FIRST — who the assistant is and who the user is, resolved
    # deterministically (not fuzzy search), so "who am I?" / "what are you?" are
    # always answerable and every reply can address the user by name. This is
    # a contract (see tests): nothing may ground ahead of identity.
    try:
        from app.services.identity import identity_lines
        _add("identity", identity_lines(store))
    except Exception as exc:
        print(f"[grounding] identity layer skipped ({exc}).")

    # Local clock — "what's due today / this week?" needs a real now. After
    # identity: the model reads the whole prompt either way, and identity-first
    # keeps "who am I?" deterministic.
    try:
        from app.services.clock import clock_line
        _add("clock", [clock_line()])
    except Exception as exc:
        print(f"[grounding] clock layer skipped ({exc}).")

    # Living user profile — the freshest facts the user has stated about
    # themselves (self-node claims + owned open work). Static identity says WHO
    # they are; this says what they currently care about / prefer / owe.
    people_list_q = bool(_PEOPLE_LISTY.search(question or ""))
    if store is not None:
        try:
            from app.services.self_profile import profile_lines
            sec = profile_lines(store)
            if sec:
                _add("user profile", sec)
        except Exception as exc:
            print(f"[grounding] user profile layer skipped ({exc}).")

    # Contacts roster FIRST for people-list questions — before WM / screen
    # can inject ambient media names (Bill Clinton from a TMZ tab, etc.).
    grounded_person_ids_early: list[int] = []
    if store is not None and people_list_q:
        try:
            sec, cids = _contacts_section(store)
            if sec:
                _add("people you know", sec)
                grounded_person_ids_early = list(cids)
        except Exception as exc:
            print(f"[grounding] contacts roster skipped ({exc}).")

    # WORKING SET first claim after identity/profile (Track A3) — same attention
    # state the field holds. Refresh WM if Now-Context moved so chat doesn't
    # read a stale snapshot waiting on a constellation poll.
    wm_person_ids: list[int] = []
    wm_fact_ids: list[int] = []
    if store is not None and not people_list_q:
        try:
            from app.services import working_memory as _wm
            _wm.ensure_fresh(store)
            slots = _wm.snapshot(store)
            sec = _wm.render_lines(slots)
            if sec:
                budget = int(_MAX_BLOCK_CHARS * 0.40)
                text = "\n".join(sec)
                if len(text) > budget:
                    # Keep header + as many slot lines as fit.
                    kept = [sec[0]]
                    used = len(sec[0])
                    for ln in sec[1:]:
                        if used + 1 + len(ln) > budget:
                            break
                        kept.append(ln)
                        used += 1 + len(ln)
                    sec = kept
                _add("working set", sec)
                for s in slots:
                    if s.get("node_type") == "person" and s.get("node_id") is not None:
                        wm_person_ids.append(int(s["node_id"]))
                    elif s.get("node_type") == "fact" and s.get("node_id") is not None:
                        wm_fact_ids.append(int(s["node_id"]))
        except Exception as exc:
            print(f"[grounding] working set layer skipped ({exc}).")

    # Learning Memory — weak study concepts when a study mode is sticky.
    if store is not None and not people_list_q:
        try:
            from app.services import learning_memory as _lme
            if _lme.study_mode_active():
                sec = _lme.render_lines(store, limit=8)
                if sec:
                    _add("weak concepts", sec)
        except Exception as exc:
            print(f"[grounding] learning memory layer skipped ({exc}).")

    grounded_person_ids: list[int] = list(grounded_person_ids_early) + list(wm_person_ids)
    grounded_fact_ids: list[int] = list(wm_fact_ids)
    if store is not None and not people_list_q:
        try:
            for name in _people_in(question, store):
                sec, pid, fids = _person_section(name, store)
                if sec:
                    _add(f"person graph: {name}", sec)
                    if pid:
                        grounded_person_ids.append(pid)
                    grounded_fact_ids.extend(fids)
        except Exception as exc:
            print(f"[grounding] person layer skipped ({exc}).")
        try:
            if _TASKY.search(question or ""):
                sec, fids = _tasks_section(store)
                if sec:
                    _add("open tasks & commitments", sec)
                    grounded_fact_ids.extend(fids)
        except Exception as exc:
            print(f"[grounding] tasks layer skipped ({exc}).")

    try:
        if (not people_list_q) and _SCREENY.search(question or ""):
            sec = _screen_section(question)
            if sec:
                _add("screen & camera", sec)
    except Exception as exc:
        print(f"[grounding] screen layer skipped ({exc}).")

    hits: list[dict] = []
    if not people_list_q:
        try:
            sem, hits = _semantic_section(question, limit=semantic_limit,
                                          min_score=min_score,
                                          email_guard=email_guard)
            if sem:
                _add("timeline memories", sem)
        except Exception as exc:
            print(f"[grounding] semantic layer skipped ({exc}).")
        try:
            act = _activity_section()
            if act:
                _add("recent desktop activity", act)
        except Exception:
            pass

    # Attention ledger (Phase 0): record what grounding pulled in, and flag
    # asked-about people the field had NOT surfaced recently as misses — the
    # negative labels learned ranking needs. Best-effort, never breaks answers.
    # `record_attention=False` keeps machine callers (self-quiz) out of the
    # ledger: a generated quiz question is not the user needing something.
    if (record_attention and store is not None
            and (grounded_person_ids or grounded_fact_ids)):
        try:
            from app.services.attention_ledger import attention_ledger
            attention_ledger.record_grounding(
                grounded_person_ids, grounded_fact_ids, store)
        except Exception as exc:
            print(f"[grounding] attention ledger skipped ({exc}).")
        # Now-Context (A2): a real user question about these nodes IS the
        # present — seed them so the field's activation lights their
        # neighborhood. Machine callers never reach this branch.
        try:
            from app.services.now_context import now_context
            now_context.observe(
                [("person", pid) for pid in grounded_person_ids]
                + [("fact", fid) for fid in grounded_fact_ids],
                weight=1.0, source="chat")
        except Exception as exc:
            print(f"[grounding] context seed skipped ({exc}).")

    block = "\n\n".join("\n".join(s) for s in sections if s)
    if len(block) > _MAX_BLOCK_CHARS:
        block = block[:_MAX_BLOCK_CHARS] + "…"
    return {"block": block, "hits": hits, "sources": sources}
