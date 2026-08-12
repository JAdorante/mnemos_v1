"""Step 2 — the extraction pass: episodic turns -> structured facts.

The consolidation layer merges raw utterances into conversational *turns*; this
pass reads *settled* turns (past the silence gap, so they won't grow) and asks
Claude to pull the structured commitments hiding in them:

    "I'll send Chris the pricing follow-up by Friday"
        -> commitment(from=me, to=Chris, text=..., due=2026-07-25)  # resolved vs local clock
    "we need to book the venue"        -> task(text=Book the venue)
    "the demo is on Monday"            -> claim(text=...)

Each fact is written to the fact tables (`app/storage.py`) with a foreign key
back to a source event and the verbatim `source_span` it came from — provenance
for the Console's approve/edit/dismiss loop and the agent's approval gate.

Why *settled* turns only: a settled turn (end older than the consolidation gap)
can't merge new utterances on the next rebuild, so extracting it exactly once is
safe — no double-counting. We mark every member event `extracted_at` so the pass
is idempotent and never reprocesses the same speech.

Model lives behind one swappable constant (`EXTRACTOR_MODEL`) so a future
ModelRouter can route this boundary without touching the extractor.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from app.config import settings
from app.services.consolidation import Turn
from app.services.vocabulary import example_terms as _example_terms
from app.events import Modality
from app.storage import Store, get_store

# The one model boundary for extraction — route it here later (ModelRouter).
# Haiku by default: side-by-side telemetry showed Haiku extraction working at
# ~1/100th of Opus's spend on this task; set QUILL_EXTRACT_MODEL to override.
EXTRACTOR_MODEL = os.environ.get("QUILL_EXTRACT_MODEL", "claude-haiku-4-5")

# Stamped on every fact_candidates row (plan 1.1) so goldens / replay can pin
# which prompt+schema produced the LLM output. Bump when _SYSTEM or _SCHEMA
# changes in a way that should invalidate prior candidates.
EXTRACT_PROMPT_VERSION = os.environ.get(
    "QUILL_EXTRACT_PROMPT_VERSION", "extract-v1")
EXTRACT_SCHEMA_VERSION = os.environ.get(
    "QUILL_EXTRACT_SCHEMA_VERSION", "facts-schema-v3")

# LLM output arrays written to fact_candidates (kind → facts dict key).
_CANDIDATE_KINDS = (
    ("task", "tasks"),
    ("commitment", "commitments"),
    ("claim", "claims"),
    ("question", "questions"),
    ("entity", "entities"),
    ("relation", "relations"),
)


def turn_hash(turn: Turn | dict) -> str:
    """Stable sha256 for a turn — keys fact_candidates for replay/dedupe."""
    if isinstance(turn, dict):
        text = turn.get("text") or ""
        speaker = turn.get("speaker") or ""
        eids = turn.get("event_ids") or []
    else:
        text = turn.text or ""
        speaker = turn.speaker or ""
        eids = turn.event_ids or []
    payload = json.dumps(
        {"text": text, "speaker": speaker, "event_ids": list(eids)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

# Neutral-by-default few-shot example names for the schema descriptions (the
# general-code invariant: no real contacts baked into logic). Data-driven when
# opted in + eval-gated — see app/services/vocabulary.py. Read once at import.
_EX = _example_terms()

def _extract_vocab_enabled() -> bool:
    """Inject the user's known-name spelling hint into the extractor prompt?
    OFF by default (bias/hallucination guardrail — see _extract_text). Flip on
    with QUILL_EXTRACT_VOCAB=1 once eval_extraction shows no regression."""
    return os.environ.get("QUILL_EXTRACT_VOCAB", "0") not in ("0", "false", "False")


# Process this many unextracted events per pass. Small batches keep each `extract`
# job short so a hang/crash costs one batch, not the whole backlog; the worker
# re-enqueues while events remain (see main.py). Large enough to make real progress.
EXTRACT_BATCH = int(os.environ.get("QUILL_EXTRACT_BATCH", "40"))

# LLM failures per turn before parking extract_status='failed' (plan 0.9).
# Without a cap, a poisoned transcript is left unmarked forever and every
# consolidate→extract / settle-nudge pass retries it (nudge spin).
EXTRACT_MAX_ATTEMPTS = int(os.environ.get("QUILL_EXTRACT_MAX_ATTEMPTS", "3"))


def _index_fact(store, fact_id: int, kind: str, text: str, ts: float) -> None:
    """Index a new fact into the shared semantic store (best-effort). Kept as a
    free function so both the audio extractor and the vision-todo ingest use it."""
    try:
        from app.services.memory import memory
        memory.index_fact(fact_id, kind, text, ts)
    except Exception as exc:
        print(f"[extract] fact index skipped ({exc}).")


def _coerce_due(value) -> str | None:
    """Normalize extractor dues to ISO when possible; keep free text otherwise."""
    try:
        from app.services.clock import coerce_due
        return coerce_due(value)
    except Exception:
        s = (str(value).strip() if value is not None else "")
        return s or None

_ASSERTIONS = ("stated_by_user", "stated_by_other", "inferred", "quoted",
              "hypothetical")
_ASSERTION_PROP = {
    "type": "string", "enum": list(_ASSERTIONS),
    "description": "How this was asserted: stated_by_user (the speaker said "
    "it about themselves/their own plan), stated_by_other (the speaker "
    "reported someone else's plan/promise as fact), inferred (implied but not "
    "directly stated), quoted (the speaker is quoting/relaying what someone "
    "else said, not asserting it themselves), hypothetical (a maybe/if/would "
    "— not a real commitment). Tag quoted or hypothetical whenever the "
    "speech is quoting someone or floating a hypothetical — these are never "
    "auto-accepted.",
}

_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "description": "Concrete action items someone needs to do. Only include "
            "explicit, actionable to-dos actually stated — never invent or infer.",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The action, as a clean imperative ('Book the venue')."},
                    "owner": {"type": "string", "description": "Who owns it — a person's name, 'me' for the speaker, or '' if unspecified."},
                    "due": {"type": "string",
                            "description": "Absolute local due date/time after "
                            "resolving relatives against RIGHT NOW: YYYY-MM-DD "
                            "or YYYY-MM-DDTHH:MM:SS. Empty '' if none stated."},
                    "confidence": {"type": "number", "description": "0-1: how clearly this was stated as a real task."},
                    "source_span": {"type": "string", "description": "The verbatim substring of the transcript this came from."},
                    "assertion": _ASSERTION_PROP,
                },
                "required": ["text", "owner", "due", "confidence", "source_span", "assertion"],
                "additionalProperties": False,
            },
        },
        "commitments": {
            "type": "array",
            "description": "Promises one person made to another ('I'll send you the deck'). "
            "Directional: who promised, to whom. Only explicit promises.",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The promise, as a clean statement."},
                    "from_person": {"type": "string", "description": "Who made the promise — a name or 'me' for the speaker."},
                    "to_person": {"type": "string", "description": "Who it was made to — a name, or '' if unspecified."},
                    "due": {"type": "string",
                            "description": "Absolute local due (YYYY-MM-DD or "
                            "YYYY-MM-DDTHH:MM:SS) resolved against RIGHT NOW, "
                            "else ''."},
                    "confidence": {"type": "number"},
                    "source_span": {"type": "string", "description": "Verbatim substring this came from."},
                    "assertion": _ASSERTION_PROP,
                },
                "required": ["text", "from_person", "to_person", "due", "confidence", "source_span", "assertion"],
                "additionalProperties": False,
            },
        },
        "claims": {
            "type": "array",
            "description": "Notable factual statements worth remembering (a price, a date, "
            "a decision, a preference) that are NOT tasks or commitments. When the "
            "claim is a clear money/date fact, also fill subject/predicate/object "
            "(structured belief); otherwise leave those empty and keep text only.",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "confidence": {"type": "number"},
                    "source_span": {"type": "string", "description": "Verbatim substring this came from."},
                    "assertion": _ASSERTION_PROP,
                    "subject": {
                        "type": "string",
                        "description": "What the claim is about (deal/plan/product/person name). "
                        "Empty string when not parseable as SPO.",
                    },
                    "predicate": {
                        "type": "string",
                        "enum": ["costs", "priced_at", "due_on", ""],
                        "description": "Structured claim link: costs/priced_at for money, "
                        "due_on for dates. Empty when unparseable.",
                    },
                    "object": {
                        "type": "string",
                        "description": "Literal value ('$49', '2026-08-15'). Empty when unparseable.",
                    },
                    "speaker_is_source": {
                        "type": "boolean",
                        "description": "True when the labeled turn speaker is asserting this "
                        "as their own knowledge; false when they are reporting what someone "
                        "else said ('David said it's $49').",
                    },
                    "resolves_commitment": {
                        "type": "boolean",
                        "description": "True when the speaker is reporting that an earlier "
                        "promise/commitment was already carried out ('I sent the deck', "
                        "'it's done', 'I already emailed the client'). False otherwise. "
                        "Never invent a new commitment for these — they resolve an old one.",
                    },
                },
                "required": ["text", "confidence", "source_span", "assertion"],
                "additionalProperties": False,
            },
        },
        "entities": {
            "type": "array",
            "description": "Named non-person things referenced: organizations/companies, "
            "projects/products, and places. NOT people. Only ones explicitly named.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": f"The canonical name ('{_EX['company']}', '{_EX['project']}')."},
                    "kind": {"type": "string", "enum": ["org", "project", "place", "product", "other"]},
                    "confidence": {"type": "number"},
                    "source_span": {"type": "string", "description": "Verbatim substring this came from."},
                },
                "required": ["name", "kind", "confidence", "source_span"],
                "additionalProperties": False,
            },
        },
        "relations": {
            "type": "array",
            "description": "Explicit relationships between two named things — a person and "
            f"an org/project, or two orgs. Only when clearly stated ('{_EX['person']} is at "
            f"{_EX['company']}', 'the fundraising work for {_EX['project']}'). Never infer.",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Name of the first thing (a person or entity), or 'me'."},
                    "subject_kind": {"type": "string", "enum": ["person", "entity"]},
                    "predicate": {"type": "string", "enum": ["works_at", "part_of", "member_of", "about", "located_in", "related_to"]},
                    "object": {"type": "string", "description": "Name of the second thing."},
                    "object_kind": {"type": "string", "enum": ["person", "entity"]},
                    "confidence": {"type": "number"},
                    "source_span": {"type": "string", "description": "Verbatim substring this came from."},
                },
                "required": ["subject", "subject_kind", "predicate", "object", "object_kind", "confidence", "source_span"],
                "additionalProperties": False,
            },
        },
        "questions": {
            "type": "array",
            "description": "Open questions someone asked that still need an answer "
            "('What's the valuation?', 'Did we hear back from the vendor?'). Only explicit "
            "questions worth tracking — not rhetorical filler.",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The question, cleaned."},
                    "asked_by": {"type": "string",
                                 "description": "Who asked — a name, 'me' for the speaker, or ''."},
                    "confidence": {"type": "number"},
                    "source_span": {"type": "string",
                                    "description": "Verbatim substring this came from."},
                    "assertion": _ASSERTION_PROP,
                },
                "required": ["text", "asked_by", "confidence", "source_span", "assertion"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tasks", "commitments", "claims", "questions", "entities", "relations"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are Mnemos's fact extractor. You receive a short passage of transcribed "
    "speech (one conversational turn) labeled with who spoke, and pull out the "
    "structured facts it contains: tasks, commitments, notable claims, and open "
    "questions.\n\n"
    "Input format: `[<speaker or 'unknown speaker'>]: <spoken text>`.\n\n"
    "Rules:\n"
    "- Extract ONLY what is explicitly stated. Never infer, guess, or invent a "
    "task/commitment that isn't clearly there. An empty array is the correct "
    "answer for small talk, filler, or fragments.\n"
    "- A TASK is a concrete action to be done. A COMMITMENT is a promise one "
    "person made to another. A CLAIM is a notable fact (price, date, decision) "
    "worth remembering that is neither. A QUESTION is an explicit open question "
    "someone asked that still needs an answer — put those in `questions`, not "
    "claims.\n"
    "- Ownership is relative to the labeled speaker of THIS turn. Use 'me' for "
    "owner/from_person/to_person when that labeled speaker refers to themselves "
    "('I'll send…', 'my task'). Do NOT use 'me' for a different person mentioned "
    "in the turn — use their name. If the label is 'unknown speaker', still use "
    "'me' for first-person self-reference in the speech.\n"
    "- An ENTITY is a named non-person thing: a company/org, a project/product, or "
    "a place. A RELATION is an explicitly stated link between two named things "
    "(a person and an org, a project and a company). Only emit a relation when it "
    "is clearly stated — never infer an affiliation from mere co-mention.\n"
    "- `source_span` MUST be a verbatim substring of the spoken text AFTER the "
    "`]: ` label — never include the `[Speaker]:` prefix in source_span.\n"
    "- Prefer precision over recall: when in doubt, leave it out.\n"
    "- Due dates: resolve relative timing against the RIGHT NOW clock in this "
    "prompt; store absolute local ISO (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). "
    "Never leave a bare weekday like 'Friday' in `due` when the calendar day "
    "can be resolved.\n"
    "- Every task/commitment/claim carries an `assertion` tag for how it was "
    "asserted: stated_by_user (the speaker asserting about themself/their own "
    "plan), stated_by_other (the speaker reporting someone else's stated "
    "plan/promise as fact), inferred (implied, not directly stated), quoted "
    "(the speaker is quoting or relaying what someone ELSE said — 'she told "
    "me she'd send it', 'he said \"I'll be there\"'), hypothetical (a maybe/"
    "if/would-type statement, not a real commitment — 'I might send it "
    "Friday', 'if we go, I'd book the venue'). Tag quoted or hypothetical "
    "whenever the speech is quoting someone else or floating a hypothetical — "
    "never tag those as stated_by_user.\n"
    "- For CLAIMS that are clear money or date facts, also fill structured "
    "fields when parseable: subject (what it is about), predicate "
    "(`costs`/`priced_at` for money, `due_on` for dates), object (the literal "
    "value like '$49' or '2026-08-15'), and speaker_is_source (true if the "
    "labeled speaker is asserting it; false if reporting 'X said …'). Leave "
    "subject/predicate/object as empty strings when not clearly parseable — "
    "those claims stay as flat text only.\n"
    "- Set claim `resolves_commitment=true` when the speaker says they already "
    "sent/finished/completed something they previously promised ('I sent the "
    "deck', 'it's done', 'I already emailed the client'). Do NOT mint a new "
    "commitment for those — they resolve an existing one.\n"
    "- When a USER'S LIVE NOTE block is present, treat it as an importance / "
    "disambiguation hint only. Prefer extracting the commitments, decisions, "
    "and claims the note points at — but `source_span` MUST still be a "
    "verbatim substring of the spoken transcript, never of the note text."
)


class Extractor:
    def __init__(self, store: Store | None = None) -> None:
        self._store = store
        self._client = None

    def _ensure_store(self) -> Store:
        if self._store is None:
            self._store = get_store()
        return self._store

    def _ensure_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _extract_text(self, turn_or_text, *, speaker: str | None = None
                      ) -> dict[str, Any]:
        """Call Claude on one speaker-labeled turn; return structured facts.

        Accepts a Turn / turn-dict (plan 2.1) or a bare string for document/chat
        paths. Renders `[<speaker or 'unknown speaker'>]: <text>` so ownership
        ('me') is relative to the labeled speaker.

        Routed through the ModelRouter so the call is logged (latency/tokens/cost)
        alongside vision — one measurable view of total spend in /console/models.
        """
        from app.services.consolidation import format_turn_transcript
        from app.services.model_router import router

        if isinstance(turn_or_text, Turn):
            labeled = format_turn_transcript(turn_or_text)
        elif isinstance(turn_or_text, dict) and "text" in turn_or_text:
            labeled = format_turn_transcript(turn_or_text)
        else:
            text = str(turn_or_text or "")
            label = (speaker or "").strip() or "unknown speaker"
            labeled = f"[{label}]: {text.strip()}"

        system = _SYSTEM
        # Local clock so "Friday" / "tomorrow" resolve to absolute dues the
        # ranking / fulfillment layers can actually use (ISO only).
        try:
            from app.services.clock import clock_instruction
            system = system + "\n\n" + clock_instruction()
        except Exception:
            pass
        # #11 (opt-in, eval-gated): give the extractor the user's known names so it
        # spells them canonically ("Abby Nengel", not "Abby Nagle"). OFF by default
        # — unlike the ASR front-end, a name list in an *extractor* prompt can induce
        # a hallucinated fact about a named person, so only enable it once
        # eval_extraction shows no precision/faithfulness regression on your data
        # (QUILL_EXTRACT_VOCAB=1). The hint itself is worded spelling-only.
        if _extract_vocab_enabled():
            try:
                from app.services.vocabulary import vocabulary
                hint = vocabulary.spelling_hint()
                if hint:
                    system = system + "\n\n" + hint
            except Exception:
                pass
        # Tier 4 (opt-in): add the user's DISMISSED facts as dynamic negatives, so
        # the extractor learns their bar for "worth keeping" from their own verdicts.
        try:
            from app.services.feedback_learning import extraction_negatives_block
            neg = extraction_negatives_block()
            if neg:
                system = system + "\n\n" + neg
        except Exception:
            pass

        # Meeting Layer P2: co-timed notepad jots (±90s) as importance anchors.
        user_content = f"Transcript turn:\n\n{labeled}"
        try:
            from app.services import meeting_notes as _mnotes
            center = None
            if isinstance(turn_or_text, Turn):
                center = float(getattr(turn_or_text, "end", None)
                               or getattr(turn_or_text, "start", None) or 0) or None
            elif isinstance(turn_or_text, dict):
                center = float(turn_or_text.get("end")
                               or turn_or_text.get("start") or 0) or None
            if center:
                jots = _mnotes.jot_texts_near(self._ensure_store(), center)
                block = _mnotes.format_anchor_block(jots)
                if block:
                    user_content = f"{user_content}\n\n{block}"
        except Exception:
            pass

        return router.complete_json(
            "extract", system=system,
            messages=[{"role": "user", "content": user_content}],
            schema=_SCHEMA, max_tokens=1024, model=EXTRACTOR_MODEL,
        )

    # --- turn selection ---------------------------------------------------
    def _pending_turns(self, now: float) -> tuple[list[Turn], float | None]:
        """Settled turns whose member events haven't been extracted yet, plus
        the seconds until the earliest *unsettled* text turn becomes eligible
        (or None if there is none).

        Recomputes turns from the unextracted audio events only; because a
        settled turn can't merge future utterances, grouping just this slice
        yields the same boundaries the full rebuild would for these events.

        The second value drives the tail-latency nudge: after you stop talking,
        nothing else re-triggers extraction until the next audio event, so the
        caller schedules a one-shot re-run timed to when the last turn settles.
        Only *text-bearing* turns count toward it, so text-less fragments (which
        never produce a fact and are never marked) can't cause an endless
        reschedule.
        """
        store = self._ensure_store()
        rows = [(eid, ev) for eid, ev in store.unextracted_events(
                    limit=EXTRACT_BATCH, modality=Modality.AUDIO.value)]
        rows.sort(key=lambda r: r[1].time)
        if not rows:
            return [], None
        # One shared definition of "settled" (consolidation.settled_turns) — the
        # extractor, the #6 router, and telemetry all agree on when a turn is final,
        # instead of each re-deriving the gap arithmetic here. Grouping just this
        # unextracted slice yields the same boundaries the full rebuild would,
        # because a settled turn can't merge future utterances.
        from app.services.consolidation import settled_turns
        return settled_turns(rows, now)

    # --- persistence ------------------------------------------------------
    def _attendee_priors_for_turn(self, turn_start: float | None,
                                  turn_end: float | None) -> list[dict]:
        """Calendar-invite attendees for a turn inside a linked session (P1)."""
        if turn_start is None:
            return []
        try:
            from app.services import meeting_join
            return meeting_join.attendees_for_time(
                self._ensure_store(), float(turn_start),
                float(turn_end if turn_end is not None else turn_start))
        except Exception:
            return []

    def _resolve_person_id(self, name: str, now: float,
                           *, event_id: int | None = None,
                           event_source: str = "",
                           window: str = "",
                           text: str = "",
                           grammatical_role: str = "unknown",
                           relationship_boost: float = 0.6,
                           turn_speaker: str | None = None,
                           turn_start: float | None = None,
                           turn_end: float | None = None,
                           attendee_priors: list[dict] | None = None) -> int | None:
        # People v2: mention ledger + candidate resolution. Legacy path when
        # QUILL_PEOPLE_V2=0.
        # Plan 2.1: 'me' is relative to the labeled turn speaker — maps to the
        # enrolled user's self node ONLY when that speaker is the enrolled user.
        from app.services import self_profile
        from app.services.people_pipeline import enabled, resolve_person_mention
        from app.services.resolution import resolver
        store = self._ensure_store()
        priors = attendee_priors
        if priors is None and turn_start is not None:
            priors = self._attendee_priors_for_turn(turn_start, turn_end)
        if self_profile.is_self_name(name):
            return self._resolve_me_relative_to_speaker(
                turn_speaker, now, event_id=event_id,
                event_source=event_source, window=window, text=text,
                grammatical_role=grammatical_role,
                relationship_boost=relationship_boost)
        # People v3 P3 (flag-gated): a provisional diarization label
        # ("Speaker 3") used as a party name never resolves — and never
        # mints — a person. The caller escrows the fact against the track.
        from app.services import people_escrow
        if people_escrow.enabled() and people_escrow.is_provisional_label(name):
            return None
        if enabled():
            res = resolve_person_mention(
                name, store=store, event_id=event_id,
                event_source=event_source or "audio.whisper",
                window=window, text=text, grammatical_role=grammatical_role,
                now=now, relationship_boost=relationship_boost,
                attendee_priors=priors or None,
            )
            # Also attribute contacts from the turn text when we got a person.
            if res.person_id and text:
                from app.services.people_pipeline import attribute_contacts_from_text
                attribute_contacts_from_text(
                    text, store=store,
                    person_id=res.person_id, person_name=name,
                    event_id=event_id, now=now,
                    event_source=event_source or "audio.whisper",
                    window=window)
            # People v2 is authoritative: reject / leave_open must NOT fall
            # through to ungated store.resolve_person (that minted Speaker N).
            return int(res.person_id) if res.person_id else None
        # Legacy path only when People v2 is off.
        pid = store.resolve_person(name, ts=now)
        return int(pid) if pid else None

    def _resolve_me_relative_to_speaker(
            self, turn_speaker: str | None, now: float, *,
            event_id: int | None = None, event_source: str = "",
            window: str = "", text: str = "",
            grammatical_role: str = "unknown",
            relationship_boost: float = 0.6) -> int | None:
        """Map extractor 'me' to a person id relative to the labeled speaker.

        - Speaker is enrolled user → self node
        - Speaker is another known label → that speaker's person id
        - Unknown / empty speaker → None (never park 'me' on the user)
        """
        from app.services import self_profile
        from app.services.people_pipeline import enabled, resolve_person_mention
        from app.services.resolution import resolver

        store = self._ensure_store()
        spk = (turn_speaker or "").strip()
        if self_profile.speaker_is_enrolled_user(spk, store):
            return self_profile.self_person_id(store)
        if not spk or spk.lower() == "unknown speaker":
            return None
        # People v3 P3 (flag-gated): an unbound voice track's 'me' does not
        # resolve to (or mint) a person — the caller escrows against the
        # track instead, and the rebind job attributes it once labeled.
        from app.services import people_escrow
        if people_escrow.enabled() and people_escrow.is_provisional_label(spk):
            return None
        # Diarization placeholders are not people — never mint "Speaker 6".
        import re as _re
        if _re.match(r"(?i)^speaker(\s*\d+)?$", spk):
            return None
        # Other labeled speaker saying "I'll…" — ownership is theirs.
        if enabled():
            res = resolve_person_mention(
                spk, store=store, event_id=event_id,
                event_source=event_source or "audio.whisper",
                window=window, text=text or spk,
                grammatical_role=grammatical_role or "speaker",
                now=now, relationship_boost=max(relationship_boost, 0.85),
            )
            if res.person_id:
                return res.person_id
            return None
        # Legacy path only when People v2 is off.
        pid = store.resolve_person(spk, ts=now)
        return int(pid) if pid else None

    def _persist_entities(self, facts: dict[str, Any], anchor: int | None,
                          now: float, *, event_source: str = "",
                          window: str = "", text: str = "") -> tuple[int, int]:
        """Persist entity nodes + asserted relation edges. Idempotent: entity
        resolution dedupes by name, edge insert upserts. Shared by the live
        extractor and the backfill (which persists ONLY these, not facts).

        `event_source` / `window` / `text` feed People v2 source_policy so
        relation subjects like "Bill Clinton" from a TMZ tab do not mint people.
        KG-A: `knowledge_entities=False` surfaces (news/social/terminal) may
        BIND existing orgs/tools but must not mint new ones.
        """
        store = self._ensure_store()
        ne = nr = 0
        from app.services.resolution import resolver
        from app.services import kg_beliefs
        mint_ok, source_class = kg_beliefs.allow_entity_mint(
            event_source=event_source, window=window, text=text)

        for e in facts.get("entities", []):
            name = (e.get("name") or "").strip()
            if not name:
                continue
            if mint_ok:
                resolver.resolve_entity(name, e.get("kind"), ts=now)
                ne += 1
            else:
                # Bind-only: still touch last_seen if the org/tool already exists.
                eid = store.find_entity_exact(name)
                if eid:
                    try:
                        store.touch_entity(eid, now, alias=name)
                    except Exception:
                        pass
                    ne += 1

        def _node(name: str, kind: str):
            nm = (name or "").strip()
            if not nm:
                return None
            # "I work at Acme" → an edge FROM the user's own graph node.
            from app.services import self_profile
            if self_profile.is_self_name(nm):
                pid = self_profile.self_person_id(store)
                return ("person", pid) if pid else None
            if kind == "person":
                pid = self._resolve_person_id(
                    nm, now, event_id=anchor,
                    event_source=event_source or "audio.whisper",
                    window=window, text=text,
                    grammatical_role="relation", relationship_boost=0.45)
                return ("person", pid) if pid else None
            if mint_ok:
                eid = resolver.resolve_entity(nm, ts=now)
                return ("entity", eid) if eid else None
            eid = store.find_entity_exact(nm)
            return ("entity", eid) if eid else None

        for r in facts.get("relations", []):
            subj = _node(r.get("subject", ""), r.get("subject_kind"))
            obj = _node(r.get("object", ""), r.get("object_kind"))
            pred = r.get("predicate")
            if subj and obj and pred and subj != obj:
                span = (r.get("source_span") or "")[:400]
                store.add_relation(subj[0], subj[1], pred, obj[0], obj[1],
                                   origin="asserted", source_event_id=anchor,
                                   confidence=r.get("confidence"), ts=now,
                                   quote=span or None, source_class=source_class)
                nr += 1
        return ne, nr

    def _record_faithfulness(self, fact: dict, source_text: str) -> None:
        """Score one emitted fact's source_span against the speech it came from
        (the hallucination guard, #9). Best-effort — never breaks persistence."""
        try:
            from app.services.cog_telemetry import (cog_telemetry, FAITHFULNESS,
                                                    span_is_faithful)
            span = fact.get("source_span", "")
            cog_telemetry.record(
                FAITHFULNESS, span_is_faithful(span, source_text),
                span=span[:120], text=(fact.get("text") or "")[:120])
        except Exception:
            pass

    def _gate(self, kind: str, item: dict, turn: Turn):
        """Run one candidate fact through the write-time hygiene gate
        (field validation / confidence floor / span faithfulness / assertion
        class / dedup / supersede). Returns the Verdict, or a fallback verdict
        on any gate failure — hygiene must never cost a fact, but a
        quoted/hypothetical assertion still never falls back to auto-insert."""
        assertion = item.get("assertion")
        try:
            from app.services.fact_gate import gate_fact
            ids = [int(i) for i in (turn.event_ids or []) if i is not None]
            return gate_fact(kind, item.get("text") or "",
                             item.get("confidence"),
                             item.get("source_span", ""), turn.text,
                             assertion=assertion, payload=item,
                             event_range=((min(ids), max(ids)) if ids else None),
                             store=self._ensure_store())
        except Exception:
            if assertion in ("quoted", "hypothetical"):
                class _Review:  # duck-typed review verdict
                    action = "review"
                    reason = f"assertion={assertion} requires human review"
                    dup_fact_id = None
                    supersede_ids: tuple = ()
                return _Review()
            class _Insert:  # duck-typed insert verdict
                action = "insert"
                reason = ""
                dup_fact_id = None
                supersede_ids: tuple = ()
            return _Insert()

    def _event_correlation_id(self, store: Store, event_id: int | None) -> str | None:
        """Best-effort lookup of the source event's correlation_id (plan 1.5),
        so every candidate born from a given event traces back to it."""
        if not event_id:
            return None
        try:
            ev = store.get_event(event_id)
            if not ev:
                return None
            meta = json.loads(ev.get("meta") or "{}")
            return meta.get("correlation_id") or None
        except Exception:
            return None

    def _write_fact_candidates(self, turn: Turn, facts: dict[str, Any],
                               now: float) -> list[int]:
        """Land every LLM output row as a fact_candidate (plan 1.1).

        Runs before the hygiene gate so dropped/deduped items still leave an
        auditable row with prompt_version. `add_fact_candidate` dedupes on
        turn_hash+kind+payload_json (plan 1.2), so replaying the same turn
        never creates a twin row. Does not change fact materialization.
        """
        store = self._ensure_store()
        th = turn_hash(turn)
        anchor = turn.event_ids[0] if turn.event_ids else None
        speaker = (turn.speaker or "") or None
        correlation_id = self._event_correlation_id(store, anchor)
        ids: list[int] = []
        for kind, key in _CANDIDATE_KINDS:
            for item in facts.get(key) or []:
                if not isinstance(item, dict):
                    continue
                payload = dict(item)
                conf = payload.get("confidence")
                if conf is not None and not isinstance(conf, (int, float)):
                    conf = None
                assertion = payload.get("assertion")  # populated in plan 1.3
                if assertion is not None:
                    assertion = str(assertion) or None
                try:
                    cid = store.add_fact_candidate(
                        turn_hash=th, kind=kind, payload=payload,
                        source_span=payload.get("source_span") or None,
                        speaker=speaker, assertion=assertion,
                        confidence=float(conf) if conf is not None else None,
                        model=EXTRACTOR_MODEL,
                        prompt_version=EXTRACT_PROMPT_VERSION,
                        schema_version=EXTRACT_SCHEMA_VERSION,
                        status="pending",
                        source_event_id=anchor, correlation_id=correlation_id,
                        created_at=now,
                    )
                    ids.append(cid)
                except Exception as exc:
                    print(f"[extract] fact_candidate write skipped ({exc})")
        return ids

    def _persist(self, turn: Turn, facts: dict[str, Any], now: float) -> int:
        """Materialize this turn's LLM output into facts, routed through
        fact_candidates (plan 1.2): every task/commitment/claim is looked up
        by its (turn_hash, kind, payload) candidate row, gated, then either
        materialized or stamped drop/dedup/review — never both. Because
        `add_fact_candidate` dedupes on that same key, replaying an
        already-processed turn finds each candidate at a non-'pending' status
        and skips it, so fact counts stay identical across replays."""
        store = self._ensure_store()
        anchor = turn.event_ids[0] if turn.event_ids else None
        th = turn_hash(turn)
        n = 0

        # Plan 1.1/1.2: land (or find) every LLM output row as a candidate
        # before gating — dropped/deduped/reviewed items still leave an
        # auditable row, and a second pass over the same turn is a no-op.
        self._write_fact_candidates(turn, facts, now)

        # Meeting Layer P1: resolve once per turn so every mention in the
        # turn shares the same calendar-attendee prior set.
        _priors = self._attendee_priors_for_turn(
            getattr(turn, "start", None), getattr(turn, "end", None))

        def _person(name: str, *, role: str = "unknown", boost: float = 0.6) -> int | None:
            return self._resolve_person_id(
                name, now, event_id=anchor,
                event_source="audio.whisper", text=turn.text,
                grammatical_role=role, relationship_boost=boost,
                turn_speaker=turn.speaker or "",
                turn_start=getattr(turn, "start", None),
                turn_end=getattr(turn, "end", None),
                attendee_priors=_priors)

        # People v3 P3: this turn's durable voice-track id when the speaker is
        # an unbound provisional label AND QUILL_PEOPLE_ESCROW is on, else None
        # (flag off: no track is minted, nothing below changes behavior).
        escrow_track_id: int | None = None
        try:
            from app.services import people_escrow
            escrow_track_id = people_escrow.track_for_turn(store, turn, now)
        except Exception as exc:
            print(f"[extract] escrow track skipped ({exc}).")

        def _escrows_to_track(party_name: str, pid: int | None) -> bool:
            """Does this unresolved party mean the turn's unbound speaker?
            ('me'/'I' relative to the track, or the label itself.)"""
            if escrow_track_id is None or pid is not None:
                return False
            from app.services import self_profile as _sp
            nm = (party_name or "").strip()
            return bool(nm) and (
                _sp.is_self_name(nm)
                or nm.lower() == (turn.speaker or "").strip().lower())

        def _apply(v, fid: int) -> None:
            # A 'supersede' verdict: the just-inserted fact replaces the old.
            for old in v.supersede_ids:
                store.supersede_fact(old, fid, now)
            # WS-F: stamp the window's upper event bound so overlap dedup can
            # compare ranges (the anchor is the lower bound, already on the row).
            _ev = [int(i) for i in (turn.event_ids or []) if i is not None]
            if len(_ev) > 1:
                try:
                    store.set_fact_event_hi(fid, max(_ev))
                except Exception:
                    pass

        def _candidate(kind: str, item: dict) -> dict | None:
            try:
                return store.find_fact_candidate(th, kind, dict(item))
            except Exception:
                return None

        for t in facts.get("tasks", []):
            if not t.get("text"):
                continue
            cand = _candidate("task", t)
            if cand and cand.get("status") != "pending":
                continue  # already gated in a prior pass — replay-safe
            cid = cand["id"] if cand else None
            v = self._gate("task", t, turn)
            reason = getattr(v, "reason", "") or ""
            if v.action == "drop":
                if cid:
                    store.set_fact_candidate_status(cid, "dropped", verdict_reason=reason)
                continue
            if v.action == "review":
                if cid:
                    store.set_fact_candidate_status(cid, "review", verdict_reason=reason)
                continue
            if v.action == "dedup":
                store.touch_fact(v.dup_fact_id, now, t.get("confidence"))
                if cid:
                    store.set_fact_candidate_status(cid, "deduped", verdict_reason=reason)
                continue
            owner_name = t.get("owner", "")
            owner_pid = _person(owner_name, role="owner", boost=0.85)
            escrowed = _escrows_to_track(owner_name, owner_pid)
            fid = store.add_task(
                t["text"], source_event_id=anchor,
                source_span=t.get("source_span", ""),
                confidence=t.get("confidence"),
                owner_person_id=owner_pid,
                due=_coerce_due(t.get("due")), extracted_at=now,
            )
            if escrowed:
                # P3: the owner is this turn's unbound voice track — keep the
                # extraction but park it (out of every surface) until the
                # track is bound and the rebind job reactivates it. It must
                # not retire active facts or trigger offers while escrowed.
                store.escrow_fact(fid, escrow_track_id, task_owner=True)
                if cid:
                    store.set_fact_candidate_status(
                        cid, "accepted",
                        verdict_reason=(reason + " [escrowed to track "
                                        f"{escrow_track_id}]").strip())
                self._record_faithfulness(t, turn.text)
                n += 1
                continue
            _apply(v, fid)
            if cid:
                store.set_fact_candidate_status(cid, "accepted", verdict_reason=reason)
            _index_fact(store, fid, "task", t["text"], now)
            self._record_faithfulness(t, turn.text)
            # Proactively ask if I should action this heard task (gated by
            # confidence + cooldown; no-op when the agent/offer is disabled).
            from app.services.task_offer import offer_task
            offer_task(t["text"], t.get("confidence"), fid)
            n += 1
        for c in facts.get("commitments", []):
            if not c.get("text"):
                continue
            cand = _candidate("commitment", c)
            if cand and cand.get("status") != "pending":
                continue
            cid = cand["id"] if cand else None
            v = self._gate("commitment", c, turn)
            reason = getattr(v, "reason", "") or ""
            if v.action == "drop":
                if cid:
                    store.set_fact_candidate_status(cid, "dropped", verdict_reason=reason)
                continue
            if v.action == "review":
                if cid:
                    store.set_fact_candidate_status(cid, "review", verdict_reason=reason)
                continue
            if v.action == "dedup":
                store.touch_fact(v.dup_fact_id, now, c.get("confidence"))
                if cid:
                    store.set_fact_candidate_status(cid, "deduped", verdict_reason=reason)
                continue
            from_name = c.get("from_person", "")
            from_pid = _person(from_name, role="from", boost=0.8)
            escrowed = _escrows_to_track(from_name, from_pid)
            fid = store.add_commitment(
                c["text"], source_event_id=anchor,
                source_span=c.get("source_span", ""),
                confidence=c.get("confidence"),
                from_person_id=from_pid,
                to_person_id=_person(c.get("to_person", ""), role="to", boost=0.75),
                due=_coerce_due(c.get("due")), extracted_at=now,
            )
            if escrowed:
                # P3: committer is the unbound voice track — escrow (see tasks).
                store.escrow_fact(fid, escrow_track_id, commitment_from=True)
                if cid:
                    store.set_fact_candidate_status(
                        cid, "accepted",
                        verdict_reason=(reason + " [escrowed to track "
                                        f"{escrow_track_id}]").strip())
                self._record_faithfulness(c, turn.text)
                n += 1
                continue
            _apply(v, fid)
            if cid:
                store.set_fact_candidate_status(cid, "accepted", verdict_reason=reason)
            _index_fact(store, fid, "commitment", c["text"], now)
            self._record_faithfulness(c, turn.text)
            n += 1
        for cl in facts.get("claims", []):
            if not cl.get("text"):
                continue
            cand = _candidate("claim", cl)
            if cand and cand.get("status") != "pending":
                continue
            cid = cand["id"] if cand else None
            v = self._gate("claim", cl, turn)
            reason = getattr(v, "reason", "") or ""
            if v.action == "drop":
                if cid:
                    store.set_fact_candidate_status(cid, "dropped", verdict_reason=reason)
                continue
            if v.action == "review":
                if cid:
                    store.set_fact_candidate_status(cid, "review", verdict_reason=reason)
                continue
            if v.action == "dedup":
                store.touch_fact(v.dup_fact_id, now, cl.get("confidence"))
                if cid:
                    store.set_fact_candidate_status(cid, "deduped", verdict_reason=reason)
                continue
            fid = store.add_claim(
                cl["text"], source_event_id=anchor,
                source_span=cl.get("source_span", ""),
                confidence=cl.get("confidence"), extracted_at=now,
            )
            # P3: a first-person claim from an unbound voice track is ABOUT a
            # speaker who hasn't earned identity yet — escrow it against the
            # track (no index / KG belief / offers until the rebind).
            from app.services import self_profile
            if (escrow_track_id is not None
                    and self_profile.is_first_person(cl["text"])):
                store.escrow_fact(fid, escrow_track_id)
                if cid:
                    store.set_fact_candidate_status(
                        cid, "accepted",
                        verdict_reason=(reason + " [escrowed to track "
                                        f"{escrow_track_id}]").strip())
                self._record_faithfulness(cl, turn.text)
                n += 1
                continue
            _apply(v, fid)
            if cid:
                store.set_fact_candidate_status(cid, "accepted", verdict_reason=reason)
            _index_fact(store, fid, "claim", cl["text"], now)
            self._record_faithfulness(cl, turn.text)
            # Plan 2.5: parseable SPO claims dual-write into kg_beliefs;
            # unparseable stay flat facts only.
            self._persist_claim_belief(
                cl, fact_id=fid, turn=turn, anchor=anchor, now=now)
            # First-person claims attach to the self node ONLY when the labeled
            # speaker is the enrolled user (plan 2.1) — never when Marc says "I…".
            from app.services import self_profile
            if (self_profile.is_first_person(cl["text"])
                    and self_profile.speaker_is_enrolled_user(
                        turn.speaker or "", store)):
                self_profile.link_self(store, fid, now)
            # Plan 4.2 (a): resolve hint → offer only, never auto-complete.
            if cl.get("resolves_commitment"):
                try:
                    from app.services import commitment_complete as cc
                    cc.offer_matches_for_text(
                        cl.get("source_span") or cl["text"],
                        source="speech_resolve",
                        event_id=anchor, store=store, force=True)
                except Exception as exc:
                    print(f"[extractor] resolve offer skipped ({exc}).")
            n += 1
        for q in facts.get("questions", []):
            if not q.get("text"):
                continue
            cand = _candidate("question", q)
            if cand and cand.get("status") != "pending":
                continue
            cid = cand["id"] if cand else None
            v = self._gate("question", q, turn)
            reason = getattr(v, "reason", "") or ""
            if v.action == "drop":
                if cid:
                    store.set_fact_candidate_status(cid, "dropped",
                                                    verdict_reason=reason)
                continue
            if v.action == "review":
                if cid:
                    store.set_fact_candidate_status(cid, "review",
                                                    verdict_reason=reason)
                continue
            if v.action == "dedup":
                store.touch_fact(v.dup_fact_id, now, q.get("confidence"))
                if cid:
                    store.set_fact_candidate_status(cid, "deduped",
                                                    verdict_reason=reason)
                continue
            fid = store.add_question(
                q["text"], source_event_id=anchor,
                source_span=q.get("source_span", ""),
                confidence=q.get("confidence"), extracted_at=now,
            )
            _apply(v, fid)
            if cid:
                store.set_fact_candidate_status(cid, "accepted",
                                                verdict_reason=reason)
            _index_fact(store, fid, "question", q["text"], now)
            self._record_faithfulness(q, turn.text)
            n += 1

        # Plan 4.2 (a): deterministic "I sent/done" fallback when the model
        # omitted resolves_commitment — still offer-only.
        try:
            from app.services import commitment_complete as cc
            if cc.looks_like_resolve(turn.text or ""):
                cc.offer_matches_for_text(
                    turn.text or "",
                    source="speech_resolve",
                    event_id=anchor, store=store)
        except Exception as exc:
            print(f"[extractor] resolve scan skipped ({exc}).")

        # Entity nodes + asserted relation edges (the graph's non-person side).
        self._persist_entities(facts, anchor, now)
        return n

    def _persist_claim_belief(
        self, cl: dict, *, fact_id: int, turn: Turn,
        anchor: int | None, now: float,
    ) -> None:
        """Dual-write structured claim → kg_beliefs when SPO is complete."""
        subj = (cl.get("subject") or "").strip()
        pred = (cl.get("predicate") or "").strip()
        obj = (cl.get("object") or "").strip()
        if not (subj and pred and obj):
            return
        from app.services import kg_beliefs
        if pred not in kg_beliefs._CLAIM_PREDICATES:
            return
        store = self._ensure_store()
        try:
            from app.services import source_policy as sp
            pol = sp.policy_for_event(
                event_source="audio.whisper", text=turn.text or "")
            source_class = pol.source_class
        except Exception:
            source_class = "private_conversation"

        # Subject: person if resolvable, else value/topic entity on THIS store
        # (never the process-global Resolver — tests and multi-DB installs
        # must dual-write into the bound Store).
        subj_pid = self._resolve_person_id(
            subj, now, event_id=anchor,
            event_source="audio.whisper", text=turn.text or "",
            grammatical_role="claim_subject", relationship_boost=0.5,
            turn_speaker=turn.speaker or "")
        if subj_pid:
            subj_type, subj_id = "person", int(subj_pid)
        else:
            eid = store.find_entity_exact(subj) or store.resolve_entity(
                subj, "other", ts=now)
            if not eid:
                return
            subj_type, subj_id = "entity", int(eid)

        # Object: literal money/date value node. Bypass name_quality entity
        # gates — "$49" is rejected as punctuation by is_plausible_entity,
        # but is a valid belief object for costs/priced_at/due_on.
        obj_id = store.find_entity_exact(obj) or store.resolve_entity(
            obj, "other", ts=now)
        if not obj_id:
            return

        speaker = (turn.speaker or "").strip() or None
        # When speaker_is_source is false and text names a reporter
        # ("David said…"), prefer that name as the attributed speaker.
        attributed = speaker
        sis = cl.get("speaker_is_source")
        if sis is False:
            import re
            m = re.search(
                r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+said\b",
                cl.get("text") or "")
            if m:
                attributed = m.group(1)

        try:
            kg_beliefs.record_from_claim(
                store,
                subj_type=subj_type, subj_id=subj_id,
                predicate=pred, obj_type="entity", obj_id=int(obj_id),
                fact_id=fact_id, source_event_id=anchor,
                confidence=cl.get("confidence"), ts=now,
                quote=(cl.get("source_span") or cl.get("text") or "")[:400],
                source_class=source_class,
                speaker=attributed,
                speaker_is_source=bool(sis) if sis is not None else None,
            )
        except Exception as exc:
            print(f"[extract] claim→belief skipped ({exc})")

    # --- backfill (populate entities on already-extracted turns) ----------
    def backfill_entities(self, *, limit: int | None = None, min_chars: int = 20,
                          verbose: bool = False) -> dict[str, int]:
        """Re-read existing turns and persist ONLY entities + relations — so the
        graph gets org/project nodes for speech captured before enrichment
        existed, WITHOUT duplicating the tasks/commitments already extracted.
        Idempotent, so it's safe to re-run. Skips short/fragment turns."""
        import time as _time

        store = self._ensure_store()
        now = _time.time()
        # Ensure turns exist (the backfill reads them). If the table is empty —
        # e.g. it was never consolidated in this DB, or a concurrent rebuild's
        # DELETE+INSERT window — rebuild them first so we never no-op silently.
        if store.turn_count() == 0:
            from app.services import consolidation
            consolidation.rebuild(store)
        turns = [t for t in store.recent_turns(100000)
                 if len((t.get("text") or "").strip()) >= min_chars]
        if limit:
            turns = turns[:limit]
        processed = ent_seen = rels = 0
        for t in turns:
            try:
                out = self._extract_text(t)  # speaker-labeled (plan 2.1)
            except Exception as exc:
                print(f"[backfill] LLM error ({exc}); skipping a turn.")
                continue
            anchor = (t.get("event_ids") or [None])[0]
            e, r = self._persist_entities(out, anchor, now)
            ent_seen += e
            rels += r
            processed += 1
            if verbose and (e or r):
                print(f"  +{e} ent +{r} rel :: {t['text'][:64]!r}")
        return {"turns_processed": processed, "entity_mentions": ent_seen,
                "relations": rels, "distinct_entities": len(store.all_entities())}

    # --- vision to-do ingestion (step 4: page -> DB tasks) ----------------
    def ingest_todo_items(self, items: list[str], *, title: str = "",
                          source_event_id: int | None = None,
                          confidences: list[float] | None = None,
                          ts: float | None = None) -> list[int]:
        """Turn the discrete items of a vision-detected to-do list into `task`
        facts, so a page held to the camera becomes standing open tasks (with a
        status lifecycle) — not a one-shot chat offer. Deduped against currently
        open tasks by normalized text, so re-showing the same page is a no-op.

        `confidences` (aligned 1:1 with `items`, the #6 per-item vision confidence)
        is stored per task, so a smudged line enters weaker than a crisp one and
        the action gate treats them differently — the vision twin of a spoken
        task's ASR confidence."""
        import time as _time

        store = self._ensure_store()
        now = ts if ts is not None else _time.time()
        existing = {(t["text"] or "").strip().lower() for t in store.open_tasks()}
        created: list[int] = []
        for i, it in enumerate(items):
            text = (it or "").strip()
            if not text or text.lower() in existing:
                continue
            conf = (confidences[i] if confidences and i < len(confidences)
                    and isinstance(confidences[i], (int, float)) else None)
            fid = store.add_task(
                text, source_event_id=source_event_id,
                source_span=(f"{title}: {text}" if title else text),
                confidence=conf, extracted_at=now)
            existing.add(text.lower())
            created.append(fid)
            _index_fact(store, fid, "task", text, now)
        return created

    # --- public API -------------------------------------------------------
    def run_once(self, *, verbose: bool = False) -> dict[str, Any]:
        """Extract one batch of settled, unextracted turns (bounded by
        EXTRACT_BATCH). `remaining` reports unextracted audio events still left,
        so the caller can re-enqueue to drain a backlog incrementally.
        `next_settle_in` is the seconds until an already-captured but not-yet-
        settled turn becomes extractable (None if none) — the caller uses it to
        schedule a nudge so the last thing said before a silence surfaces
        without waiting for the next sound."""
        store = self._ensure_store()
        now = time.time()
        turns, next_settle_in = self._pending_turns(now)
        if not turns:
            remaining = len(store.unextracted_events(
                limit=10000, modality=Modality.AUDIO.value))
            return {"turns": 0, "facts": 0, "events_marked": 0, "failed": 0,
                    "remaining": remaining, "next_settle_in": next_settle_in}

        from app.services import intent as _intent
        from app.services import utterance_router as _ur
        route = _intent.enabled()
        type_route = _ur.route_enabled()          # #6: opt-in, default off

        total_facts = 0
        marked: list[int] = []   # success / skip (status=ok)
        parked: list[int] = []   # failed after max attempts
        skipped = 0
        failed = 0
        for turn in turns:
            # #6 (opt-in): a DICTATION turn is verbatim content, not conversation
            # to mine for tasks — skip extraction (the raw transcript is still
            # stored). Off by default; only fires under QUILL_UTTERANCE_ROUTE=1.
            if type_route and _ur.classify(turn.text).type == _ur.DICTATION:
                marked.extend(turn.event_ids)
                skipped += 1
                if verbose:
                    print(f"[extract] skip (dictation): {turn.text[:70]!r}")
                continue
            # IntentRouter pre-filter: a settled turn with zero actionable/claim
            # signal produces no fact, so skip the LLM call and mark it done. Skip
            # is precision-only (verified 0 unsafe skips over all past turns), so
            # this never drops a real task — the extractor stays the arbiter for
            # anything carrying a signal.
            if route and not _intent.classify(turn.text).should_extract:
                marked.extend(turn.event_ids)
                skipped += 1
                if verbose:
                    print(f"[extract] skip (non-actionable): {turn.text[:70]!r}")
                continue
            try:
                facts = self._extract_text(turn)  # speaker-labeled (plan 2.1)
            except Exception as exc:
                attempts = store.bump_extract_attempts(turn.event_ids)
                if attempts >= EXTRACT_MAX_ATTEMPTS:
                    # Park: mark extracted as failed so consolidate/nudge cannot
                    # spin forever on a poisoned turn (plan 0.9).
                    store.park_extract_failed(turn.event_ids, now)
                    failed += 1
                    parked.extend(turn.event_ids)
                    print(f"[extract] LLM error on turn ({exc}); "
                          f"parked failed after {attempts} attempt(s).")
                else:
                    print(f"[extract] LLM error on turn ({exc}); "
                          f"leaving for retry ({attempts}/{EXTRACT_MAX_ATTEMPTS}).")
                continue
            n = self._persist(turn, facts, now)
            total_facts += n
            marked.extend(turn.event_ids)
            if verbose:
                print(f"[extract] turn ({turn.n_utterances} utt, "
                      f"{len(turn.event_ids)} ev) -> {n} fact(s): "
                      f"{turn.text[:70]!r}")
        store.mark_extracted(marked, now, status="ok")
        remaining = sum(1 for _, ev in store.unextracted_events(limit=10000)
                        if ev.modality == Modality.AUDIO)
        return {"turns": len(turns), "facts": total_facts,
                "events_marked": len(marked) + len(parked), "skipped": skipped,
                "failed": failed,
                "remaining": remaining, "next_settle_in": next_settle_in}


extractor = Extractor()
