"""Pipeline fix list, Nov 5 2026 — the Pipeline rows.

F01 one persist path; F02 document content as chunk vectors; F03 every
text-bearing event is mined; F04 mail bodies under consent + a privacy
floor; F06 content dedup; F26 ingest-level salience; F28 scanned PDFs.
Each test is the row's "done when" check. Offline: the extractor's model
call is stubbed, the vector index is a fake, no network.
"""
from __future__ import annotations

import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.events import Event, Modality
from app.storage import Store

NOW = 1_762_300_000.0
EMPTY = {"tasks": [], "commitments": [], "claims": [], "entities": [],
         "relations": []}


def _store(tmp: Path) -> Store:
    return Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")


def _quiet():
    """Keep fact writes offline: no index, no similarity probe, no offers."""
    return [
        patch("app.services.extractor._index_fact", lambda *a, **k: None),
        patch("app.services.fact_gate._similar_active", return_value=[]),
        patch("app.services.task_offer.offer_task", lambda *a, **k: None),
        patch("app.services.identity.user_identity",
              return_value={"name": "Test Person", "source": "profile"}),
    ]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_pipe_"))
        self.store = _store(self.tmp)
        self._patches = _quiet()
        for p in self._patches:
            p.start()
        from app.services import self_profile
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        self._env = patch.dict(os.environ, {"QUILL_USAGE_LEDGER": "0",
                                            "QUILL_HEADS": "off"}, clear=False)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        for p in self._patches:
            p.stop()
        try:
            self.store.close()
        except Exception:
            pass


# --- F01: one persist path ----------------------------------------------------
class OnePersistPathTests(_Base):
    def test_the_thin_document_path_is_gone(self) -> None:
        from app.services import documents
        self.assertFalse(hasattr(documents, "_persist_facts"))

    def test_chat_quoted_hypothetical_lands_in_review_not_as_fact(self) -> None:
        """A typed turn relaying someone else's hypothetical used to auto-insert
        as a fact through documents._persist_facts (no assertion class)."""
        from app.services.extractor import persist_facts
        text = "Marc said if the pilot slips we might drop the venue"
        facts = {**EMPTY, "claims": [{
            "text": "the venue might be dropped", "confidence": 0.9,
            "source_span": "we might drop the venue",
            "assertion": "hypothetical"}]}
        n = persist_facts(self.store, facts, None, text, NOW,
                          event_source="chat.user", speaker="Test Person")
        self.assertEqual(n, 0)
        self.assertEqual(self.store.list_facts(kind="claim", limit=10), [])
        cands = self.store.list_fact_candidates()
        self.assertEqual([c["status"] for c in cands], ["review"])
        self.assertIn("hypothetical", cands[0]["verdict_reason"])

    def test_chat_sourced_fact_leaves_a_candidate_row(self) -> None:
        from app.services import chat_ingest
        import app.services.worker as worker_mod
        import app.storage as storage_mod
        ev = Event(time=NOW, modality=Modality.TEXT,
                   raw="I'll send Marc the deck by Friday",
                   summary="[chat] ...", source="chat.user")
        eid = self.store.insert(ev)
        facts = {**EMPTY, "commitments": [{
            "text": "send Marc the deck by Friday", "from_person": "me",
            "to_person": "Marc", "confidence": 0.9,
            "source_span": "send Marc the deck by Friday"}]}
        queued = []
        fake_worker = SimpleNamespace(enqueue=lambda k, *a, **kw: queued.append(k) or 1)
        with patch.object(storage_mod, "get_store", return_value=self.store), \
             patch.object(worker_mod, "worker", fake_worker), \
             patch("app.services.extractor.extractor._extract_text",
                   return_value=facts):
            chat_ingest.run_job({"event_id": eid, "text": ev.raw})
        rows = self.store.list_fact_candidates()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "accepted")
        self.assertEqual(rows[0]["source_event_id"], eid)
        self.assertEqual(self.store.list_facts(kind="commitment", limit=5)[0]["source_event_id"], eid)
        self.assertIn("graph", queued)

    def test_typed_due_on_claim_dual_writes_a_kg_predicate(self) -> None:
        """KG belief dual-write used to run from the audio path only."""
        from app.services.extractor import persist_facts
        pid = self.store.resolve_entity("Atlas rollout", "project", ts=NOW)
        text = "The Atlas rollout is due on 2026-12-01"
        facts = {**EMPTY, "claims": [{
            "text": "Atlas rollout is due on 2026-12-01", "confidence": 0.9,
            "source_span": "Atlas rollout is due on 2026-12-01",
            "subject": "Atlas rollout", "subject_kind": "entity",
            "predicate": "due_on", "object": "2026-12-01",
            "object_kind": "entity", "assertion": "asserted"}]}
        n = persist_facts(self.store, facts, None, text, NOW,
                          event_source="chat.user", speaker="Test Person")
        self.assertEqual(n, 1)
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT COUNT(*) AS n FROM kg_predicates WHERE predicate='due_on' "
                "AND subj_id=?", (int(pid),)).fetchone()
        self.assertEqual(int(row["n"]), 1)

    def test_me_is_the_user_for_their_own_words_only(self) -> None:
        from app.services import self_profile
        from app.services.extractor import persist_facts
        facts = {**EMPTY, "tasks": [{"text": "send the revised deck",
                                     "owner": "me", "confidence": 0.9,
                                     "source_span": "send the revised deck"}]}
        persist_facts(self.store, facts, None, "I'll send the revised deck",
                      NOW, event_source="chat.user", speaker="Test Person")
        mine = self.store.list_facts(kind="task", limit=5)[0]
        self.assertEqual(mine["owner"], "Test Person")
        self.assertIsNotNone(self_profile.self_person_id(self.store))
        # A teammate's answer: "me" is the teammate, never the user.
        facts2 = {**EMPTY, "tasks": [{"text": "send the budget sheet",
                                      "owner": "me", "confidence": 0.9,
                                      "source_span": "send the budget sheet"}]}
        persist_facts(self.store, facts2, None,
                      "[from Sarah Chen's Sparrow] I'll send the budget sheet",
                      NOW + 1, event_source="peer.answer", speaker="Sarah Chen")
        theirs = [f for f in self.store.list_facts(kind="task", limit=5)
                  if f["text"].lower() == "send the budget sheet"][0]
        self.assertNotEqual(theirs["owner"], "Test Person")

    def test_source_policy_governs_what_a_source_may_create(self) -> None:
        """A research answer may yield claims, never commitments or tasks."""
        from app.services.extractor import persist_facts
        text = ("Book the venue by Friday. We will ship the pilot in May. "
                "Acme raised a Series B in 2025.")
        facts = {"tasks": [{"text": "Book the venue", "confidence": 0.9,
                            "source_span": "Book the venue by Friday"}],
                 "commitments": [{"text": "ship the pilot in May",
                                  "confidence": 0.9, "from_person": "",
                                  "to_person": "",
                                  "source_span": "We will ship the pilot in May"}],
                 "claims": [{"text": "Acme raised a Series B in 2025",
                             "confidence": 0.9,
                             "source_span": "Acme raised a Series B in 2025"}],
                 "entities": [], "relations": []}
        n = persist_facts(self.store, facts, None, text, NOW,
                          event_source="chat.research")
        self.assertEqual(n, 1)
        kinds = sorted(f["kind"] for f in self.store.list_facts(limit=10))
        self.assertEqual(kinds, ["claim"])
        dropped = [c for c in self.store.list_fact_candidates()
                   if c["status"] == "dropped"]
        self.assertEqual(len(dropped), 2)
        self.assertTrue(all("source policy" in (c["verdict_reason"] or "")
                            for c in dropped))

    def test_bulk_sources_never_fire_live_task_offers(self) -> None:
        from app.services.extractor import persist_facts
        calls = []
        facts = {**EMPTY, "tasks": [{"text": "book the venue", "confidence": 0.9,
                                     "source_span": "book the venue"}]}
        with patch("app.services.task_offer.offer_task",
                   lambda *a, **k: calls.append(a)):
            persist_facts(self.store, facts, None, "please book the venue",
                          NOW, event_source="documents.scan")
        self.assertEqual(calls, [])


# --- F03 / F26: the text lane -------------------------------------------------
MAIL_BODY = ("Hi Test,\n\nCan you send me the Atlas budget by Thursday? "
             "We also need to book the venue for the kickoff. The vendor quote "
             "came in at $4,200.\n\nThanks,\nDana")


class TextLaneTests(_Base):
    def _fake_extract(self, text, *, speaker=None):
        self.seen.append((text, speaker))
        if "Call Dana" in text:
            return {**EMPTY, "tasks": [{"text": "Call Dana about the budget",
                                        "owner": "me", "confidence": 0.9,
                                        "source_span": "Call Dana about the Atlas budget"}]}
        if "Atlas budget" in text:
            return {**EMPTY, "tasks": [{"text": "send the Atlas budget",
                                        "owner": "Test Person",
                                        "confidence": 0.9,
                                        "source_span": "send me the Atlas budget"}]}
        return EMPTY

    def setUp(self) -> None:
        super().setUp()
        self.seen: list = []
        from app.services import ingest_salience
        ingest_salience.reset()
        self.addCleanup(ingest_salience.reset)

    def test_mail_body_and_phone_note_produce_facts_in_one_cycle(self) -> None:
        from app.services.extractor import Extractor
        from app.services.connectors import scheduler
        stub = SimpleNamespace(id="google")
        mail_eid = scheduler.land_item(stub, {
            "kind": "mail", "external_id": "<m1@acme>", "ts": NOW - 5,
            "title": "Atlas budget", "text": "From: Dana\nSubject: Atlas budget",
            "from": "Dana Whitfield <dana@acme.com>", "body": MAIL_BODY,
            "people": ["Dana Whitfield"]}, store=self.store)
        note_eid = self.store.insert(Event(
            time=NOW - 4, modality=Modality.SYSTEM,
            raw="Call Dana about the Atlas budget tomorrow",
            summary="[phone:note] ...", source="phone.note",
            meta={"origin": "phone", "kind": "note"}))
        query_eid = self.store.insert(Event(
            time=NOW - 3, modality=Modality.TEXT, raw="is Dana leaving?",
            summary="[chat] is Dana leaving?", source="chat.query",
            meta={"section": "chat", "kind": "query"}))
        chat_eid = self.store.insert(Event(
            time=NOW - 2, modality=Modality.TEXT, raw="I told Marc we ship Friday",
            summary="[chat] ...", source="chat.user", meta={"section": "chat"}))
        ops_eid = self.store.insert(Event(
            time=NOW - 1, modality=Modality.SYSTEM, raw="priority pull done",
            summary="[org] priority", source="org.priority"))
        ex = Extractor(self.store)
        with patch.object(ex, "_extract_text", side_effect=self._fake_extract), \
             patch("time.time", return_value=NOW):
            res = ex.run_text_once()
        self.assertEqual(res["events"], 2, res)
        self.assertEqual(res["facts"], 2, res)
        # Rows other lanes own never make the job re-enqueue itself.
        self.assertFalse(res["remaining"])
        tasks = {t["text"].lower() for t in self.store.list_facts(kind="task", limit=10)}
        self.assertEqual(tasks, {"send the atlas budget", "call dana about the budget"})
        # The sender is the speaker for mail; the user for their own note.
        speakers = {s for _, s in self.seen}
        self.assertEqual(speakers, {"Dana Whitfield", "Test Person"})
        mail_text = next(t for t, _ in self.seen if "Atlas budget" in t)
        self.assertTrue(mail_text.startswith("From: Dana Whitfield"))
        # Done-when: mined rows are marked; the query is marked skipped; the
        # typed chat turn (owned by its own job) is untouched; ops noise is
        # marked so it stops filling the window.
        for eid, status in ((mail_eid, "ok"), (note_eid, "ok"),
                            (query_eid, "skipped"), (ops_eid, "skipped")):
            row = self.store.get_event(eid)
            self.assertIsNotNone(row["extracted_at"], eid)
            self.assertEqual(row["extract_status"], status, eid)
        self.assertIsNone(self.store.get_event(chat_eid)["extracted_at"])
        # Provenance: each fact anchors to the event it came from.
        anchors = {t["source_event_id"] for t in self.store.list_facts(kind="task", limit=10)}
        self.assertEqual(anchors, {mail_eid, note_eid})
        # The per-event completion signal.
        import json
        meta = json.loads(self.store.get_event(mail_eid)["meta"])
        self.assertEqual(meta["extraction"]["facts"], 1)

    def test_lane_decision_table(self) -> None:
        from app.services.extractor import lane_decision as d
        self.assertEqual(d("google.mail", "document", {})[0], "mine")
        self.assertEqual(d("outlook.calendar", "system", {})[0], "mine")
        self.assertEqual(d("phone.note", "system", {})[0], "mine")
        self.assertEqual(d("chat.attach", "document", {})[0], "mine")
        self.assertEqual(d("chat.attach", "vision", {})[0], "mine")
        self.assertEqual(d("chat.query", "text", {})[0], "skip")
        self.assertEqual(d("chat.user", "text", {"kind": "query"})[0], "skip")
        for owned in ("chat.user", "assistant.claude", "chat.research",
                      "peer.answer", "documents.scan", "desktop.screen"):
            self.assertEqual(d(owned, "text", {})[0], "leave", owned)
        self.assertEqual(d("audio.whisper", "audio", {})[0], "leave")
        self.assertEqual(d("webcam", "vision", {})[0], "leave")
        for noise in ("org.priority", "fleet.feed", "exhaust.gmail", "peer.pair"):
            self.assertEqual(d(noise, "system", {})[0], "skip", noise)

    def test_a_model_failure_leaves_the_event_for_retry(self) -> None:
        from app.services.extractor import Extractor
        eid = self.store.insert(Event(
            time=NOW, modality=Modality.SYSTEM,
            raw="Call Dana about the Atlas budget tomorrow", source="phone.note",
            meta={"kind": "note"}))
        ex = Extractor(self.store)
        with patch.object(ex, "_extract_text", side_effect=RuntimeError("llm down")):
            res = ex.run_text_once()
        self.assertEqual(res["events"], 0)
        row = self.store.get_event(eid)
        self.assertIsNone(row["extracted_at"])
        self.assertEqual(int(row["extract_attempts"]), 1)

    def test_extraction_status_route_reports_the_outcome(self) -> None:
        from app.api import routes
        eid = self.store.insert(Event(time=NOW, modality=Modality.DOCUMENT,
                                      raw="x" * 50, source="chat.attach"))
        with patch("app.storage.get_store", return_value=self.store):
            pending = routes.event_extraction(eid)
            self.store.set_event_meta_key(eid, "extraction", {"facts": 3, "chunks": 2})
            self.store.mark_extracted([eid], NOW)
            done = routes.event_extraction(eid)
        self.assertFalse(pending["done"])
        self.assertEqual(pending["status"], "pending")
        self.assertTrue(done["done"])
        self.assertEqual((done["facts"], done["chunks"]), (3, 2))


class SalienceTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        from app.services import ingest_salience as sal
        self.sal = sal
        sal.reset()
        self.addCleanup(sal.reset)
        self.store.resolve_entity("Atlas rollout", "project", ts=NOW)

    def test_known_entity_and_signal_chunks_are_kept_the_rest_skipped(self) -> None:
        text = ("The Atlas rollout moved to December after the review.\n\n"
                "yeah haha so tired lol ok sure thing\n\n"
                "Please book the venue before Friday.")
        plan = self.sal.plan(text, source="google.mail", store=self.store,
                             now=NOW, chunk_size=70)
        self.assertEqual([c.index for c in plan.chunks], [0, 2])
        self.assertEqual(plan.chunks[0].reason, "known entity")
        self.assertEqual(plan.skipped, 1)
        self.assertEqual(plan.reasons.get("intent"), 1)

    def test_per_item_cap_prefers_entity_anchored_chunks(self) -> None:
        text = ("Please book the venue before Friday.\n\n"
                "The Atlas rollout moved to December after the review.")
        with patch.dict(os.environ, {"QUILL_EXTRACT_CHUNKS_PER_ITEM": "1"}):
            plan = self.sal.plan(text, source="google.mail", store=self.store,
                                 now=NOW, chunk_size=70)
        self.assertEqual([c.index for c in plan.chunks], [1])
        self.assertEqual(plan.reasons.get("cap"), 1)

    def test_daily_budget_per_source_family(self) -> None:
        text = "Please book the venue before Friday."
        with patch.dict(os.environ, {"QUILL_EXTRACT_BUDGET_GOOGLE": "1"}):
            first = self.sal.plan(text, source="google.mail", store=self.store, now=NOW)
            self.assertEqual(len(first.chunks), 1)
            self.sal.charge("google.mail", 1, now=NOW)
            second = self.sal.plan(text, source="google.mail", store=self.store, now=NOW)
            self.assertTrue(second.budget_exhausted)
            self.assertEqual(second.chunks, [])
            # Another family is unaffected.
            other = self.sal.plan(text, source="phone.note", store=self.store, now=NOW)
            self.assertEqual(len(other.chunks), 1)

    def test_bypass_flag_extracts_everything(self) -> None:
        text = "yeah haha so tired lol ok sure thing"
        with patch.dict(os.environ, {"QUILL_INGEST_SALIENCE": "0"}):
            plan = self.sal.plan(text, source="google.mail", store=self.store, now=NOW)
        self.assertEqual(len(plan.chunks), 1)

    def test_deferred_events_are_marked_and_counted_by_the_lane(self) -> None:
        from app.services.extractor import Extractor
        eid = self.store.insert(Event(
            time=NOW, modality=Modality.SYSTEM,
            raw="Call Dana about the Atlas budget tomorrow", source="phone.note",
            meta={"kind": "note"}))
        ex = Extractor(self.store)
        with patch.dict(os.environ, {"QUILL_EXTRACT_BUDGET_PHONE": "0"}), \
             patch.object(ex, "_extract_text", return_value=EMPTY):
            res = ex.run_text_once()
        self.assertEqual(res["deferred"], 1)
        self.assertEqual(self.store.get_event(eid)["extract_status"], "deferred")


# --- F02: document content as chunk vectors ----------------------------------
class _FakeVectors:
    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.ranges: list = []
        self.hits: list = []

    def add(self, id, time_, modality, text, vector):
        self.rows[int(id)] = {"id": int(id), "time": time_, "modality": modality,
                              "text": text}

    def add_many(self, rows):
        for r in rows:
            self.rows[int(r["id"])] = dict(r)

    def search(self, _vec, k=10, modality=None):
        return [dict(h) for h in self.hits][:k]

    def delete_ids(self, ids):
        for i in ids:
            self.rows.pop(int(i), None)
        return len(ids)

    def delete_ranges(self, ranges):
        self.ranges.extend(ranges)
        for lo, hi in ranges:
            for i in [i for i in self.rows if lo <= i < hi]:
                self.rows.pop(i)
        return len(ranges)

    def list_ids(self):
        return set(self.rows)


class ChunkVectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_chunks_"))
        self.env = patch.dict(os.environ, {
            "QUILL_USAGE_LEDGER": "0", "QUILL_MEMORY_CHUNK_CHARS": "400",
            "QUILL_MEMORY_CHUNK_MIN": "600"}, clear=False)
        self.env.start()
        self.store = _store(self.tmp)
        from app.services.memory import MemoryEngine
        self.engine = MemoryEngine(store=self.store)
        self.vectors = _FakeVectors()
        self.engine._vectors = self.vectors
        self.engine._semantic = True
        self.engine._embed = lambda text: [0.0] * 8

    def tearDown(self) -> None:
        self.env.stop()
        self.store.close()

    def _doc(self) -> tuple[int, Event, str]:
        pages = [f"Page {i} of the report. " + ("Lorem ipsum detail. " * 15)
                 for i in range(1, 9)]
        needle = "the vendor quote came in at four thousand two hundred"
        pages[5] += needle
        raw = "\n\n".join(pages)
        ev = Event(time=NOW, modality=Modality.DOCUMENT, raw=raw,
                   summary=f"[attach] report.pdf ({len(raw)} chars)",
                   source="chat.attach", meta={"title": "report.pdf"})
        return self.store.insert(ev), ev, needle

    def test_document_indexes_its_content_not_the_filename_line(self) -> None:
        from app.services import memory as mem
        eid, ev, needle = self._doc()
        n = self.engine.index_event(eid, ev)
        self.assertGreater(n, 2)
        event_row = self.vectors.rows[eid]
        self.assertTrue(event_row["text"].startswith("report.pdf\nPage 1"))
        chunk_ids = [i for i in self.vectors.rows if mem.is_chunk_id(i)]
        self.assertEqual(len(chunk_ids), n - 1)
        for cid in chunk_ids:
            parent, idx = mem.parent_of_chunk_id(cid)
            self.assertEqual(parent, eid)
            self.assertEqual(self.vectors.rows[cid]["modality"], "document")
        self.assertTrue(any(needle in self.vectors.rows[c]["text"] for c in chunk_ids))
        # It is on the in-process timeline too (producers that insert directly).
        self.assertEqual(len(self.engine._events), 1)

    def test_a_chunk_hit_returns_the_document_with_the_passage(self) -> None:
        from app.services import memory as mem
        eid, ev, needle = self._doc()
        self.engine.index_event(eid, ev)
        cid = next(c for c in self.vectors.rows if mem.is_chunk_id(c)
                   and needle in self.vectors.rows[c]["text"])
        self.vectors.hits = [
            {"id": eid, "time": NOW, "text": "report.pdf", "score": 0.31},
            {"id": cid, "time": NOW, "text": self.vectors.rows[cid]["text"],
             "score": 0.88},
        ]
        with patch.object(self.engine, "_keyword_hits", return_value=[]):
            out = self.engine.search("vendor quote", limit=5)
        self.assertEqual(len(out), 1)
        hit = out[0]
        self.assertEqual(hit["event_id"], eid)
        self.assertEqual(hit["parent_event_id"], eid)
        self.assertIn(needle, hit["chunk_text"])
        self.assertEqual(hit["chunk_index"], mem.parent_of_chunk_id(cid)[1])
        self.assertGreaterEqual(hit["score"], 0.88)

    def test_short_or_non_text_events_get_no_chunk_rows(self) -> None:
        from app.services import memory as mem
        ev = Event(time=NOW, modality=Modality.AUDIO, raw="x" * 2000,
                   summary="a turn", source="audio.whisper")
        eid = self.store.insert(ev)
        self.engine.index_event(eid, ev)
        self.assertEqual([i for i in self.vectors.rows if mem.is_chunk_id(i)], [])
        self.assertEqual(self.vectors.rows[eid]["text"], "a turn")
        ev2 = Event(time=NOW, modality=Modality.TEXT, raw="short note",
                    summary="[chat] short note", source="chat.user")
        eid2 = self.store.insert(ev2)
        self.engine.index_event(eid2, ev2)
        self.assertEqual(self.vectors.rows[eid2]["text"], "short note")

    def test_deleting_the_event_drops_its_chunk_rows(self) -> None:
        from app.services import memory as mem
        from app.storage import add_delete_hook, remove_delete_hook
        eid, ev, _ = self._doc()
        self.engine.index_event(eid, ev)
        add_delete_hook(self.engine._on_store_change)
        try:
            self.store.erase_event(eid)
        finally:
            remove_delete_hook(self.engine._on_store_change)
        self.assertIn(mem.chunk_id_range(eid), self.vectors.ranges)
        self.assertEqual([i for i in self.vectors.rows if mem.is_chunk_id(i)], [])

    def test_backfill_adds_chunks_for_documents_indexed_before_chunking(self) -> None:
        from app.services import memory as mem
        eid, ev, _ = self._doc()
        self.vectors.add(eid, NOW, "document", ev.summary, [0.0] * 8)
        rows = self.store.all_with_ids()
        fake_embedder = SimpleNamespace(
            encode_many=lambda texts: [SimpleNamespace(tolist=lambda: [0.0] * 8)
                                       for _ in texts])
        with patch("app.services.embeddings.embedder", fake_embedder):
            n = self.engine._backfill_chunks(self.vectors, rows, self.vectors.list_ids())
        self.assertGreater(n, 2)
        self.assertIn(mem.chunk_vector_id(eid, 0), self.vectors.rows)


# --- F06: content dedup ------------------------------------------------------
class ContentDedupTests(_Base):
    def test_reattaching_a_file_returns_the_existing_event_and_mines_nothing(self) -> None:
        from app.services import attachments
        queued = []
        with patch.object(attachments, "upload_dir", return_value=self.tmp), \
             patch("app.storage.get_store", return_value=self.store), \
             patch("app.services.attachments._index_event"), \
             patch("app.services.documents.extract_text",
                   return_value="Executive brief on Physical AI for Foundry."), \
             patch.object(attachments, "_queue_fact_mine",
                          side_effect=lambda a, **k: queued.append(a) or 7):
            first = attachments.ingest_bytes("brief.txt", b"the same bytes")
            again = attachments.ingest_bytes("renamed.txt", b"the same bytes")
        self.assertTrue(first["ok"] and again["ok"])
        self.assertEqual(again["event_id"], first["event_id"])
        self.assertTrue(again["duplicate"])
        self.assertFalse(again["facts_pending"])
        self.assertIn("Physical AI", again["context"])
        self.assertEqual(queued, [first["event_id"]])
        self.assertEqual(len(self.store.all_with_ids()), 1)
        self.assertEqual(len(list(self.tmp.glob("*_brief.txt"))), 1)
        self.assertEqual(list(self.tmp.glob("*_renamed.txt")), [])

    def test_retyping_the_same_statement_is_one_memory(self) -> None:
        from app.services import chat_ingest
        import app.services.worker as worker_mod
        import app.storage as storage_mod
        queued = []
        fake_worker = SimpleNamespace(enqueue=lambda k, *a, **kw: queued.append(k) or 1)
        with patch.object(storage_mod, "get_store", return_value=self.store), \
             patch.object(worker_mod, "worker", fake_worker), \
             patch("app.services.attachments._index_event", lambda *a: None):
            a = chat_ingest.ingest("I told Marc we ship the pilot next week")
            b = chat_ingest.ingest("I told Marc we ship the pilot   next week")
        self.assertEqual(a, b)
        self.assertEqual(queued, ["chat_ingest"])
        self.assertEqual(len(self.store.all_with_ids()), 1)

    def test_document_scan_skips_the_same_text_under_another_name(self) -> None:
        from app.services import documents as D
        from app.services.extractor import extractor
        docs = self.tmp / "docs"
        docs.mkdir()
        (docs / "plan.txt").write_text("Coral restoration grant proposal.", encoding="utf-8")
        (docs / "plan copy.txt").write_text("Coral restoration grant proposal.", encoding="utf-8")
        cfg = SimpleNamespace(documents=SimpleNamespace(
            enabled=True, exts=frozenset({".txt"}), max_docs=40,
            max_bytes=3_000_000, max_chars=40_000, chunk_chars=2500,
            max_depth=4, roots_raw="", state_path=str(self.tmp / "state.json")))
        with patch.object(D, "settings", cfg), \
             patch.object(extractor, "_extract_text", return_value=EMPTY):
            res = D.ingest(roots=[docs], store=self.store)
        self.assertEqual(res["documents"], 1)
        self.assertEqual(res["duplicates"], 1)
        self.assertEqual(len(self.store.all_with_ids()), 1)


# --- F04: mail bodies under consent and a privacy floor ----------------------
def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


class MailBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_bodies_"))
        from app.services import capture_consent as cc
        self.cc = cc
        self._p = patch.object(cc, "_path", return_value=self.tmp / "consent.json")
        self._p.start()
        cc._cached = None

    def tearDown(self) -> None:
        self._p.stop()
        self.cc._cached = None

    def test_body_consent_is_its_own_record_and_survives_sync_toggles(self) -> None:
        self.assertFalse(self.cc.allows_connector_bodies("google"))
        rec = self.cc.record_connector_bodies("google", enabled=True, sentence="bodies too")
        self.assertTrue(rec["bodies"])
        self.assertTrue(self.cc.allows_connector_bodies("google"))
        self.cc.record_connector("google", enabled=True, sentence="headers")
        self.assertTrue(self.cc.allows_connector_bodies("google"))
        self.assertTrue(self.cc.allows_connector("google"))
        self.cc.record_connector_bodies("google", enabled=False)
        self.assertFalse(self.cc.allows_connector_bodies("google"))
        self.assertTrue(self.cc.allows_connector("google"))

    def test_google_scope_follows_consent_and_body_scope_needs_it(self) -> None:
        from app.services import exhaust_ingest as ex
        self.assertEqual(ex.requested_scopes(), ex.SCOPES)
        with self.assertRaises(PermissionError):
            ex.assert_metadata_scopes(f"{ex.BODY_SCOPE} {ex.CALENDAR_SCOPE}")
        self.cc.record_connector_bodies("google", enabled=True, sentence="x")
        self.assertEqual(ex.requested_scopes(), (ex.BODY_SCOPE, ex.CALENDAR_SCOPE))
        ex.assert_metadata_scopes(f"{ex.BODY_SCOPE} {ex.CALENDAR_SCOPE}")
        ex.assert_metadata_scopes(" ".join(ex.SCOPES))   # the old grant still works
        with self.assertRaises(PermissionError):
            ex.assert_metadata_scopes(f"{ex.BODY_SCOPE} {ex.CALENDAR_SCOPE} "
                                      "https://www.googleapis.com/auth/gmail.modify")

    def test_gmail_full_fetch_returns_the_text_body(self) -> None:
        from app.services import exhaust_ingest as ex
        msg = {"id": "m1", "threadId": "t1", "internalDate": str(int((NOW - 60) * 1000)),
               "payload": {"mimeType": "multipart/alternative",
                           "headers": [{"name": "From", "value": "Dana <dana@acme.com>"},
                                       {"name": "Subject", "value": "Atlas budget"}],
                           "parts": [
                               {"mimeType": "text/html",
                                "body": {"data": _b64url("<p>Hi <b>there</b></p>")}},
                               {"mimeType": "text/plain",
                                "body": {"data": _b64url("Hi there\nsend the budget")}}]}}
        urls = []

        def fake_get(url):
            urls.append(url)
            if url.endswith("messages?maxResults=100"):
                return {"messages": [{"id": "m1"}]}
            return msg

        with patch.object(ex, "_google_get", fake_get):
            rows = ex.fetch_gmail_headers(days=1, now=NOW, include_subject=True,
                                          include_body=True)
        self.assertEqual(rows[0]["body"], "Hi there\nsend the budget")
        self.assertEqual(rows[0]["headers"]["subject"], "Atlas budget")
        self.assertIn("format=full", urls[1])
        # Without body consent the metadata guard still aborts on body data.
        with patch.object(ex, "_google_get", fake_get):
            with self.assertRaises(RuntimeError):
                ex.fetch_gmail_headers(days=1, now=NOW, include_subject=True)

    def test_html_only_body_is_stripped_to_text(self) -> None:
        from app.services import exhaust_ingest as ex
        payload = {"mimeType": "text/html",
                   "body": {"data": _b64url("<div>Quote: <b>$4,200</b><br>Thanks</div>")}}
        self.assertEqual(ex.gmail_body_text(payload), "Quote: $4,200\nThanks")

    def test_landed_body_is_a_document_at_least_personal(self) -> None:
        from app.services import privacy_class as pc
        from app.services.connectors import scheduler
        from app.services.connectors.google import google
        item = google._mail_item({"id": "<m1@acme>", "thread_id": "t1", "ts": NOW,
                                  "headers": {"from": "Dana <dana@acme.com>",
                                              "subject": "Lunch"},
                                  "body": "Lunch at noon Thursday?"})
        self.assertEqual(item["body"], "Lunch at noon Thursday?")
        self.assertEqual(item["meta"]["privacy_class"], "personal")
        store = _store(self.tmp)
        try:
            eid = scheduler.land_item(google, item, store=store)
            row = store.get_event(eid)
            self.assertEqual(row["modality"], "document")
            self.assertGreaterEqual(pc.rank(row["privacy_class"]), pc.rank("personal"))
            import json
            self.assertTrue(json.loads(row["meta"])["body_consent"])
        finally:
            store.close()

    def test_google_fetch_items_asks_for_bodies_only_with_consent_and_grant(self) -> None:
        from app.services import exhaust_ingest as ex
        from app.services.connectors.google import google
        seen = {}

        def fake_headers(**kw):
            seen.update(kw)
            return []

        with patch.object(ex, "fetch_gmail_headers", fake_headers), \
             patch.object(ex, "fetch_calendar_events", return_value=[]):
            google.fetch_items(cursor={}, now=NOW)
            self.assertFalse(seen["include_body"])
            self.cc.record_connector_bodies("google", enabled=True, sentence="x")
            google.fetch_items(cursor={}, now=NOW)
            self.assertFalse(seen["include_body"])       # consent but no grant yet
            with patch.object(ex, "body_scope_granted", return_value=True):
                google.fetch_items(cursor={}, now=NOW)
            self.assertTrue(seen["include_body"])

    def test_outlook_scope_and_body_follow_the_same_rule(self) -> None:
        from app.services.connectors import outlook as ol
        with self.assertRaises(PermissionError):
            ol.assert_metadata_scopes("Mail.Read Calendars.Read")
        self.cc.record_connector_bodies("outlook", enabled=True, sentence="x")
        ol.assert_metadata_scopes("Mail.Read Calendars.Read")
        ol.assert_metadata_scopes("Mail.ReadBasic Calendars.Read")
        self.assertEqual(ol.requested_scopes(), ("Mail.Read", "Calendars.Read"))
        page = {"value": [{"id": "A", "conversationId": "c1",
                           "internetMessageId": "<m1@contoso.com>",
                           "receivedDateTime": "2025-09-21T15:04:05Z",
                           "from": {"emailAddress": {"name": "Priya",
                                                     "address": "priya@contoso.com"}},
                           "subject": "Q4", "isDraft": False,
                           "body": {"contentType": "html",
                                    "content": "<p>Contract <i>attached</i></p>"}}]}
        urls = []

        def fake_graph(url):
            urls.append(url)
            return page

        with patch.object(ol, "_graph_get", fake_graph):
            rows = ol.fetch_mail_headers(days=1, now=NOW, include_subject=True,
                                         include_body=True)
        self.assertEqual(rows[0]["body"], "Contract attached")
        self.assertIn("body", urls[0])


# --- F28: scanned PDFs ----------------------------------------------------------
def _image_only_pdf() -> bytes:
    """A one-page PDF whose content stream draws nothing: no text objects."""
    objs = [b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>",
            b"<</Length 0>>stream\n\nendstream"]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj" % i + body + b"endobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer<</Root 1 0 R/Size %d>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return out


class _FakeOcr:
    def __init__(self, text: str, available: bool = True) -> None:
        self.text, self._ok = text, available

    def available(self) -> bool:
        return self._ok

    def recognize(self, rgb):
        from app.perception.ocr import OcrLineResult, OcrResult
        return OcrResult(lines=[OcrLineResult(text=ln) for ln in self.text.splitlines()],
                         engine="fake")


class ScannedPdfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_scan_"))
        self.pdf = self.tmp / "scan.pdf"
        self.pdf.write_bytes(_image_only_pdf())

    def test_image_only_pdf_yields_text_through_the_ocr_engine(self) -> None:
        import numpy as np
        from app.services import documents as D
        page = np.zeros((20, 20, 3), dtype=np.uint8)
        with patch.object(D, "_rasterize_pdf_pages", return_value=[page]), \
             patch("app.perception.ocr.get_default_engine",
                   return_value=_FakeOcr("Invoice 42\ndue Friday")):
            self.assertEqual(D.extract_text(self.pdf), "Invoice 42\ndue Friday")

    def test_no_engine_and_no_vlm_means_empty_not_invented(self) -> None:
        import numpy as np
        from app.services import documents as D
        page = np.zeros((20, 20, 3), dtype=np.uint8)
        with patch.object(D, "_rasterize_pdf_pages", return_value=[page]), \
             patch("app.perception.ocr.get_default_engine",
                   return_value=_FakeOcr("", available=False)), \
             patch.dict(os.environ, {"QUILL_PDF_OCR_VLM": "0"}):
            self.assertEqual(D.extract_text(self.pdf), "")

    def test_text_pdfs_never_hit_ocr(self) -> None:
        from app.services import documents as D
        from tests.test_documents import _make_pdf
        fp = self.tmp / "text.pdf"
        fp.write_bytes(_make_pdf("Invoice for Acme due Friday and more words here"))
        with patch.object(D, "_ocr_pdf", side_effect=AssertionError("ocr called")):
            self.assertIn("Invoice for Acme", D.extract_text(fp))

    def test_default_engine_falls_back_to_tesseract_then_null(self) -> None:
        from app.perception import ocr
        eng = ocr.get_default_engine()
        self.assertIn(type(eng).__name__, ("WindowsMediaOcr", "TesseractOcr", "NullOcr"))
        if type(eng).__name__ == "NullOcr":
            self.assertFalse(ocr.TesseractOcr().available())


if __name__ == "__main__":
    unittest.main()


class TextLaneBatchingTests(_Base):
    def test_batch_cap_reports_remaining_and_fresh_falls_through_to_backlog(self) -> None:
        from app.services import extractor as X
        for i in range(3):
            self.store.insert(Event(time=NOW - 7200 - i, modality=Modality.SYSTEM,
                                    raw=f"Call Dana about budget item {i} tomorrow",
                                    source="phone.note", meta={"kind": "note"}))
        # A fresh row another lane owns must not hide the backlog.
        self.store.insert(Event(time=NOW - 1, modality=Modality.TEXT,
                                raw="I told Marc we ship Friday", source="chat.user"))
        ex = X.Extractor(self.store)
        with patch.object(X, "TEXT_LANE_BATCH", 2), \
             patch.object(ex, "_extract_text", return_value=EMPTY), \
             patch("time.time", return_value=NOW):
            first = ex.run_text_once()
            second = ex.run_text_once()
        self.assertEqual((first["lane"], first["events"], first["remaining"]),
                         ("backlog", 2, True))
        self.assertEqual((second["events"], second["remaining"]), (1, False))


class ChatDedupScopeTests(_Base):
    """F06 review: the fingerprint ignores case, whitespace and trailing
    punctuation; it is keyed on source and time-windowed; and a short,
    generic statement is never collapsed into an earlier one."""

    def _ingest(self, text, *, t):
        from app.services import chat_ingest
        import app.services.worker as worker_mod
        import app.storage as storage_mod
        fake_worker = SimpleNamespace(enqueue=lambda k, *a, **kw: 1)
        with patch.object(storage_mod, "get_store", return_value=self.store), \
             patch.object(worker_mod, "worker", fake_worker), \
             patch("app.services.attachments._index_event", lambda *a: None), \
             patch("time.time", return_value=t):
            return chat_ingest.ingest(text)

    def test_punctuation_and_case_do_not_make_a_second_memory(self) -> None:
        a = self._ingest("I told Marc we ship the pilot next week.", t=NOW)
        b = self._ingest("i told marc we ship the pilot next week", t=NOW + 60)
        self.assertEqual(a, b)

    def test_short_generic_statements_never_collapse(self) -> None:
        a = self._ingest("Sounds good, ship it.", t=NOW)
        b = self._ingest("Sounds good, ship it.", t=NOW + 60)
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.store.all_with_ids()), 2)

    def test_the_window_closes(self) -> None:
        a = self._ingest("I told Marc we ship the pilot next week", t=NOW)
        b = self._ingest("I told Marc we ship the pilot next week", t=NOW + 2 * 86400)
        self.assertNotEqual(a, b)

    def test_a_question_and_a_statement_with_the_same_words_are_distinct(self) -> None:
        a = self._ingest("Marc said we ship the pilot next week", t=NOW)
        b = self._ingest("Did Marc say we ship the pilot next week?", t=NOW + 1)
        self.assertNotEqual(a, b)   # different source: chat.user vs chat.query


class OneDoorInvariantTests(_Base):
    """F03 review: the text lane is scheduling, not a second persist path.
    Speech, the text lane and persist_facts must all reach the same
    Extractor._persist and hand the gate the same arguments."""

    def test_every_lane_reaches_the_same_persist_with_the_same_gate_args(self) -> None:
        from app.services import extractor as X
        from app.services.consolidation import Turn
        persist_calls: list = []
        gate_calls: list = []
        real_persist, real_gate = X.Extractor._persist, X.Extractor._gate

        def spy_persist(self_, turn, facts, now, **kw):
            persist_calls.append((real_persist, kw))
            return real_persist(self_, turn, facts, now, **kw)

        def spy_gate(self_, kind, item, turn, **kw):
            gate_calls.append(((kind, type(item), type(turn)), kw))
            return real_gate(self_, kind, item, turn, **kw)

        facts = {**EMPTY, "tasks": [{"text": "book the venue", "confidence": 0.9,
                                     "source_span": "book the venue"}]}
        ex = X.Extractor(self.store)
        audio_eid = self.store.insert(Event(time=NOW - 30, modality=Modality.AUDIO,
                                            raw="we need to book the venue",
                                            source="audio.whisper"))
        note_eid = self.store.insert(Event(time=NOW - 20, modality=Modality.SYSTEM,
                                           raw="Remember to book the venue today",
                                           source="phone.note", meta={"kind": "note"}))
        turn = Turn(start=NOW - 30, end=NOW - 29, speaker="Test Person",
                    text="we need to book the venue", event_ids=[audio_eid],
                    n_utterances=1)
        with patch.object(X.Extractor, "_persist", spy_persist), \
             patch.object(X.Extractor, "_gate", spy_gate), \
             patch.object(ex, "_pending_turns", return_value=([turn], None)), \
             patch.object(ex, "_extract_text", return_value=facts), \
             patch("time.time", return_value=NOW):
            speech = ex.run_once()
            lane = ex.run_text_once()
            X.persist_facts(self.store, facts, None, "please book the venue",
                            NOW, event_source="chat.attach")
        self.assertEqual(speech["facts"], 1, speech)
        self.assertEqual(lane["facts"], 1, lane)
        # Three callers, one door.
        self.assertEqual(len(persist_calls), 3)
        self.assertTrue(all(fn is real_persist for fn, _ in persist_calls))
        # The lanes differ only in the data they pass, never in the gate.
        self.assertTrue(all(kw.get("index", True) is True for _, kw in persist_calls))
        self.assertEqual(len(gate_calls), 3)
        self.assertEqual({args for args, _ in gate_calls},
                         {("task", dict, type(turn))})
        self.assertEqual([kw for _, kw in gate_calls],
                         [{"index": True}] * 3)
        self.assertIsNotNone(self.store.get_event(note_eid)["extracted_at"])

    def test_no_producer_module_writes_facts_or_defines_a_persist(self) -> None:
        """The structural guard: the modules that feed the pipeline may
        store events and enqueue work, never write fact rows, call the gate,
        or grow their own persist. That is how documents._persist_facts
        happened once."""
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        producers = [
            "app/services/documents.py", "app/services/chat_ingest.py",
            "app/services/attachments.py", "app/services/research_ingest.py",
            "app/services/screen_extract.py", "app/perception/l3_workers.py",
            "app/services/connectors/scheduler.py", "app/services/phone_channel.py",
            "app/services/ingest_salience.py",
        ]
        forbidden = re.compile(
            r"\.add_(task|commitment|claim|question|idea)\(|\bgate_fact\(|"
            r"def _persist\w*\(|add_fact_candidate\(|set_fact_candidate_status\(")
        for rel in producers:
            src = (root / rel).read_text(encoding="utf-8")
            code = "\n".join(ln for ln in src.splitlines()
                             if not ln.lstrip().startswith("#"))
            hit = forbidden.search(code)
            self.assertIsNone(hit, f"{rel} grew a fact-writing path: {hit and hit.group(0)}")
            self.assertIn("persist_facts", src) if rel in (
                "app/services/documents.py", "app/services/chat_ingest.py",
                "app/services/research_ingest.py", "app/services/screen_extract.py",
                "app/perception/l3_workers.py") else None


class HeldPersonCandidateTests(_Base):
    """F59: a document may mint a person, but that person waits in review
    (promotion_state 'candidate') and the document never promotes them;
    the same name in typed chat still earns promotion."""

    def _commit(self, src: str, text: str) -> None:
        from app.services.extractor import persist_facts
        facts = {**EMPTY, "commitments": [{
            "text": "send Dana Whitfield the signed contract", "from_person": "me",
            "to_person": "Dana Whitfield", "confidence": 0.9,
            "source_span": "send Dana Whitfield the signed contract"}]}
        persist_facts(self.store, facts, None, text, NOW,
                      event_source=src, speaker="Test Person")

    def _state(self) -> str | None:
        for p in self.store.all_people():
            if "whitfield" in (p.get("name") or "").lower():
                return p.get("promotion_state") or "candidate"
        return None

    def test_policy_table_holds_document_people(self) -> None:
        from app.services import source_policy as sp
        sp._raw_policies.cache_clear()
        for cls in ("user_authored_document", "shared_document"):
            p = sp.policy_for(cls)
            self.assertFalse(p.create_person_candidates, cls)
            self.assertTrue(p.hold_person_candidates, cls)
            self.assertTrue(p.may_mint_people, cls)
        self.assertFalse(sp.policy_for("news_page").may_mint_people)
        self.assertEqual(sp.classify_source(event_source="chat.attach"),
                         "user_authored_document")

    def test_document_mints_a_candidate_and_never_promotes_it(self) -> None:
        text = "Contract: send Dana Whitfield the signed contract by Monday."
        self._commit("chat.attach", text)
        self.assertEqual(self._state(), "candidate")
        for _ in range(3):      # later chunks / re-reads of the same document
            self._commit("chat.attach", text)
        self.assertEqual(self._state(), "candidate")

    def test_typed_chat_still_promotes(self) -> None:
        # Chat is a conversation class: the party resolve plus the chat name
        # harvest count as two strong mentions, so one typed turn already
        # earns `recognized`. The contrast with the document path is the
        # point: same name, same number of mentions, no promotion there.
        text = "I'll send Dana Whitfield the signed contract tomorrow"
        self._commit("chat.user", text)
        self.assertEqual(self._state(), "recognized")
