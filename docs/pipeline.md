# The data pipeline: who mines what, and through which door

Every event that carries text ends up in the same place: `Extractor._persist`.
What differs between sources is which *lane* reads the event and what the
source may create once it gets there. This page is the map; the fix list
rows it closes are F01, F02, F03, F04, F06, F26 and F28 (November 5 2026).

## Lanes

| Lane | Reads | Marks | Where |
| --- | --- | --- | --- |
| Speech | `audio` events, settled turns | yes | `extractor.run_once` |
| Screen | `desktop.screen` vision frames | yes | `screen_extract.run_once` or L3 |
| Per-event jobs | `chat.user`, `assistant.*`, `chat.research`, `peer.answer` | yes | `chat_ingest.run_job`, `research_ingest.run_job`, `peer_channel.run_ingest_job` |
| Document scan | `documents.scan` (inline, consented action) | yes | `documents.ingest` |
| **Text lane** | everything else with text: connector items (`google.mail`, `outlook.calendar`, `imessage.*` …), `phone.*`, `chat.attach`, `onboarding.upload`, future text sources | yes | `extractor.run_text_once` |

`extractor.lane_decision(source, modality, meta)` is the ownership table for
the text lane: `mine`, `leave` (another lane owns it, never touched), or
`skip` (nobody will ever mine it, so it is marked `skipped` and stops
filling the oldest-first window: chat questions, org priorities, the fleet
feed, the cold-start contact rows, peer pairing notices).

The text lane runs as the `extract_text` worker job. It is nudged by the
producers that land items (connector sync, phone ingest, attachments),
chained from every speech pass, and enqueued once at boot. Fresh events
(`QUILL_TEXT_EXTRACT_FRESH_S`, 600 s) go first.

`GET /events/{id}/extraction` answers "has this event been mined, and what
came of it" for any event. Uploads return `job_id` and `facts_pending`.

## The one door

```
persist_facts(store, facts, anchor, text, now, *, event_source, window="",
              speaker=None, index=True, me_is_user=None, event_time=None)
```

builds a synthetic turn and calls `Extractor._persist`, so a non-speech
source gets: the `fact_candidates` ledger, the hygiene gate (confidence
floor, verbatim span, field validation), the assertion review (quoted /
hypothetical never auto-insert), structural and embedding dedup, entity
anchored supersession, the KG belief dual-write, context stamping where
it applies, and the usage ledger.

Per-source data, not per-source code:

- `event_source` / `window` choose the source policy class
  (`data/source_policies.json`). A class that may not create commitments
  loses its tasks and commitments; one that may not create claims loses
  its claims. The dropped candidates record the reason.
- `me_is_user`: whether "me" / "I" is the enrolled user. Default: yes for
  the user's own typed chat, phone notes, uploads and documents; no for a
  teammate's answer, a cloud assistant's note, a research answer. Speech
  decides per speaker label as before.
- `index=False` for a throwaway store (previews, fixtures): no vector
  index, no dedup probe, but the confidence floor and assertion review
  still apply.
- Live "shall I do this?" offers fire for speech only.

## Salience before spend (F26)

`ingest_salience.plan(text, source=..., store=...)` decides which chunks of
a document or connector item deserve a model call:

1. the learned `extract_triage` head's active skip wins (shadow mode never
   influences);
2. a chunk that names a known entity or person is kept;
3. the intent router's "no actionable signal" skips;
4. the rest are kept.

Kept chunks are ranked entity-anchored first, cut to
`QUILL_EXTRACT_CHUNKS_PER_ITEM` (12) and the family's remaining daily
budget (`QUILL_EXTRACT_BUDGET_DEFAULT` 400, `QUILL_EXTRACT_BUDGET_<FAMILY>`
such as `GOOGLE`, `PHONE`, `CHAT`), then restored to document order.
`charge(source, n)` records spend; the counter is in-process and restarts
with the day or the process. A budget-exhausted event is marked
`deferred` and stays searchable through its chunk vectors.
`QUILL_INGEST_SALIENCE=0` extracts everything.

## Chunk vectors (F02)

`memory.index_event(eid, ev)` writes the event row (title plus leading
content for documents and typed text; the summary otherwise) and, for text
longer than `QUILL_MEMORY_CHUNK_MIN` (600), one chunk row per
`QUILL_MEMORY_CHUNK_CHARS` (1,200). Chunk row ids are
`CHUNK_ID_OFFSET + event_id * CHUNK_STRIDE + index`, so a hit maps back to
its parent without a schema change; the hit payload carries
`parent_event_id`, `chunk_index` and `chunk_text`, and the result set
dedupes to one row per document keeping the strongest passage. Deletion
and compaction drop the chunk range; `vector_gc` sweeps orphans; the
startup backfill adds chunks for older documents, at most
`QUILL_MEMORY_CHUNK_BACKFILL` (300) per boot.

## Content dedup (F06)

`events.content_sha1` holds the byte digest for uploads and the normalised
text digest (whitespace, case and trailing punctuation ignored) for typed
and read text. `Store.find_event_by_content_sha1` is the probe:
attachments return the existing event (`duplicate: true`), `chat_ingest`
treats the same statement from the same source within `QUILL_CHAT_DEDUP_S`
(6 h) as one memory when it is at least `QUILL_CHAT_DEDUP_MIN_CHARS` (24)
long, and the document scan skips a copy under another name.

## People from documents (F59)

Documents are real sources of people (attendee lists, org charts,
contracts) but must not decide who is recognised. The policy field
`hold_person_candidates` (on `user_authored_document` and
`shared_document`) lets a strong mention mint a person who stays
`promotion_state = candidate`: the adjudicator reviews candidates and the
People tab hides them by default. A held source never calls the promotion
bump, so re-reading the document a hundred times leaves the person in
review. `create_person_candidates` keeps its old meaning (mint and
promote) for conversation classes. `SourcePolicy.may_mint_people` is the
union the pipeline consults.

## The invariant

`tests/test_pipeline_unify.py::OneDoorInvariantTests` drives the speech
lane, the text lane and `persist_facts` through a spy on
`Extractor._persist` and `Extractor._gate`, and asserts all three reach
the same method with the same gate arguments; a structural guard asserts
that no producer module writes fact rows, calls the gate, or defines a
persist of its own. A second lane is scheduling, never a second door.

## Mail bodies (F04)

Off by default. Per connector, `POST /connectors/{id}/bodies {enabled}`
records a consent whose sentence names the wider permission
(`gmail.readonly` for Google, `Mail.Read` for Outlook). With consent on,
the next sign-in asks for that scope and the scope guard accepts it; bodies
are fetched only when consent and the granted scope both hold, so an old
token keeps reading headers until the user reconnects (the Connectors
sheet says "Reconnect to grant"). A body lands as a DOCUMENT event with
`privacy_class` no lower than `personal`, and the text lane mines it with
the sender as the speaker. Bodies are capped at
`QUILL_MAIL_BODY_MAX_CHARS` (20,000); HTML is stripped to text.

Before a seat can use this with Google, `gmail.readonly` must be added to
the OAuth consent screen.

## Scanned PDFs (F28)

`documents._read_pdf` falls back to OCR when a PDF has pages but under 40
chars of text: pages are rasterised (pypdfium2, else `pdftoppm`) and read
by `app.perception.ocr.get_default_engine()` (Windows.Media.Ocr, then
tesseract via pytesseract or the CLI, then none). Without an engine the
vision model's `ocr_text` is used for at most `QUILL_PDF_OCR_MAX_PAGES`
(6) pages (`QUILL_PDF_OCR_VLM=0` disables that). `QUILL_PDF_OCR=0`
disables the whole fallback.

## Tests

`tests/test_pipeline_unify.py` holds one check per fix-list "done when".
The older seam tests (`test_chat_ingest`, `test_peer_channel`,
`test_screen_extract_backlog`, `test_self_profile`,
`test_attachments_defer`) now patch `app.services.extractor.persist_facts`
and `attachments._queue_fact_mine`.
