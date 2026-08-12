"""Persistent storage for Mnemos's memory timeline.

* Events -> SQLite (`data/quill.db`), stdlib only.
* Raw audio utterances -> WAV files (`data/audio/<ts>.wav`), linked from the
  event's `meta["audio_path"]`.

Thread-safe: the audio worker thread writes events, so the connection is opened
with check_same_thread=False and guarded by a lock.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

from app.config import settings
from app.events import Event, Modality

_JSON_FIELDS = ("people", "tasks", "entities", "meta")

# Approval-binding window: a packet's executable args are only valid this long
# after minting (plan 0.3). Commit gates (0.4+) refuse once expires_at passes.
_PACKET_TTL_S = 900.0


def job_backoff_s(attempts: int, *, base_s: float | None = None,
                  cap_s: float | None = None) -> float:
    """Seconds to wait before a failed job is claimable again (plan 0.10).

    Exponential in the post-claim attempt count: attempt 1 → base^1, etc.
    """
    base = float(base_s if base_s is not None
                 else settings.worker.backoff_base_s)
    cap = float(cap_s if cap_s is not None else settings.worker.backoff_cap_s)
    n = max(1, int(attempts))
    try:
        wait = base ** n
    except OverflowError:
        wait = cap
    return float(min(cap, max(0.0, wait)))


def canonicalize_packet_fields(fields: dict | None) -> str:
    """Stable JSON for executable packet args — key-sorted, compact separators."""
    return json.dumps(fields or {}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def hash_packet_payload(fields: dict | None) -> str:
    """sha256 hex of canonicalize_packet_fields(fields). Used at record time and
    re-checked at the commit gate so drift fails closed."""
    return hashlib.sha256(
        canonicalize_packet_fields(fields).encode("utf-8")).hexdigest()


def _emb_to_blob(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _blob_to_emb(b):
    return np.frombuffer(b, dtype=np.float32) if b else None


class Store:
    def __init__(self, db_path: Path | None = None, audio_dir: Path | None = None) -> None:
        self.db_path = Path(db_path or settings.storage.db_path)
        self.audio_dir = Path(audio_dir or settings.storage.audio_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    time       REAL    NOT NULL,
                    modality   TEXT    NOT NULL,
                    raw        TEXT    NOT NULL,
                    summary    TEXT,
                    source     TEXT,
                    confidence REAL,
                    people     TEXT,
                    tasks      TEXT,
                    entities   TEXT,
                    meta       TEXT,
                    audio_path TEXT
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(time)")
            # Derived layer: consolidated conversational turns. event_ids /
            # audio_paths are JSON lists linking each turn back to its source
            # utterances (provenance). Rebuilt wholesale by the consolidation pass.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    start        REAL    NOT NULL,
                    end          REAL    NOT NULL,
                    speaker      TEXT,
                    text         TEXT    NOT NULL,
                    event_ids    TEXT,
                    audio_paths  TEXT,
                    n_utterances INTEGER
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_start ON turns(start)")

            # --- sessions (turns -> coherent conversation/work blocks) --------
            # One level above turns: adjacent turns separated by a long silence
            # are grouped into a session — the natural unit for session-level
            # summary/reflection and (later) session-intent routing. Derived and
            # rebuildable, like turns; member turn/event ids link back for provenance.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    start        REAL    NOT NULL,
                    end          REAL    NOT NULL,
                    speakers     TEXT,              -- JSON list of speakers seen
                    text         TEXT    NOT NULL,   -- concatenated turn texts
                    turn_ids     TEXT,              -- JSON list (informational)
                    event_ids    TEXT,              -- JSON list (flattened provenance)
                    n_turns      INTEGER,
                    n_utterances INTEGER,
                    -- Meeting Layer P1: calendar join (additive; NULL when ad-hoc)
                    calendar_event_id TEXT,         -- stable calendar uid key
                    meeting_meta TEXT               -- JSON: title, attendees[], organizer
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start)")
            # idx_sessions_cal is created after the guarded ALTER below — an
            # existing sessions table may lack calendar_event_id until then.

            # --- activities (desktop events -> app-focus blocks) --------------
            # The desktop analog of sessions: desktop.screen + desktop.click
            # events folded into "what was I doing?" blocks (one per app focus
            # stretch), with a window/focus trail and a short summary. Derived
            # and rebuildable, like turns/sessions; member event ids link back.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS activities (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    start         REAL    NOT NULL,
                    end           REAL    NOT NULL,
                    app           TEXT,           -- foreground application
                    windows       TEXT,           -- JSON focus trail (titles seen)
                    summary       TEXT    NOT NULL,
                    event_ids     TEXT,           -- JSON list (desktop provenance)
                    n_screens     INTEGER,
                    n_clicks      INTEGER,
                    -- Multimodal context join: co-timed audio/webcam events that
                    -- fall inside [start, end]. Kept in a separate JSON column so
                    -- desktop provenance (event_ids) stays distinguishable from
                    -- enrichment provenance.
                    n_audio       INTEGER,
                    n_webcam      INTEGER,
                    ctx_event_ids TEXT             -- JSON list (audio + webcam ids)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start)")

            # --- extracted facts (episodic events -> structured facts) --------
            # `facts` is the spine: every extracted fact is one row with a kind,
            # a foreign key back to the source event, and the exact source span
            # it came from (provenance for the approval gate / debugging).
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS facts (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind            TEXT    NOT NULL,   -- task | commitment | claim
                    text            TEXT,              -- claim text (tasks/commitments keep theirs in typed tables)
                    source_event_id INTEGER,           -- FK -> events.id
                    source_span     TEXT,              -- verbatim provenance quote ONLY (may be empty)
                    confidence      REAL,
                    extracted_at    REAL    NOT NULL,
                    FOREIGN KEY (source_event_id) REFERENCES events(id)
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_kind ON facts(kind)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_src ON facts(source_event_id)")

            # --- fact_candidates (plan 1.1): raw LLM extract rows before / beside
            # facts. Every output item lands here with prompt_version so goldens
            # and /console/trace can replay without depending on gate outcomes.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fact_candidates (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    turn_hash       TEXT    NOT NULL,
                    kind            TEXT    NOT NULL,   -- task|commitment|claim|entity|relation
                    payload_json    TEXT    NOT NULL,
                    source_span     TEXT,
                    speaker         TEXT,
                    assertion       TEXT,              -- filled by plan 1.3
                    confidence      REAL,
                    model           TEXT,
                    prompt_version  TEXT    NOT NULL,
                    schema_version  TEXT    NOT NULL,
                    status          TEXT    NOT NULL DEFAULT 'pending',
                    verdict_reason  TEXT,
                    source_event_id INTEGER,
                    correlation_id  TEXT,              -- plan 1.5: trace_chain lookup key
                    created_at      REAL    NOT NULL,
                    FOREIGN KEY (source_event_id) REFERENCES events(id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fc_turn ON fact_candidates(turn_hash)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fc_status ON fact_candidates(status)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fc_kind ON fact_candidates(kind)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fc_correlation "
                "ON fact_candidates(correlation_id)")

            # Claim paraphrase != provenance quote: pre-migration DBs stored a
            # claim's text IN source_span (add_claim substituted text when the
            # span was empty, silently breaking the verbatim invariant). The
            # span column was therefore the only surviving copy of the text —
            # move it over once, then span carries only real quotes.
            fcols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(facts)").fetchall()}
            if fcols and "text" not in fcols:
                self._conn.execute("ALTER TABLE facts ADD COLUMN text TEXT")
                self._conn.execute(
                    "UPDATE facts SET text = source_span WHERE kind = 'claim' "
                    "AND text IS NULL AND source_span IS NOT NULL AND source_span != ''")
                self._conn.commit()

            # People/entities are resolution targets: the Nth mention of "Chris"
            # should map to one row. `embedding` (npy bytes) is filled in step 3;
            # nullable now so step 1 works with exact-name matching only.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS people (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_name TEXT    NOT NULL,
                    aliases        TEXT,               -- JSON list of seen spellings
                    embedding      BLOB,
                    first_seen     REAL,
                    last_seen      REAL
                )
                """
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_people_name ON people(canonical_name)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_name TEXT    NOT NULL,
                    kind           TEXT,               -- project | org | place | thing | ...
                    aliases        TEXT,
                    embedding      BLOB,
                    first_seen     REAL,
                    last_seen      REAL
                )
                """
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_name ON entities(canonical_name)")

            # Structured person details (phone / email / role / …) the USER set
            # by hand in the People tab. One row per (person, field); the
            # fact_id points at the approved claim written alongside, so the
            # override stays traceable and chat grounding sees the same truth.
            # Memory-MINED values are computed on read, never stored here.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS person_attrs (
                    person_id  INTEGER NOT NULL,       -- FK -> people.id
                    key        TEXT    NOT NULL,       -- phone | email | role | ...
                    value      TEXT    NOT NULL,
                    fact_id    INTEGER,                -- FK -> facts.id (the claim)
                    updated_at REAL    NOT NULL,
                    PRIMARY KEY (person_id, key)
                )
                """
            )

            # Typed detail tables hang off a fact row (fact_id FK). A task carries
            # its own lifecycle (status), which is what lets the to-do watcher act
            # on "open tasks" and later mark them done.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    fact_id         INTEGER PRIMARY KEY,   -- FK -> facts.id
                    text            TEXT    NOT NULL,
                    owner_person_id INTEGER,               -- FK -> people.id
                    due             TEXT,
                    status          TEXT    NOT NULL DEFAULT 'open',  -- open|done|cancelled
                    FOREIGN KEY (fact_id) REFERENCES facts(id),
                    FOREIGN KEY (owner_person_id) REFERENCES people(id)
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS commitments (
                    fact_id        INTEGER PRIMARY KEY,   -- FK -> facts.id
                    text           TEXT    NOT NULL,
                    from_person_id INTEGER,               -- FK -> people.id
                    to_person_id   INTEGER,               -- FK -> people.id
                    due            TEXT,
                    status         TEXT    NOT NULL DEFAULT 'open',
                    -- Plan 4.1: rich lifecycle; `status` stays derived compat
                    -- (open|done|cancelled) for list_facts callers.
                    state          TEXT    NOT NULL DEFAULT 'detected',
                    completion_evidence_json TEXT,
                    last_surfaced  REAL,
                    counterparty_expects INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (fact_id) REFERENCES facts(id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS commitment_transitions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    fact_id         INTEGER NOT NULL,
                    from_state      TEXT    NOT NULL,
                    to_state        TEXT    NOT NULL,
                    reason          TEXT,
                    evidence_json   TEXT,
                    actor           TEXT,
                    created_at      REAL    NOT NULL,
                    FOREIGN KEY (fact_id) REFERENCES commitments(fact_id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cmt_tx_fact "
                "ON commitment_transitions(fact_id, created_at)")

            # --- durable job queue --------------------------------------------
            # The processing pipeline (consolidate, later extract) runs off this
            # table, not inline on the capture/request path: capture enqueues a
            # row, one background worker drains it, and a crash mid-job just
            # leaves a re-runnable row. One Postgres/queue, minus the Postgres.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind         TEXT    NOT NULL,          -- consolidate | extract | ...
                    payload      TEXT,                      -- JSON args (nullable)
                    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending|running|done|dead
                    attempts     INTEGER NOT NULL DEFAULT 0,
                    error        TEXT,
                    available_at REAL,                      -- backoff: claimable when <= now
                    created_at   REAL    NOT NULL,
                    updated_at   REAL    NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")

            # --- knowledge graph: typed edges between nodes -------------------
            # Nodes live in their own tables (people, entities, facts, events);
            # `relations` is the connective tissue — one row per typed edge, with
            # provenance (source_event_id) and a weight (co-occurrence count).
            # A node is addressed by (type, id): person|entity|fact|event.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS relations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    subj_type       TEXT    NOT NULL,
                    subj_id         INTEGER NOT NULL,
                    predicate       TEXT    NOT NULL,
                    obj_type        TEXT    NOT NULL,
                    obj_id          INTEGER NOT NULL,
                    weight          REAL    NOT NULL DEFAULT 1,
                    -- 'derived' edges are recomputed by graph.rebuild (mentions,
                    -- co-occurrence, provenance); 'asserted' edges come from the
                    -- extractor (works_at, part_of, …) and must survive a rebuild.
                    origin          TEXT    NOT NULL DEFAULT 'derived',
                    source_event_id INTEGER,
                    confidence      REAL,
                    created_at      REAL
                )
                """
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_edge ON "
                "relations(subj_type, subj_id, predicate, obj_type, obj_id)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rel_subj ON relations(subj_type, subj_id)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rel_obj ON relations(obj_type, obj_id)")

            # --- reflections: durable personal intelligence -------------------
            # Reflection turns stored facts into "what changed / what matters /
            # what's next" over a period. It mirrors the facts layer: a header row
            # (the reflection) plus one child row PER insight, so each insight is
            # individually reviewable (approve/edit/dismiss), carries its own
            # provenance (source_fact_ids), and can be converted to a task — the
            # same trust posture as the extractor's facts, not an opaque summary.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reflections (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope        TEXT    NOT NULL,   -- daily | weekly | monthly | project | person
                    subject_type TEXT,               -- global | project | person
                    subject_id   INTEGER,            -- entity.id / people.id when scoped, else NULL
                    period_start REAL,
                    period_end   REAL,
                    summary      TEXT,               -- one-line synthesis
                    model        TEXT,
                    confidence   REAL,
                    created_at   REAL    NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_refl_scope ON reflections(scope)")
            # One insight per row. `kind` is wide from day one (v1 emits a subset)
            # so weekly/monthly/project/person reflection is additive, not a reshape.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reflection_items (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    reflection_id     INTEGER NOT NULL,   -- FK -> reflections.id
                    kind              TEXT    NOT NULL,   -- change|pattern|risk|open_loop|
                                                          -- project_update|relationship_update|
                                                          -- policy|recommendation
                    text              TEXT    NOT NULL,
                    detail            TEXT,               -- the 'why' / recommended action
                    subject           TEXT,               -- person/project this item is about
                    confidence        REAL,
                    source_fact_ids   TEXT,               -- JSON array of fact ids (provenance)
                    review            TEXT,               -- NULL | approved | edited | dismissed
                    converted_fact_id INTEGER,            -- set when converted to a task
                    created_at        REAL    NOT NULL,
                    FOREIGN KEY (reflection_id) REFERENCES reflections(id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_refl_item_parent "
                "ON reflection_items(reflection_id)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_refl_item_review "
                "ON reflection_items(review)")

            # --- Phase 5 substrate: agent runs, action packets, feedback ------
            # The Personal Agent Layer sits above the browser/desktop agents.
            # These four tables make its work inspectable and evaluable: one row
            # per run, per compiled action packet, per step, and per human
            # verdict. Crucially, an `edit` verdict (the user correcting a draft)
            # is the richest training signal and previously evaporated — it now
            # lands in agent_feedback. New tables (not ALTERs), so they self-create
            # on the canonical DB with no migration.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal            TEXT    NOT NULL,
                    agent_type      TEXT,               -- browser | desktop | writing | ...
                    surface         TEXT,               -- browser | desktop | none
                    intent          TEXT,               -- from the routing envelope
                    risk_level      TEXT,               -- low | medium | high
                    status          TEXT    NOT NULL DEFAULT 'running',
                    dry_run         TEXT,               -- plan|navigate|draft|approval|full
                    source_fact_ids TEXT,               -- JSON list (provenance -> facts)
                    person_id       INTEGER,            -- FK -> people.id
                    project_id      INTEGER,            -- FK -> entities.id
                    correlation_id  TEXT,               -- plan 1.5: trace_chain lookup key
                    started_at      REAL    NOT NULL,
                    completed_at    REAL,
                    cost            REAL,
                    latency         REAL,
                    steps           INTEGER,
                    success_score   REAL,
                    failure_reason  TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_status ON agent_runs(status)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_started ON agent_runs(started_at)")

            # The structured, source-grounded unit the brain hands to the hands —
            # a superset of the browser agent's approval packet. `decision` is
            # NULL until the human weighs in (approve|edit|cancel).
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS action_packets (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_run_id      INTEGER,          -- FK -> agent_runs.id
                    goal              TEXT,
                    summary           TEXT,             -- one-line human summary
                    fields_json       TEXT,             -- action/to/subject/body/why/source
                    context_json      TEXT,             -- memories used to ground it
                    source_fact_ids   TEXT,             -- JSON list
                    approval_required INTEGER NOT NULL DEFAULT 1,
                    risk_level        TEXT,
                    suggested_agent   TEXT,
                    execution_surface TEXT,             -- browser | desktop
                    success_criteria  TEXT,             -- JSON list
                    fallback          TEXT,
                    decision          TEXT,             -- approve|edit|cancel (NULL=pending)
                    payload_hash      TEXT,             -- sha256 of canonical fields_json
                    expires_at        REAL,             -- created_at + TTL; commit refuses after
                    approved_at       REAL,             -- when human approved (button|typed)
                    approved_via      TEXT,             -- button | typed
                    executed_hash     TEXT,             -- hash actually committed (dup-send)
                    executed_at       REAL,             -- when verified send stamped (1h window)
                    created_at        REAL    NOT NULL,
                    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_packets_run ON action_packets(agent_run_id)")

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_steps (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_run_id  INTEGER,              -- FK -> agent_runs.id
                    step_index    INTEGER,
                    action_type   TEXT,                 -- click|type|request_approval|done|...
                    input         TEXT,                 -- JSON args (redacted)
                    output        TEXT,                 -- result detail
                    verification  TEXT,                 -- verify note / reason
                    status        TEXT,                 -- verified | failed | done | outcome_uncertain | ...
                    created_at    REAL    NOT NULL,
                    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_steps_run ON agent_steps(agent_run_id)")

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_feedback (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_run_id  INTEGER,              -- FK -> agent_runs.id
                    packet_id     INTEGER,              -- FK -> action_packets.id
                    feedback_type TEXT    NOT NULL,     -- approved|edited|cancelled|failed|useful|annoying
                    user_edit     TEXT,                 -- the revision instruction on 'edited'
                    notes         TEXT,
                    created_at    REAL    NOT NULL,
                    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_feedback_run ON agent_feedback(agent_run_id)")

            # --- audio pipeline telemetry (#9) --------------------------------
            # One row per utterance the capture loop handles — KEPT or DROPPED —
            # so the Audio Health console can separate "the audio was bad" from
            # "Whisper failed", chart latency, and watch drop/low-confidence rates.
            # Written best-effort off the audio thread; never on the request path.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audio_telemetry (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts                 REAL    NOT NULL,   -- recorded (published/drop) time
                    event_id           INTEGER,           -- FK -> events.id (NULL if dropped)
                    outcome            TEXT    NOT NULL,   -- kept | dropped
                    drop_reason        TEXT,              -- bad_audio|hallucination_phrase|duplicate|empty|asr_error|...
                    audio_duration_ms  REAL,
                    quality            TEXT,              -- good | noisy | bad (audio_quality)
                    snr_est            REAL,
                    rms                REAL,
                    clipping_pct       REAL,
                    speech_ratio       REAL,
                    model              TEXT,              -- whisper model id
                    asr_latency_ms     REAL,              -- transcribe wall-time
                    total_latency_ms   REAL,              -- speech-end -> published
                    queue_depth        INTEGER,           -- utterances waiting at dequeue
                    avg_logprob        REAL,
                    no_speech_prob     REAL,
                    low_confidence     INTEGER,
                    filter_verdict     TEXT,              -- ingest_filter reason (ok|low_confidence|...)
                    speaker            TEXT,
                    speaker_known      INTEGER,
                    speaker_confidence REAL,
                    char_count         INTEGER
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audiotele_ts ON audio_telemetry(ts)")

            # --- attention ledger (Cognitive OS Phase 0) ----------------------
            # One row per node SURFACED to the user by any attention consumer
            # (constellation field, chat grounding, offers), carrying the score
            # decomposition it was surfaced with. User reactions (pin / hide /
            # evidence dwell / miss) close the row via `outcome`. This is the
            # training data for learned ranking — instrument-only today.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attention_impressions (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts            REAL    NOT NULL,
                    node_type     TEXT    NOT NULL,   -- person | entity | fact
                    node_id       INTEGER NOT NULL,
                    surface       TEXT    NOT NULL,   -- field | grounding | offer | brief | horizon
                    layer         TEXT,               -- focus | periphery (field only)
                    score         REAL,               -- gravity today; P(need) later
                    decomposition TEXT,               -- json: per-term contributions
                    context_id    INTEGER,            -- FK -> context_snapshots.id
                    outcome       TEXT,               -- pin|unpin|hide|reclassify|click|dwell|miss|...
                    outcome_ts    REAL,
                    detail        TEXT                -- json, e.g. {"dwell_ms": 4200}
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_att_node "
                "ON attention_impressions(node_type, node_id, ts)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_att_ts "
                "ON attention_impressions(ts)")

            # What the user was inside of when impressions were logged — the
            # seed of the Now-Context (field v2). Sparse: at most one row per
            # snapshot interval; impressions reference the latest row.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS context_snapshots (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts            REAL    NOT NULL,
                    app           TEXT,               -- latest desktop activity line
                    calendar_next TEXT,               -- reserved (A2: calendar horizon)
                    mode          TEXT,               -- reserved (A2: mode inference)
                    seeds         TEXT                -- reserved (A2: seed weights json)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ctxsnap_ts ON context_snapshots(ts)")

            # Field constellation snapshots — lightweight ring buffer for
            # /field/diff (entered/left/rising/falling/aging). Not an archive;
            # the event log remains the archive. Retention pruned on write.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS field_snapshots (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    version       TEXT    NOT NULL,
                    ts            REAL    NOT NULL,
                    focus_ids     TEXT    NOT NULL,  -- json list
                    periphery_ids TEXT    NOT NULL,  -- json list
                    per_node      TEXT    NOT NULL   -- json {id: {gravity, due_ts?, last_seen_ts?, kind?}}
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_field_snap_ts ON field_snapshots(ts)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_field_snap_ver ON field_snapshots(version)")

            # Dirty marks for incremental graph rebuild (WS5). Extraction tags
            # touched nodes; rebuild(scope=dirty) re-derives only their neighborhood.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_dirty (
                    node_type TEXT NOT NULL,
                    node_id   INTEGER NOT NULL,
                    ts        REAL NOT NULL,
                    PRIMARY KEY (node_type, node_id)
                )
                """
            )

            # --- weekly self-report (Phase 0 harness) -------------------------
            # The subjective metrics nothing else can measure: perceived
            # cognitive-load relief, trust, and interruption quality — asked
            # once a week, 1..5 scales. The before-numbers the attention track
            # has to beat.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS self_reports (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts              REAL    NOT NULL,
                    load_score      INTEGER,   -- 1 worse .. 5 much lighter
                    trust_score     INTEGER,   -- 1 none .. 5 full
                    interrupt_score INTEGER,   -- 1 annoying .. 5 always welcome
                    note            TEXT
                )
                """
            )

            # --- edge dynamics (Track A2) -------------------------------------
            # Per-edge attention conductance, recomputed wholesale at graph
            # rebuild: edge class (obligation/social/...), PMI-normalized
            # co-occurrence strength, and the final conductance the spreading-
            # activation engine propagates through.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS edge_dynamics (
                    relation_id INTEGER PRIMARY KEY,   -- FK -> relations.id
                    class       TEXT,
                    pmi         REAL,
                    conductance REAL,
                    updated_at  REAL
                )
                """
            )

            # --- memory traces (Track A1) -------------------------------------
            # One row per attendable node: base-level access history (the K
            # newest access times exactly + a compressed older tail) and the
            # long-run value V. A/U/att_state/home are reserved for the
            # activation field (A2+) so this table never needs migrating.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS node_dynamics (
                    node_type      TEXT    NOT NULL,   -- person | entity | fact
                    node_id        INTEGER NOT NULL,
                    V              REAL    NOT NULL DEFAULT 0.35,
                    A              REAL    NOT NULL DEFAULT 0,
                    A_ts           REAL,
                    U              REAL    NOT NULL DEFAULT 0,
                    att_state      TEXT    NOT NULL DEFAULT 'dormant',
                    prospective    INTEGER NOT NULL DEFAULT 0,
                    access_recent  TEXT,               -- json: newest K access times
                    access_n_older INTEGER NOT NULL DEFAULT 0,
                    access_t_older REAL,               -- mean ts of the folded tail
                    home_x         REAL,
                    home_y         REAL,
                    updated_at     REAL,
                    PRIMARY KEY (node_type, node_id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_nd_state "
                "ON node_dynamics(att_state)")

            # --- attention replay runs (Track A1 nightly gate) ----------------
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attention_replay_runs (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         REAL    NOT NULL,
                    days       REAL,
                    gate       REAL,
                    status     TEXT,          -- pass | fail | insufficient
                    passed     INTEGER,       -- 1/0/NULL
                    renders    INTEGER,
                    mean_tau   REAL,
                    min_tau    REAL,
                    max_tau    REAL,
                    detail     TEXT           -- json blob of the full result
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attn_replay_ts "
                "ON attention_replay_runs(ts)")

            # --- working memory slots (Track A3) ------------------------------
            # Persisted focus set so field + grounding + planner share one
            # attention state across restarts (Field §11 / §14).
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wm_slots (
                    slot         INTEGER PRIMARY KEY,  -- 0..11
                    node_type    TEXT,
                    node_id      INTEGER,
                    entered_at   REAL,
                    score        REAL,
                    cluster_head INTEGER DEFAULT 0,
                    cluster_n    INTEGER DEFAULT 1,
                    reason       TEXT                  -- json: why + label + members
                )
                """
            )

            # --- ranking model (Track A4) -------------------------------------
            # Versioned β weights for learned ranking. Active row is the one
            # with status='active'. Priors come from GRAVITY; kill switch
            # QUILL_ATTENTION_LEARN=0 freezes at prior.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ranking_model (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          REAL    NOT NULL,
                    version     TEXT,
                    status      TEXT    NOT NULL DEFAULT 'active',
                    beta        TEXT    NOT NULL,   -- json: feature -> weight
                    beta_var    TEXT,               -- json: feature -> variance
                    prior       TEXT,               -- json: shipped prior snapshot
                    n_updates   INTEGER DEFAULT 0,
                    drift       REAL,
                    note        TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ranking_status "
                "ON ranking_model(status, ts)")

            # --- attention predictions / horizon (Track A4) -------------------
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attention_predictions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          REAL    NOT NULL,
                    node_type   TEXT,
                    node_id     INTEGER,
                    p_need      REAL,
                    when_s      REAL,              -- seconds until expected need
                    reason      TEXT,              -- json: human reasons
                    source      TEXT,              -- calendar | hazard | rhythm
                    dismissed   INTEGER DEFAULT 0,
                    event_key   TEXT               -- calendar event identity
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attn_pred_ts "
                "ON attention_predictions(ts)")

            # --- ranking promote runs (Track A4 nightly gate) -----------------
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ranking_promote_runs (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         REAL    NOT NULL,
                    days       REAL,
                    status     TEXT,          -- promote | hold | insufficient
                    promoted   INTEGER,       -- 1/0
                    n_labeled  INTEGER,
                    prior_acc  REAL,
                    cand_acc   REAL,
                    prior_ll   REAL,
                    cand_ll    REAL,
                    reason     TEXT,
                    detail     TEXT           -- json blob of the full result
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rank_promote_ts "
                "ON ranking_promote_runs(ts)")

            # --- entity details (Track B) -------------------------------------
            # User-asserted attribute overrides for entities — the person_attrs
            # pattern generalized: per-attribute value, the APPROVED claim that
            # backs it, and when it was asserted. Mined values are computed on
            # read (services/entity_details.py), never stored.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_attrs (
                    entity_id  INTEGER NOT NULL,
                    key        TEXT    NOT NULL,   -- status | owner | url | location
                    value      TEXT    NOT NULL,
                    fact_id    INTEGER,            -- FK -> facts.id (backing claim)
                    updated_at REAL,
                    PRIMARY KEY (entity_id, key)
                )
                """
            )

            # --- memory economy (Track C) ---------------------------------------
            # Compacted events keep a span-preserving stub in `events.raw` (I-1);
            # the ORIGINAL row is copied here first, verbatim JSON, so compaction
            # is fully reversible. Nothing is ever deleted in v1.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events_archive (
                    event_id    INTEGER PRIMARY KEY,   -- FK -> events.id
                    archived_at REAL    NOT NULL,
                    row         TEXT    NOT NULL       -- full original row, JSON
                )
                """
            )
            # Storage-growth curve — the memory economy's first-class metric
            # (roadmap C2 exit: sublinear growth over a 4-week window).
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS storage_growth (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          REAL    NOT NULL,
                    db_bytes    INTEGER,
                    lance_bytes INTEGER,
                    n_events    INTEGER,
                    n_facts     INTEGER,
                    n_turns     INTEGER,
                    n_compacted INTEGER
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_storage_growth_ts "
                "ON storage_growth(ts)")
            # Sweep audit trail (ranking_promote_runs pattern) — powers due_for().
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS economy_runs (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts      REAL    NOT NULL,
                    scored  INTEGER,
                    absorbed INTEGER,
                    candidates INTEGER,
                    compacted  INTEGER,
                    detail  TEXT                      -- json blob of the result
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_economy_runs_ts "
                "ON economy_runs(ts)")

            # --- learned predictors + hardening (Track F) -----------------------
            # Registry of which model answers each prediction task. Exactly one
            # active row per task; heuristics are seeded as v1. A learned model
            # becomes active ONLY via the bench promote gate, and rollback just
            # re-activates the previous row.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS predictor_models (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    task         TEXT    NOT NULL,   -- next_app | next_contact | next_document
                    version      TEXT    NOT NULL,
                    kind         TEXT    NOT NULL,   -- heuristic | learned
                    active       INTEGER NOT NULL DEFAULT 0,
                    activated_at REAL,
                    metrics      TEXT,               -- json: last bench result
                    note         TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_predictor_models_task "
                "ON predictor_models(task, active)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS predictor_bench_runs (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       REAL    NOT NULL,
                    task     TEXT    NOT NULL,
                    model    TEXT,
                    status   TEXT,          -- ok | insufficient
                    n_points INTEGER,
                    hit1     REAL,
                    hit3     REAL,
                    mrr      REAL,
                    detail   TEXT           -- json blob of the full result
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_predictor_bench_ts "
                "ON predictor_bench_runs(ts)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hardening_runs (
                    id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts     REAL    NOT NULL,
                    kind   TEXT    NOT NULL,   -- restore_drill | ...
                    ok     INTEGER,
                    detail TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hardening_runs_ts "
                "ON hardening_runs(ts)")

            # --- Knowledge Graph v2 (KG-A): belief + evidence bags ------------
            # Predicates are temporal beliefs; evidence is append-only provenance.
            # Dual-written beside `relations` so constellation keeps working.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_predicates (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    subj_type       TEXT    NOT NULL,
                    subj_id         INTEGER NOT NULL,
                    predicate       TEXT    NOT NULL,
                    obj_type        TEXT    NOT NULL,
                    obj_id          INTEGER NOT NULL,
                    layer           TEXT    NOT NULL DEFAULT 'asserted',
                    confidence      REAL    NOT NULL DEFAULT 0.5,
                    valid_from      REAL,
                    valid_to        REAL,
                    first_seen      REAL    NOT NULL,
                    last_seen       REAL    NOT NULL,
                    status          TEXT    NOT NULL DEFAULT 'active',
                    superseded_by   INTEGER,
                    protected       INTEGER NOT NULL DEFAULT 0,
                    relation_key    TEXT,              -- legacy relations join key
                    created_at      REAL    NOT NULL,
                    updated_at      REAL    NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kg_pred_subj "
                "ON kg_predicates(subj_type, subj_id, status)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kg_pred_edge "
                "ON kg_predicates(subj_type, subj_id, predicate, obj_type, obj_id, status)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_evidence (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    predicate_id    INTEGER NOT NULL,
                    event_id        INTEGER,
                    fact_id         INTEGER,
                    modality        TEXT,
                    source_class    TEXT,
                    quote           TEXT,
                    quote_hash      TEXT,
                    extractor_conf  REAL,
                    faithfulness    REAL,
                    observed_at     REAL    NOT NULL,
                    weight          REAL    NOT NULL DEFAULT 1.0,
                    created_by      TEXT    NOT NULL DEFAULT 'system',
                    meta_json       TEXT,
                    FOREIGN KEY (predicate_id) REFERENCES kg_predicates(id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kg_ev_pred "
                "ON kg_evidence(predicate_id, observed_at)")
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_ev_dedupe "
                "ON kg_evidence(predicate_id, event_id, quote_hash)")

            # KG v2 Change 1: blocking keys. Identity is the node's opaque
            # canonical_id (people/entities column, random hex — NEVER parsed
            # for meaning); normalized names/aliases live here as lookup hints.
            # PK includes node_id: the same key value MAY map to several nodes
            # (that ambiguity is what the resolver adjudicates). Rows are
            # append-only; merges COPY the loser's keys to the winner.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_node_keys (
                    node_type  TEXT NOT NULL,   -- person | entity
                    node_id    INTEGER NOT NULL,
                    key_type   TEXT NOT NULL,   -- norm_name | phonetic | domain | alias_norm
                    key_value  TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (node_type, key_type, key_value, node_id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_node_keys_lookup "
                "ON kg_node_keys(node_type, key_type, key_value)")

            # KG v2 Change 2: temporal node attributes. valid_from IS NULL
            # means atemporal ("always/unknown"). SQLite permits NULLs inside
            # ordinary-table PRIMARY KEYs and treats them as distinct, so a
            # (node,key,valid_from) PK would NOT deduplicate atemporal rows —
            # hence the surrogate id + expression unique index below, which
            # folds NULL to the -1 sentinel for the uniqueness contract only
            # (queries keep readable NULL semantics).
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_node_attrs (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_type  TEXT    NOT NULL,   -- person | entity
                    node_id    INTEGER NOT NULL,
                    key        TEXT    NOT NULL,
                    value      TEXT    NOT NULL,
                    confidence REAL,
                    valid_from REAL,               -- NULL = atemporal
                    valid_to   REAL,
                    created_at REAL    NOT NULL,
                    updated_at REAL    NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_attr_temporal "
                "ON kg_node_attrs(node_type, node_id, key, "
                "ifnull(valid_from, -1))")

            # KG v2 Change 4: adjudication log — every human/auto decision on
            # the graph (evidence verdicts, merges, splits, conflict calls)
            # with the FULL feature context frozen at decision time. This is
            # the weight-fitting flywheel. LOCAL-ONLY (I-8): this table must
            # never sync or export — it is on the telemetry/export denylist.
            # features_json is a frozen snapshot; never recompute retroactively.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_adjudications (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind          TEXT NOT NULL,  -- evidence_confirm|evidence_reject|merge_accept|merge_reject|split_accept|split_reject|conflict_flag|conflict_both_true|belief_lock
                    predicate_id  INTEGER,
                    evidence_id   INTEGER,
                    node_a        INTEGER,
                    node_b        INTEGER,
                    features_json TEXT NOT NULL,  -- FROZEN feature vector at decision time
                    model_score   REAL,           -- what the system believed pre-decision
                    decision      TEXT NOT NULL,  -- accept|reject|defer|both_true
                    decided_by    TEXT NOT NULL,  -- user|auto
                    created_at    REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kg_adj_kind "
                "ON kg_adjudications(kind, created_at)")

            # KG v2 Change 8: nightly dual-write parity reports (shadow period
            # only). Report-only — the job NEVER auto-repairs (I-2); cutover
            # (M3) is gated on 7 consecutive zero-critical reports.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_parity_reports (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          REAL    NOT NULL,
                    critical    INTEGER NOT NULL DEFAULT 0,
                    report_json TEXT    NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kg_parity_ts "
                "ON kg_parity_reports(ts)")

            # KG v2 Change 4: versioned config (source weights etc.) so fitted
            # values can ship without a code change. version bumps on every set.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_config (
                    key        TEXT    PRIMARY KEY,
                    version    INTEGER NOT NULL DEFAULT 1,
                    value_json TEXT    NOT NULL,
                    updated_at REAL    NOT NULL
                )
                """
            )

            # Standing triggers ("when it sees X it does Y") — DATA, not code:
            # the engine (services/triggers/) is identical for every user; each
            # row is one user-specific behavior. `condition`/`action`/`gating`/
            # `stats`/`provenance` are JSON blobs so the vocabulary can grow
            # without migrations. Rows are never hard-deleted from the UI —
            # `retired` keeps the pattern_key visible to the miner so a
            # dismissed suggestion is a durable negative example.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS triggers (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT    NOT NULL,
                    origin      TEXT    NOT NULL DEFAULT 'custom',  -- custom | suggested | builtin
                    status      TEXT    NOT NULL DEFAULT 'active',  -- active | suggested | paused | retired
                    signal      TEXT    NOT NULL,   -- name from triggers/signals.py catalog
                    condition   TEXT,               -- JSON predicates (entity/person/text_any/app)
                    action      TEXT    NOT NULL,   -- JSON {verb, goal, ...} — targets bound HERE, at authoring
                    gating      TEXT,               -- JSON {cooldown_s, max_band}
                    stats       TEXT,               -- JSON {fires, offers, accepts, dismisses}
                    provenance  TEXT,               -- JSON {source, utterance, pattern_key}
                    created_at  REAL    NOT NULL,
                    updated_at  REAL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_triggers_status "
                "ON triggers(status, signal)")

            self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Additive, idempotent migrations for DBs created before a column existed.

        SQLite only supports ADD COLUMN, which is all we need here: mark which
        events have already been through the extractor so it never re-processes
        them. Keyed on events (not turns) because turns are rebuilt wholesale.
        """
        with self._lock:
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(events)").fetchall()}
            if "extracted_at" not in cols:
                self._conn.execute("ALTER TABLE events ADD COLUMN extracted_at REAL")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_extracted "
                    "ON events(extracted_at)")
                self._conn.commit()
            # Extract retry cap (plan 0.9): count LLM failures per event; after
            # max attempts the turn is parked extract_status='failed' so a
            # poisoned transcript can't spin the extract/nudge loop forever.
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(events)").fetchall()}
            if "extract_attempts" not in cols:
                self._conn.execute(
                    "ALTER TABLE events ADD COLUMN extract_attempts "
                    "INTEGER NOT NULL DEFAULT 0")
            if "extract_status" not in cols:
                self._conn.execute(
                    "ALTER TABLE events ADD COLUMN extract_status TEXT")
            if "extract_attempts" not in cols or "extract_status" not in cols:
                self._conn.commit()
            # Track C lifecycle: NULL means 'fresh' (pre-economy rows stay valid
            # without a backfill UPDATE). retention/retention_ts are the nightly
            # score — metadata only, retrieval never filters on them in v1.
            if "lifecycle" not in cols:
                self._conn.execute("ALTER TABLE events ADD COLUMN lifecycle TEXT")
                self._conn.execute("ALTER TABLE events ADD COLUMN retention REAL")
                self._conn.execute(
                    "ALTER TABLE events ADD COLUMN retention_ts REAL")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_lifecycle "
                    "ON events(lifecycle)")
                self._conn.commit()
            # facts.review: the human verdict from the Console (approve/edit/dismiss).
            # NULL = not yet reviewed; the training signal that makes facts trustworthy.
            fcols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(facts)").fetchall()}
            if fcols and "review" not in fcols:
                self._conn.execute("ALTER TABLE facts ADD COLUMN review TEXT")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_facts_review ON facts(review)")
                self._conn.commit()
            # facts lifecycle (memory hygiene): `state` marks a fact active or
            # superseded-by-a-newer-fact, `superseded_by` points at its
            # replacement, and `updated_at` is the last time the fact was
            # (re)asserted — the recency signal retrieval ranks on. Backfilled
            # from extracted_at so old rows sort sanely.
            if fcols and "state" not in fcols:
                self._conn.execute(
                    "ALTER TABLE facts ADD COLUMN state TEXT NOT NULL "
                    "DEFAULT 'active'")
                self._conn.execute(
                    "ALTER TABLE facts ADD COLUMN superseded_by INTEGER")
                self._conn.execute("ALTER TABLE facts ADD COLUMN updated_at REAL")
                self._conn.execute(
                    "UPDATE facts SET updated_at = extracted_at "
                    "WHERE updated_at IS NULL")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_facts_state ON facts(state)")
                self._conn.commit()
            # relations.origin: added after the table shipped, so a DB created with
            # the first `relations` schema needs the column back-filled ('derived').
            rcols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(relations)").fetchall()}
            if rcols and "origin" not in rcols:
                self._conn.execute(
                    "ALTER TABLE relations ADD COLUMN origin TEXT NOT NULL "
                    "DEFAULT 'derived'")
                self._conn.commit()
            # activities multimodal columns: the table shipped desktop-only, so a
            # live DB created before the audio/webcam join needs the columns added
            # (SQLite has no IF NOT EXISTS for columns — hence the PRAGMA check).
            acols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(activities)").fetchall()}
            for col, decl in (("n_audio", "INTEGER"), ("n_webcam", "INTEGER"),
                              ("ctx_event_ids", "TEXT")):
                if acols and col not in acols:
                    self._conn.execute(
                        f"ALTER TABLE activities ADD COLUMN {col} {decl}")
                    self._conn.commit()

            # People Intelligence v2 — promotion / actor / soft-merge columns.
            pcols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(people)").fetchall()}
            for col, decl in (
                ("actor_type", "TEXT NOT NULL DEFAULT 'human_person'"),
                ("promotion_state", "TEXT NOT NULL DEFAULT 'candidate'"),
                ("canonical_person_id", "INTEGER"),
                ("public_figure", "INTEGER NOT NULL DEFAULT 0"),
                ("hide_from_people", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if pcols and col not in pcols:
                    self._conn.execute(
                        f"ALTER TABLE people ADD COLUMN {col} {decl}")
                    self._conn.commit()

            # KG v2 Change 1: opaque canonical identity. canonical_id is a
            # random 128-bit hex minted at create/backfill time; it carries NO
            # semantics (never derived from the name, never parsed). Merge
            # keys must not mean anything — semantics live in kg_node_keys.
            for tbl, tcols in (("people", pcols), ("entities", None)):
                if tcols is None:
                    tcols = {r["name"] for r in self._conn.execute(
                        f"PRAGMA table_info({tbl})").fetchall()}
                if tcols and "canonical_id" not in tcols:
                    self._conn.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN canonical_id TEXT")
                    self._conn.commit()
                self._conn.execute(
                    f"UPDATE {tbl} SET canonical_id = lower(hex(randomblob(16))) "
                    "WHERE canonical_id IS NULL")
                self._conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{tbl}_canonical_id "
                    f"ON {tbl}(canonical_id)")
                self._conn.commit()

            # KG v2 Change 2: kg_evidence dedupe index had nullable columns
            # (event_id, quote_hash) inside a UNIQUE index — NULLs compare
            # distinct, so dupes could slip in. One-shot: collapse existing
            # duplicates (keep the highest-weight row, then lowest id — evidence
            # is append-only so nothing else is mutated), then swap the index
            # for an expression index that folds NULL to sentinels.
            has_old_ev_idx = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='idx_kg_ev_dedupe'").fetchone()
            if has_old_ev_idx:
                dupes = self._conn.execute(
                    "SELECT predicate_id, ifnull(event_id,0) AS e, "
                    "ifnull(quote_hash,'') AS q, COUNT(*) AS n "
                    "FROM kg_evidence GROUP BY 1,2,3 HAVING n > 1").fetchall()
                for d in dupes:
                    keep = self._conn.execute(
                        "SELECT id FROM kg_evidence WHERE predicate_id=? "
                        "AND ifnull(event_id,0)=? AND ifnull(quote_hash,'')=? "
                        "ORDER BY weight DESC, id ASC LIMIT 1",
                        (d["predicate_id"], d["e"], d["q"])).fetchone()
                    gone = self._conn.execute(
                        "DELETE FROM kg_evidence WHERE predicate_id=? "
                        "AND ifnull(event_id,0)=? AND ifnull(quote_hash,'')=? "
                        "AND id != ?",
                        (d["predicate_id"], d["e"], d["q"], keep["id"])).rowcount
                    print(f"[kg-migrate] collapsed {gone} duplicate evidence "
                          f"rows on predicate {d['predicate_id']} "
                          f"(kept id {keep['id']}).")
                self._conn.execute("DROP INDEX idx_kg_ev_dedupe")
                self._conn.commit()
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_kg_ev_dedupe "
                "ON kg_evidence(predicate_id, ifnull(event_id,0), "
                "ifnull(quote_hash,''))")
            self._conn.commit()

            # KG v2 Change 3: conflict flag — set ONLY for the simultaneous
            # pattern (overlapping evidence windows); the symmetric penalty in
            # recompute is conditional on it. Sequential conflicts become
            # temporal splits and never penalize either belief.
            kcols = {r["name"] for r in self._conn.execute(
                "PRAGMA table_info(kg_predicates)").fetchall()}
            if kcols and "conflict" not in kcols:
                self._conn.execute(
                    "ALTER TABLE kg_predicates ADD COLUMN conflict "
                    "INTEGER NOT NULL DEFAULT 0")
                self._conn.commit()
            # KG v2 Change 5: lazy posteriors. Intake only flips
            # posterior_stale; the math runs on read or in the batch sweep.
            # logit_sum caches the time-invariant Σ evidence terms (decay is a
            # function of `now`, so a "final" posterior is never stored);
            # weights_version records which source-weight table version the
            # sum was built with — a bump forces a full re-scan.
            for col, decl in (
                ("posterior_stale", "INTEGER NOT NULL DEFAULT 0"),
                ("logit_sum", "REAL"),
                ("computed_at", "REAL"),
                ("weights_version", "INTEGER"),
            ):
                if kcols and col not in kcols:
                    self._conn.execute(
                        f"ALTER TABLE kg_predicates ADD COLUMN {col} {decl}")
                    self._conn.commit()
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_kg_pred_stale "
                "ON kg_predicates(posterior_stale)")
            self._conn.commit()

            # Soft-hide for ambient/news orgs & tools (KG ambient cleanup).
            ecols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(entities)").fetchall()}
            if ecols and "hidden" not in ecols:
                self._conn.execute(
                    "ALTER TABLE entities ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_entities_hidden ON entities(hidden)")
                self._conn.commit()

            # Approval binding (plan 0.3): hash + expiry on every packet so the
            # commit gate can refuse drift/stale approvals. Additive only —
            # pre-migration rows keep NULL hash/expiry (gates treat missing as
            # unbound legacy; new packets always mint both).
            apcols = {r["name"] for r in
                      self._conn.execute("PRAGMA table_info(action_packets)").fetchall()}
            if apcols:
                for col, decl in (
                    ("payload_hash", "TEXT"),
                    ("expires_at", "REAL"),
                    ("approved_at", "REAL"),
                    ("approved_via", "TEXT"),
                    ("executed_hash", "TEXT"),
                    ("executed_at", "REAL"),
                ):
                    if col not in apcols:
                        self._conn.execute(
                            f"ALTER TABLE action_packets ADD COLUMN {col} {decl}")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_packets_executed_hash "
                    "ON action_packets(executed_hash)")
                self._conn.commit()

            # Jobs dead-letter (plan 0.10): available_at for backoff; park
            # exhausted retries as status='dead' (legacy 'error' renamed).
            jcols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if jcols and "available_at" not in jcols:
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN available_at REAL")
            if jcols:
                self._conn.execute(
                    "UPDATE jobs SET status = 'dead' WHERE status = 'error'")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_available "
                    "ON jobs(status, available_at)")
                self._conn.commit()

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS person_mentions (
                    mention_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    raw_text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    discourse_role TEXT NOT NULL DEFAULT 'unknown',
                    grammatical_role TEXT NOT NULL DEFAULT 'unknown',
                    observed_at REAL NOT NULL,
                    extractor_version TEXT NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    person_probability REAL,
                    extraction_confidence REAL,
                    actor_types TEXT,
                    identity_hints TEXT,
                    resolution_status TEXT NOT NULL DEFAULT 'unresolved',
                    resolved_person_id INTEGER,
                    resolution_confidence REAL,
                    relationship_relevance REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pm_event ON person_mentions(event_id)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pm_norm ON person_mentions(normalized_text)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pm_person ON person_mentions(resolved_person_id)")

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS identity_candidates (
                    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mention_id INTEGER NOT NULL,
                    person_id INTEGER,
                    is_new INTEGER NOT NULL DEFAULT 0,
                    score REAL NOT NULL,
                    rank INTEGER NOT NULL,
                    pos_evidence TEXT,
                    neg_evidence TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ic_mention ON identity_candidates(mention_id)")

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS resolution_decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mention_id INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    chosen_person_id INTEGER,
                    confidence REAL NOT NULL,
                    threshold_policy TEXT,
                    resolver_version TEXT NOT NULL,
                    decided_at REAL NOT NULL,
                    actor TEXT NOT NULL DEFAULT 'system'
                )
                """
            )

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS person_contact_points (
                    contact_point_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    value_normalized TEXT NOT NULL,
                    value_display TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    attribution_method TEXT NOT NULL,
                    verification_status TEXT NOT NULL DEFAULT 'unverified',
                    source_event_id INTEGER,
                    evidence_quote TEXT,
                    discourse_role TEXT,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    valid_from REAL,
                    valid_to REAL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT NOT NULL,
                    supersedes_id INTEGER,
                    pipeline_version TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pcp_person ON person_contact_points(person_id)")

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS merge_operations (
                    merge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    survivor_person_id INTEGER NOT NULL,
                    absorbed_person_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    reason TEXT,
                    confidence REAL,
                    resolver_version TEXT,
                    actor TEXT NOT NULL,
                    decided_at REAL NOT NULL,
                    reversed_at REAL,
                    evidence_json TEXT
                )
                """
            )
            self._conn.commit()

            # correlation_id (plan 1.5): a live DB created before this column
            # existed needs it added; new writes always resolve/mint one.
            fccols = {r["name"] for r in
                      self._conn.execute("PRAGMA table_info(fact_candidates)").fetchall()}
            if fccols and "correlation_id" not in fccols:
                self._conn.execute(
                    "ALTER TABLE fact_candidates ADD COLUMN correlation_id TEXT")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_fc_correlation "
                    "ON fact_candidates(correlation_id)")
                self._conn.commit()
            arcols = {r["name"] for r in
                      self._conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
            if arcols and "correlation_id" not in arcols:
                self._conn.execute(
                    "ALTER TABLE agent_runs ADD COLUMN correlation_id TEXT")
                self._conn.commit()

            # Plan 4.1 — commitment state machine (additive).
            self._migrate_commitment_state()

            # Meeting Layer P1 — calendar ↔ session join columns (guarded ALTER,
            # entities.hidden precedent). New CREATE TABLE already includes them.
            scols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if scols:
                for col, decl in (
                    ("calendar_event_id", "TEXT"),
                    ("meeting_meta", "TEXT"),
                ):
                    if col not in scols:
                        self._conn.execute(
                            f"ALTER TABLE sessions ADD COLUMN {col} {decl}")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sessions_cal "
                    "ON sessions(calendar_event_id)")
                self._conn.commit()
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id              TEXT PRIMARY KEY,
                    calendar        TEXT,
                    uid             TEXT,
                    title           TEXT,
                    start           REAL    NOT NULL,
                    end             REAL    NOT NULL,
                    all_day         INTEGER NOT NULL DEFAULT 0,
                    location        TEXT,
                    organizer_json  TEXT,
                    attendees_json  TEXT,
                    source_event_id INTEGER,
                    updated_at      REAL    NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cal_events_start "
                "ON calendar_events(start)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cal_events_window "
                "ON calendar_events(start, end)")
            self._conn.commit()

            # --- People v3 P3 (WS-A): voice-track escrow ----------------------
            # A durable identity for an anonymous diarization track ("Speaker 3")
            # so evidence heard BEFORE the speaker is labeled can be escrowed
            # against the track and rebound retroactively. status: open (still
            # anonymous) | bound (a named person claimed it — bound_person_id).
            # A label may recur across restarts (cluster ids reset), so lookups
            # go through the newest OPEN track per label; binding closes it and
            # a later "Speaker 3" mints a fresh track.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS speaker_tracks (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    label           TEXT    NOT NULL,
                    status          TEXT    NOT NULL DEFAULT 'open',
                    bound_person_id INTEGER,            -- FK -> people.id
                    created_at      REAL    NOT NULL,
                    bound_at        REAL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_spktrack_label "
                "ON speaker_tracks(label, status)")
            # Rebind audit (merge_operations idiom): one append-only row per
            # rebind job run — what track, which person, how many rows moved.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS escrow_rebind_log (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id      INTEGER NOT NULL,
                    person_id     INTEGER NOT NULL,
                    n_facts       INTEGER NOT NULL DEFAULT 0,
                    n_tasks       INTEGER NOT NULL DEFAULT 0,
                    n_commitments INTEGER NOT NULL DEFAULT 0,
                    actor         TEXT,
                    reason        TEXT,
                    created_at    REAL    NOT NULL
                )
                """
            )
            # Guarded ALTERs (entities.hidden precedent): escrow attribution
            # columns on live DBs. Written/read ONLY by flag-gated escrow code —
            # default queries keep excluding escrowed rows via facts.state.
            fcols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(facts)").fetchall()}
            if fcols and "speaker_track_id" not in fcols:
                self._conn.execute(
                    "ALTER TABLE facts ADD COLUMN speaker_track_id INTEGER")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_facts_spktrack "
                    "ON facts(speaker_track_id)")
            tucols = {r["name"] for r in
                      self._conn.execute("PRAGMA table_info(turns)").fetchall()}
            if tucols and "speaker_track_id" not in tucols:
                self._conn.execute(
                    "ALTER TABLE turns ADD COLUMN speaker_track_id INTEGER")
            tkcols = {r["name"] for r in
                      self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if tkcols and "owner_track_id" not in tkcols:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN owner_track_id INTEGER")
            ccols = {r["name"] for r in
                     self._conn.execute("PRAGMA table_info(commitments)").fetchall()}
            if ccols and "from_track_id" not in ccols:
                self._conn.execute(
                    "ALTER TABLE commitments ADD COLUMN from_track_id INTEGER")
            self._conn.commit()

    def _migrate_commitment_state(self) -> None:
        """Add commitments.state + evidence columns and transitions log."""
        try:
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(commitments)").fetchall()}
        except Exception:
            return
        if not cols:
            return
        altered = False
        for col, decl in (
            ("state", "TEXT NOT NULL DEFAULT 'detected'"),
            ("completion_evidence_json", "TEXT"),
            ("last_surfaced", "REAL"),
            ("counterparty_expects", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE commitments ADD COLUMN {col} {decl}")
                altered = True
        if altered:
            # Backfill state from legacy status (SQLite DEFAULT fills 'detected').
            self._conn.execute(
                """
                UPDATE commitments SET state = CASE status
                    WHEN 'done' THEN 'completed'
                    WHEN 'cancelled' THEN 'cancelled'
                    ELSE 'active'
                END
                """
            )
            self._conn.execute(
                """
                UPDATE commitments SET state = 'superseded'
                WHERE status = 'cancelled' AND fact_id IN (
                    SELECT id FROM facts WHERE state = 'superseded'
                )
                """
            )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS commitment_transitions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_id         INTEGER NOT NULL,
                from_state      TEXT    NOT NULL,
                to_state        TEXT    NOT NULL,
                reason          TEXT,
                evidence_json   TEXT,
                actor           TEXT,
                created_at      REAL    NOT NULL,
                FOREIGN KEY (fact_id) REFERENCES commitments(fact_id)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cmt_tx_fact "
            "ON commitment_transitions(fact_id, created_at)")
        self._conn.commit()

    # ------------------------------ audio clips --------------------------
    def save_wav(self, audio: np.ndarray, ts: float, sample_rate: int,
                 *, suffix: str = "") -> str:
        """Write a float32 mono utterance to a 16-bit PCM WAV. Returns the path.
        `suffix` distinguishes companion clips for the same utterance — e.g. the
        enhanced/denoised copy (#12) is saved as `<ts>.enhanced.wav` alongside the
        raw `<ts>.wav`, so both are addressable provenance."""
        path = self.audio_dir / f"{ts:.3f}{suffix}.wav"
        pcm = np.clip(audio, -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype("<i2")
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16.tobytes())
        return str(path)

    # ------------------------------ events -------------------------------
    def insert(self, event: Event) -> int:
        # Plan 1.5: every event carries a correlation_id so a fact/candidate/
        # agent_run derived from it can be traced back with trace_chain().
        # Mutates the caller's meta in place so it sees the minted id too.
        if event.meta is None:
            event.meta = {}
        if not event.meta.get("correlation_id"):
            import uuid
            event.meta["correlation_id"] = uuid.uuid4().hex
        # Plan 6.1: deterministic privacy_class for egress gating.
        try:
            from app.services.privacy_class import stamp_event
            stamp_event(event)
        except Exception as exc:
            print(f"[storage] privacy_class stamp skipped ({exc}).")
            event.meta.setdefault("privacy_class", "internal")
        d = event.to_dict()
        row = {k: (json.dumps(d[k]) if k in _JSON_FIELDS else d[k]) for k in (
            "time", "modality", "raw", "summary", "source", "confidence",
            "people", "tasks", "entities", "meta",
        )}
        row["audio_path"] = d.get("meta", {}).get("audio_path")
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO events
                    (time, modality, raw, summary, source, confidence,
                     people, tasks, entities, meta, audio_path)
                VALUES
                    (:time, :modality, :raw, :summary, :source, :confidence,
                     :people, :tasks, :entities, :meta, :audio_path)
                """,
                row,
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def _row_to_event(self, r: sqlite3.Row) -> Event:
        return Event(
            time=r["time"],
            modality=Modality(r["modality"]),
            raw=r["raw"],
            summary=r["summary"] or "",
            source=r["source"] or "",
            confidence=r["confidence"],
            people=json.loads(r["people"] or "[]"),
            tasks=json.loads(r["tasks"] or "[]"),
            entities=json.loads(r["entities"] or "[]"),
            meta=json.loads(r["meta"] or "{}"),
        )

    def recent_events(self, *, source_substr: str | None = None,
                      limit: int = 80, since: float | None = None) -> list[dict]:
        """Newest events as plain dicts (calendar feeder / diagnostics)."""
        with self._lock:
            sql = ("SELECT id, time, modality, raw, summary, source, meta "
                   "FROM events WHERE 1=1")
            args: list = []
            if source_substr:
                sql += " AND source LIKE ?"
                args.append(f"%{source_substr}%")
            if since is not None:
                sql += " AND time >= ?"
                args.append(float(since))
            sql += " ORDER BY time DESC LIMIT ?"
            args.append(int(limit))
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            try:
                meta = json.loads(r["meta"] or "{}")
            except Exception:
                meta = {}
            out.append({
                "id": int(r["id"]), "time": r["time"],
                "modality": r["modality"], "raw": r["raw"] or "",
                "summary": r["summary"] or "", "source": r["source"] or "",
                "meta": meta,
            })
        return out

    def events_in_window(
        self, t0: float, t1: float, *, source: str | None = None,
        modality: str | None = None, limit: int = 100,
    ) -> list[dict]:
        """Events with time in [t0, t1], oldest-first (Meeting Layer P2 jots)."""
        with self._lock:
            sql = ("SELECT id, time, modality, raw, summary, source, meta "
                   "FROM events WHERE time >= ? AND time <= ?")
            args: list = [float(t0), float(t1)]
            if source:
                sql += " AND source = ?"
                args.append(source)
            if modality:
                sql += " AND modality = ?"
                args.append(modality)
            sql += " ORDER BY time ASC LIMIT ?"
            args.append(int(limit))
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            try:
                meta = json.loads(r["meta"] or "{}")
            except Exception:
                meta = {}
            out.append({
                "id": int(r["id"]), "time": r["time"],
                "modality": r["modality"], "raw": r["raw"] or "",
                "summary": r["summary"] or "", "source": r["source"] or "",
                "meta": meta,
            })
        return out

    def all(self) -> list[Event]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY time").fetchall()
        return [self._row_to_event(r) for r in rows]

    def all_with_ids(self) -> list[tuple[int, Event]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY time").fetchall()
        return [(int(r["id"]), self._row_to_event(r)) for r in rows]

    def append_provenance_correction(self, event_id: int, correction: dict) -> bool:
        """Append one correction to an event's provenance chain (#12), atomically:
        read the meta JSON, append to meta['provenance']['corrections'], write back —
        all under the lock so concurrent appends can't clobber each other. If the
        event has no provenance yet (e.g. captured before #12), a minimal chain is
        created so the correction is never dropped. Returns False if the event is
        gone. Best-effort caller (services/provenance.py) swallows exceptions."""
        with self._lock:
            row = self._conn.execute(
                "SELECT meta FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                return False
            try:
                meta = json.loads(row["meta"] or "{}")
            except (ValueError, TypeError):
                meta = {}
            prov = meta.get("provenance")
            if not isinstance(prov, dict):
                prov = {"corrections": []}
                meta["provenance"] = prov
            prov.setdefault("corrections", []).append(correction)
            self._conn.execute(
                "UPDATE events SET meta = ? WHERE id = ?",
                (json.dumps(meta), event_id))
            self._conn.commit()
            return True

    def by_ids_map(self, ids: list[int]) -> dict[int, Event]:
        """Fetch events by id, returned as an {id: Event} map."""
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders})", ids
            ).fetchall()
        return {int(r["id"]): self._row_to_event(r) for r in rows}

    def search(self, query: str, limit: int = 20) -> list[Event]:
        q = f"%{query.strip()}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE raw LIKE ? ORDER BY time DESC LIMIT ?",
                (q, limit),
            ).fetchall()
        return [self._row_to_event(r) for r in rows][::-1]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    # ------------------------------ extraction bookkeeping ---------------
    def unextracted_events(self, limit: int = 200,
                           modality: str | None = None,
                           source: str | None = None,
                           since: float | None = None) -> list[tuple[int, Event]]:
        """Oldest-first events not yet run through the extractor.

        Pass `modality` (e.g. 'audio') to filter in SQL. Without it, a modality
        the extractor never marks (vision frames) can fill the oldest-first
        window and starve the modality it does process — head-of-line blocking.

        Same trap one level deeper: pass `source` too when only one source of
        a modality gets mined. screen_extract wants vision/desktop.screen; a
        Python-side filter let thousands of never-marked webcam frames fill
        the whole window and starve every screen frame behind them (live bug,
        July 20 2026 — 1,390 screen frames stuck behind 3,782 webcam rows).

        `since` (epoch seconds) restricts to events at/after that time — the
        fresh-lane query, so new information can jump an old backlog.
        """
        sql = "SELECT * FROM events WHERE extracted_at IS NULL"
        params: list = []
        if modality is not None:
            sql += " AND modality = ?"
            params.append(modality)
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        if since is not None:
            sql += " AND time >= ?"
            params.append(since)
        sql += " ORDER BY time LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [(int(r["id"]), self._row_to_event(r)) for r in rows]

    def mark_extracted(self, event_ids: list[int], ts: float,
                       *, status: str = "ok") -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self._lock:
            self._conn.execute(
                f"UPDATE events SET extracted_at = ?, extract_status = ? "
                f"WHERE id IN ({placeholders})",
                [ts, status, *event_ids],
            )
            self._conn.commit()

    def bump_extract_attempts(self, event_ids: list[int]) -> int:
        """Increment extract_attempts for each id; return the max after bump."""
        if not event_ids:
            return 0
        placeholders = ",".join("?" for _ in event_ids)
        with self._lock:
            self._conn.execute(
                f"UPDATE events SET extract_attempts = extract_attempts + 1 "
                f"WHERE id IN ({placeholders})",
                event_ids,
            )
            row = self._conn.execute(
                f"SELECT MAX(extract_attempts) AS m FROM events "
                f"WHERE id IN ({placeholders})",
                event_ids,
            ).fetchone()
            self._conn.commit()
        return int(row["m"] or 0) if row is not None else 0

    def park_extract_failed(self, event_ids: list[int], ts: float) -> None:
        """Park a poisoned turn: mark extracted with status failed (no retry)."""
        self.mark_extracted(event_ids, ts, status="failed")

    def erase_event(self, event_id: int, *, vacuum: bool = False) -> dict:
        """Forget one event; citing facts become ``state='evidence_removed'``.

        Distinct from :meth:`erase_events_window` (privacy true-erasure that
        deletes fact rows). Memory forget keeps the fact shell so the removal
        is auditable; ``vector_gc`` drops the Lance row after the grace window.
        Returns ``{event_id, fact_ids, events, relations}``.
        """
        import time as _time
        eid = int(event_id)
        now = _time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM events WHERE id = ?", (eid,)).fetchone()
            if row is None:
                return {"event_id": eid, "fact_ids": [], "events": 0,
                        "relations": 0, "ok": False, "reason": "missing"}
            fact_ids = [int(r["id"]) for r in self._conn.execute(
                "SELECT id FROM facts WHERE source_event_id = ?",
                (eid,)).fetchall()]
            n_rel = self._conn.execute(
                "DELETE FROM relations WHERE "
                "(subj_type='event' AND subj_id = ?) OR "
                "(obj_type='event' AND obj_id = ?)",
                (eid, eid)).rowcount
            if fact_ids:
                marks = ",".join("?" for _ in fact_ids)
                self._conn.execute(
                    f"UPDATE facts SET state = 'evidence_removed', "
                    f"updated_at = ?, source_event_id = NULL "
                    f"WHERE id IN ({marks})",
                    [now, *fact_ids])
                self._conn.execute(
                    f"UPDATE tasks SET status = 'cancelled' "
                    f"WHERE fact_id IN ({marks}) AND status = 'open'",
                    fact_ids)
                for fid in fact_ids:
                    crow = self._conn.execute(
                        "SELECT state FROM commitments WHERE fact_id = ?",
                        (fid,)).fetchone()
                    if crow and (crow["state"] or "") in (
                            "detected", "active", "in_progress", "waiting"):
                        try:
                            self._transition_commitment_unlocked(
                                fid, "cancelled",
                                reason="evidence_removed",
                                evidence={"source": "erase_event",
                                          "event_id": eid},
                                actor="system", ts=now)
                        except Exception:
                            self._conn.execute(
                                "UPDATE commitments SET status = 'cancelled', "
                                "state = 'cancelled' WHERE fact_id = ?",
                                (fid,))
            # Drop any archived original for this event (compaction undo trail).
            try:
                self._conn.execute(
                    "DELETE FROM events_archive WHERE event_id = ?", (eid,))
            except Exception:
                pass
            n_events = self._conn.execute(
                "DELETE FROM events WHERE id = ?", (eid,)).rowcount
            self._conn.commit()
            if vacuum:
                try:
                    self._conn.execute("VACUUM")
                except Exception as exc:
                    print(f"[storage] post-erase VACUUM skipped ({exc}).")
        return {"event_id": eid, "fact_ids": fact_ids, "events": int(n_events),
                "relations": int(n_rel), "ok": True}

    def strip_event_audio(self, event_ids: list[int]) -> dict:
        """Meeting Layer P5 — delete WAV receipts; keep transcript text.

        Clears ``audio_path`` / enhanced paths on the event. Marks citing facts
        ``state='evidence_removed'`` for vector_gc, but does **not** cancel open
        commitments/tasks (ledger + note stay usable; playback is gone).
        """
        import time as _time
        from pathlib import Path as _Path

        ids = sorted({int(x) for x in (event_ids or []) if x is not None})
        if not ids:
            return {"ok": True, "n_files": 0, "n_events": 0, "n_facts": 0,
                    "paths": []}
        now = _time.time()
        removed_paths: list[str] = []
        n_events = 0
        fact_ids: list[int] = []
        with self._lock:
            marks = ",".join("?" for _ in ids)
            rows = self._conn.execute(
                f"SELECT id, meta, audio_path FROM events WHERE id IN ({marks})",
                ids).fetchall()
            for row in rows:
                try:
                    meta = json.loads(row["meta"] or "{}")
                except (ValueError, TypeError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                paths = []
                for key in ("audio_path", "enhanced_audio_path"):
                    p = meta.get(key) or ""
                    if p:
                        paths.append(str(p))
                    meta.pop(key, None)
                col_ap = row["audio_path"] if "audio_path" in row.keys() else None
                if col_ap:
                    paths.append(str(col_ap))
                meta["audio_stripped"] = True
                meta["audio_stripped_at"] = now
                self._conn.execute(
                    "UPDATE events SET meta = ?, audio_path = NULL WHERE id = ?",
                    (json.dumps(meta), int(row["id"])))
                n_events += 1
                for p in paths:
                    if p and p not in removed_paths:
                        removed_paths.append(p)
            # Mark citing facts evidence_removed (index GC) without cancelling
            # open commitments/tasks — transcript-only notes stay functional.
            frows = self._conn.execute(
                f"SELECT id FROM facts WHERE source_event_id IN ({marks})",
                ids).fetchall()
            fact_ids = [int(r["id"]) for r in frows]
            if fact_ids:
                fmarks = ",".join("?" for _ in fact_ids)
                self._conn.execute(
                    f"UPDATE facts SET state = 'evidence_removed', "
                    f"updated_at = ? WHERE id IN ({fmarks})",
                    [now, *fact_ids])
            # Drop audio_paths references on turns (best-effort JSON rewrite).
            try:
                turns = self._conn.execute(
                    "SELECT id, audio_paths FROM turns").fetchall()
                drop = set(removed_paths)
                for t in turns:
                    try:
                        aps = json.loads(t["audio_paths"] or "[]")
                    except Exception:
                        continue
                    if not aps:
                        continue
                    kept = [p for p in aps if p not in drop]
                    if len(kept) != len(aps):
                        self._conn.execute(
                            "UPDATE turns SET audio_paths = ? WHERE id = ?",
                            (json.dumps(kept), int(t["id"])))
            except Exception:
                pass
            self._conn.commit()
        n_files = 0
        for p in removed_paths:
            try:
                path = _Path(p)
                if path.is_file():
                    path.unlink()
                    n_files += 1
            except Exception as exc:
                print(f"[storage] strip audio unlink skipped ({p}: {exc}).")
        return {
            "ok": True, "n_files": n_files, "n_events": n_events,
            "n_facts": len(fact_ids), "fact_ids": fact_ids,
            "paths": removed_paths, "event_ids": ids,
        }

    def fact_ids_for_vector_gc(self, *, older_than: float,
                               limit: int = 5000) -> list[int]:
        """Fact ids whose store row is dismissed/superseded/archived/
        evidence_removed and whose lifecycle clock is at or before
        ``older_than`` (unix ts). Used by plan 6.6 ``vector_gc``."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id FROM facts
                WHERE (
                    state IN ('superseded', 'archived', 'evidence_removed')
                    OR review = 'dismissed'
                )
                AND COALESCE(updated_at, extracted_at) <= ?
                ORDER BY COALESCE(updated_at, extracted_at) ASC
                LIMIT ?
                """,
                (float(older_than), int(limit)),
            ).fetchall()
        return [int(r["id"]) for r in rows]

    def erase_events_window(self, t0: float, t1: float,
                            source_like: str = "desktop.%",
                            *, vacuum: bool = True) -> dict:
        """True-erasure cascade for events in [t0, t1) matching source_like:
        the events, facts derived from them, and relations touching either —
        deleted, not tombstoned. Returns the ids so the caller (perception
        erasure) can also drop LanceDB vectors and frame files. VACUUM clears
        freelist remnants so the deleted text does not survive in raw pages
        (skippable for bulk callers that vacuum once at the end)."""
        with self._lock:
            ev_rows = self._conn.execute(
                "SELECT id, meta, audio_path FROM events WHERE time >= ? AND "
                "time < ? AND source LIKE ?", (t0, t1, source_like)).fetchall()
            event_ids = [int(r["id"]) for r in ev_rows]
            frame_paths: list[str] = []
            for r in ev_rows:
                try:
                    meta = json.loads(r["meta"]) if r["meta"] else {}
                    fp = meta.get("frame_path")
                    if fp:
                        frame_paths.append(str(fp))
                except Exception:
                    continue
            fact_ids: list[int] = []
            n_events = n_facts = n_rel = 0
            if event_ids:
                marks = ",".join("?" for _ in event_ids)
                fact_ids = [int(r["id"]) for r in self._conn.execute(
                    f"SELECT id FROM facts WHERE source_event_id IN ({marks})",
                    event_ids).fetchall()]
                n_rel += self._conn.execute(
                    f"DELETE FROM relations WHERE "
                    f"(subj_type='event' AND subj_id IN ({marks})) OR "
                    f"(obj_type='event' AND obj_id IN ({marks}))",
                    event_ids + event_ids).rowcount
                if fact_ids:
                    fmarks = ",".join("?" for _ in fact_ids)
                    n_rel += self._conn.execute(
                        f"DELETE FROM relations WHERE "
                        f"(subj_type='fact' AND subj_id IN ({fmarks})) OR "
                        f"(obj_type='fact' AND obj_id IN ({fmarks}))",
                        fact_ids + fact_ids).rowcount
                    n_facts = self._conn.execute(
                        f"DELETE FROM facts WHERE id IN ({fmarks})",
                        fact_ids).rowcount
                n_events = self._conn.execute(
                    f"DELETE FROM events WHERE id IN ({marks})",
                    event_ids).rowcount
                self._conn.commit()
                if vacuum:
                    try:
                        self._conn.execute("VACUUM")
                    except Exception as exc:
                        print(f"[storage] post-erasure VACUUM skipped ({exc}).")
        return {"event_ids": event_ids, "fact_ids": fact_ids,
                "frame_paths": frame_paths, "events": n_events,
                "facts": n_facts, "relations": n_rel}

    # ------------------------------ people / entities --------------------
    def resolve_person(self, name: str, *, ts: float | None = None) -> int:
        """Exact-name resolution: return the existing person id or insert one.

        Embedding-based fuzzy resolution ("Chris" == "Christopher") lands in
        step 3; step 1 collapses only exact (case-insensitive) name matches.
        """
        key = (name or "").strip()
        if not key:
            return 0
        created = False
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM people WHERE canonical_name = ? COLLATE NOCASE",
                (key,),
            ).fetchone()
            if row is not None:
                if ts is not None:
                    self._conn.execute(
                        "UPDATE people SET last_seen = ? WHERE id = ?", (ts, row["id"]))
                    self._conn.commit()
                pid = int(row["id"])
            else:
                cur = self._conn.execute(
                    "INSERT INTO people (canonical_name, aliases, first_seen, "
                    "last_seen, canonical_id) "
                    "VALUES (?, ?, ?, ?, lower(hex(randomblob(16))))",
                    (key, json.dumps([key]), ts, ts),
                )
                self._conn.commit()
                pid = int(cur.lastrowid)
                created = True
        # Dirty mark MUST be outside _lock — mark_graph_dirty takes the same
        # non-reentrant Lock and deadlocks if called while holding it.
        if created:
            self.add_node_keys("person", pid, key, ts=ts)
        self.mark_graph_dirty("person", pid, ts=ts)
        return pid

    def find_entity_exact(self, name: str) -> int | None:
        """Exact canonical-name match — bind-only path when minting is denied."""
        key = (name or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM entities WHERE canonical_name = ? COLLATE NOCASE",
                (key,),
            ).fetchone()
        return int(row["id"]) if row else None

    def resolve_entity(self, name: str, kind: str | None = None,
                       *, ts: float | None = None) -> int:
        key = (name or "").strip()
        if not key:
            return 0
        try:
            from app.services.name_quality import normalize_entity_kind
            kind = normalize_entity_kind(kind)
        except Exception:
            kind = (kind or "").strip().lower() or "idea"
        created = False
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM entities WHERE canonical_name = ? COLLATE NOCASE",
                (key,),
            ).fetchone()
            if row is not None:
                if ts is not None:
                    self._conn.execute(
                        "UPDATE entities SET last_seen = ? WHERE id = ?", (ts, row["id"]))
                    self._conn.commit()
                eid = int(row["id"])
            else:
                cur = self._conn.execute(
                    "INSERT INTO entities (canonical_name, kind, aliases, "
                    "first_seen, last_seen, canonical_id) "
                    "VALUES (?, ?, ?, ?, ?, lower(hex(randomblob(16))))",
                    (key, kind, json.dumps([key]), ts, ts),
                )
                self._conn.commit()
                eid = int(cur.lastrowid)
                created = True
        # Dirty mark MUST be outside _lock — mark_graph_dirty takes the same
        # non-reentrant Lock and deadlocks if called while holding it.
        if created:
            self.add_node_keys("entity", eid, key, ts=ts)
        self.mark_graph_dirty("entity", eid, ts=ts)
        return eid

    # --- resolution primitives (step 3: fuzzy person/entity matching) ----
    # The Resolver service (app/services/resolution.py) owns the matching policy
    # and the embedder; the store just provides these building blocks so it stays
    # dependency-light (no sentence-transformers import here).
    def find_person_exact(self, name: str) -> int | None:
        """Exact canonical-name match, following soft-merge redirects.

        Absorbed / hidden rows are not returned as themselves: if they point at
        a survivor via canonical_person_id, that id is returned; otherwise the
        match is ignored so callers do not keep touching dead nodes.
        """
        key = (name or "").strip()
        if not key:
            return None
        with self._lock:
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(people)").fetchall()}
            if "canonical_person_id" in cols:
                row = self._conn.execute(
                    "SELECT id, canonical_person_id, hide_from_people "
                    "FROM people WHERE canonical_name = ? COLLATE NOCASE",
                    (key,),
                ).fetchone()
                if row is None:
                    return None
                canon = row["canonical_person_id"]
                if canon is not None:
                    return int(canon)
                if row["hide_from_people"]:
                    return None
                return int(row["id"])
            row = self._conn.execute(
                "SELECT id FROM people WHERE canonical_name = ? COLLATE NOCASE",
                (key,),
            ).fetchone()
        return int(row["id"]) if row else None

    def list_people_embed(self) -> list[dict]:
        """All people with their name + decoded embedding (None if unset)."""
        with self._lock:
            # Prefer v2 columns when present (after migration).
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(people)").fetchall()}
            extra = ""
            if "canonical_person_id" in cols:
                extra = (", canonical_person_id, hide_from_people, "
                         "promotion_state, actor_type")
            rows = self._conn.execute(
                f"SELECT id, canonical_name, aliases, embedding{extra} "
                "FROM people").fetchall()
        out = []
        for r in rows:
            d = {"id": int(r["id"]), "name": r["canonical_name"],
                 "aliases": json.loads(r["aliases"] or "[]"),
                 "embedding": _blob_to_emb(r["embedding"])}
            if "canonical_person_id" in r.keys():
                d["canonical_person_id"] = r["canonical_person_id"]
                d["hide_from_people"] = bool(r["hide_from_people"] or 0)
                d["promotion_state"] = r["promotion_state"] or "candidate"
                d["actor_type"] = r["actor_type"] or "human_person"
            out.append(d)
        return out

    def insert_person(self, name: str, embedding=None, ts: float | None = None,
                      *, actor_type: str = "human_person",
                      promotion_state: str = "candidate") -> int:
        key = (name or "").strip()
        with self._lock:
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(people)").fetchall()}
            if "promotion_state" in cols:
                cur = self._conn.execute(
                    "INSERT INTO people (canonical_name, aliases, embedding, "
                    "first_seen, last_seen, actor_type, promotion_state, "
                    "canonical_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, lower(hex(randomblob(16))))",
                    (key, json.dumps([key]),
                     _emb_to_blob(embedding) if embedding is not None else None,
                     ts, ts, actor_type, promotion_state),
                )
            else:
                cur = self._conn.execute(
                    "INSERT INTO people (canonical_name, aliases, embedding, "
                    "first_seen, last_seen, canonical_id) "
                    "VALUES (?, ?, ?, ?, ?, lower(hex(randomblob(16))))",
                    (key, json.dumps([key]),
                     _emb_to_blob(embedding) if embedding is not None else None,
                     ts, ts),
                )
            self._conn.commit()
            nid = int(cur.lastrowid)
        self.add_node_keys("person", nid, key, ts=ts)
        return nid

    def touch_person(self, pid: int, ts: float | None = None,
                     alias: str | None = None) -> None:
        """Bump last_seen and, if this spelling is new, record it as an alias —
        so a fuzzy merge ('Christopher' -> 'Chris') stays inspectable."""
        with self._lock:
            row = self._conn.execute(
                "SELECT aliases FROM people WHERE id = ?", (pid,)).fetchone()
            if row is None:
                return
            aliases = json.loads(row["aliases"] or "[]")
            a = (alias or "").strip()
            changed = new_alias = False
            if a and a.lower() not in {x.lower() for x in aliases}:
                aliases.append(a)
                changed = new_alias = True
            if changed:
                self._conn.execute(
                    "UPDATE people SET last_seen = ?, aliases = ? WHERE id = ?",
                    (ts, json.dumps(aliases), pid))
            else:
                self._conn.execute(
                    "UPDATE people SET last_seen = ? WHERE id = ?", (ts, pid))
            self._conn.commit()
        # New spellings become blocking keys too (Change 1).
        if new_alias:
            self.add_node_keys("person", int(pid), a, key_type="alias_norm",
                               ts=ts)
        # Mentions are access events on the person's memory trace (A1).
        self.record_node_access("person", int(pid), ts)

    # ------------------------------ person details -----------------------
    def person_attrs(self, person_id: int) -> dict[str, dict]:
        """User-asserted detail overrides for one person, keyed by field."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value, fact_id, updated_at FROM person_attrs "
                "WHERE person_id = ?", (person_id,)).fetchall()
        return {r["key"]: {"value": r["value"], "fact_id": r["fact_id"],
                           "updated_at": r["updated_at"]} for r in rows}

    def set_person_attr(self, person_id: int, key: str, value: str,
                        fact_id: int | None, ts: float) -> int | None:
        """Upsert one user-asserted detail field. Returns the fact_id of the
        PREVIOUS assertion (if any) so the caller can supersede that claim."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id FROM person_attrs WHERE person_id=? AND key=?",
                (person_id, key)).fetchone()
            prev = row["fact_id"] if row else None
            self._conn.execute(
                "INSERT INTO person_attrs (person_id, key, value, fact_id, "
                "updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(person_id, key) DO UPDATE SET "
                "value=excluded.value, fact_id=excluded.fact_id, "
                "updated_at=excluded.updated_at",
                (person_id, key, value, fact_id, ts))
            self._conn.commit()
            return prev

    def clear_person_attr(self, person_id: int, key: str) -> int | None:
        """Drop a user override — the memory-mined value (if any) shows through
        again. Returns the override's fact_id so the caller can retire the
        claim that carried it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id FROM person_attrs WHERE person_id=? AND key=?",
                (person_id, key)).fetchone()
            self._conn.execute(
                "DELETE FROM person_attrs WHERE person_id=? AND key=?",
                (person_id, key))
            self._conn.commit()
            return row["fact_id"] if row else None

    # ------------------------------ people v2 ----------------------------
    def insert_person_mention(
        self, *, event_id: int, raw_text: str, normalized_text: str,
        discourse_role: str, grammatical_role: str, observed_at: float,
        extractor_version: str, pipeline_version: str,
        person_probability: float | None = None,
        extraction_confidence: float | None = None,
        actor_types: list | None = None,
        identity_hints: dict | None = None,
        resolution_status: str = "unresolved",
        resolved_person_id: int | None = None,
        resolution_confidence: float | None = None,
        relationship_relevance: float | None = None,
    ) -> int:
        now = observed_at
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO person_mentions ("
                "event_id, raw_text, normalized_text, discourse_role, "
                "grammatical_role, observed_at, extractor_version, "
                "pipeline_version, person_probability, extraction_confidence, "
                "actor_types, identity_hints, resolution_status, "
                "resolved_person_id, resolution_confidence, "
                "relationship_relevance, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, raw_text, normalized_text, discourse_role,
                 grammatical_role, observed_at, extractor_version,
                 pipeline_version, person_probability, extraction_confidence,
                 json.dumps(actor_types or []),
                 json.dumps(identity_hints or {}),
                 resolution_status, resolved_person_id, resolution_confidence,
                 relationship_relevance, now, now))
            self._conn.commit()
            return int(cur.lastrowid)

    def insert_identity_candidate(
        self, *, mention_id: int, person_id: int | None, is_new: bool,
        score: float, rank: int, pos_evidence: dict, neg_evidence: dict,
        created_at: float,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO identity_candidates ("
                "mention_id, person_id, is_new, score, rank, pos_evidence, "
                "neg_evidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (mention_id, person_id, 1 if is_new else 0, score, rank,
                 json.dumps(pos_evidence or {}),
                 json.dumps(neg_evidence or {}), created_at))
            self._conn.commit()
            return int(cur.lastrowid)

    def insert_resolution_decision(
        self, *, mention_id: int, decision: str,
        chosen_person_id: int | None, confidence: float,
        threshold_policy: str, resolver_version: str, decided_at: float,
        actor: str = "system",
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO resolution_decisions ("
                "mention_id, decision, chosen_person_id, confidence, "
                "threshold_policy, resolver_version, decided_at, actor) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (mention_id, decision, chosen_person_id, confidence,
                 threshold_policy, resolver_version, decided_at, actor))
            self._conn.commit()
            return int(cur.lastrowid)

    def upsert_contact_point(
        self, *, person_id: int, type_: str, value_display: str,
        value_normalized: str, confidence: float, attribution_method: str,
        verification_status: str, source_event_id: int | None,
        evidence_quote: str | None, discourse_role: str | None,
        ts: float, created_by: str, pipeline_version: str,
    ) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT contact_point_id FROM person_contact_points "
                "WHERE person_id=? AND type=? AND value_normalized=? "
                "AND status='active'",
                (person_id, type_, value_normalized)).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE person_contact_points SET last_seen_at=?, "
                    "confidence=CASE WHEN confidence > ? THEN confidence ELSE ? END, "
                    "evidence_quote=COALESCE(?, evidence_quote) "
                    "WHERE contact_point_id=?",
                    (ts, confidence, confidence, evidence_quote,
                     int(row["contact_point_id"])))
                self._conn.commit()
                return int(row["contact_point_id"])
            cur = self._conn.execute(
                "INSERT INTO person_contact_points ("
                "person_id, type, value_normalized, value_display, confidence, "
                "attribution_method, verification_status, source_event_id, "
                "evidence_quote, discourse_role, first_seen_at, last_seen_at, "
                "status, created_by, pipeline_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (person_id, type_, value_normalized, value_display, confidence,
                 attribution_method, verification_status, source_event_id,
                 evidence_quote, discourse_role, ts, ts, "active",
                 created_by, pipeline_version))
            self._conn.commit()
            return int(cur.lastrowid)

    def list_contact_points(self, person_id: int, *, type_: str | None = None,
                            active_only: bool = True) -> list[dict]:
        q = ("SELECT * FROM person_contact_points WHERE person_id=?"
             + (" AND status='active'" if active_only else "")
             + (" AND type=?" if type_ else "")
             + " ORDER BY confidence DESC, last_seen_at DESC")
        args: list = [person_id]
        if type_:
            args.append(type_)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def archive_contact_point(self, contact_point_id: int) -> bool:
        """Soft-delete one contact point (user removed it from DETAILS)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE person_contact_points SET status='archived' "
                "WHERE contact_point_id=? AND status='active'",
                (int(contact_point_id),))
            self._conn.commit()
            return cur.rowcount > 0

    def delete_relation(self, subj_type: str, subj_id: int, predicate: str,
                        obj_type: str, obj_id: int) -> bool:
        """Remove one specific edge (user removed an org/team from DETAILS)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM relations WHERE subj_type=? AND subj_id=? AND "
                "predicate=? AND obj_type=? AND obj_id=?",
                (subj_type, int(subj_id), predicate, obj_type, int(obj_id)))
            self._conn.commit()
            return cur.rowcount > 0

    def list_person_mentions(self, *, person_id: int | None = None,
                             unresolved_only: bool = False,
                             limit: int = 50) -> list[dict]:
        clauses, args = [], []
        if person_id is not None:
            clauses.append("resolved_person_id=?")
            args.append(person_id)
        if unresolved_only:
            clauses.append("resolution_status='unresolved'")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM person_mentions {where} "
                "ORDER BY observed_at DESC LIMIT ?",
                [*args, limit]).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------ edge dynamics ------------------------
    def all_relations(self) -> list[dict]:
        """Every graph edge, raw — the activation engine's input."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, subj_type, subj_id, predicate, obj_type, obj_id, "
                "weight, origin, confidence, created_at FROM relations"
            ).fetchall()
        return [dict(r) for r in rows]

    def replace_edge_dynamics(self, rows: list[dict]) -> int:
        """Wholesale swap at rebuild time (derived edges get new ids each
        rebuild, so the sidecar follows the same lifecycle)."""
        with self._lock:
            self._conn.execute("DELETE FROM edge_dynamics")
            if rows:
                import time as _time
                now = _time.time()
                self._conn.executemany(
                    "INSERT OR REPLACE INTO edge_dynamics "
                    "(relation_id, class, pmi, conductance, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(int(r["relation_id"]), r.get("class"), r.get("pmi"),
                      float(r.get("conductance") or 0.0), now) for r in rows])
            self._conn.commit()
            return len(rows)

    def conductive_edges(self, min_c: float = 0.01) -> list[tuple]:
        """(subj_type, subj_id, obj_type, obj_id, conductance) for every edge
        the activation engine should propagate through — attendable endpoints
        only (events are provenance, not attention targets)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.subj_type, r.subj_id, r.obj_type, r.obj_id, "
                "d.conductance FROM relations r "
                "JOIN edge_dynamics d ON d.relation_id = r.id "
                "WHERE d.conductance >= ? "
                "AND r.subj_type != 'event' AND r.obj_type != 'event' "
                "AND NOT (r.subj_type = r.obj_type AND r.subj_id = r.obj_id)",
                (min_c,)).fetchall()
        return [(r["subj_type"], int(r["subj_id"]), r["obj_type"],
                 int(r["obj_id"]), float(r["conductance"])) for r in rows]

    # ------------------------------ memory traces ------------------------
    def node_dynamics_map(self, keys: list[tuple[str, int]]) -> dict:
        """Trace rows for a candidate set, keyed by (node_type, node_id)."""
        if not keys:
            return {}
        out: dict[tuple[str, int], dict] = {}
        with self._lock:
            for node_type, node_id in keys:
                row = self._conn.execute(
                    "SELECT * FROM node_dynamics "
                    "WHERE node_type = ? AND node_id = ?",
                    (node_type, int(node_id))).fetchone()
                if row:
                    out[(node_type, int(node_id))] = dict(row)
        return out

    def record_node_access(self, node_type: str, node_id: int,
                           ts: float | None = None) -> None:
        """One access event on a node's trace: creation, re-assertion,
        retrieval into grounding, or user engagement. Maintains the K-newest
        ring + compressed tail; never raises (trace upkeep must not break
        the operation being traced)."""
        import time as _time
        try:
            from app.services.traces import fold_access
            ts = float(ts or _time.time())
            with self._lock:
                row = self._conn.execute(
                    "SELECT access_recent, access_n_older, access_t_older "
                    "FROM node_dynamics WHERE node_type = ? AND node_id = ?",
                    (node_type, int(node_id))).fetchone()
                recent = json.loads(row["access_recent"] or "[]") if row else []
                n_older = int(row["access_n_older"] or 0) if row else 0
                t_older = row["access_t_older"] if row else None
                recent, n_older, t_older = fold_access(recent, n_older,
                                                       t_older, ts)
                self._conn.execute(
                    "INSERT INTO node_dynamics (node_type, node_id, "
                    "access_recent, access_n_older, access_t_older, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(node_type, node_id) DO UPDATE SET "
                    "access_recent=excluded.access_recent, "
                    "access_n_older=excluded.access_n_older, "
                    "access_t_older=excluded.access_t_older, "
                    "updated_at=excluded.updated_at",
                    (node_type, int(node_id), json.dumps(recent), n_older,
                     t_older, ts))
                self._conn.commit()
        except Exception as exc:
            print(f"[storage] node access skipped ({exc}).")

    def bump_node_value(self, node_type: str, node_id: int,
                        outcome: str) -> None:
        """Move a node's long-run value V on an engagement outcome."""
        import time as _time
        try:
            from app.services.traces import V_DEFAULT, v_bump
            with self._lock:
                row = self._conn.execute(
                    "SELECT V FROM node_dynamics "
                    "WHERE node_type = ? AND node_id = ?",
                    (node_type, int(node_id))).fetchone()
                v = float(row["V"]) if row else V_DEFAULT
                self._conn.execute(
                    "INSERT INTO node_dynamics (node_type, node_id, V, "
                    "updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(node_type, node_id) DO UPDATE SET "
                    "V=excluded.V, updated_at=excluded.updated_at",
                    (node_type, int(node_id), v_bump(v, outcome),
                     _time.time()))
                self._conn.commit()
        except Exception as exc:
            print(f"[storage] value bump skipped ({exc}).")

    def seed_node_dynamics(self, node_type: str, node_id: int, *,
                           v: float, access: list[float]) -> bool:
        """Backfill: create a trace row only where none exists — live rows
        (already accumulating real accesses) are never overwritten."""
        import time as _time
        access = sorted(float(t) for t in access if t)
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO node_dynamics "
                "(node_type, node_id, V, access_recent, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (node_type, int(node_id), float(v), json.dumps(access),
                 _time.time()))
            self._conn.commit()
            return cur.rowcount > 0

    def replace_wm_slots(self, rows: list[dict]) -> int:
        """Atomically swap the Working Memory slot table (Track A3)."""
        payload = []
        for r in rows:
            reason = r.get("reason")
            if reason is not None and not isinstance(reason, str):
                reason = json.dumps(reason)
            payload.append((
                int(r["slot"]),
                r.get("node_type"),
                int(r["node_id"]) if r.get("node_id") is not None else None,
                float(r.get("entered_at") or 0),
                float(r.get("score") or 0),
                int(r.get("cluster_head") or 0),
                int(r.get("cluster_n") or 1),
                reason,
            ))
        with self._lock:
            self._conn.execute("DELETE FROM wm_slots")
            if payload:
                self._conn.executemany(
                    "INSERT INTO wm_slots (slot, node_type, node_id, entered_at, "
                    "score, cluster_head, cluster_n, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    payload,
                )
            self._conn.commit()
        return len(payload)

    def load_wm_slots(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM wm_slots ORDER BY slot ASC").fetchall()
        return [dict(r) for r in rows]

    def set_att_state(self, node_type: str, node_id: int, state: str) -> None:
        """Update attention-state label on node_dynamics (Focused/Active/…)."""
        import time as _time
        with self._lock:
            self._conn.execute(
                "INSERT INTO node_dynamics (node_type, node_id, att_state, "
                "updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(node_type, node_id) DO UPDATE SET "
                "att_state=excluded.att_state, updated_at=excluded.updated_at",
                (node_type, int(node_id), state, _time.time()))
            self._conn.commit()

    def set_node_urgency(self, node_type: str, node_id: int, u: float,
                         *, state: str = "urgent") -> None:
        """D8 attention-only: write U + att_state (meta-memory at-risk)."""
        import time as _time
        with self._lock:
            self._conn.execute(
                "INSERT INTO node_dynamics (node_type, node_id, U, att_state, "
                "updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(node_type, node_id) DO UPDATE SET "
                "U=excluded.U, att_state=excluded.att_state, "
                "updated_at=excluded.updated_at",
                (node_type, int(node_id), float(u), state, _time.time()))
            self._conn.commit()

    def list_att_state(self, state: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_type, node_id, att_state FROM node_dynamics "
                "WHERE att_state = ?",
                (state,)).fetchall()
        return [dict(r) for r in rows]

    # --- A4 ranking_model / attention_predictions ---------------------------
    def save_ranking_model(self, *, beta: dict, beta_var: dict | None = None,
                           prior: dict | None = None, version: str = "v1",
                           n_updates: int = 0, drift: float | None = None,
                           note: str | None = None,
                           activate: bool = True) -> int:
        import time as _time
        with self._lock:
            if activate:
                self._conn.execute(
                    "UPDATE ranking_model SET status='archived' "
                    "WHERE status='active'")
            cur = self._conn.execute(
                "INSERT INTO ranking_model "
                "(ts, version, status, beta, beta_var, prior, n_updates, drift, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_time.time(), version,
                 "active" if activate else "candidate",
                 json.dumps(beta),
                 json.dumps(beta_var or {}),
                 json.dumps(prior or {}),
                 int(n_updates), drift, note))
            self._conn.commit()
            return int(cur.lastrowid)

    def active_ranking_model(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ranking_model WHERE status='active' "
                "ORDER BY ts DESC LIMIT 1").fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("beta", "beta_var", "prior"):
            raw = d.get(k)
            if isinstance(raw, str):
                try:
                    d[k] = json.loads(raw)
                except Exception:
                    d[k] = {}
        return d

    def replace_attention_predictions(self, rows: list[dict]) -> int:
        """Swap today's horizon predictions (keep dismissed history lightly)."""
        import time as _time
        now = _time.time()
        payload = []
        for r in rows:
            reason = r.get("reason")
            if reason is not None and not isinstance(reason, str):
                reason = json.dumps(reason)
            payload.append((
                float(r.get("ts") or now),
                r.get("node_type"),
                int(r["node_id"]) if r.get("node_id") is not None else None,
                float(r.get("p_need") or 0),
                r.get("when_s"),
                reason,
                r.get("source") or "calendar",
                int(r.get("dismissed") or 0),
                r.get("event_key"),
            ))
        with self._lock:
            # Drop non-dismissed rows older than a day; keep dismissals as signal.
            self._conn.execute(
                "DELETE FROM attention_predictions "
                "WHERE dismissed=0 AND ts < ?",
                (now - 86400,))
            self._conn.execute(
                "DELETE FROM attention_predictions WHERE dismissed=0")
            if payload:
                self._conn.executemany(
                    "INSERT INTO attention_predictions "
                    "(ts, node_type, node_id, p_need, when_s, reason, source, "
                    " dismissed, event_key) VALUES (?,?,?,?,?,?,?,?,?)",
                    payload)
            self._conn.commit()
        return len(payload)

    def list_attention_predictions(self, *, include_dismissed: bool = False,
                                   limit: int = 12) -> list[dict]:
        with self._lock:
            if include_dismissed:
                rows = self._conn.execute(
                    "SELECT * FROM attention_predictions "
                    "ORDER BY p_need DESC, ts DESC LIMIT ?",
                    (int(limit),)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM attention_predictions WHERE dismissed=0 "
                    "ORDER BY p_need DESC, ts DESC LIMIT ?",
                    (int(limit),)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("reason"), str):
                try:
                    d["reason"] = json.loads(d["reason"])
                except Exception:
                    pass
            out.append(d)
        return out

    def dismiss_attention_prediction(self, pred_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE attention_predictions SET dismissed=1 WHERE id=?",
                (int(pred_id),))
            self._conn.commit()
            return cur.rowcount > 0

    def node_dynamics_counts(self) -> dict[str, int]:
        """How many traces exist per node_type — A1 console card."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_type, COUNT(*) AS n FROM node_dynamics "
                "GROUP BY node_type").fetchall()
        out = {r["node_type"]: int(r["n"]) for r in rows}
        out["total"] = sum(out.values())
        return out

    def add_attention_replay_run(self, result: dict) -> int:
        """Persist one priors-continuity replay result for the console/worker."""
        import time as _time
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO attention_replay_runs "
                "(ts, days, gate, status, passed, renders, mean_tau, min_tau, "
                " max_tau, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    float(result.get("ts") or _time.time()),
                    result.get("days"), result.get("gate"),
                    result.get("status"),
                    (None if result.get("passed") is None
                     else (1 if result.get("passed") else 0)),
                    result.get("renders"), result.get("mean_tau"),
                    result.get("min_tau"), result.get("max_tau"),
                    json.dumps(result),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def last_attention_replay_run(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attention_replay_runs ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("passed") is not None:
            d["passed"] = bool(d["passed"])
        return d

    def add_ranking_promote_run(self, result: dict) -> int:
        """Persist one β promote-or-hold result for the console/worker."""
        import time as _time
        prior = result.get("prior") or {}
        cand = result.get("candidate") or {}
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO ranking_promote_runs "
                "(ts, days, status, promoted, n_labeled, prior_acc, cand_acc, "
                " prior_ll, cand_ll, reason, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    float(result.get("ts") or _time.time()),
                    result.get("days"),
                    result.get("status"),
                    1 if result.get("promoted") else 0,
                    result.get("n_labeled"),
                    prior.get("accuracy"), cand.get("accuracy"),
                    prior.get("logloss"), cand.get("logloss"),
                    result.get("reason"),
                    json.dumps(result),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def last_ranking_promote_run(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ranking_promote_runs ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["promoted"] = bool(d.get("promoted"))
        return d

    # ------------------------------ memory economy (Track C) -------------
    def get_event(self, event_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE id = ?", (int(event_id),)).fetchone()
        return dict(row) if row else None

    def table_counts(self) -> dict:
        """Row counts for the storage-growth snapshot."""
        out = {}
        with self._lock:
            for name in ("events", "facts", "turns", "events_archive"):
                out[name] = int(self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"])
        return out

    def events_lifecycle_stats(self) -> dict:
        """Counts by lifecycle (NULL folds into 'fresh') + retention coverage."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(lifecycle, 'fresh') AS lc, COUNT(*) AS n "
                "FROM events GROUP BY lc").fetchall()
            scored = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE retention IS NOT NULL"
            ).fetchone()
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events").fetchone()
        counts = {r["lc"]: int(r["n"]) for r in rows}
        return {
            "counts": counts,
            "total": int(total["n"]) if total else 0,
            "scored": int(scored["n"]) if scored else 0,
        }

    def events_for_economy(self, *, limit: int = 1000,
                           before: float | None = None) -> list[dict]:
        """Non-compacted events (oldest first) with their derived-layer footprint
        — inputs to retention scoring. `before` bounds by event time."""
        sql = (
            "SELECT e.id, e.time, e.modality, e.source, e.confidence, "
            "       e.extracted_at, COALESCE(e.lifecycle, 'fresh') AS lifecycle, "
            "       e.retention, e.summary IS NOT NULL AS has_summary, "
            "       LENGTH(e.raw) AS raw_len, "
            "       (SELECT COUNT(*) FROM facts f "
            "        WHERE f.source_event_id = e.id) AS n_facts "
            "FROM events e "
            "WHERE COALESCE(e.lifecycle, 'fresh') != 'compacted'"
        )
        args: list = []
        if before is not None:
            sql += " AND e.time < ?"
            args.append(float(before))
        sql += " ORDER BY e.time ASC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def economy_signals_for_events(self, event_ids: list[int],
                                   *, since_days: float = 90.0) -> dict[int, dict]:
        """Ledger recall + open-work + max V + contradiction for citing facts.

        Used by the retention sweep so scores learn from attention impressions
        without waiting for a separate Track C learning pass. contradiction is
        the fraction of citing facts with state='superseded'.
        """
        import time as _time
        ids = [int(x) for x in event_ids if x is not None]
        if not ids:
            return {}
        since = _time.time() - float(since_days) * 86400.0
        out: dict[int, dict] = {
            i: {"recall_n": 0, "has_open": False, "v_max": 0.0,
                "contradiction": 0.0, "_n_facts": 0, "_n_super": 0}
            for i in ids
        }
        ph = ",".join("?" * len(ids))
        with self._lock:
            fact_rows = self._conn.execute(
                f"SELECT f.id AS fact_id, f.source_event_id AS eid, "
                f"COALESCE(t.status, c.status) AS status, "
                f"COALESCE(f.state, 'active') AS state "
                f"FROM facts f "
                f"LEFT JOIN tasks t ON t.fact_id = f.id "
                f"LEFT JOIN commitments c ON c.fact_id = f.id "
                f"WHERE f.source_event_id IN ({ph})",
                ids,
            ).fetchall()
            fact_to_eid: dict[int, int] = {}
            for r in fact_rows:
                eid = int(r["eid"])
                fid = int(r["fact_id"])
                fact_to_eid[fid] = eid
                out[eid]["_n_facts"] += 1
                if (r["state"] or "active") == "superseded":
                    out[eid]["_n_super"] += 1
                if ((r["status"] or "") == "open"
                        and (r["state"] or "active") == "active"):
                    out[eid]["has_open"] = True
            for eid, slot in out.items():
                n = int(slot.pop("_n_facts", 0) or 0)
                ns = int(slot.pop("_n_super", 0) or 0)
                slot["contradiction"] = (ns / n) if n else 0.0
            fids = list(fact_to_eid.keys())
            if fids:
                phf = ",".join("?" * len(fids))
                imp = self._conn.execute(
                    f"SELECT node_id, COUNT(*) AS n FROM attention_impressions "
                    f"WHERE node_type='fact' AND node_id IN ({phf}) AND ts >= ? "
                    f"GROUP BY node_id",
                    [*fids, since],
                ).fetchall()
                for r in imp:
                    eid = fact_to_eid.get(int(r["node_id"]))
                    if eid is not None:
                        out[eid]["recall_n"] += int(r["n"] or 0)
                dyn = self._conn.execute(
                    f"SELECT node_id, V FROM node_dynamics "
                    f"WHERE node_type='fact' AND node_id IN ({phf})",
                    fids,
                ).fetchall()
                for r in dyn:
                    eid = fact_to_eid.get(int(r["node_id"]))
                    if eid is None:
                        continue
                    try:
                        v = float(r["V"] or 0)
                    except Exception:
                        v = 0.0
                    if v > out[eid]["v_max"]:
                        out[eid]["v_max"] = v
        return out

    def apply_event_retention(self, updates: list[tuple[int, str | None, float]],
                              ts: float) -> int:
        """Bulk-persist sweep results: (event_id, lifecycle-or-None, retention).
        Metadata only — raw/summary are never touched here."""
        if not updates:
            return 0
        with self._lock:
            for eid, lifecycle, retention in updates:
                if lifecycle is not None:
                    self._conn.execute(
                        "UPDATE events SET lifecycle = ?, retention = ?, "
                        "retention_ts = ? WHERE id = ?",
                        (lifecycle, float(retention), float(ts), int(eid)))
                else:
                    self._conn.execute(
                        "UPDATE events SET retention = ?, retention_ts = ? "
                        "WHERE id = ?",
                        (float(retention), float(ts), int(eid)))
            self._conn.commit()
        return len(updates)

    def fact_spans_for_event(self, event_id: int) -> list[dict]:
        """Joined facts citing this event — verbatim spans for the compaction
        stub (I-1) and open/active status for the do-not-compact protection."""
        with self._lock:
            rows = self._conn.execute(
                self._FACT_SELECT + " WHERE f.source_event_id = ?",
                (int(event_id),)).fetchall()
        return [dict(r) for r in rows]

    def compact_event(self, event_id: int, stub_raw: str, ts: float) -> bool:
        """Replace an event's raw text with a span-preserving stub, archiving
        the FULL original row first (verbatim JSON) so this is reversible.
        Refuses to double-compact. Summary/meta/audio_path stay untouched."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE id = ?", (int(event_id),)).fetchone()
            if row is None:
                return False
            d = dict(row)
            if (d.get("lifecycle") or "fresh") == "compacted":
                return False
            self._conn.execute(
                "INSERT OR IGNORE INTO events_archive (event_id, archived_at, row) "
                "VALUES (?, ?, ?)",
                (int(event_id), float(ts), json.dumps(d)))
            self._conn.execute(
                "UPDATE events SET raw = ?, lifecycle = 'compacted' WHERE id = ?",
                (stub_raw, int(event_id)))
            self._conn.commit()
            return True

    def restore_event(self, event_id: int) -> bool:
        """Undo a compaction: put the archived original raw back and return the
        event to 'absorbed'. The archive row is removed (the event IS the
        original again)."""
        with self._lock:
            arch = self._conn.execute(
                "SELECT row FROM events_archive WHERE event_id = ?",
                (int(event_id),)).fetchone()
            if arch is None:
                return False
            try:
                original = json.loads(arch["row"])
            except Exception:
                return False
            self._conn.execute(
                "UPDATE events SET raw = ?, lifecycle = 'absorbed' WHERE id = ?",
                (original.get("raw") or "", int(event_id)))
            self._conn.execute(
                "DELETE FROM events_archive WHERE event_id = ?",
                (int(event_id),))
            self._conn.commit()
            return True

    def compacted_events(self, since: float | None = None,
                         limit: int = 200) -> list[dict]:
        """Events compacted since `ts` — the 'forgotten this month' review list."""
        sql = ("SELECT a.event_id, a.archived_at, e.time, e.source, e.modality, "
               "       e.summary, e.raw AS stub "
               "FROM events_archive a JOIN events e ON e.id = a.event_id")
        args: list = []
        if since is not None:
            sql += " WHERE a.archived_at >= ?"
            args.append(float(since))
        sql += " ORDER BY a.archived_at DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def add_storage_growth(self, snap: dict) -> int:
        import time as _time
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO storage_growth (ts, db_bytes, lance_bytes, "
                "n_events, n_facts, n_turns, n_compacted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (float(snap.get("ts") or _time.time()),
                 snap.get("db_bytes"), snap.get("lance_bytes"),
                 snap.get("n_events"), snap.get("n_facts"),
                 snap.get("n_turns"), snap.get("n_compacted")))
            self._conn.commit()
            return int(cur.lastrowid)

    def last_storage_growth(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM storage_growth ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list_storage_growth(self, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM storage_growth ORDER BY ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def add_economy_run(self, result: dict) -> int:
        import time as _time
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO economy_runs (ts, scored, absorbed, candidates, "
                "compacted, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (float(result.get("ts") or _time.time()),
                 result.get("scored"), result.get("absorbed"),
                 result.get("candidates"), result.get("compacted"),
                 json.dumps(result)))
            self._conn.commit()
            return int(cur.lastrowid)

    def last_economy_run(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM economy_runs ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    # ------------------------ predictors + hardening (Track F) -----------
    def active_predictor_model(self, task: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM predictor_models WHERE task = ? AND active = 1 "
                "ORDER BY activated_at DESC LIMIT 1", (task,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["metrics"] = json.loads(d.get("metrics") or "null")
        except Exception:
            pass
        return d

    def save_predictor_model(self, *, task: str, version: str, kind: str,
                             metrics: dict | None = None,
                             note: str | None = None,
                             activate: bool = False,
                             ts: float | None = None) -> int:
        import time as _time
        ts = float(ts if ts is not None else _time.time())
        with self._lock:
            if activate:
                self._conn.execute(
                    "UPDATE predictor_models SET active = 0 WHERE task = ?",
                    (task,))
            cur = self._conn.execute(
                "INSERT INTO predictor_models (task, version, kind, active, "
                "activated_at, metrics, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task, version, kind, 1 if activate else 0,
                 ts if activate else None,
                 json.dumps(metrics) if metrics is not None else None, note))
            self._conn.commit()
            return int(cur.lastrowid)

    def predictor_model_history(self, task: str, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictor_models WHERE task = ? "
                "ORDER BY id DESC LIMIT ?", (task, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def activate_predictor_model(self, model_id: int) -> bool:
        """Rollback path: re-activate a specific registry row."""
        import time as _time
        with self._lock:
            row = self._conn.execute(
                "SELECT task FROM predictor_models WHERE id = ?",
                (int(model_id),)).fetchone()
            if not row:
                return False
            self._conn.execute(
                "UPDATE predictor_models SET active = 0 WHERE task = ?",
                (row["task"],))
            self._conn.execute(
                "UPDATE predictor_models SET active = 1, activated_at = ? "
                "WHERE id = ?", (_time.time(), int(model_id)))
            self._conn.commit()
            return True

    def add_predictor_bench_run(self, result: dict) -> int:
        import time as _time
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO predictor_bench_runs (ts, task, model, status, "
                "n_points, hit1, hit3, mrr, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (float(result.get("ts") or _time.time()), result.get("task"),
                 result.get("model"), result.get("status"),
                 result.get("n_points"), result.get("hit1"),
                 result.get("hit3"), result.get("mrr"),
                 json.dumps(result)))
            self._conn.commit()
            return int(cur.lastrowid)

    def last_predictor_bench_run(self, task: str | None = None) -> dict | None:
        sql = "SELECT * FROM predictor_bench_runs"
        args: list = []
        if task:
            sql += " WHERE task = ?"
            args.append(task)
        sql += " ORDER BY ts DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def add_hardening_run(self, *, kind: str, ok: bool, detail: dict,
                          ts: float | None = None) -> int:
        import time as _time
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO hardening_runs (ts, kind, ok, detail) "
                "VALUES (?, ?, ?, ?)",
                (float(ts if ts is not None else _time.time()), kind,
                 1 if ok else 0, json.dumps(detail)))
            self._conn.commit()
            return int(cur.lastrowid)

    def last_hardening_run(self, kind: str | None = None) -> dict | None:
        sql = "SELECT * FROM hardening_runs"
        args: list = []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY ts DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        if not row:
            return None
        d = dict(row)
        d["ok"] = bool(d.get("ok"))
        try:
            d["detail"] = json.loads(d.get("detail") or "{}")
        except Exception:
            pass
        return d

    # ------------------------------ self reports -------------------------
    def add_self_report(self, *, load_score: int | None,
                        trust_score: int | None,
                        interrupt_score: int | None,
                        note: str | None = None,
                        ts: float | None = None) -> int:
        import time as _time
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO self_reports (ts, load_score, trust_score, "
                "interrupt_score, note) VALUES (?, ?, ?, ?, ?)",
                (ts or _time.time(), load_score, trust_score,
                 interrupt_score, note))
            self._conn.commit()
            return int(cur.lastrowid)

    def last_self_report_ts(self) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS t FROM self_reports").fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    def list_self_reports(self, limit: int = 26) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM self_reports ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------ supersessions ------------------------
    def recent_supersessions(self, limit: int = 50) -> list[dict]:
        """Newest supersede decisions (the adjudicator's 'update' verdicts and
        user re-edits), as old→new pairs the human can review and reverse.
        Only pairs whose NEW fact is still active are offered — a chain's
        middle links are history, not open decisions."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, superseded_by, updated_at FROM facts "
                "WHERE state = 'superseded' AND superseded_by IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT ?", (limit * 2,)).fetchall()
        pairs = [(int(r["id"]), int(r["superseded_by"]), r["updated_at"])
                 for r in rows]
        ids = [i for p in pairs for i in p[:2]]
        fmap = self.facts_by_ids(list(dict.fromkeys(ids))) if ids else {}
        out = []
        for old_id, new_id, when in pairs:
            old, new = fmap.get(old_id), fmap.get(new_id)
            if not old or not new:
                continue
            if (new.get("state") or "active") != "active":
                continue   # superseded again later — not the live decision
            out.append({
                "old_id": old_id, "new_id": new_id, "when": when,
                "kind": old.get("kind"),
                "old_text": old.get("text") or old.get("source_span") or "",
                "new_text": new.get("text") or new.get("source_span") or "",
                "old_confidence": old.get("confidence"),
                "new_confidence": new.get("confidence"),
            })
            if len(out) >= limit:
                break
        return out

    def revert_supersession(self, old_id: int) -> bool:
        """The user says the OLD fact was right: swap the supersede direction.
        Old becomes active again (its typed rows reopened), new becomes the
        superseded one. Only valid while new is still active."""
        import time as _time
        now = _time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT superseded_by FROM facts "
                "WHERE id = ? AND state = 'superseded'", (old_id,)).fetchone()
            if not row or not row["superseded_by"]:
                return False
            new_id = int(row["superseded_by"])
            new_state = self._conn.execute(
                "SELECT state FROM facts WHERE id = ?", (new_id,)).fetchone()
            if not new_state or (new_state["state"] or "active") != "active":
                return False
            self._conn.execute(
                "UPDATE facts SET state = 'active', superseded_by = NULL, "
                "updated_at = ? WHERE id = ?", (now, old_id))
            self._conn.execute(
                "UPDATE facts SET state = 'superseded', superseded_by = ?, "
                "updated_at = ? WHERE id = ?", (old_id, now, new_id))
            for tbl in ("tasks", "commitments"):
                self._conn.execute(
                    f"UPDATE {tbl} SET status = 'open' "
                    "WHERE fact_id = ? AND status = 'cancelled'", (old_id,))
                self._conn.execute(
                    f"UPDATE {tbl} SET status = 'cancelled' "
                    "WHERE fact_id = ? AND status = 'open'", (new_id,))
            self._conn.commit()
            return True

    # ------------------------------ entity details -----------------------
    def entity_attrs(self, entity_id: int) -> dict[str, dict]:
        """User-asserted detail overrides for one entity, keyed by field."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value, fact_id, updated_at FROM entity_attrs "
                "WHERE entity_id = ?", (entity_id,)).fetchall()
        return {r["key"]: {"value": r["value"], "fact_id": r["fact_id"],
                           "updated_at": r["updated_at"]} for r in rows}

    def set_entity_attr(self, entity_id: int, key: str, value: str,
                        fact_id: int | None, ts: float) -> int | None:
        """Upsert one user-asserted entity detail. Returns the fact_id of the
        PREVIOUS assertion (if any) so the caller can supersede that claim."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id FROM entity_attrs WHERE entity_id=? AND key=?",
                (entity_id, key)).fetchone()
            prev = row["fact_id"] if row else None
            self._conn.execute(
                "INSERT INTO entity_attrs (entity_id, key, value, fact_id, "
                "updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_id, key) DO UPDATE SET "
                "value=excluded.value, fact_id=excluded.fact_id, "
                "updated_at=excluded.updated_at",
                (entity_id, key, value, fact_id, ts))
            self._conn.commit()
            return prev

    def clear_entity_attr(self, entity_id: int, key: str) -> int | None:
        """Drop a user override — the memory-mined value (if any) shows through
        again. Returns the override's fact_id so the caller can retire it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id FROM entity_attrs WHERE entity_id=? AND key=?",
                (entity_id, key)).fetchone()
            self._conn.execute(
                "DELETE FROM entity_attrs WHERE entity_id=? AND key=?",
                (entity_id, key))
            self._conn.commit()
            return row["fact_id"] if row else None

    # ------------------------------ fact_candidates (plan 1.1/1.2) -------
    @staticmethod
    def _candidate_payload_json(payload: dict | None) -> str:
        return json.dumps(payload or {}, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)

    def _find_fact_candidate_locked(self, turn_hash: str, kind: str,
                                    payload_json: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM fact_candidates WHERE turn_hash = ? AND kind = ? "
            "AND payload_json = ? ORDER BY id ASC LIMIT 1",
            (turn_hash, kind, payload_json)).fetchone()

    def find_fact_candidate(self, turn_hash: str, kind: str,
                            payload: dict | None = None) -> dict | None:
        """Exact match on canonical payload_json (plan 1.2 dedupe key)."""
        payload_json = self._candidate_payload_json(payload)
        with self._lock:
            row = self._find_fact_candidate_locked(turn_hash, kind, payload_json)
        if not row:
            return None
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload_json") or "{}")
        except Exception:
            d["payload"] = {}
        return d

    def add_fact_candidate(
        self, *, turn_hash: str, kind: str, payload: dict | None = None,
        source_span: str | None = None, speaker: str | None = None,
        assertion: str | None = None, confidence: float | None = None,
        model: str | None = None, prompt_version: str,
        schema_version: str, status: str = "pending",
        verdict_reason: str | None = None,
        source_event_id: int | None = None,
        correlation_id: str | None = None,
        created_at: float | None = None,
    ) -> int:
        """Persist one raw LLM extract row for replay / goldens / trace.

        Dedupes on turn_hash+kind+payload_json: a second write of the same
        candidate (e.g. a replayed pass) returns the existing row's id instead
        of inserting a twin, so downstream materialization sees one candidate
        per distinct LLM output — the key that makes replay idempotent."""
        import time as _t

        now = float(created_at) if created_at is not None else _t.time()
        payload_json = self._candidate_payload_json(payload)
        with self._lock:
            existing = self._find_fact_candidate_locked(turn_hash, kind, payload_json)
            if existing:
                return int(existing["id"])
            cur = self._conn.execute(
                """
                INSERT INTO fact_candidates
                    (turn_hash, kind, payload_json, source_span, speaker,
                     assertion, confidence, model, prompt_version,
                     schema_version, status, verdict_reason, source_event_id,
                     correlation_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (turn_hash, kind, payload_json, source_span, speaker,
                 assertion, confidence, model, prompt_version, schema_version,
                 status, verdict_reason, source_event_id, correlation_id, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_fact_candidates(self, *, turn_hash: str | None = None,
                             status: str | None = None,
                             limit: int = 200) -> list[dict]:
        """Recent candidates, optionally filtered by turn_hash / status."""
        sql = "SELECT * FROM fact_candidates WHERE 1=1"
        params: list = []
        if turn_hash is not None:
            sql += " AND turn_hash = ?"
            params.append(turn_hash)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload_json") or "{}")
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out

    def set_fact_candidate_status(self, candidate_id: int, status: str,
                                  *, verdict_reason: str | None = None) -> None:
        """Stamp gate / materialize outcome onto a candidate (plan 1.2+)."""
        if not candidate_id:
            return
        with self._lock:
            if verdict_reason is not None:
                self._conn.execute(
                    "UPDATE fact_candidates SET status = ?, verdict_reason = ? "
                    "WHERE id = ?",
                    (status, verdict_reason, candidate_id))
            else:
                self._conn.execute(
                    "UPDATE fact_candidates SET status = ? WHERE id = ?",
                    (status, candidate_id))
            self._conn.commit()

    def trace_chain(self, correlation_id: str) -> dict:
        """Full audit chain for one correlation_id (plan 1.5): the source
        events it was minted on, the raw fact_candidates rows, the facts
        materialized from those same events, and any agent_runs tagged with
        the same id — so a fact (or an agent action) traces back to the exact
        utterance that produced it. A stale/missing id returns empty lists."""
        if not correlation_id:
            return {"correlation_id": correlation_id, "events": [],
                    "candidates": [], "facts": [], "agent_runs": []}
        with self._lock:
            ev_rows = self._conn.execute(
                "SELECT * FROM events WHERE meta LIKE ?",
                (f'%"correlation_id": "{correlation_id}"%',)).fetchall()
            cand_rows = self._conn.execute(
                "SELECT * FROM fact_candidates WHERE correlation_id = ? "
                "ORDER BY id ASC", (correlation_id,)).fetchall()
            run_rows = self._conn.execute(
                "SELECT * FROM agent_runs WHERE correlation_id = ? "
                "ORDER BY id ASC", (correlation_id,)).fetchall()
            event_ids = [int(r["id"]) for r in ev_rows]
            fact_rows = []
            if event_ids:
                placeholders = ",".join("?" * len(event_ids))
                fact_rows = self._conn.execute(
                    f"SELECT * FROM facts WHERE source_event_id IN "
                    f"({placeholders}) ORDER BY id ASC", event_ids).fetchall()

        events = []
        for r in ev_rows:
            try:
                meta = json.loads(r["meta"] or "{}")
            except Exception:
                meta = {}
            events.append({
                "id": int(r["id"]), "time": r["time"], "modality": r["modality"],
                "raw": r["raw"] or "", "summary": r["summary"] or "",
                "source": r["source"] or "", "meta": meta,
            })
        candidates = []
        for r in cand_rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload_json") or "{}")
            except Exception:
                d["payload"] = {}
            candidates.append(d)
        return {
            "correlation_id": correlation_id,
            "events": events,
            "candidates": candidates,
            "facts": [dict(r) for r in fact_rows],
            "agent_runs": [dict(r) for r in run_rows],
        }

    # ------------------------------ facts --------------------------------
    def add_task(self, text: str, *, source_event_id: int | None = None,
                 source_span: str = "", confidence: float | None = None,
                 owner_person_id: int | None = None, due: str | None = None,
                 extracted_at: float) -> int:
        """Insert a `task` fact + its typed row. Returns the fact id."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts (kind, source_event_id, source_span, confidence, "
                "extracted_at) VALUES ('task', ?, ?, ?, ?)",
                (source_event_id, source_span, confidence, extracted_at),
            )
            fid = int(cur.lastrowid)
            self._conn.execute(
                "INSERT INTO tasks (fact_id, text, owner_person_id, due, status) "
                "VALUES (?, ?, ?, ?, 'open')",
                (fid, text, owner_person_id, due),
            )
            self._conn.commit()
        # Creation is the first access on the fact's memory trace (A1).
        self.record_node_access("fact", fid, extracted_at)
        self.mark_graph_dirty("fact", fid, ts=extracted_at)
        if owner_person_id:
            self.mark_graph_dirty("person", int(owner_person_id),
                                  ts=extracted_at)
        return fid

    def add_commitment(self, text: str, *, source_event_id: int | None = None,
                       source_span: str = "", confidence: float | None = None,
                       from_person_id: int | None = None,
                       to_person_id: int | None = None, due: str | None = None,
                       extracted_at: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts (kind, source_event_id, source_span, confidence, "
                "extracted_at) VALUES ('commitment', ?, ?, ?, ?)",
                (source_event_id, source_span, confidence, extracted_at),
            )
            fid = int(cur.lastrowid)
            self._conn.execute(
                "INSERT INTO commitments (fact_id, text, from_person_id, to_person_id, "
                "due, status, state, counterparty_expects) "
                "VALUES (?, ?, ?, ?, ?, 'open', 'detected', 0)",
                (fid, text, from_person_id, to_person_id, due),
            )
            self._conn.commit()
        self.record_node_access("fact", fid, extracted_at)
        self.mark_graph_dirty("fact", fid, ts=extracted_at)
        for pid in (from_person_id, to_person_id):
            if pid:
                self.mark_graph_dirty("person", int(pid), ts=extracted_at)
        return fid

    def add_claim(self, text: str, *, source_event_id: int | None = None,
                  source_span: str = "", confidence: float | None = None,
                  extracted_at: float) -> int:
        """A claim is a fact with no typed detail table — text lives in
        facts.text. `source_span` is strictly the verbatim provenance quote:
        empty when there is none (user-typed claims), never a paraphrase
        substitute — extracted claims with no span are the gate's job to drop."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts (kind, text, source_event_id, source_span, "
                "confidence, extracted_at) VALUES ('claim', ?, ?, ?, ?, ?)",
                (text, source_event_id, source_span, confidence, extracted_at),
            )
            fid = int(cur.lastrowid)
            self._conn.commit()
        self.record_node_access("fact", fid, extracted_at)
        return fid

    def add_question(self, text: str, *, source_event_id: int | None = None,
                     source_span: str = "", confidence: float | None = None,
                     extracted_at: float) -> int:
        """Open question from extraction (plan 4.3) — flat fact, kind=question."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts (kind, text, source_event_id, source_span, "
                "confidence, extracted_at) VALUES ('question', ?, ?, ?, ?, ?)",
                (text, source_event_id, source_span, confidence, extracted_at),
            )
            fid = int(cur.lastrowid)
            self._conn.commit()
        self.record_node_access("fact", fid, extracted_at)
        return fid

    def open_tasks(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT t.fact_id, t.text, t.due, t.status, t.owner_person_id,
                       p.canonical_name AS owner, f.source_event_id, f.source_span,
                       f.confidence, f.extracted_at
                FROM tasks t
                JOIN facts f ON f.id = t.fact_id
                LEFT JOIN people p ON p.id = t.owner_person_id
                WHERE t.status = 'open'
                  AND COALESCE(f.state, 'active') != 'escrowed'
                ORDER BY f.extracted_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_task_status(self, fact_id: int, status: str) -> bool:
        if status not in ("open", "done", "cancelled"):
            raise ValueError(f"invalid task status: {status}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status = ? WHERE fact_id = ?", (status, fact_id))
            self._conn.commit()
            return cur.rowcount > 0

    def facts_by_kind(self, kind: str, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM facts WHERE kind = ? ORDER BY extracted_at DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def fact_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])

    # ------------------------------ knowledge graph ----------------------
    def all_people(self) -> list[dict]:
        out = []
        with self._lock:
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(people)").fetchall()}
            extra = ""
            if "promotion_state" in cols:
                extra = (", actor_type, promotion_state, canonical_person_id, "
                         "public_figure, hide_from_people")
            rows = self._conn.execute(
                f"SELECT id, canonical_name, aliases, last_seen{extra} "
                "FROM people ORDER BY id").fetchall()
        for r in rows:
            try:
                aliases = json.loads(r["aliases"] or "[]")
            except Exception:
                aliases = []
            d = {"id": int(r["id"]), "name": r["canonical_name"],
                 "aliases": aliases, "last_seen": r["last_seen"]}
            if "promotion_state" in r.keys():
                d.update({
                    "actor_type": r["actor_type"] or "human_person",
                    "promotion_state": r["promotion_state"] or "candidate",
                    "canonical_person_id": r["canonical_person_id"],
                    "public_figure": bool(r["public_figure"] or 0),
                    "hide_from_people": bool(r["hide_from_people"] or 0),
                })
            out.append(d)
        return out

    def all_entities(self, *, include_hidden: bool = False) -> list[dict]:
        with self._lock:
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(entities)").fetchall()}
            has_hidden = "hidden" in cols
            sql = "SELECT id, canonical_name, kind, aliases, last_seen"
            if has_hidden:
                sql += ", hidden"
            sql += " FROM entities"
            if has_hidden and not include_hidden:
                sql += " WHERE COALESCE(hidden, 0) = 0"
            sql += " ORDER BY id"
            rows = self._conn.execute(sql).fetchall()
        out = []
        for r in rows:
            d = {"id": int(r["id"]), "name": r["canonical_name"], "kind": r["kind"],
                 "aliases": json.loads(r["aliases"] or "[]"),
                 "last_seen": r["last_seen"]}
            if has_hidden:
                d["hidden"] = bool(r["hidden"] or 0)
            out.append(d)
        return out

    def set_person_hidden(self, person_id: int, *, hidden: bool = True,
                          public_figure: bool = False,
                          ts: float | None = None) -> None:
        """Soft-hide a person from People/contacts/constellation (reversible)."""
        import time as _t
        ts = ts if ts is not None else _t.time()
        with self._lock:
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(people)").fetchall()}
            if "hide_from_people" not in cols:
                return
            self._conn.execute(
                "UPDATE people SET hide_from_people = ?, "
                "public_figure = CASE WHEN ? THEN 1 ELSE COALESCE(public_figure, 0) END, "
                "promotion_state = CASE WHEN ? THEN 'archived' ELSE promotion_state END, "
                "last_seen = COALESCE(?, last_seen) WHERE id = ?",
                (1 if hidden else 0, 1 if public_figure else 0,
                 1 if hidden else 0, ts, int(person_id)))
            self._conn.commit()

    def set_entity_hidden(self, entity_id: int, *, hidden: bool = True) -> None:
        """Soft-hide an org/tool from constellation (reversible)."""
        with self._lock:
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(entities)").fetchall()}
            if "hidden" not in cols:
                return
            self._conn.execute(
                "UPDATE entities SET hidden = ? WHERE id = ?",
                (1 if hidden else 0, int(entity_id)))
            self._conn.commit()

    def touch_entity(self, eid: int, ts: float | None = None,
                     alias: str | None = None) -> None:
        """Bump last_seen and record a new spelling as an alias — the entity twin
        of touch_person, so a corrected mis-hearing ('Dell Capitol') stays
        inspectable on the canonical row and re-pulls future variants."""
        with self._lock:
            row = self._conn.execute(
                "SELECT aliases FROM entities WHERE id = ?", (eid,)).fetchone()
            if row is None:
                return
            aliases = json.loads(row["aliases"] or "[]")
            a = (alias or "").strip()
            if a and a.lower() not in {x.lower() for x in aliases}:
                aliases.append(a)
                self._conn.execute(
                    "UPDATE entities SET last_seen = ?, aliases = ? WHERE id = ?",
                    (ts, json.dumps(aliases), eid))
            else:
                self._conn.execute(
                    "UPDATE entities SET last_seen = ? WHERE id = ?", (ts, eid))
            self._conn.commit()

    # --- recency-ranked, for ASR vocabulary bias (#3/#11) ----------------
    def recent_people(self, limit: int = 25) -> list[dict]:
        """People most recently seen first (last_seen DESC), with their aliases —
        the 'known people nearby' that bias Whisper toward the right spelling."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT canonical_name, aliases FROM people "
                "ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        return [{"name": r["canonical_name"], "aliases": json.loads(r["aliases"] or "[]")}
                for r in rows]

    def recent_entities(self, limit: int = 20) -> list[dict]:
        """Entities most recently seen first — active projects/orgs/products."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT canonical_name, kind FROM entities "
                "ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        return [{"name": r["canonical_name"], "kind": r["kind"]} for r in rows]

    def fact_person_links(self) -> list[tuple[int, int, str]]:
        """(fact_id, person_id, role) from the typed task/commitment rows.

        Escrowed facts (People v3 P3) are excluded: even a NAMED counterparty
        on an identity-less row must not feed person edges / people scoring
        until the rebind reactivates the fact."""
        out: list[tuple[int, int, str]] = []
        with self._lock:
            for pid, role in (("owner_person_id", "responsible_for"),):
                for r in self._conn.execute(
                        f"SELECT t.fact_id, t.{pid} AS p FROM tasks t "
                        f"JOIN facts f ON f.id = t.fact_id "
                        f"WHERE t.{pid} IS NOT NULL "
                        f"AND COALESCE(f.state, 'active') != 'escrowed'"):
                    out.append((int(r["fact_id"]), int(r["p"]), role))
            for pid, role in (("from_person_id", "committed"),
                              ("to_person_id", "owed")):
                for r in self._conn.execute(
                        f"SELECT c.fact_id, c.{pid} AS p FROM commitments c "
                        f"JOIN facts f ON f.id = c.fact_id "
                        f"WHERE c.{pid} IS NOT NULL "
                        f"AND COALESCE(f.state, 'active') != 'escrowed'"):
                    out.append((int(r["fact_id"]), int(r["p"]), role))
        return out

    def add_relation(self, subj_type: str, subj_id: int, predicate: str,
                     obj_type: str, obj_id: int, *, weight: float = 1.0,
                     origin: str = "derived", source_event_id: int | None = None,
                     confidence: float | None = None, ts: float | None = None,
                     quote: str | None = None,
                     source_class: str | None = None) -> None:
        """Insert an edge, or bump its weight if it already exists (co-occurrence).

        Asserted/user edges dual-write into kg_predicates + kg_evidence (KG-A).
        Derived rebuild edges stay out of the belief store (features only).
        """
        if not subj_id or not obj_id:
            return  # unresolved endpoint (e.g. 'me' or an unknown name) — skip
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO relations (subj_type, subj_id, predicate, obj_type,
                                       obj_id, weight, origin, source_event_id,
                                       confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subj_type, subj_id, predicate, obj_type, obj_id)
                DO UPDATE SET weight = weight + excluded.weight,
                              source_event_id = COALESCE(relations.source_event_id,
                                                         excluded.source_event_id)
                """,
                (subj_type, subj_id, predicate, obj_type, obj_id, weight, origin,
                 source_event_id, confidence, ts),
            )
            self._conn.commit()
        if origin in ("asserted", "user"):
            try:
                from app.services import kg_beliefs
                kg_beliefs.record_from_relation(
                    self, subj_type=subj_type, subj_id=int(subj_id),
                    predicate=predicate, obj_type=obj_type, obj_id=int(obj_id),
                    origin=origin, source_event_id=source_event_id,
                    confidence=confidence, ts=ts, quote=quote,
                    source_class=source_class)
            except Exception as exc:
                print(f"[kg_beliefs] dual-write skipped ({exc}).")

    def fact_entities(self, fact_ids: list[int]) -> dict[int, list[str]]:
        """{fact_id: [entity canonical_name, …]} via (fact, about, entity)
        edges — batch lookup for the trigger signal scan."""
        ids = [int(f) for f in fact_ids if f]
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT r.subj_id AS fact_id, e.canonical_name AS name
                FROM relations r
                JOIN entities e ON e.id = r.obj_id
                WHERE r.subj_type = 'fact' AND r.predicate = 'about'
                  AND r.obj_type = 'entity' AND r.subj_id IN ({marks})
                ORDER BY r.weight DESC
                """, ids).fetchall()
        out: dict[int, list[str]] = {}
        for r in rows:
            out.setdefault(int(r["fact_id"]), []).append(r["name"])
        return out

    def clear_relations(self, origin: str | None = None,
                        incident_to: set[tuple[str, int]] | None = None) -> int:
        """Delete edges. With `origin`, only that class (so a graph rebuild wipes
        'derived' edges without touching 'asserted' extractor edges).

        `incident_to`: if set, only delete edges whose subject OR object is in
        this (node_type, node_id) set — used by incremental rebuild.
        """
        with self._lock:
            if incident_to:
                self._conn.execute("DROP TABLE IF EXISTS _dirty_eps")
                self._conn.execute(
                    "CREATE TEMP TABLE _dirty_eps "
                    "(node_type TEXT, node_id INTEGER)")
                self._conn.executemany(
                    "INSERT OR IGNORE INTO _dirty_eps(node_type, node_id) "
                    "VALUES (?,?)",
                    list(incident_to))
                if origin is None:
                    cur = self._conn.execute(
                        "DELETE FROM relations WHERE "
                        "(subj_type, subj_id) IN "
                        "(SELECT node_type, node_id FROM _dirty_eps) "
                        "OR (obj_type, obj_id) IN "
                        "(SELECT node_type, node_id FROM _dirty_eps)")
                else:
                    cur = self._conn.execute(
                        "DELETE FROM relations WHERE origin = ? AND ("
                        "(subj_type, subj_id) IN "
                        "(SELECT node_type, node_id FROM _dirty_eps) "
                        "OR (obj_type, obj_id) IN "
                        "(SELECT node_type, node_id FROM _dirty_eps))",
                        (origin,))
                self._conn.execute("DROP TABLE IF EXISTS _dirty_eps")
                self._conn.commit()
                return int(cur.rowcount or 0)
            if origin is None:
                cur = self._conn.execute("DELETE FROM relations")
            else:
                cur = self._conn.execute(
                    "DELETE FROM relations WHERE origin = ?", (origin,))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def mark_graph_dirty(self, node_type: str, node_id: int,
                         *, ts: float | None = None) -> None:
        """Tag a node touched by extraction — feeds rebuild(scope=dirty).

        Acquires ``self._lock``. Callers that already hold the lock must NOT
        call this (non-reentrant Lock → deadlock); mark after releasing.
        """
        import time as _time
        if not node_type or not node_id:
            return
        ts = float(ts if ts is not None else _time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO graph_dirty(node_type, node_id, ts) "
                "VALUES (?,?,?) "
                "ON CONFLICT(node_type, node_id) DO UPDATE SET ts = excluded.ts",
                (str(node_type), int(node_id), ts))
            self._conn.commit()

    def graph_dirty_nodes(self) -> set[tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_type, node_id FROM graph_dirty").fetchall()
        return {(str(r[0]), int(r[1])) for r in rows}

    def clear_graph_dirty(
        self, nodes: set[tuple[str, int]] | None = None
    ) -> int:
        with self._lock:
            if nodes is None:
                cur = self._conn.execute("DELETE FROM graph_dirty")
                self._conn.commit()
                return int(cur.rowcount or 0)
            n = 0
            for nt, nid in nodes:
                c = self._conn.execute(
                    "DELETE FROM graph_dirty WHERE node_type = ? AND node_id = ?",
                    (nt, int(nid)))
                n += int(c.rowcount or 0)
            self._conn.commit()
            return n

    def derived_edge_set(self) -> set[tuple]:
        """Canonical derived-edge fingerprint for differential rebuild tests."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT subj_type, subj_id, predicate, obj_type, obj_id "
                "FROM relations WHERE origin = 'derived' "
                "ORDER BY 1,2,3,4,5"
            ).fetchall()
        return {
            (str(r[0]), int(r[1]), str(r[2]), str(r[3]), int(r[4]))
            for r in rows
        }

    def user_asserted_edge_set(self) -> set[tuple]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT subj_type, subj_id, predicate, obj_type, obj_id, origin "
                "FROM relations WHERE origin IN ('user','asserted') "
                "ORDER BY 1,2,3,4,5,6"
            ).fetchall()
        return {
            (str(r[0]), int(r[1]), str(r[2]), str(r[3]), int(r[4]), str(r[5]))
            for r in rows
        }

    # --- KG v2 identity keys (Change 1) ------------------------------------
    def add_node_keys(self, node_type: str, node_id: int, name: str, *,
                      key_type: str = "norm_name",
                      ts: float | None = None) -> None:
        """Write blocking keys for an observed name/alias. Idempotent."""
        import time as _time
        from app.services import kg_keys
        now = float(ts if ts is not None else _time.time())
        keys = kg_keys.blocking_keys(name, key_type=key_type)
        if not keys:
            return
        with self._lock:
            for kt, kv in keys:
                self._conn.execute(
                    "INSERT OR IGNORE INTO kg_node_keys "
                    "(node_type, node_id, key_type, key_value, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (node_type, int(node_id), kt, kv, now))
            self._conn.commit()

    def lookup_node_keys(self, node_type: str, key_type: str,
                         key_value: str) -> list[int]:
        """All node ids sharing one blocking key (ambiguity is expected)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT node_id FROM kg_node_keys "
                "WHERE node_type=? AND key_type=? AND key_value=?",
                (node_type, key_type, key_value)).fetchall()
        return [int(r["node_id"]) for r in rows]

    def list_node_keys(self, node_type: str, node_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key_type, key_value, created_at FROM kg_node_keys "
                "WHERE node_type=? AND node_id=? ORDER BY created_at",
                (node_type, int(node_id))).fetchall()
        return [dict(r) for r in rows]

    def copy_node_keys(self, node_type: str, from_id: int, to_id: int) -> None:
        """Merge support: winner inherits the loser's keys; loser keeps its own."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO kg_node_keys "
                "(node_type, node_id, key_type, key_value, created_at) "
                "SELECT node_type, ?, key_type, key_value, created_at "
                "FROM kg_node_keys WHERE node_type=? AND node_id=?",
                (int(to_id), node_type, int(from_id)))
            self._conn.commit()

    # --- KG v2 node attrs (Change 2) ---------------------------------------
    def set_node_attr(self, node_type: str, node_id: int, key: str,
                      value: str, *, confidence: float | None = None,
                      valid_from: float | None = None,
                      valid_to: float | None = None,
                      ts: float | None = None) -> int:
        """Upsert one attribute interval; (node, key, ifnull(valid_from,-1))
        is unique via uq_attr_temporal, so atemporal rows can't duplicate."""
        import time as _time
        now = float(ts if ts is not None else _time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO kg_node_attrs (node_type, node_id, key, value, "
                "confidence, valid_from, valid_to, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(node_type, node_id, key, ifnull(valid_from,-1)) "
                "DO UPDATE SET value=excluded.value, "
                "confidence=excluded.confidence, valid_to=excluded.valid_to, "
                "updated_at=excluded.updated_at",
                (node_type, int(node_id), key, value, confidence,
                 valid_from, valid_to, now, now))
            self._conn.commit()
            return int(cur.lastrowid)

    def list_node_attrs(self, node_type: str, node_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM kg_node_attrs WHERE node_type=? AND node_id=? "
                "ORDER BY key, valid_from", (node_type, int(node_id))).fetchall()
        return [dict(r) for r in rows]

    # --- KG v2 adjudications + config (Change 4) ---------------------------
    def log_adjudication(self, *, kind: str, decision: str, decided_by: str,
                         features: dict, predicate_id: int | None = None,
                         evidence_id: int | None = None,
                         node_a: int | None = None, node_b: int | None = None,
                         model_score: float | None = None,
                         ts: float | None = None) -> int:
        import time as _time
        now = float(ts if ts is not None else _time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO kg_adjudications (kind, predicate_id, evidence_id, "
                "node_a, node_b, features_json, model_score, decision, "
                "decided_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (kind, predicate_id, evidence_id, node_a, node_b,
                 json.dumps(features or {}), model_score, decision,
                 decided_by, now))
            self._conn.commit()
            return int(cur.lastrowid)

    def list_adjudications(self, *, kind: str | None = None,
                           limit: int = 100) -> list[dict]:
        with self._lock:
            if kind:
                rows = self._conn.execute(
                    "SELECT * FROM kg_adjudications WHERE kind=? "
                    "ORDER BY id DESC LIMIT ?", (kind, int(limit))).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM kg_adjudications ORDER BY id DESC LIMIT ?",
                    (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def get_kg_config(self, key: str) -> tuple[int, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, value_json FROM kg_config WHERE key=?",
                (key,)).fetchone()
        if row is None:
            return None
        try:
            return int(row["version"]), json.loads(row["value_json"])
        except Exception:
            return None

    def set_kg_config(self, key: str, value: Any,
                      ts: float | None = None) -> int:
        """Write a config value; version bumps on every write."""
        import time as _time
        now = float(ts if ts is not None else _time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO kg_config (key, version, value_json, updated_at) "
                "VALUES (?, 1, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET version=version+1, "
                "value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, json.dumps(value), now))
            row = self._conn.execute(
                "SELECT version FROM kg_config WHERE key=?", (key,)).fetchone()
            self._conn.commit()
            return int(row["version"])

    # --- KG v2 predicate lifecycle (Change 3) ------------------------------
    def list_kg_competitors(self, *, subj_type: str, subj_id: int,
                            predicate: str, exclude_id: int) -> list[dict]:
        """Open active beliefs on the same (subject, predicate) with a
        DIFFERENT object — the conflict candidates for functional predicates."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM kg_predicates WHERE subj_type=? AND subj_id=? "
                "AND predicate=? AND status='active' AND valid_to IS NULL "
                "AND id != ?",
                (subj_type, int(subj_id), predicate, int(exclude_id))).fetchall()
        return [dict(r) for r in rows]

    def supersede_kg_predicate(self, old_id: int, new_id: int, *,
                               valid_to: float,
                               ts: float | None = None) -> None:
        """Temporal split: close the old belief's interval; it stays truthful
        WITHIN [valid_from, valid_to] — this is not a deletion."""
        import time as _time
        now = float(ts if ts is not None else _time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE kg_predicates SET valid_to=?, status='superseded', "
                "superseded_by=?, updated_at=? WHERE id=?",
                (float(valid_to), int(new_id), now, int(old_id)))
            self._conn.commit()

    def reassign_kg_predicate_node(self, predicate_id: int, node_type: str,
                                   old_id: int, new_id: int,
                                   ts: float | None = None) -> bool:
        """Manual split support (Change 7): point one belief (and its whole
        evidence bag, which hangs off the predicate) at a different node.
        Mirrors the change into legacy `relations` so constellation agrees."""
        import time as _time
        now = float(ts if ts is not None else _time.time())
        pred = self.get_kg_predicate(int(predicate_id))
        if not pred:
            return False
        touched = False
        with self._lock:
            for side in ("subj", "obj"):
                if pred[f"{side}_type"] == node_type and \
                        int(pred[f"{side}_id"]) == int(old_id):
                    self._conn.execute(
                        f"UPDATE kg_predicates SET {side}_id=?, "
                        "posterior_stale=1, updated_at=? WHERE id=?",
                        (int(new_id), now, int(predicate_id)))
                    self._conn.execute(
                        f"UPDATE OR IGNORE relations SET {side}_id=? "
                        f"WHERE subj_type=? AND subj_id=? AND predicate=? "
                        "AND obj_type=? AND obj_id=?",
                        (int(new_id), pred["subj_type"], pred["subj_id"],
                         pred["predicate"], pred["obj_type"], pred["obj_id"]))
                    touched = True
            if touched:
                self._conn.commit()
        return touched

    def set_kg_predicate_conflict(self, predicate_id: int, flag: bool,
                                  ts: float | None = None) -> None:
        import time as _time
        now = float(ts if ts is not None else _time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE kg_predicates SET conflict=?, updated_at=? WHERE id=?",
                (1 if flag else 0, now, int(predicate_id)))
            self._conn.commit()

    # --- KG-A belief store -------------------------------------------------
    def upsert_kg_predicate(
        self, *, subj_type: str, subj_id: int, predicate: str,
        obj_type: str, obj_id: int, layer: str = "asserted",
        confidence: float | None = None, ts: float | None = None,
        valid_from: float | None = None,
    ) -> int:
        """Find-or-create an active open-ended predicate; bump last_seen."""
        import time as _time
        now = float(ts if ts is not None else _time.time())
        conf = float(confidence if confidence is not None else 0.5)
        with self._lock:
            row = self._conn.execute(
                "SELECT id, confidence FROM kg_predicates "
                "WHERE subj_type=? AND subj_id=? AND predicate=? "
                "AND obj_type=? AND obj_id=? AND status='active' "
                "AND valid_to IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (subj_type, int(subj_id), predicate, obj_type, int(obj_id)),
            ).fetchone()
            if row:
                pid = int(row["id"])
                # Confidence only rises from dual-write bumps; full posterior
                # recomputation lives in kg_beliefs.recompute_confidence.
                self._conn.execute(
                    "UPDATE kg_predicates SET last_seen=?, updated_at=?, "
                    "confidence=MAX(confidence, ?) WHERE id=?",
                    (now, now, conf, pid))
                self._conn.commit()
                return pid
            cur = self._conn.execute(
                "INSERT INTO kg_predicates "
                "(subj_type, subj_id, predicate, obj_type, obj_id, layer, "
                " confidence, valid_from, valid_to, first_seen, last_seen, "
                " status, protected, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'active', 0, ?, ?)",
                (subj_type, int(subj_id), predicate, obj_type, int(obj_id),
                 layer, conf, valid_from if valid_from is not None else now,
                 now, now, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def add_kg_evidence(
        self, predicate_id: int, *, event_id: int | None = None,
        fact_id: int | None = None, modality: str | None = None,
        source_class: str | None = None, quote: str | None = None,
        extractor_conf: float | None = None, faithfulness: float | None = None,
        weight: float = 1.0, created_by: str = "system",
        observed_at: float | None = None, meta: dict | None = None,
    ) -> int | None:
        """Append evidence; dedupe on (predicate, event, quote_hash)."""
        import hashlib
        import time as _time
        now = float(observed_at if observed_at is not None else _time.time())
        q = (quote or "")[:2000]
        qh = hashlib.sha256(q.encode("utf-8", errors="replace")).hexdigest()[:32]
        # UNIQUE allows multiple NULLs for event_id — use 0 sentinel for dedupe.
        eid = int(event_id) if event_id is not None else 0
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO kg_evidence "
                    "(predicate_id, event_id, fact_id, modality, source_class, "
                    " quote, quote_hash, extractor_conf, faithfulness, "
                    " observed_at, weight, created_by, meta_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(predicate_id), eid, fact_id,
                     modality, source_class, q or None, qh,
                     extractor_conf, faithfulness, now, float(weight),
                     created_by,
                     json.dumps(meta) if meta else None),
                )
                # Change 5: intake never does posterior math — just flag.
                self._conn.execute(
                    "UPDATE kg_predicates SET posterior_stale=1 WHERE id=?",
                    (int(predicate_id),))
                self._conn.commit()
                return int(cur.lastrowid)
            except Exception:
                # Unique conflict or missing table — treat as already recorded.
                self._conn.rollback()
                return None

    def get_kg_evidence(self, evidence_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM kg_evidence WHERE id=?",
                (int(evidence_id),)).fetchone()
        return dict(row) if row else None

    def set_kg_evidence_weight(self, evidence_id: int, weight: float) -> None:
        """Rejection support: zero the weight (the row itself is append-only
        provenance and is never deleted)."""
        with self._lock:
            self._conn.execute(
                "UPDATE kg_evidence SET weight=? WHERE id=?",
                (float(weight), int(evidence_id)))
            self._conn.commit()

    def list_kg_evidence(self, predicate_id: int, *, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM kg_evidence WHERE predicate_id=? "
                "ORDER BY observed_at DESC LIMIT ?",
                (int(predicate_id), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_kg_predicate(self, predicate_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM kg_predicates WHERE id=?",
                (int(predicate_id),),
            ).fetchone()
        return dict(row) if row else None

    def find_kg_predicate(
        self, *, subj_type: str, subj_id: int, predicate: str,
        obj_type: str, obj_id: int,
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM kg_predicates "
                "WHERE subj_type=? AND subj_id=? AND predicate=? "
                "AND obj_type=? AND obj_id=? AND status='active' "
                "AND valid_to IS NULL ORDER BY id DESC LIMIT 1",
                (subj_type, int(subj_id), predicate, obj_type, int(obj_id)),
            ).fetchone()
        return dict(row) if row else None

    def list_kg_predicates(
        self, *, subj_type: str | None = None, subj_id: int | None = None,
        obj_type: str | None = None, obj_id: int | None = None,
        predicate: str | None = None,
        statuses: tuple[str, ...] = ("active",), limit: int = 200,
    ) -> list[dict]:
        """Belief rows by any endpoint. Change 6: people/network queries pass
        statuses=('active','superseded') — the implicit past tense is the
        common case for relationship memory."""
        where, args = [], []
        for col, val in (("subj_type", subj_type), ("subj_id", subj_id),
                         ("obj_type", obj_type), ("obj_id", obj_id),
                         ("predicate", predicate)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        args.extend(statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM kg_predicates WHERE {' AND '.join(where)} "
                "ORDER BY (status != 'active'), last_seen DESC LIMIT ?",
                (*args, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def set_kg_predicate_confidence(self, predicate_id: int, confidence: float,
                                    ts: float | None = None) -> None:
        import time as _time
        now = float(ts if ts is not None else _time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE kg_predicates SET confidence=?, updated_at=? WHERE id=?",
                (float(confidence), now, int(predicate_id)))
            self._conn.commit()

    # --- KG v2 lazy posteriors (Change 5) ----------------------------------
    def set_kg_posterior_cache(self, predicate_id: int, *, confidence: float,
                               logit_sum: float, weights_version: int,
                               ts: float) -> None:
        """Full-recompute result: cached time-invariant sum + clear stale."""
        with self._lock:
            self._conn.execute(
                "UPDATE kg_predicates SET confidence=?, logit_sum=?, "
                "weights_version=?, computed_at=?, posterior_stale=0, "
                "updated_at=? WHERE id=?",
                (float(confidence), float(logit_sum), int(weights_version),
                 float(ts), float(ts), int(predicate_id)))
            self._conn.commit()

    def list_stale_kg_predicates(self, *, limit: int = 500) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM kg_predicates WHERE posterior_stale=1 "
                "ORDER BY id LIMIT ?", (int(limit),)).fetchall()
        return [int(r["id"]) for r in rows]

    def list_kg_evidence_times(self, predicate_id: int) -> list[float]:
        """observed_at of counted (non-rejected) evidence — the only thing a
        decay-only refresh needs; served by idx_kg_ev_pred."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT observed_at FROM kg_evidence WHERE predicate_id=? "
                "AND weight > 0", (int(predicate_id),)).fetchall()
        return [float(r["observed_at"]) for r in rows]

    def get_person(self, person_id: int) -> dict | None:
        """One person row with parsed aliases — the People tab's detail view."""
        with self._lock:
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(people)").fetchall()}
            extra = ""
            if "promotion_state" in cols:
                extra = (", actor_type, promotion_state, canonical_person_id, "
                         "public_figure, hide_from_people")
            row = self._conn.execute(
                f"SELECT id, canonical_name, aliases, first_seen, last_seen, "
                f"canonical_id{extra} "
                "FROM people WHERE id = ?", (person_id,)).fetchone()
        if row is None:
            return None
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except Exception:
            aliases = []
        out = {"id": int(row["id"]), "name": row["canonical_name"],
               "aliases": aliases, "first_seen": row["first_seen"],
               "last_seen": row["last_seen"],
               "canonical_id": row["canonical_id"]}
        if "promotion_state" in row.keys():
            out.update({
                "actor_type": row["actor_type"] or "human_person",
                "promotion_state": row["promotion_state"] or "candidate",
                "canonical_person_id": row["canonical_person_id"],
                "public_figure": bool(row["public_figure"] or 0),
                "hide_from_people": bool(row["hide_from_people"] or 0),
            })
        return out

    def set_person_promotion(self, person_id: int, state: str,
                             ts: float | None = None) -> None:
        with self._lock:
            cols = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(people)").fetchall()}
            if "promotion_state" not in cols:
                return
            self._conn.execute(
                "UPDATE people SET promotion_state = ?, last_seen = COALESCE(?, last_seen) "
                "WHERE id = ?", (state, ts, person_id))
            self._conn.commit()

    def soft_merge_people(self, survivor_id: int, absorbed_id: int, *,
                          reason: str = "", confidence: float = 0.0,
                          actor: str = "user", ts: float | None = None) -> int:
        """Redirect absorbed → survivor without deleting rows."""
        import time as _t
        ts = ts if ts is not None else _t.time()
        with self._lock:
            self._conn.execute(
                "UPDATE people SET canonical_person_id = ?, hide_from_people = 1, "
                "promotion_state = 'archived' WHERE id = ?",
                (survivor_id, absorbed_id))
            cur = self._conn.execute(
                "INSERT INTO merge_operations "
                "(survivor_person_id, absorbed_person_id, mode, reason, confidence, "
                "resolver_version, actor, decided_at) VALUES (?,?,?,?,?,?,?,?)",
                (survivor_id, absorbed_id, "soft_merge", reason, confidence,
                 "people_v2.1", actor, ts))
            self._conn.commit()
            op_id = int(cur.lastrowid)
        # Change 1: the winner inherits the loser's blocking keys (never deleted).
        self.copy_node_keys("person", absorbed_id, survivor_id)
        # People v3 P3: bound voice tracks follow the merge (each re-pointed
        # track enqueues a durable rebind job). No-op while escrow is off.
        try:
            from app.services import people_escrow
            people_escrow.on_person_merged(self, survivor_id, absorbed_id, ts)
        except Exception as exc:
            print(f"[storage] escrow merge hook skipped ({exc}).")
        # Change 4: merges feed the weight-fitting flywheel.
        try:
            self.log_adjudication(
                kind="merge_accept", decision="accept",
                decided_by=("auto" if actor != "user" else "user"),
                node_a=int(survivor_id), node_b=int(absorbed_id),
                model_score=float(confidence or 0),
                features={"reason": reason, "resolver_confidence": confidence,
                          "resolver_version": "people_v2.1", "actor": actor},
                ts=ts)
        except Exception:
            pass
        return op_id

    def rename_person(self, person_id: int, name: str) -> bool:
        """Correct a person's canonical name (the old spelling is kept as an
        alias so future mentions still resolve). False when the new name is
        empty or already belongs to another person — that's a merge, not a
        rename, and merging is deliberate tooling, not a silent side effect."""
        name = (name or "").strip()
        if not name:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT canonical_name, aliases FROM people WHERE id = ?",
                (person_id,)).fetchone()
            if row is None:
                return False
            clash = self._conn.execute(
                "SELECT id FROM people WHERE canonical_name = ? AND id != ?",
                (name, person_id)).fetchone()
            if clash is not None:
                return False
            try:
                aliases = json.loads(row["aliases"] or "[]")
            except Exception:
                aliases = []
            old = row["canonical_name"]
            if old and old.lower() != name.lower() and \
                    old.lower() not in {a.lower() for a in aliases}:
                aliases.append(old)
            self._conn.execute(
                "UPDATE people SET canonical_name = ?, aliases = ? WHERE id = ?",
                (name, json.dumps(aliases), person_id))
            self._conn.commit()
        # Both spellings stay findable as blocking keys.
        self.add_node_keys("person", person_id, name)
        if old:
            self.add_node_keys("person", person_id, old, key_type="alias_norm")
        return True

    def delete_person(self, person_id: int) -> dict:
        """Remove a person node + its graph edges, DETACHING (not deleting) any
        facts it owned — a junk owner shouldn't take a real task with it. Returns
        the deleted row (for a cleanup backup). Used by graph hygiene tooling."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, canonical_name, aliases FROM people WHERE id = ?",
                (person_id,)).fetchone()
            if row is None:
                return {}
            self._conn.execute(
                "DELETE FROM relations WHERE (subj_type='person' AND subj_id=?) "
                "OR (obj_type='person' AND obj_id=?)", (person_id, person_id))
            self._conn.execute(
                "UPDATE tasks SET owner_person_id=NULL WHERE owner_person_id=?",
                (person_id,))
            self._conn.execute(
                "UPDATE commitments SET from_person_id=NULL WHERE from_person_id=?",
                (person_id,))
            self._conn.execute(
                "UPDATE commitments SET to_person_id=NULL WHERE to_person_id=?",
                (person_id,))
            self._conn.execute(
                "DELETE FROM person_attrs WHERE person_id=?", (person_id,))
            self._conn.execute("DELETE FROM people WHERE id=?", (person_id,))
            self._conn.commit()
            return dict(row)

    def get_entity(self, entity_id: int) -> dict | None:
        """One entity row with parsed aliases — the Orgs & tools tab detail."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, canonical_name, kind, aliases, first_seen, "
                "last_seen, canonical_id "
                "FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            return None
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except Exception:
            aliases = []
        return {"id": int(row["id"]), "name": row["canonical_name"],
                "kind": row["kind"], "aliases": aliases,
                "first_seen": row["first_seen"], "last_seen": row["last_seen"],
                "canonical_id": row["canonical_id"]}

    def rename_entity(self, entity_id: int, name: str) -> bool:
        """Correct an entity's canonical name; the old spelling becomes an
        alias. False on empty/unknown/case-insensitive collision (a collision
        is a merge, which must be deliberate)."""
        name = (name or "").strip()
        if not name:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT canonical_name, aliases FROM entities WHERE id = ?",
                (entity_id,)).fetchone()
            if row is None:
                return False
            clash = self._conn.execute(
                "SELECT id FROM entities WHERE canonical_name = ? COLLATE NOCASE "
                "AND id != ?", (name, entity_id)).fetchone()
            if clash is not None:
                return False
            try:
                aliases = json.loads(row["aliases"] or "[]")
            except Exception:
                aliases = []
            old = row["canonical_name"]
            if old and old.lower() != name.lower() and \
                    old.lower() not in {a.lower() for a in aliases}:
                aliases.append(old)
            self._conn.execute(
                "UPDATE entities SET canonical_name = ?, aliases = ? WHERE id = ?",
                (name, json.dumps(aliases), entity_id))
            self._conn.commit()
            return True

    def delete_entity(self, entity_id: int) -> dict:
        """Remove an entity node + its graph edges. Returns the deleted row."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, canonical_name, kind, aliases FROM entities WHERE id = ?",
                (entity_id,)).fetchone()
            if row is None:
                return {}
            self._conn.execute(
                "DELETE FROM relations WHERE (subj_type='entity' AND subj_id=?) "
                "OR (obj_type='entity' AND obj_id=?)", (entity_id, entity_id))
            self._conn.execute(
                "DELETE FROM entity_attrs WHERE entity_id=?", (entity_id,))
            self._conn.execute("DELETE FROM entities WHERE id=?", (entity_id,))
            self._conn.commit()
            return dict(row)

    def purge_source(self, source: str) -> dict:
        """Delete every event from `source` plus the facts extracted from them
        (task/commitment typed rows, fact + event graph edges included). Returns
        the deleted event ids + fact ids (so a caller can drop their vectors and
        write a backup). Used to roll back a bad ingest wholesale (e.g. the
        document scan that ate a codebase's own docs)."""
        with self._lock:
            ev_ids = [int(r["id"]) for r in self._conn.execute(
                "SELECT id FROM events WHERE source = ?", (source,)).fetchall()]
            fact_ids: list[int] = []
            if ev_ids:
                ph = ",".join("?" * len(ev_ids))
                fact_ids = [int(r["id"]) for r in self._conn.execute(
                    f"SELECT id FROM facts WHERE source_event_id IN ({ph})",
                    ev_ids).fetchall()]
            if fact_ids:
                fph = ",".join("?" * len(fact_ids))
                self._conn.execute(f"DELETE FROM tasks WHERE fact_id IN ({fph})", fact_ids)
                self._conn.execute(f"DELETE FROM commitments WHERE fact_id IN ({fph})", fact_ids)
                self._conn.execute(
                    f"DELETE FROM relations WHERE (subj_type='fact' AND subj_id IN ({fph})) "
                    f"OR (obj_type='fact' AND obj_id IN ({fph}))", fact_ids + fact_ids)
                self._conn.execute(f"DELETE FROM facts WHERE id IN ({fph})", fact_ids)
            if ev_ids:
                eph = ",".join("?" * len(ev_ids))
                self._conn.execute(
                    f"DELETE FROM relations WHERE (subj_type='event' AND subj_id IN ({eph})) "
                    f"OR (obj_type='event' AND obj_id IN ({eph}))", ev_ids + ev_ids)
                self._conn.execute(f"DELETE FROM events WHERE id IN ({eph})", ev_ids)
            self._conn.commit()
            return {"events": ev_ids, "facts": fact_ids}

    def relations_of(self, node_type: str, node_id: int) -> dict:
        """All edges touching a node, split into outgoing/incoming."""
        with self._lock:
            out = self._conn.execute(
                "SELECT * FROM relations WHERE subj_type = ? AND subj_id = ? "
                "ORDER BY weight DESC", (node_type, node_id)).fetchall()
            inc = self._conn.execute(
                "SELECT * FROM relations WHERE obj_type = ? AND obj_id = ? "
                "ORDER BY weight DESC", (node_type, node_id)).fetchall()
        return {"out": [dict(r) for r in out], "in": [dict(r) for r in inc]}

    def relation_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0])

    def delete_edges_between(self, type_a: str, id_a: int, type_b: str, id_b: int,
                             *, predicates: list[str] | None = None) -> int:
        """Remove edges (both directions) between two nodes. Optional predicate filter."""
        with self._lock:
            if predicates:
                ph = ",".join("?" * len(predicates))
                cur = self._conn.execute(
                    f"""
                    DELETE FROM relations WHERE predicate IN ({ph}) AND (
                      (subj_type = ? AND subj_id = ? AND obj_type = ? AND obj_id = ?)
                      OR (subj_type = ? AND subj_id = ? AND obj_type = ? AND obj_id = ?)
                    )
                    """,
                    (*predicates, type_a, id_a, type_b, id_b, type_b, id_b, type_a, id_a),
                )
            else:
                cur = self._conn.execute(
                    """
                    DELETE FROM relations WHERE
                      (subj_type = ? AND subj_id = ? AND obj_type = ? AND obj_id = ?)
                      OR (subj_type = ? AND subj_id = ? AND obj_type = ? AND obj_id = ?)
                    """,
                    (type_a, id_a, type_b, id_b, type_b, id_b, type_a, id_a),
                )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def user_hidden_pairs(self) -> set[tuple[str, int, str, int]]:
        """Undirected pairs the human has hidden from the constellation."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT subj_type, subj_id, obj_type, obj_id FROM relations "
                "WHERE origin = 'user' AND predicate = 'hides'"
            ).fetchall()
        out: set[tuple[str, int, str, int]] = set()
        for r in rows:
            a = (r["subj_type"], int(r["subj_id"]))
            b = (r["obj_type"], int(r["obj_id"]))
            out.add((*a, *b) if a <= b else (*b, *a))
        return out

    def user_linked_pairs(self) -> list[tuple[str, int, str, int, float]]:
        """Manual links the human asserted (survive graph.rebuild)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT subj_type, subj_id, obj_type, obj_id, weight FROM relations "
                "WHERE origin = 'user' AND predicate = 'linked'"
            ).fetchall()
        return [(r["subj_type"], int(r["subj_id"]), r["obj_type"], int(r["obj_id"]),
                 float(r["weight"] or 1)) for r in rows]

    def user_pinned_nodes(self) -> set[tuple[str, int]]:
        """Nodes the human pinned into the constellation field."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT subj_type, subj_id FROM relations "
                "WHERE origin = 'user' AND predicate = 'pins'"
            ).fetchall()
        return {(r["subj_type"], int(r["subj_id"])) for r in rows}

    def set_constellation_pin(self, node_type: str, node_id: int, pinned: bool) -> bool:
        """Pin/unpin a constellation node (self-loop relation, origin=user)."""
        if node_type not in ("person", "entity", "fact") or not node_id:
            return False
        with self._lock:
            self._conn.execute(
                "DELETE FROM relations WHERE origin = 'user' AND predicate = 'pins' "
                "AND subj_type = ? AND subj_id = ?",
                (node_type, node_id),
            )
            self._conn.commit()
        if pinned:
            self.add_relation(
                node_type, node_id, "pins", node_type, node_id,
                weight=1.0, origin="user", confidence=1.0,
            )
        return True

    def constellation_hidden_nodes(self) -> set[tuple[str, int]]:
        """Nodes the human hid from the constellation field."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT subj_type, subj_id FROM relations "
                "WHERE origin = 'user' AND predicate = 'constellation_hidden'"
            ).fetchall()
        return {(r["subj_type"], int(r["subj_id"])) for r in rows}

    def set_constellation_hidden(self, node_type: str, node_id: int,
                                 hidden: bool) -> bool:
        if node_type not in ("person", "entity", "fact") or not node_id:
            return False
        with self._lock:
            self._conn.execute(
                "DELETE FROM relations WHERE origin = 'user' "
                "AND predicate = 'constellation_hidden' "
                "AND subj_type = ? AND subj_id = ?",
                (node_type, node_id),
            )
            self._conn.commit()
        if hidden:
            self.add_relation(
                node_type, node_id, "constellation_hidden", node_type, node_id,
                weight=1.0, origin="user", confidence=1.0,
            )
        return True

    # --------------------------- attention ledger -------------------------
    def add_context_snapshot(self, ts: float, *, app: str | None = None,
                             calendar_next: str | None = None,
                             mode: str | None = None,
                             seeds: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO context_snapshots (ts, app, calendar_next, mode, seeds) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, app, calendar_next, mode, seeds),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def latest_context_snapshot(self, *, max_age_s: float | None = None) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM context_snapshots ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        if max_age_s is not None:
            import time as _time
            if _time.time() - float(d["ts"]) > max_age_s:
                return None
        return d

    def add_attention_impressions(self, rows: list[dict]) -> int:
        """Bulk-insert surfaced-node rows. Each row: node_type, node_id, surface,
        and optionally layer/score/decomposition/context_id/outcome/detail.
        Rows arriving with an outcome (e.g. a miss) are born closed."""
        if not rows:
            return 0
        import time as _time
        now = _time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO attention_impressions "
                "(ts, node_type, node_id, surface, layer, score, decomposition, "
                " context_id, outcome, outcome_ts, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(
                    float(r.get("ts") or now), r["node_type"], int(r["node_id"]),
                    r["surface"], r.get("layer"), r.get("score"),
                    r.get("decomposition"), r.get("context_id"),
                    r.get("outcome"),
                    float(r["outcome_ts"]) if r.get("outcome_ts") else (
                        now if r.get("outcome") else None),
                    r.get("detail"),
                ) for r in rows],
            )
            self._conn.commit()
            return len(rows)

    def last_attention_ts(self, node_type: str, node_id: int,
                          surface: str) -> float | None:
        """Newest impression time for a node on a surface (miss detection:
        'was this ever recently ON the field?')."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS t FROM attention_impressions "
                "WHERE node_type = ? AND node_id = ? AND surface = ?",
                (node_type, node_id, surface),
            ).fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    def set_attention_outcome(self, node_type: str, node_id: int, outcome: str,
                              *, detail: str | None = None,
                              within_s: float = 6 * 3600.0) -> int:
        """Close the newest still-open impression for a node with the user's
        reaction. If nothing is open in the window, insert a standalone closed
        row — a reaction is signal even when the surfacing predates the ledger."""
        import time as _time
        now = _time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM attention_impressions "
                "WHERE node_type = ? AND node_id = ? AND outcome IS NULL "
                "AND ts >= ? ORDER BY ts DESC LIMIT 1",
                (node_type, node_id, now - within_s),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE attention_impressions "
                    "SET outcome = ?, outcome_ts = ?, detail = COALESCE(?, detail) "
                    "WHERE id = ?",
                    (outcome, now, detail, int(row["id"])),
                )
                self._conn.commit()
                return int(row["id"])
            cur = self._conn.execute(
                "INSERT INTO attention_impressions "
                "(ts, node_type, node_id, surface, outcome, outcome_ts, detail) "
                "VALUES (?, ?, ?, 'reaction', ?, ?, ?)",
                (now, node_type, node_id, outcome, now, detail),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def latest_attention_decomp(self, node_type: str, node_id: int
                                ) -> dict | None:
        """Newest score decomposition for a node — training features for β."""
        with self._lock:
            row = self._conn.execute(
                "SELECT decomposition FROM attention_impressions "
                "WHERE node_type = ? AND node_id = ? "
                "AND decomposition IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1",
                (node_type, int(node_id)),
            ).fetchone()
        if not row or not row["decomposition"]:
            return None
        raw = row["decomposition"]
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return None
        return raw if isinstance(raw, dict) else None

    def close_attention_window(self, surface: str, t0: float, t1: float,
                               outcome: str, *,
                               detail: str | None = None
                               ) -> list[tuple[str, int]]:
        """Close every still-open impression on a surface inside [t0, t1] —
        the chat-verdict join: a verdict on an answer labels the grounding
        impressions recorded when that answer was composed. Returns the
        affected node keys so the caller can move their value traces."""
        import time as _time
        now = _time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_type, node_id FROM attention_impressions "
                "WHERE surface = ? AND outcome IS NULL AND ts BETWEEN ? AND ?",
                (surface, t0, t1)).fetchall()
            self._conn.execute(
                "UPDATE attention_impressions "
                "SET outcome = ?, outcome_ts = ?, detail = COALESCE(?, detail) "
                "WHERE surface = ? AND outcome IS NULL AND ts BETWEEN ? AND ?",
                (outcome, now, detail, surface, t0, t1),
            )
            self._conn.commit()
        return [(r["node_type"], int(r["node_id"])) for r in rows]

    def close_latest_offer_outcome(self, outcome: str, *,
                                   detail: str | None = None,
                                   text_hint: str = "") -> bool:
        """Close the newest still-open offer-surface impression.

        Prefer a detail text match when `text_hint` is given (so concurrent
        offers don't steal each other's outcomes); otherwise close the newest
        open offer row. Returns False when nothing was open.
        """
        import time as _time
        now = _time.time()
        with self._lock:
            row = None
            if text_hint:
                row = self._conn.execute(
                    "SELECT id FROM attention_impressions "
                    "WHERE surface = 'offer' AND outcome IS NULL "
                    "AND detail LIKE ? ORDER BY ts DESC LIMIT 1",
                    (f"%{text_hint[:60]}%",),
                ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT id FROM attention_impressions "
                    "WHERE surface = 'offer' AND outcome IS NULL "
                    "ORDER BY ts DESC LIMIT 1",
                ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "UPDATE attention_impressions "
                "SET outcome = ?, outcome_ts = ?, detail = COALESCE(?, detail) "
                "WHERE id = ?",
                (outcome, now, detail, int(row["id"])),
            )
            self._conn.commit()
            return True

    def attention_stats(self, *, days: float = 7.0) -> dict:
        """Ledger aggregate for /console/attention: volume per surface, closed
        outcomes, engagement rate on field impressions, and the miss count."""
        import time as _time
        since = _time.time() - days * 86400.0
        with self._lock:
            by_surface = {r["surface"]: int(r["n"]) for r in self._conn.execute(
                "SELECT surface, COUNT(*) AS n FROM attention_impressions "
                "WHERE ts >= ? GROUP BY surface", (since,)).fetchall()}
            outcomes = {r["outcome"]: int(r["n"]) for r in self._conn.execute(
                "SELECT outcome, COUNT(*) AS n FROM attention_impressions "
                "WHERE ts >= ? AND outcome IS NOT NULL GROUP BY outcome",
                (since,)).fetchall()}
            field = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS engaged "
                "FROM attention_impressions WHERE ts >= ? AND surface = 'field'",
                (since,)).fetchone()
        total = int(field["total"] or 0)
        engaged = int(field["engaged"] or 0)
        offer_total = int(by_surface.get("offer") or 0)
        offer_accepted = int(outcomes.get("accepted") or 0)
        offer_dismissed = int(outcomes.get("dismissed") or 0)
        offer_closed = offer_accepted + offer_dismissed
        return {
            "days": days,
            "by_surface": by_surface,
            "outcomes": outcomes,
            "field_impressions": total,
            "field_engaged": engaged,
            "field_engagement_rate": round(engaged / total, 4) if total else None,
            "misses": outcomes.get("miss", 0),
            "offers": offer_total,
            "offer_accepted": offer_accepted,
            "offer_dismissed": offer_dismissed,
            "offer_accept_rate": (round(offer_accepted / offer_closed, 4)
                                  if offer_closed else None),
        }

    def set_entity_kind(self, entity_id: int, kind: str) -> bool:
        try:
            from app.services.name_quality import normalize_entity_kind
            # Strict: reject garbage labels (wizard) instead of collapsing to idea.
            kind = normalize_entity_kind(kind, unknown=None)
        except Exception:
            kind = (kind or "").strip().lower()
            if kind in ("company", "organization"):
                kind = "org"
            if kind in ("software", "app", "service", "product", "platform"):
                kind = "tool"
            if kind in ("location", "venue"):
                kind = "place"
            if kind in ("", "other", "?"):
                kind = "idea"
        if kind not in ("project", "org", "idea", "thing", "place", "tool"):
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE entities SET kind = ? WHERE id = ?", (kind, entity_id))
            self._conn.commit()
            return cur.rowcount > 0

    def reclassify_fact_kind(self, fact_id: int, new_kind: str) -> bool:
        """Move a fact between task and commitment typed tables."""
        new_kind = (new_kind or "").strip().lower()
        if new_kind not in ("task", "commitment"):
            return False
        fact = self.get_fact(fact_id)
        if not fact:
            return False
        old = (fact.get("kind") or "").lower()
        if old == new_kind:
            return True
        if old not in ("task", "commitment"):
            return False
        text = fact.get("text") or fact.get("source_span") or ""
        due = fact.get("due")
        status = fact.get("status") or "open"
        with self._lock:
            if old == "task":
                row = self._conn.execute(
                    "SELECT owner_person_id FROM tasks WHERE fact_id = ?",
                    (fact_id,)).fetchone()
                owner = int(row["owner_person_id"]) if row and row["owner_person_id"] else None
                self._conn.execute("DELETE FROM tasks WHERE fact_id = ?", (fact_id,))
                self._conn.execute(
                    "UPDATE facts SET kind = 'commitment' WHERE id = ?", (fact_id,))
                self._conn.execute(
                    "INSERT INTO commitments (fact_id, text, from_person_id, "
                    "to_person_id, due, status) VALUES (?, ?, ?, NULL, ?, ?)",
                    (fact_id, text, owner, due, status),
                )
            else:
                row = self._conn.execute(
                    "SELECT from_person_id, to_person_id FROM commitments "
                    "WHERE fact_id = ?", (fact_id,)).fetchone()
                owner = None
                if row:
                    owner = row["from_person_id"] or row["to_person_id"]
                    owner = int(owner) if owner else None
                self._conn.execute(
                    "DELETE FROM commitments WHERE fact_id = ?", (fact_id,))
                self._conn.execute(
                    "UPDATE facts SET kind = 'task' WHERE id = ?", (fact_id,))
                self._conn.execute(
                    "INSERT INTO tasks (fact_id, text, owner_person_id, due, status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (fact_id, text, owner, due, status),
                )
            self._conn.commit()
        return True

    def convert_person_to_entity(self, person_id: int, entity_kind: str) -> dict | None:
        """Recategorize a mis-tagged person as a project/org/idea entity."""
        entity_kind = (entity_kind or "").strip().lower()
        if entity_kind in ("company", "organization"):
            entity_kind = "org"
        if entity_kind in ("software", "app", "service", "product", "platform"):
            entity_kind = "tool"
        if entity_kind not in ("project", "org", "idea", "thing", "place", "tool"):
            return None
        people = {p["id"]: p for p in self.all_people()}
        person = people.get(person_id)
        if not person:
            return None
        eid = self.resolve_entity(person["name"], kind=entity_kind)
        if not eid:
            return None
        self.set_entity_kind(eid, entity_kind)
        self.set_constellation_hidden("person", person_id, True)
        return {"person_id": person_id, "entity_id": eid,
                "kind": entity_kind, "name": person["name"]}

    # --- facts for the Console (step 5: approve/edit/dismiss) -------------
    _FACT_SELECT = """
        SELECT f.id AS fact_id, f.kind, f.source_event_id, f.source_span,
               f.confidence, f.extracted_at, f.review,
               f.state, f.superseded_by,
               COALESCE(f.updated_at, f.extracted_at) AS updated_at,
               e.time AS source_time, e.modality AS source_modality,
               e.source AS event_source,
               COALESCE(t.text, c.text, NULLIF(f.text, ''), f.source_span) AS text,
               COALESCE(t.status, c.status) AS status,
               COALESCE(t.due, c.due) AS due,
               c.state AS commitment_state,
               c.completion_evidence_json AS completion_evidence_json,
               c.last_surfaced AS last_surfaced,
               c.counterparty_expects AS counterparty_expects,
               pt.canonical_name AS owner,
               pf.canonical_name AS from_person,
               pto.canonical_name AS to_person
        FROM facts f
        LEFT JOIN tasks t        ON t.fact_id = f.id
        LEFT JOIN commitments c  ON c.fact_id = f.id
        LEFT JOIN events e       ON e.id = f.source_event_id
        LEFT JOIN people pt      ON pt.id = t.owner_person_id
        LEFT JOIN people pf      ON pf.id = c.from_person_id
        LEFT JOIN people pto     ON pto.id = c.to_person_id
    """

    # Sources whose tasks/commitments are mined from text the user merely SAW
    # (not said, typed, or wrote) — attribution is weak, so those rows need a
    # human verdict before they count as real work anywhere that acts on or
    # displays the open board. Claims are unaffected (context, not work), and
    # the Console review surfaces read without `actionable` so junk stays
    # visible exactly where it gets pruned.
    WEAK_ATTRIBUTION_SOURCES = ("desktop.screen",)

    def list_facts(self, kind: str | None = None, status: str | None = None,
                   review: str | None = None, limit: int = 200,
                   actionable: bool = False,
                   include_escrowed: bool = False) -> list[dict]:
        """Joined fact rows for the Console. `review` may be 'none' to select
        not-yet-reviewed facts. Status/review filtering is done in Python to keep
        the NULL semantics obvious.

        `actionable=True` additionally drops unreviewed tasks/commitments from
        weak-attribution sources (screen-mined "work") — live failure July 20
        2026: a backlog drain minted 34 open "tasks" from email subject lines,
        slideware, and the app's own chat UI, and every board/answer/offer
        surface presented them as real. Approving one in the Console (or the
        write path) lifts it onto the boards."""
        with self._lock:
            rows = self._conn.execute(
                self._FACT_SELECT + " ORDER BY f.extracted_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Escrowed rows (People v3 P3) have not earned identity yet: they
            # stay out of every list_facts consumer (grounding, boards, home
            # scoring, review queues) until the rebind job reactivates them.
            if not include_escrowed and (d.get("state") or "") == "escrowed":
                continue
            if kind and d["kind"] != kind:
                continue
            if status and (d["status"] or "") != status:
                continue
            if review == "none" and d["review"] is not None:
                continue
            if review and review != "none" and d["review"] != review:
                continue
            if (actionable and d["kind"] in ("task", "commitment")
                    and d["review"] is None
                    and (d.get("event_source") or "")
                    in self.WEAK_ATTRIBUTION_SOURCES):
                continue
            out.append(d)
            if len(out) >= limit:
                break
        return out

    def get_fact(self, fact_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                self._FACT_SELECT + " WHERE f.id = ?", (fact_id,)).fetchone()
        return dict(row) if row else None

    def review_fact(self, fact_id: int, review: str) -> bool:
        """Record the human verdict. 'dismissed' also cancels the typed row."""
        if review not in ("approved", "dismissed", "edited"):
            raise ValueError(f"invalid review: {review}")
        import time as _time
        now = _time.time()
        with self._lock:
            # updated_at stamps dismiss/edit so vector_gc can age the row.
            cur = self._conn.execute(
                "UPDATE facts SET review = ?, updated_at = ? WHERE id = ?",
                (review, now, fact_id))
            if review == "dismissed":
                self._conn.execute(
                    "UPDATE tasks SET status = 'cancelled' WHERE fact_id = ?",
                    (fact_id,))
                crow = self._conn.execute(
                    "SELECT state FROM commitments WHERE fact_id = ?",
                    (fact_id,)).fetchone()
                if crow:
                    try:
                        self._transition_commitment_unlocked(
                            fact_id, "cancelled",
                            reason="dismiss",
                            evidence={"source": "user_dismiss"},
                            actor="user", ts=_time.time())
                    except Exception:
                        # Fall back to status-only if already terminal.
                        self._conn.execute(
                            "UPDATE commitments SET status = 'cancelled', "
                            "state = 'cancelled' WHERE fact_id = ?",
                            (fact_id,))
            elif review == "approved":
                # Promote detected → active (review stays the human verdict).
                crow = self._conn.execute(
                    "SELECT state FROM commitments WHERE fact_id = ?",
                    (fact_id,)).fetchone()
                if crow and (crow["state"] or "") == "detected":
                    try:
                        self._transition_commitment_unlocked(
                            fact_id, "active",
                            reason="approve",
                            evidence={"source": "user_approve"},
                            actor="user", ts=_time.time())
                    except Exception:
                        pass
            self._conn.commit()
            return cur.rowcount > 0

    def memory_version(self) -> str:
        """Opaque change token for live UIs (constellation refresh): bumps
        whenever facts, people, entities, or relations change. One cheap
        aggregate query — safe to poll every few seconds."""
        with self._lock:
            row = self._conn.execute(
                "SELECT (SELECT COUNT(*) FROM facts),"
                " (SELECT MAX(COALESCE(updated_at, extracted_at)) FROM facts),"
                " (SELECT COUNT(*) FROM relations),"
                " (SELECT MAX(created_at) FROM relations),"
                " (SELECT COUNT(*) FROM people),"
                " (SELECT MAX(last_seen) FROM people),"
                " (SELECT COUNT(*) FROM entities),"
                " (SELECT MAX(last_seen) FROM entities)"
            ).fetchone()
        return "-".join(str(x if x is not None else 0) for x in tuple(row))

    def add_field_snapshot(
        self,
        *,
        version: str,
        ts: float,
        focus_ids: list[str],
        periphery_ids: list[str],
        per_node: dict,
    ) -> int:
        """Persist one constellation projection for /field/diff."""
        import json
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO field_snapshots "
                "(version, ts, focus_ids, periphery_ids, per_node) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(version), float(ts),
                 json.dumps(list(focus_ids)),
                 json.dumps(list(periphery_ids)),
                 json.dumps(per_node)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def latest_field_snapshot(self) -> dict | None:
        import json
        with self._lock:
            row = self._conn.execute(
                "SELECT id, version, ts, focus_ids, periphery_ids, per_node "
                "FROM field_snapshots ORDER BY ts DESC, id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "version": row[1],
            "ts": float(row[2]),
            "focus_ids": json.loads(row[3] or "[]"),
            "periphery_ids": json.loads(row[4] or "[]"),
            "per_node": json.loads(row[5] or "{}"),
        }

    def field_snapshot_at_or_before(
        self, *, since_ts: float | None = None, since_version: str | None = None
    ) -> dict | None:
        """Snapshot at/before a timestamp, or matching a version string."""
        import json
        with self._lock:
            if since_version:
                row = self._conn.execute(
                    "SELECT id, version, ts, focus_ids, periphery_ids, per_node "
                    "FROM field_snapshots WHERE version = ? "
                    "ORDER BY ts DESC, id DESC LIMIT 1",
                    (str(since_version),),
                ).fetchone()
                if not row and since_ts is None:
                    # Fall back to nearest earlier snapshot by time if version miss
                    return None
            elif since_ts is not None:
                row = self._conn.execute(
                    "SELECT id, version, ts, focus_ids, periphery_ids, per_node "
                    "FROM field_snapshots WHERE ts <= ? "
                    "ORDER BY ts DESC, id DESC LIMIT 1",
                    (float(since_ts),),
                ).fetchone()
            else:
                row = None
        if not row:
            return None
        return {
            "id": int(row[0]),
            "version": row[1],
            "ts": float(row[2]),
            "focus_ids": json.loads(row[3] or "[]"),
            "periphery_ids": json.loads(row[4] or "[]"),
            "per_node": json.loads(row[5] or "{}"),
        }

    def prune_field_snapshots(
        self, *, retain_days: float = 30.0, max_n: int = 200, now: float | None = None
    ) -> int:
        """Ring-buffer retention — delete oldest beyond day/count caps."""
        import time as _time
        now = float(now if now is not None else _time.time())
        cutoff = now - float(retain_days) * 86400.0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM field_snapshots WHERE ts < ?", (cutoff,))
            deleted = int(cur.rowcount or 0)
            # Cap by count: keep newest max_n
            ids = [
                int(r[0]) for r in self._conn.execute(
                    "SELECT id FROM field_snapshots ORDER BY ts DESC, id DESC"
                ).fetchall()
            ]
            if len(ids) > max_n:
                drop = ids[max_n:]
                self._conn.executemany(
                    "DELETE FROM field_snapshots WHERE id = ?",
                    [(i,) for i in drop])
                deleted += len(drop)
            self._conn.commit()
        return deleted

    def touch_fact(self, fact_id: int, ts: float,
                   confidence: float | None = None) -> bool:
        """Re-assertion of an existing ACTIVE fact (the dedup path): refresh
        updated_at — the recency signal — and keep the strongest confidence
        seen, instead of inserting a twin row."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE facts SET updated_at = ?, "
                "confidence = MAX(COALESCE(confidence, 0), COALESCE(?, 0)) "
                "WHERE id = ? AND state = 'active'",
                (ts, confidence, fact_id))
            self._conn.commit()
            touched = cur.rowcount > 0
        if touched:
            # Re-assertion is an access event on the fact's memory trace
            # (outside the lock — record_node_access takes it itself).
            self.record_node_access("fact", fact_id, ts)
        return touched

    def archive_fact(self, fact_id: int, ts: float) -> bool:
        """Retire a fact without a replacement (hygiene sweep: junk, empties,
        below-floor confidence). Distinct from 'superseded' (replaced by a
        newer fact) and from review='dismissed' (a human verdict)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE facts SET state = 'archived', updated_at = ? "
                "WHERE id = ? AND state = 'active'", (ts, fact_id))
            if cur.rowcount:
                self._conn.execute(
                    "UPDATE tasks SET status = 'cancelled' "
                    "WHERE fact_id = ? AND status = 'open'", (fact_id,))
                crow = self._conn.execute(
                    "SELECT state FROM commitments WHERE fact_id = ?",
                    (fact_id,)).fetchone()
                if crow and (crow["state"] or "") in (
                        "detected", "active", "in_progress", "waiting"):
                    try:
                        self._transition_commitment_unlocked(
                            fact_id, "cancelled",
                            reason="archive",
                            evidence={"source": "archive_fact"},
                            actor="system", ts=ts)
                    except Exception:
                        self._conn.execute(
                            "UPDATE commitments SET status = 'cancelled', "
                            "state = 'cancelled' "
                            "WHERE fact_id = ? AND status = 'open'",
                            (fact_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def supersede_fact(self, old_id: int, new_id: int, ts: float) -> bool:
        """A newer fact replaces `old_id` (contradiction/update — "meeting
        moved to 3pm"). The old row keeps its provenance but leaves active
        retrieval; an open typed row is cancelled so task views stay clean."""
        if old_id == new_id:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE facts SET state = 'superseded', superseded_by = ?, "
                "updated_at = ? WHERE id = ? AND state = 'active'",
                (new_id, ts, old_id))
            if cur.rowcount:
                self._conn.execute(
                    "UPDATE tasks SET status = 'cancelled' "
                    "WHERE fact_id = ? AND status = 'open'", (old_id,))
                crow = self._conn.execute(
                    "SELECT state FROM commitments WHERE fact_id = ?",
                    (old_id,)).fetchone()
                if crow and (crow["state"] or "") in (
                        "detected", "active", "in_progress", "waiting"):
                    try:
                        self._transition_commitment_unlocked(
                            old_id, "superseded",
                            reason="supersede",
                            evidence={"source": "supersede_fact",
                                      "superseded_by": int(new_id)},
                            actor="system", ts=ts)
                    except Exception:
                        self._conn.execute(
                            "UPDATE commitments SET status = 'cancelled', "
                            "state = 'superseded' "
                            "WHERE fact_id = ? AND status = 'open'",
                            (old_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def search_facts_like(self, query: str, limit: int = 20) -> list[dict]:
        """Substring fallback over ACTIVE, non-dismissed facts — so facts still
        surface when the vector index is unavailable (they used to vanish from
        retrieval entirely on that path)."""
        q = f"%{(query or '').strip()}%"
        if q == "%%":
            return []
        with self._lock:
            rows = self._conn.execute(
                self._FACT_SELECT
                + " WHERE f.state = 'active'"
                  " AND (f.review IS NULL OR f.review != 'dismissed')"
                  " AND COALESCE(t.text, c.text, f.source_span) LIKE ?"
                  " ORDER BY COALESCE(f.updated_at, f.extracted_at) DESC"
                  " LIMIT ?",
                (q, limit)).fetchall()
        return [dict(r) for r in rows]

    def set_fact_status(self, fact_id: int, status: str) -> bool:
        """Set the lifecycle status on whichever typed row backs this fact.
        Bumps facts.updated_at (same as set_fact_due): a completion is a
        lifecycle moment — recency ranking and the trigger signal scan
        (task_done / progress_on) both key off it.

        Commitments (plan 4.1) route through the state machine; `status` is
        kept as a derived compat column. Tasks keep the tri-state update.
        """
        if status not in ("open", "done", "cancelled"):
            raise ValueError(f"invalid status: {status}")
        import time as _time
        from app.services import commitment_state as cs
        with self._lock:
            crow = self._conn.execute(
                "SELECT state, status FROM commitments WHERE fact_id = ?",
                (fact_id,)).fetchone()
            if crow:
                to_state = cs.state_for_status(status)
                evidence = None
                reason = f"set_status:{status}"
                if to_state == "completed":
                    evidence = {"source": "user_mark_done"}
                    reason = "user_done"
                elif to_state == "cancelled":
                    evidence = {"source": "set_fact_status"}
                    reason = "user_cancel"
                elif to_state == "active":
                    reason = "reopen"
                    evidence = {"source": "user_reopen"}
                self._transition_commitment_unlocked(
                    fact_id, to_state, reason=reason,
                    evidence=evidence, actor="user", ts=_time.time())
                self._conn.commit()
                return True
            cur = self._conn.execute(
                "UPDATE tasks SET status = ? WHERE fact_id = ?", (status, fact_id))
            n = cur.rowcount
            if n:
                self._conn.execute(
                    "UPDATE facts SET updated_at = ? WHERE id = ?",
                    (_time.time(), fact_id))
            self._conn.commit()
            return n > 0

    def transition_commitment(
        self, fact_id: int, to_state: str, *,
        reason: str | None = None,
        evidence: dict | str | None = None,
        actor: str = "user",
        ts: float | None = None,
    ) -> dict:
        """Apply a legal commitment state transition (plan 4.1).

        Completing requires cited evidence. Returns
        `{ok, fact_id, from_state, to_state, status}`.
        """
        import time as _time
        ts = float(ts if ts is not None else _time.time())
        with self._lock:
            out = self._transition_commitment_unlocked(
                fact_id, to_state, reason=reason, evidence=evidence,
                actor=actor, ts=ts)
            self._conn.commit()
            return out

    def _transition_commitment_unlocked(
        self, fact_id: int, to_state: str, *,
        reason: str | None = None,
        evidence: dict | str | None = None,
        actor: str = "user",
        ts: float,
    ) -> dict:
        """Caller must hold `self._lock`. Does not commit."""
        import json
        from app.services import commitment_state as cs
        from app.services.commitment_state import TransitionError

        row = self._conn.execute(
            "SELECT state, status, completion_evidence_json FROM commitments "
            "WHERE fact_id = ?", (fact_id,)).fetchone()
        if not row:
            raise TransitionError(f"no commitment for fact_id={fact_id}")
        from_state = (row["state"] or "detected").strip().lower()
        to_state = (to_state or "").strip().lower()
        cs.require_legal(from_state, to_state)
        ev = cs.normalize_evidence(evidence)
        if to_state == "completed" and not cs.evidence_ok_for_completed(ev):
            raise TransitionError(
                "completed requires completion evidence "
                "(evidence_event_id/source/note)"
            )
        if from_state == to_state:
            return {
                "ok": True, "fact_id": int(fact_id),
                "from_state": from_state, "to_state": to_state,
                "status": cs.status_for(to_state), "noop": True,
            }
        compat = cs.status_for(to_state)
        evidence_json = json.dumps(ev) if ev else None
        if to_state == "completed":
            self._conn.execute(
                "UPDATE commitments SET state = ?, status = ?, "
                "completion_evidence_json = ? WHERE fact_id = ?",
                (to_state, compat, evidence_json, fact_id))
        else:
            self._conn.execute(
                "UPDATE commitments SET state = ?, status = ? WHERE fact_id = ?",
                (to_state, compat, fact_id))
        self._conn.execute(
            "INSERT INTO commitment_transitions "
            "(fact_id, from_state, to_state, reason, evidence_json, actor, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fact_id, from_state, to_state, reason, evidence_json,
             actor, float(ts)))
        self._conn.execute(
            "UPDATE facts SET updated_at = ? WHERE id = ?",
            (float(ts), fact_id))
        return {
            "ok": True, "fact_id": int(fact_id),
            "from_state": from_state, "to_state": to_state,
            "status": compat, "noop": False,
        }

    def list_commitment_transitions(
        self, fact_id: int, *, limit: int = 50,
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, fact_id, from_state, to_state, reason, "
                "evidence_json, actor, created_at "
                "FROM commitment_transitions WHERE fact_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (fact_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            raw = d.get("evidence_json")
            if isinstance(raw, str) and raw.strip():
                try:
                    import json
                    d["evidence"] = json.loads(raw)
                except Exception:
                    d["evidence"] = None
            else:
                d["evidence"] = None
            out.append(d)
        return out

    def touch_commitment_surfaced(
        self, fact_id: int, ts: float | None = None,
    ) -> bool:
        """Record last_surfaced for open-loop snooze (plan 4.3)."""
        import time as _time
        ts = float(ts if ts is not None else _time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE commitments SET last_surfaced = ? WHERE fact_id = ?",
                (ts, fact_id))
            self._conn.commit()
            return cur.rowcount > 0

    def set_counterparty_expects(
        self, fact_id: int, expects: bool,
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE commitments SET counterparty_expects = ? WHERE fact_id = ?",
                (1 if expects else 0, fact_id))
            self._conn.commit()
            return cur.rowcount > 0

    def set_fact_due(self, fact_id: int, due: str | None, ts: float) -> bool:
        """Set/clear the due field on whichever typed row backs this fact,
        bumping updated_at (a due change is a re-assertion)."""
        try:
            from app.services.clock import coerce_due
            due = coerce_due(due)
        except Exception:
            due = (due or "").strip() or None
        with self._lock:
            n = 0
            for tbl in ("tasks", "commitments"):
                cur = self._conn.execute(
                    f"UPDATE {tbl} SET due = ? WHERE fact_id = ?",
                    (due, fact_id))
                n += cur.rowcount
            if n:
                self._conn.execute(
                    "UPDATE facts SET updated_at = ? WHERE id = ?",
                    (ts, fact_id))
            self._conn.commit()
            return n > 0

    def edit_fact_text(self, fact_id: int, text: str) -> bool:
        """Correct a fact's text (the human fixing a mis-extraction). Updates the
        typed row for tasks/commitments, or the span for a claim; marks reviewed."""
        text = (text or "").strip()
        if not text:
            return False
        with self._lock:
            n = 0
            for tbl in ("tasks", "commitments"):
                cur = self._conn.execute(
                    f"UPDATE {tbl} SET text = ? WHERE fact_id = ?", (text, fact_id))
                n += cur.rowcount
            if n == 0:  # a claim (no typed row) — its text lives in the span
                cur = self._conn.execute(
                    "UPDATE facts SET source_span = ? WHERE id = ?", (text, fact_id))
                n = cur.rowcount
            self._conn.execute(
                "UPDATE facts SET review = 'edited' WHERE id = ?", (fact_id,))
            self._conn.commit()
            return n > 0

    def facts_by_ids(self, ids: list[int]) -> dict[int, dict]:
        """{fact_id: joined-fact-row} — for hydrating semantic search hits."""
        if not ids:
            return {}
        ph = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                self._FACT_SELECT + f" WHERE f.id IN ({ph})", ids).fetchall()
        return {int(r["fact_id"]): dict(r) for r in rows}

    def facts_since(self, since: float, *, limit: int = 500,
                    exclude_dismissed: bool = True,
                    exclude_superseded: bool = True) -> list[dict]:
        """Joined fact rows learned since `since` (by extracted_at) — the raw
        material a daily reflection reads. Dismissed facts are excluded by default
        so the reflector never reasons over what the human already rejected;
        superseded facts likewise — their replacement row carries the truth."""
        with self._lock:
            rows = self._conn.execute(
                self._FACT_SELECT
                + " WHERE f.extracted_at >= ? ORDER BY f.extracted_at DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if exclude_dismissed and d.get("review") == "dismissed":
                continue
            if exclude_superseded and (d.get("state") or "active") != "active":
                continue  # superseded AND archived: only living facts reflect
            out.append(d)
        return out

    # ------------------------------ reflections --------------------------
    def add_reflection(self, *, scope: str, period_start: float | None,
                       period_end: float | None, summary: str = "",
                       model: str | None = None, confidence: float | None = None,
                       subject_type: str | None = None,
                       subject_id: int | None = None, created_at: float) -> int:
        """Insert a reflection header. Insights hang off it via add_reflection_item."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reflections (scope, subject_type, subject_id, "
                "period_start, period_end, summary, model, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scope, subject_type, subject_id, period_start, period_end,
                 summary, model, confidence, created_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def add_reflection_item(self, reflection_id: int, *, kind: str, text: str,
                            detail: str = "", subject: str = "",
                            confidence: float | None = None,
                            source_fact_ids: list[int] | None = None,
                            created_at: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reflection_items (reflection_id, kind, text, detail, "
                "subject, confidence, source_fact_ids, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (reflection_id, kind, text, detail, subject, confidence,
                 json.dumps(source_fact_ids or []), created_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_reflections(self, scope: str | None = None,
                         limit: int = 30) -> list[dict]:
        sql = "SELECT * FROM reflections"
        params: list = []
        if scope:
            sql += " WHERE scope = ?"
            params.append(scope)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def latest_reflection(self, scope: str = "daily") -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reflections WHERE scope = ? "
                "ORDER BY created_at DESC LIMIT 1", (scope,)).fetchone()
        return dict(row) if row else None

    def get_reflection(self, reflection_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reflections WHERE id = ?", (reflection_id,)).fetchone()
        return dict(row) if row else None

    def reflection_items(self, reflection_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reflection_items WHERE reflection_id = ? ORDER BY id",
                (reflection_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["source_fact_ids"] = json.loads(d.get("source_fact_ids") or "[]")
            out.append(d)
        return out

    def get_reflection_item(self, item_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reflection_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["source_fact_ids"] = json.loads(d.get("source_fact_ids") or "[]")
        return d

    def review_reflection_item(self, item_id: int, review: str) -> bool:
        """Record the human verdict on one insight (the reflection training signal)."""
        if review not in ("approved", "dismissed", "edited"):
            raise ValueError(f"invalid review: {review}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reflection_items SET review = ? WHERE id = ?",
                (review, item_id))
            self._conn.commit()
            return cur.rowcount > 0

    def edit_reflection_item_text(self, item_id: int, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reflection_items SET text = ?, review = 'edited' WHERE id = ?",
                (text, item_id))
            self._conn.commit()
            return cur.rowcount > 0

    def set_reflection_item_converted(self, item_id: int, fact_id: int) -> bool:
        """Link the task created from this insight, and mark it approved — the
        human-gated bridge from a reflection into the live tasks loop."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reflection_items SET converted_fact_id = ?, "
                "review = COALESCE(review, 'approved') WHERE id = ?",
                (fact_id, item_id))
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------ turns --------------------------------
    def replace_turns(self, turns: list) -> None:
        """Atomically swap the whole turns table for a freshly-computed set.

        People v3 P3: with QUILL_PEOPLE_ESCROW on, turns whose speaker is a
        provisional track label are stamped with the OPEN track id (lookup
        only — tracks are minted by the extractor's escrow path). Flag off:
        the new column is neither read nor written.
        """
        track_ids: dict[str, int] = {}
        try:
            from app.services import people_escrow
            if people_escrow.enabled():
                by_label = self.open_speaker_track_ids()
                track_ids = {lb: tid for lb, tid in by_label.items()
                             if people_escrow.is_provisional_label(lb)}
        except Exception as exc:
            print(f"[storage] turn track stamping skipped ({exc}).")
        if track_ids:
            payload_t = [
                (t.start, t.end, t.speaker, t.text,
                 json.dumps(t.event_ids), json.dumps(t.audio_paths),
                 t.n_utterances, track_ids.get((t.speaker or "").strip()))
                for t in turns
            ]
            with self._lock:
                self._conn.execute("DELETE FROM turns")
                self._conn.executemany(
                    """
                    INSERT INTO turns (start, end, speaker, text, event_ids,
                                       audio_paths, n_utterances,
                                       speaker_track_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload_t,
                )
                self._conn.commit()
            return
        payload = [
            (t.start, t.end, t.speaker, t.text,
             json.dumps(t.event_ids), json.dumps(t.audio_paths), t.n_utterances)
            for t in turns
        ]
        with self._lock:
            self._conn.execute("DELETE FROM turns")
            self._conn.executemany(
                """
                INSERT INTO turns (start, end, speaker, text, event_ids,
                                   audio_paths, n_utterances)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            self._conn.commit()

    # ------------------------------ sessions -----------------------------
    def replace_sessions(self, sessions: list) -> None:
        """Atomically swap the whole sessions table for a freshly-computed set."""
        payload = []
        for s in sessions:
            meta = getattr(s, "meeting_meta", None)
            cal_id = getattr(s, "calendar_event_id", None)
            payload.append((
                s.start, s.end, json.dumps(s.speakers), s.text,
                json.dumps(s.turn_ids), json.dumps(s.event_ids),
                s.n_turns, s.n_utterances,
                cal_id,
                json.dumps(meta) if meta is not None else None,
            ))
        with self._lock:
            self._conn.execute("DELETE FROM sessions")
            self._conn.executemany(
                """
                INSERT INTO sessions (start, end, speakers, text, turn_ids,
                                      event_ids, n_turns, n_utterances,
                                      calendar_event_id, meeting_meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            self._conn.commit()

    def recent_sessions(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY start DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            try:
                meta = json.loads(r["meeting_meta"]) if r["meeting_meta"] else None
            except Exception:
                meta = None
            keys = r.keys()
            out.append({
                "id": int(r["id"]),
                "start": r["start"], "end": r["end"],
                "speakers": json.loads(r["speakers"] or "[]"),
                "text": r["text"],
                "turn_ids": json.loads(r["turn_ids"] or "[]"),
                "event_ids": json.loads(r["event_ids"] or "[]"),
                "n_turns": r["n_turns"], "n_utterances": r["n_utterances"],
                "duration_s": round((r["end"] or 0) - (r["start"] or 0), 2),
                "calendar_event_id": (
                    r["calendar_event_id"] if "calendar_event_id" in keys
                    else None),
                "meeting_meta": meta,
            })
        return out

    def session_count(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM sessions").fetchone()[0])

    # --------------------------- calendar events -------------------------
    def upsert_calendar_event(
        self, *, event_id: str, calendar: str | None, uid: str | None,
        title: str | None, start: float, end: float, all_day: bool = False,
        location: str | None = None, organizer: dict | None = None,
        attendees: list | None = None, source_event_id: int | None = None,
        updated_at: float | None = None,
    ) -> None:
        """Insert or replace one normalized calendar event (Meeting Layer P1)."""
        import time as _time
        ts = float(updated_at if updated_at is not None else _time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO calendar_events (
                    id, calendar, uid, title, start, end, all_day, location,
                    organizer_json, attendees_json, source_event_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    calendar=excluded.calendar,
                    uid=excluded.uid,
                    title=excluded.title,
                    start=excluded.start,
                    end=excluded.end,
                    all_day=excluded.all_day,
                    location=excluded.location,
                    organizer_json=excluded.organizer_json,
                    attendees_json=excluded.attendees_json,
                    source_event_id=COALESCE(excluded.source_event_id,
                                             calendar_events.source_event_id),
                    updated_at=excluded.updated_at
                """,
                (event_id, calendar, uid, title, float(start), float(end),
                 1 if all_day else 0, location,
                 json.dumps(organizer) if organizer is not None else None,
                 json.dumps(attendees or []),
                 source_event_id, ts),
            )
            self._conn.commit()

    def list_calendar_events(
        self, *, start_min: float | None = None, start_max: float | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Calendar events overlapping an optional window, oldest-first."""
        with self._lock:
            sql = "SELECT * FROM calendar_events WHERE 1=1"
            args: list = []
            # Overlap: event.end >= window_start AND event.start <= window_end
            if start_min is not None:
                sql += " AND end >= ?"
                args.append(float(start_min))
            if start_max is not None:
                sql += " AND start <= ?"
                args.append(float(start_max))
            sql += " ORDER BY start ASC LIMIT ?"
            args.append(int(limit))
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            try:
                organizer = json.loads(r["organizer_json"]) if r["organizer_json"] else None
            except Exception:
                organizer = None
            try:
                attendees = json.loads(r["attendees_json"] or "[]")
            except Exception:
                attendees = []
            out.append({
                "id": r["id"], "calendar": r["calendar"], "uid": r["uid"],
                "title": r["title"], "start": r["start"], "end": r["end"],
                "all_day": bool(r["all_day"]), "location": r["location"],
                "organizer": organizer, "attendees": attendees,
                "source_event_id": r["source_event_id"],
                "updated_at": r["updated_at"],
            })
        return out

    def find_person_by_contact(
        self, type_: str, value_normalized: str,
    ) -> int | None:
        """Active contact-point lookup (email/phone → person_id)."""
        key = (value_normalized or "").strip().lower()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT person_id FROM person_contact_points "
                "WHERE type=? AND value_normalized=? AND status='active' "
                "ORDER BY confidence DESC LIMIT 1",
                (type_, key),
            ).fetchone()
            if row:
                return int(row["person_id"])
            # Fallback: user-asserted person_attrs email/phone.
            if type_ in ("email", "phone"):
                row = self._conn.execute(
                    "SELECT person_id FROM person_attrs "
                    "WHERE key=? AND lower(value)=? LIMIT 1",
                    (type_, key),
                ).fetchone()
                if row:
                    return int(row["person_id"])
        return None

    # ----------------------------- activities ----------------------------
    def replace_activities(self, activities: list) -> None:
        """Atomically swap the whole activities table for a fresh set."""
        payload = [
            (a.start, a.end, a.app, json.dumps(a.windows), a.summary,
             json.dumps(a.event_ids), a.n_screens, a.n_clicks,
             getattr(a, "n_audio", 0), getattr(a, "n_webcam", 0),
             json.dumps(getattr(a, "ctx_event_ids", []) or []))
            for a in activities
        ]
        with self._lock:
            self._conn.execute("DELETE FROM activities")
            self._conn.executemany(
                """
                INSERT INTO activities (start, end, app, windows, summary,
                                        event_ids, n_screens, n_clicks,
                                        n_audio, n_webcam, ctx_event_ids)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            self._conn.commit()

    def recent_activities(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activities ORDER BY start DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "start": r["start"], "end": r["end"], "app": r["app"] or "",
                "windows": json.loads(r["windows"] or "[]"),
                "summary": r["summary"],
                "event_ids": json.loads(r["event_ids"] or "[]"),
                "n_screens": r["n_screens"], "n_clicks": r["n_clicks"],
                "n_audio": r["n_audio"] or 0, "n_webcam": r["n_webcam"] or 0,
                "ctx_event_ids": json.loads(r["ctx_event_ids"] or "[]"),
                "duration_s": round((r["end"] or 0) - (r["start"] or 0), 2),
            }
            for r in rows
        ]

    def activity_count(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM activities").fetchone()[0])

    def app_usage(self, since_ts: float = 0.0) -> dict:
        """{app_name: focused_seconds} from the activities table since a time —
        the observed 'apps you actually use' signal (onboarding recency rank).
        Excludes the generic 'desktop' bucket. Empty when no capture has run."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT app, SUM(COALESCE(end, start) - start) AS secs "
                "FROM activities WHERE start >= ? GROUP BY app", (since_ts,)
            ).fetchall()
        out: dict[str, float] = {}
        for r in rows:
            app = (r["app"] or "").strip()
            if app and app.lower() != "desktop":
                out[app] = float(r["secs"] or 0.0)
        return out

    def recent_turns(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM turns ORDER BY start DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "start": r["start"], "end": r["end"], "speaker": r["speaker"] or "",
                "text": r["text"], "n_utterances": r["n_utterances"],
                "event_ids": json.loads(r["event_ids"] or "[]"),
                "audio_paths": json.loads(r["audio_paths"] or "[]"),
                "duration_s": round((r["end"] or 0) - (r["start"] or 0), 2),
            }
            for r in rows
        ]

    def turn_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0])

    # ------------------------------ jobs ---------------------------------
    def enqueue_job(self, kind: str, payload: str | None = None,
                    *, unique_pending: bool = False) -> int:
        """Add a pending job. With `unique_pending`, a pending job of the same
        kind coalesces (returns the existing id) — so a burst of utterances
        queues at most one consolidate, not one per utterance."""
        import time as _t

        now = _t.time()
        with self._lock:
            if unique_pending:
                row = self._conn.execute(
                    "SELECT id FROM jobs WHERE kind = ? AND status = 'pending' "
                    "ORDER BY id LIMIT 1", (kind,),
                ).fetchone()
                if row is not None:
                    return int(row["id"])
            cur = self._conn.execute(
                "INSERT INTO jobs (kind, payload, status, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (kind, payload, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def claim_job(self) -> dict | None:
        """Atomically take the oldest claimable pending job -> running.

        Jobs in backoff (available_at > now) stay pending but are skipped until
        the backoff window elapses (plan 0.10).
        """
        import time as _t

        now = _t.time()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM jobs
                 WHERE status = 'pending'
                   AND (available_at IS NULL OR available_at <= ?)
                 ORDER BY id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, "
                "available_at = NULL, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            self._conn.commit()
            return {"id": int(row["id"]), "kind": row["kind"],
                    "payload": row["payload"], "attempts": int(row["attempts"]) + 1}

    def finish_job(self, job_id: int) -> None:
        import time as _t

        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status = 'done', error = NULL, "
                "available_at = NULL, updated_at = ? WHERE id = ?",
                (_t.time(), job_id),
            )
            self._conn.commit()

    def fail_job(self, job_id: int, error: str, max_attempts: int) -> str:
        """Requeue with backoff if attempts remain, else park as dead.

        Returns the resulting status (`pending` or `dead`).
        """
        import time as _t

        now = _t.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
            attempts = int(row["attempts"]) if row else max_attempts
            if attempts >= max_attempts:
                status = "dead"
                available_at = None
            else:
                status = "pending"
                available_at = now + job_backoff_s(attempts)
            self._conn.execute(
                """
                UPDATE jobs SET status = ?, error = ?, available_at = ?,
                                updated_at = ?
                 WHERE id = ?
                """,
                (status, error[:1000], available_at, now, job_id),
            )
            self._conn.commit()
            return status

    def requeue_stale_jobs(self) -> int:
        """Reset orphaned 'running' jobs back to 'pending'. With a single worker,
        anything still 'running' at startup was abandoned by a dead process and
        would otherwise be lost forever. Returns how many were recovered."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'pending', available_at = NULL "
                "WHERE status = 'running'")
            self._conn.commit()
            return cur.rowcount

    def job_stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        stats = {"pending": 0, "running": 0, "done": 0, "dead": 0, "error": 0}
        for r in rows:
            stats[r["status"]] = int(r["n"])
        # Legacy rows may still say 'error' until migrate runs; surface as dead.
        stats["dead"] = int(stats.get("dead", 0)) + int(stats.get("error", 0))
        return stats

    def recent_jobs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, status, attempts, error, available_at, "
                "updated_at FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def dead_jobs(self, limit: int = 20) -> list[dict]:
        """Poisoned jobs parked after max attempts (console dead-letter view)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, kind, status, attempts, error, available_at,
                       created_at, updated_at
                  FROM jobs
                 WHERE status IN ('dead', 'error')
                 ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------ audio telemetry (#9) -----------------
    _AUDIO_TELE_COLS = (
        "event_id", "outcome", "drop_reason", "audio_duration_ms", "quality",
        "snr_est", "rms", "clipping_pct", "speech_ratio", "model",
        "asr_latency_ms", "total_latency_ms", "queue_depth", "avg_logprob",
        "no_speech_prob", "low_confidence", "filter_verdict", "speaker",
        "speaker_known", "speaker_confidence", "char_count",
    )

    def record_audio_telemetry(self, *, ts: float | None = None, **fields) -> int:
        """Insert one per-utterance telemetry row. Best-effort: unknown keys are
        ignored and any DB hiccup returns -1 rather than raising into the audio
        thread. `outcome` ('kept'|'dropped') is required by the schema."""
        import time as _t

        row = {c: fields.get(c) for c in self._AUDIO_TELE_COLS}
        row["ts"] = ts if ts is not None else _t.time()
        cols = ["ts", *self._AUDIO_TELE_COLS]
        placeholders = ",".join(f":{c}" for c in cols)
        try:
            with self._lock:
                cur = self._conn.execute(
                    f"INSERT INTO audio_telemetry ({','.join(cols)}) "
                    f"VALUES ({placeholders})", row)
                self._conn.commit()
                return int(cur.lastrowid)
        except Exception as exc:  # telemetry must never break capture
            print(f"[telemetry] audio row failed: {exc}")
            return -1

    def audio_health(self, window_s: float = 3600.0) -> dict:
        """Aggregate the last `window_s` of audio telemetry into the Audio Health
        summary: throughput, drop reasons, quality mix, ASR/end-to-end latency
        (avg/p50/p95/max), and low-confidence / unknown-speaker rates."""
        import time as _t

        cutoff = _t.time() - window_s
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM audio_telemetry WHERE ts >= ? ORDER BY ts",
                (cutoff,)).fetchall()]
        n = len(rows)
        kept = [r for r in rows if r["outcome"] == "kept"]
        dropped = [r for r in rows if r["outcome"] == "dropped"]

        drops: dict[str, int] = {}
        for r in dropped:
            k = r.get("drop_reason") or "other"
            drops[k] = drops.get(k, 0) + 1

        qdist = {"good": 0, "noisy": 0, "bad": 0}
        for r in rows:
            if r.get("quality") in qdist:
                qdist[r["quality"]] += 1

        def _lat(vals: list) -> dict:
            vals = sorted(v for v in vals if v is not None)
            if not vals:
                return {}

            def pct(q: float):
                return round(vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))], 1)

            return {"avg": round(sum(vals) / len(vals), 1), "p50": pct(0.5),
                    "p95": pct(0.95), "max": round(vals[-1], 1)}

        def _avg(key: str, rowset: list):
            vals = [r.get(key) for r in rowset if r.get(key) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        low_n = sum(1 for r in kept if r.get("low_confidence"))
        spk = [r for r in kept if r.get("speaker") is not None]
        spk_unknown = sum(1 for r in spk if not r.get("speaker_known"))
        scale = 3600.0 / window_s if window_s else 0.0

        return {
            "window_s": window_s,
            "utterances": n, "kept": len(kept), "dropped": len(dropped),
            "per_hour": {
                "utterances": round(n * scale, 1),
                "kept": round(len(kept) * scale, 1),
                "dropped": round(len(dropped) * scale, 1),
            },
            "drops_by_reason": dict(sorted(drops.items(), key=lambda kv: -kv[1])),
            "quality_dist": qdist,
            "asr_latency_ms": _lat([r.get("asr_latency_ms") for r in kept]),
            "total_latency_ms": _lat([r.get("total_latency_ms") for r in kept]),
            "low_confidence_rate": round(low_n / len(kept), 3) if kept else None,
            "speaker_unknown_rate": round(spk_unknown / len(spk), 3) if spk else None,
            "avg_snr": _avg("snr_est", rows),
            "avg_clipping": _avg("clipping_pct", rows),
        }

    def close(self) -> None:
        """Close the underlying connection. Mainly for tests / teardown — the
        process-wide singleton normally lives for the whole run."""
        with self._lock:
            self._conn.close()

    # ------------------------------ agent runs (Phase 5) -----------------
    # The Personal Agent Layer writes through these: a run is opened when a goal
    # starts, annotated once routed (surface/intent/risk), and closed with the
    # outcome. Action packets, steps, and the human's verdict hang off the run.
    def start_agent_run(self, goal: str, *, agent_type: str | None = None,
                        surface: str | None = None, intent: str | None = None,
                        risk_level: str | None = None, dry_run: str | None = None,
                        source_fact_ids: list | None = None,
                        person_id: int | None = None,
                        project_id: int | None = None,
                        correlation_id: str | None = None) -> int:
        import time as _t

        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO agent_runs
                    (goal, agent_type, surface, intent, risk_level, status,
                     dry_run, source_fact_ids, person_id, project_id,
                     correlation_id, started_at)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (goal, agent_type, surface, intent, risk_level, dry_run,
                 json.dumps(source_fact_ids or []), person_id, project_id,
                 correlation_id, _t.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def annotate_agent_run(self, run_id: int, **fields) -> None:
        """Update route-derived fields on an in-flight run. Only whitelisted,
        non-None fields are applied (so a partial route can't blank a column)."""
        if not run_id:
            return
        allowed = {"agent_type", "surface", "intent", "risk_level", "dry_run",
                   "person_id", "project_id", "source_fact_ids", "correlation_id"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            params.append(json.dumps(v) if k == "source_fact_ids" else v)
            sets.append(f"{k} = ?")
        if not sets:
            return
        params.append(run_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE agent_runs SET {', '.join(sets)} WHERE id = ?", params)
            self._conn.commit()

    def finish_agent_run(self, run_id: int, *, status: str,
                         cost: float | None = None, steps: int | None = None,
                         success_score: float | None = None,
                         failure_reason: str | None = None) -> None:
        if not run_id:
            return
        import time as _t

        now = _t.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            latency = (now - row["started_at"]) if row and row["started_at"] else None
            self._conn.execute(
                """
                UPDATE agent_runs
                   SET status = ?, completed_at = ?, latency = ?, cost = ?,
                       steps = ?, success_score = ?, failure_reason = ?
                 WHERE id = ?
                """,
                (status, now, latency, cost, steps, success_score,
                 failure_reason, run_id),
            )
            self._conn.commit()

    def record_action_packet(self, *, agent_run_id: int | None = None,
                             goal: str = "", summary: str = "",
                             fields: dict | None = None, context: list | None = None,
                             source_fact_ids: list | None = None,
                             approval_required: bool = True,
                             risk_level: str | None = None,
                             suggested_agent: str | None = None,
                             execution_surface: str | None = None,
                             success_criteria: list | None = None,
                             fallback: str | None = None,
                             ttl_s: float | None = None) -> int:
        import time as _t

        now = _t.time()
        payload_hash = hash_packet_payload(fields)
        expires_at = now + (ttl_s if ttl_s is not None else _PACKET_TTL_S)
        # Persist the same canonical bytes we hashed so a later re-read of
        # fields_json round-trips to the same payload_hash (commit gate 0.4).
        fields_json = canonicalize_packet_fields(fields)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO action_packets
                    (agent_run_id, goal, summary, fields_json, context_json,
                     source_fact_ids, approval_required, risk_level, suggested_agent,
                     execution_surface, success_criteria, fallback, decision,
                     payload_hash, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (agent_run_id, goal, summary, fields_json,
                 json.dumps(context or []), json.dumps(source_fact_ids or []),
                 1 if approval_required else 0, risk_level, suggested_agent,
                 execution_surface, json.dumps(success_criteria or []), fallback,
                 payload_hash, expires_at, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def set_packet_decision(self, packet_id: int, decision: str, *,
                            approved_via: str | None = None) -> None:
        """Stamp a human verdict. Approvals also record approved_at/via (plan 0.6)."""
        if not packet_id:
            return
        import time as _t

        with self._lock:
            if decision == "approve":
                self._conn.execute(
                    """
                    UPDATE action_packets
                       SET decision = ?, approved_at = ?, approved_via = ?
                     WHERE id = ?
                    """,
                    (decision, _t.time(),
                     approved_via if approved_via in ("button", "typed") else approved_via,
                     packet_id))
            else:
                self._conn.execute(
                    "UPDATE action_packets SET decision = ? WHERE id = ?",
                    (decision, packet_id))
            self._conn.commit()

    def get_action_packet(self, packet_id: int) -> dict | None:
        """One action_packets row, fields_json parsed into `fields`."""
        if not packet_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM action_packets WHERE id = ?",
                (packet_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["fields"] = json.loads(d.get("fields_json") or "{}")
        except Exception:
            d["fields"] = {}
        return d

    def set_packet_executed_hash(self, packet_id: int, executed_hash: str,
                                 *, executed_at: float | None = None) -> None:
        """Stamp the hash that was actually committed (plan 0.8 dup-send)."""
        if not packet_id or not executed_hash:
            return
        import time as _t

        when = float(executed_at) if executed_at is not None else _t.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE action_packets
                   SET executed_hash = ?, executed_at = ?
                 WHERE id = ?
                """,
                (executed_hash, when, packet_id))
            self._conn.commit()

    def find_recent_executed_hash(self, executed_hash: str, *,
                                  within_s: float = 3600.0) -> dict | None:
        """Most recent packet with this executed_hash stamped in the window.

        Used to refuse a duplicate send-class commit within an hour (plan 0.8).
        """
        if not executed_hash:
            return None
        import time as _t

        cutoff = _t.time() - float(within_s)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM action_packets
                 WHERE executed_hash = ?
                   AND executed_at IS NOT NULL
                   AND executed_at >= ?
                 ORDER BY executed_at DESC LIMIT 1
                """,
                (executed_hash, cutoff)).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["fields"] = json.loads(d.get("fields_json") or "{}")
        except Exception:
            d["fields"] = {}
        return d

    def latest_pending_packet(self, agent_run_id: int | None = None) -> dict | None:
        """Most recent action_packets row still awaiting a human decision."""
        with self._lock:
            if agent_run_id is not None:
                row = self._conn.execute(
                    """
                    SELECT * FROM action_packets
                     WHERE decision IS NULL AND agent_run_id = ?
                     ORDER BY id DESC LIMIT 1
                    """, (agent_run_id,)).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT * FROM action_packets
                     WHERE decision IS NULL
                     ORDER BY id DESC LIMIT 1
                    """).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["fields"] = json.loads(d.get("fields_json") or "{}")
        except Exception:
            d["fields"] = {}
        return d

    def record_agent_steps(self, agent_run_id: int, steps: list[dict]) -> int:
        """Bulk-insert step rows. Each dict may carry step_index / action_type /
        input / output / verification / status; `input` is JSON-encoded.

        Plan 5.1: soft-normalize status — unknown/empty → outcome_uncertain
        (LLM-only default); never invent `verified` here."""
        if not agent_run_id or not steps:
            return 0
        import time as _t
        try:
            from app.services.outcome_verify import normalize_status, OUTCOME_UNCERTAIN
        except Exception:
            def normalize_status(status, *, default="outcome_uncertain"):
                return (status or default)
            OUTCOME_UNCERTAIN = "outcome_uncertain"

        now = _t.time()
        rows = []
        for s in steps:
            st = s.get("status")
            if not st:
                st = OUTCOME_UNCERTAIN
            else:
                st = normalize_status(st, default=OUTCOME_UNCERTAIN)
            rows.append(
                (agent_run_id, s.get("step_index"), s.get("action_type"),
                 json.dumps(s.get("input")), s.get("output"),
                 s.get("verification"), st, now)
            )
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO agent_steps
                    (agent_run_id, step_index, action_type, input, output,
                     verification, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, rows)
            self._conn.commit()
        return len(rows)

    def record_agent_feedback(self, agent_run_id: int | None, feedback_type: str, *,
                              packet_id: int | None = None,
                              user_edit: str | None = None,
                              notes: str | None = None) -> int:
        import time as _t

        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO agent_feedback
                    (agent_run_id, packet_id, feedback_type, user_edit, notes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (agent_run_id, packet_id, feedback_type, user_edit, notes, _t.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def recent_agent_runs(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["source_fact_ids"] = json.loads(d.get("source_fact_ids") or "[]")
            out.append(d)
        return out

    def agent_run(self, run_id: int) -> dict | None:
        """One run fully hydrated with its packets, steps, and feedback."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            if r is None:
                return None
            packets = self._conn.execute(
                "SELECT * FROM action_packets WHERE agent_run_id = ? ORDER BY id",
                (run_id,)).fetchall()
            steps = self._conn.execute(
                "SELECT * FROM agent_steps WHERE agent_run_id = ? ORDER BY step_index, id",
                (run_id,)).fetchall()
            feedback = self._conn.execute(
                "SELECT * FROM agent_feedback WHERE agent_run_id = ? ORDER BY id",
                (run_id,)).fetchall()
        d = dict(r)
        d["source_fact_ids"] = json.loads(d.get("source_fact_ids") or "[]")
        d["packets"] = [dict(p) for p in packets]
        # `steps` is the integer count column (kept, consistent with the list
        # view); the hydrated step rows go under `step_log` to avoid shadowing it.
        d["step_log"] = [dict(s) for s in steps]
        d["feedback"] = [dict(f) for f in feedback]
        return d

    def learning_edit_pairs(self, limit: int = 20) -> list[dict]:
        """(original draft, human revision) pairs from EDITED action packets — the
        Tier-4 drafting-preference signal. Joins each 'edited' feedback row to its
        packet, so we have both the draft that was shown and how the human rewrote
        it. Newest first. Best-effort read (returns [] if the tables are empty)."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT fb.user_edit AS user_edit, fb.notes AS notes,
                           fb.created_at AS created_at, p.fields_json AS fields_json,
                           p.summary AS summary, p.goal AS goal
                    FROM agent_feedback fb
                    LEFT JOIN action_packets p ON p.id = fb.packet_id
                    WHERE fb.feedback_type = 'edited'
                      AND fb.user_edit IS NOT NULL AND TRIM(fb.user_edit) <> ''
                    ORDER BY fb.id DESC LIMIT ?
                    """, (limit,)).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["fields"] = json.loads(d.pop("fields_json") or "{}")
            except Exception:
                d["fields"] = {}
            out.append(d)
        return out

    def learning_intent_verdicts(self, limit: int = 200) -> list[dict]:
        """Recent (intent, feedback_type, created_at) rows joining each verdict to
        its run's intent — the Tier-4 trust-dial signal. Newest first."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT r.intent AS intent, r.surface AS surface,
                           fb.feedback_type AS feedback_type, fb.created_at AS created_at
                    FROM agent_feedback fb
                    JOIN agent_runs r ON r.id = fb.agent_run_id
                    WHERE r.intent IS NOT NULL AND r.intent <> ''
                    ORDER BY fb.id DESC LIMIT ?
                    """, (limit,)).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def agent_run_stats(self) -> dict:
        """Sprint-2 eval metrics: run outcomes + the human-verdict rates that tell
        us whether agent actions are actually useful (edit/cancel/approval)."""
        with self._lock:
            total = int(self._conn.execute(
                "SELECT COUNT(*) FROM agent_runs").fetchone()[0])
            by_status = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM agent_runs GROUP BY status").fetchall()
            fb = self._conn.execute(
                "SELECT feedback_type, COUNT(*) AS n FROM agent_feedback "
                "GROUP BY feedback_type").fetchall()
            n_packets = int(self._conn.execute(
                "SELECT COUNT(*) FROM action_packets").fetchone()[0])
        status = {r["status"]: int(r["n"]) for r in by_status}
        feedback = {r["feedback_type"]: int(r["n"]) for r in fb}
        decided = sum(feedback.get(k, 0) for k in ("approved", "edited", "cancelled"))

        def _rate(k: str):
            return round(feedback.get(k, 0) / decided, 3) if decided else None

        return {
            "runs": total,
            "by_status": status,
            "success_rate": round(status.get("success", 0) / total, 3) if total else None,
            "packets": n_packets,
            "feedback": feedback,
            "approval_rate": _rate("approved"),
            "edit_rate": _rate("edited"),
            "cancel_rate": _rate("cancelled"),
        }

    # ------------------------------------------------------------------
    # Triggers — standing "when X, offer Y" rows (services/triggers/).

    _TRIGGER_JSON = ("condition", "action", "gating", "stats", "provenance")

    def _trigger_row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in self._TRIGGER_JSON:
            try:
                d[k] = json.loads(d[k]) if d.get(k) else {}
            except Exception:
                d[k] = {}
        return d

    def add_trigger(self, name: str, signal: str, *, action: dict,
                    condition: dict | None = None, gating: dict | None = None,
                    provenance: dict | None = None, origin: str = "custom",
                    status: str = "active", created_at: float) -> int:
        """Insert a trigger row. `action` targets (recipient/person) are bound
        here at authoring time — the engine never lets matched content fill
        them (the injection rail)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO triggers (name, origin, status, signal, condition, "
                "action, gating, stats, provenance, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, origin, status, signal,
                 json.dumps(condition or {}), json.dumps(action or {}),
                 json.dumps(gating or {}),
                 json.dumps({"fires": 0, "offers": 0, "accepts": 0,
                             "dismisses": 0}),
                 json.dumps(provenance or {}), created_at, created_at))
            self._conn.commit()
            return int(cur.lastrowid)

    def list_triggers(self, status: str | None = None, origin: str | None = None,
                      signal: str | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT * FROM triggers"
        clauses, args = [], []
        if status:
            clauses.append("status = ?")
            args.append(status)
        if origin:
            clauses.append("origin = ?")
            args.append(origin)
        if signal:
            clauses.append("signal = ?")
            args.append(signal)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._trigger_row(r) for r in rows]

    def get_trigger(self, trigger_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM triggers WHERE id = ?", (trigger_id,)).fetchone()
        return self._trigger_row(row) if row else None

    def set_trigger_status(self, trigger_id: int, status: str, ts: float) -> bool:
        if status not in ("active", "suggested", "paused", "retired"):
            raise ValueError(f"invalid trigger status: {status}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE triggers SET status = ?, updated_at = ? WHERE id = ?",
                (status, ts, trigger_id))
            self._conn.commit()
            return cur.rowcount > 0

    def update_trigger(self, trigger_id: int, ts: float, *,
                       name: str | None = None, condition: dict | None = None,
                       action: dict | None = None,
                       gating: dict | None = None) -> bool:
        sets, args = ["updated_at = ?"], [ts]
        if name is not None:
            sets.append("name = ?")
            args.append(name)
        for col, val in (("condition", condition), ("action", action),
                         ("gating", gating)):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(json.dumps(val))
        args.append(trigger_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE triggers SET {', '.join(sets)} WHERE id = ?", args)
            self._conn.commit()
            return cur.rowcount > 0

    def bump_trigger_stat(self, trigger_id: int, key: str, ts: float,
                          n: int = 1) -> dict:
        """Increment one stats counter (fires/offers/accepts/dismisses) and
        return the updated stats dict — read-modify-write under the lock."""
        with self._lock:
            row = self._conn.execute(
                "SELECT stats FROM triggers WHERE id = ?",
                (trigger_id,)).fetchone()
            if not row:
                return {}
            try:
                stats = json.loads(row["stats"] or "{}")
            except Exception:
                stats = {}
            stats[key] = int(stats.get(key, 0)) + n
            self._conn.execute(
                "UPDATE triggers SET stats = ?, updated_at = ? WHERE id = ?",
                (json.dumps(stats), ts, trigger_id))
            self._conn.commit()
            return stats

    # ------------------------------ speaker-track escrow (People v3 P3) ---
    # All methods here are reached ONLY from flag-gated code
    # (QUILL_PEOPLE_ESCROW via services/people_escrow.py) — with the flag off
    # nothing reads or writes the escrow columns.

    def get_or_create_speaker_track(self, label: str, *, ts: float) -> int:
        """Durable id for a provisional diarization label. Reuses the newest
        OPEN track for the label (stable across a session and until bound);
        once a track is bound, the next occurrence of the label starts fresh."""
        label = (label or "").strip()
        if not label:
            raise ValueError("empty speaker track label")
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM speaker_tracks WHERE label = ? AND "
                "status = 'open' ORDER BY id DESC LIMIT 1", (label,)).fetchone()
            if row is not None:
                return int(row["id"])
            cur = self._conn.execute(
                "INSERT INTO speaker_tracks (label, status, created_at) "
                "VALUES (?, 'open', ?)", (label, ts))
            self._conn.commit()
            return int(cur.lastrowid)

    def get_speaker_track(self, track_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM speaker_tracks WHERE id = ?",
                (track_id,)).fetchone()
        return dict(row) if row else None

    def open_speaker_track(self, label: str) -> dict | None:
        """The newest OPEN track for a label (the one escrow writes against)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM speaker_tracks WHERE label = ? AND "
                "status = 'open' ORDER BY id DESC LIMIT 1",
                ((label or "").strip(),)).fetchone()
        return dict(row) if row else None

    def open_speaker_track_ids(self) -> dict[str, int]:
        """{label: track_id} for every open track — replace_turns uses this to
        stamp turns.speaker_track_id without creating tracks."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT label, MAX(id) AS id FROM speaker_tracks "
                "WHERE status = 'open' GROUP BY label").fetchall()
        return {r["label"]: int(r["id"]) for r in rows}

    def bind_speaker_track(self, track_id: int, person_id: int, *,
                           ts: float) -> bool:
        """Close a track onto a named person. Idempotent: re-binding to the
        SAME person succeeds; a different person is refused (that re-point
        happens only through the merge hook, deliberately)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status, bound_person_id FROM speaker_tracks "
                "WHERE id = ?", (track_id,)).fetchone()
            if row is None:
                return False
            if (row["status"] or "") == "bound":
                return int(row["bound_person_id"] or 0) == int(person_id)
            self._conn.execute(
                "UPDATE speaker_tracks SET status = 'bound', "
                "bound_person_id = ?, bound_at = ? WHERE id = ?",
                (int(person_id), ts, track_id))
            self._conn.commit()
            return True

    def repoint_speaker_tracks(self, old_person_id: int, new_person_id: int,
                               *, ts: float) -> list[int]:
        """Merge support: move every track bound to `old_person_id` onto
        `new_person_id`. Returns the affected track ids (each needs a rebind
        job so already-rebound rows follow the merge)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM speaker_tracks WHERE status = 'bound' AND "
                "bound_person_id = ?", (int(old_person_id),)).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                ph = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"UPDATE speaker_tracks SET bound_person_id = ?, "
                    f"bound_at = ? WHERE id IN ({ph})",
                    [int(new_person_id), ts, *ids])
                self._conn.commit()
        return ids

    def escrow_fact(self, fact_id: int, track_id: int, *,
                    task_owner: bool = False,
                    commitment_from: bool = False) -> None:
        """Mark a just-inserted fact as escrowed against a voice track. The
        state flip is what keeps the row out of every default surface
        (grounding/retrieval/scoring/constellation all filter on state)."""
        with self._lock:
            self._conn.execute(
                "UPDATE facts SET state = 'escrowed', speaker_track_id = ? "
                "WHERE id = ?", (int(track_id), int(fact_id)))
            if task_owner:
                self._conn.execute(
                    "UPDATE tasks SET owner_track_id = ? WHERE fact_id = ?",
                    (int(track_id), int(fact_id)))
            if commitment_from:
                self._conn.execute(
                    "UPDATE commitments SET from_track_id = ? WHERE fact_id = ?",
                    (int(track_id), int(fact_id)))
            self._conn.commit()

    def escrowed_facts_for_track(self, track_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                self._FACT_SELECT
                + " WHERE f.speaker_track_id = ? AND f.state = 'escrowed'"
                  " ORDER BY f.extracted_at",
                (int(track_id),)).fetchall()
        return [dict(r) for r in rows]

    def rebind_speaker_track_rows(self, track_id: int, person_id: int, *,
                                  previous_person_id: int | None = None,
                                  ts: float) -> dict:
        """Rewrite escrowed rows for a track onto a person — the rebind job's
        write step. Idempotent: person columns are only set where they are
        NULL, already the target person, or the merge's previous person; the
        state flip matches only rows still 'escrowed'. Review/confidence are
        untouched — reactivated facts keep their normal ASR evidence tier and
        re-enter the same review queue. Returns counts + the reactivated
        facts (id, kind, text) so the caller can index them."""
        prev = int(previous_person_id) if previous_person_id else int(person_id)
        with self._lock:
            activated = [
                {"id": int(r["id"]), "kind": r["kind"],
                 "text": r["text"] or r["source_span"] or ""}
                for r in self._conn.execute(
                    "SELECT f.id, f.kind, "
                    "COALESCE(t.text, c.text, f.text) AS text, f.source_span "
                    "FROM facts f "
                    "LEFT JOIN tasks t ON t.fact_id = f.id "
                    "LEFT JOIN commitments c ON c.fact_id = f.id "
                    "WHERE f.speaker_track_id = ? AND f.state = 'escrowed'",
                    (int(track_id),)).fetchall()]
            n_tasks = self._conn.execute(
                "UPDATE tasks SET owner_person_id = ? WHERE owner_track_id = ? "
                "AND (owner_person_id IS NULL OR owner_person_id IN (?, ?))",
                (int(person_id), int(track_id), int(person_id), prev)).rowcount
            n_commitments = self._conn.execute(
                "UPDATE commitments SET from_person_id = ? "
                "WHERE from_track_id = ? "
                "AND (from_person_id IS NULL OR from_person_id IN (?, ?))",
                (int(person_id), int(track_id), int(person_id), prev)).rowcount
            n_facts = self._conn.execute(
                "UPDATE facts SET state = 'active', updated_at = ? "
                "WHERE speaker_track_id = ? AND state = 'escrowed'",
                (ts, int(track_id))).rowcount
            self._conn.commit()
        # Rebound rows change the person's neighborhood — outside the lock
        # (mark_graph_dirty takes the same non-reentrant Lock).
        self.mark_graph_dirty("person", int(person_id), ts=ts)
        for f in activated:
            self.mark_graph_dirty("fact", f["id"], ts=ts)
        return {"facts": int(n_facts), "tasks": int(n_tasks),
                "commitments": int(n_commitments), "activated": activated}

    def log_escrow_rebind(self, *, track_id: int, person_id: int,
                          n_facts: int, n_tasks: int, n_commitments: int,
                          actor: str = "system", reason: str = "",
                          created_at: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO escrow_rebind_log (track_id, person_id, n_facts, "
                "n_tasks, n_commitments, actor, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(track_id), int(person_id), int(n_facts), int(n_tasks),
                 int(n_commitments), actor, reason, created_at))
            self._conn.commit()
            return int(cur.lastrowid)

    def speaker_track_status(self) -> dict:
        """Escrow observability: per-track escrowed-row counts + bind state."""
        with self._lock:
            tracks = [dict(r) for r in self._conn.execute(
                "SELECT * FROM speaker_tracks ORDER BY id").fetchall()]
            for t in tracks:
                t["escrowed_facts"] = int(self._conn.execute(
                    "SELECT COUNT(*) AS n FROM facts WHERE "
                    "speaker_track_id = ? AND state = 'escrowed'",
                    (t["id"],)).fetchone()["n"])
        return {"tracks": tracks}

    def trigger_pattern_exists(self, pattern_key: str) -> bool:
        """Has the miner already suggested this pattern (ANY status — a retired
        row is a durable 'stop suggesting this' verdict)?"""
        if not pattern_key:
            return False
        like = f'%"pattern_key": {json.dumps(pattern_key)}%'
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM triggers WHERE provenance LIKE ? LIMIT 1",
                (like,)).fetchone()
        return row is not None


_store: Store | None = None
_store_lock = threading.Lock()


def get_store() -> Store:
    """Process-wide shared Store (lazy: no DB/dirs created until first use)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = Store()
    return _store
