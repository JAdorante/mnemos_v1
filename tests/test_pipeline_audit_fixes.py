"""Pipeline audit (2026-10-06), top three fixes.

1. One egress rule reads the stored privacy_class: MCP, peer answers, the org
   digest and the fleet share path hold back sensitive / never-send content,
   and classification fails closed.
2. The event bus isolates subscribers: one raising subscriber no longer drops
   the event for every subscriber after it.
3. One delete-and-expire path: erase / purge / compaction reach the KG v2
   evidence quotes, the Lance index and the timeline mirror; v1 edge deletes
   retract the v2 belief; expired signals stop surfacing in search.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, EventBus, Modality
from app.services import privacy_class as pc
from app.storage import Store, add_delete_hook, remove_delete_hook

NOW = 1_756_000_000.0
SENSITIVE_TEXT = "Refill the prescription before the patient intake on Friday"
PLAIN_TEXT = "Ship the quarterly deck on Friday"


class _StoreBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_audit_"))
        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": str(self.tmp),
                                           "QUILL_USAGE_LEDGER": "0"},
                              clear=False)
        self.env.start()
        self.store = Store(db_path=self.tmp / "quill.db",
                           audio_dir=self.tmp / "audio")

    def tearDown(self) -> None:
        self.store.close()
        self.env.stop()

    def add_event(self, raw: str, *, ts: float = NOW, summary: str = "",
                  source: str = "test", meta: dict | None = None) -> int:
        return self.store.insert(Event(time=ts, modality=Modality.TEXT, raw=raw,
                                       summary=summary, source=source,
                                       meta=dict(meta or {})))


# --- 1. egress ----------------------------------------------------------------
class EgressRuleTests(_StoreBase):
    def test_text_rule_holds_back_sensitive_and_lets_work_through(self) -> None:
        self.assertEqual(pc.egress_refusal(SENSITIVE_TEXT),
                         "privacy_class=sensitive")
        self.assertIsNone(pc.egress_refusal(PLAIN_TEXT))
        self.assertIsNone(pc.egress_refusal("email ada@example.com"))  # personal

    def test_text_rule_fails_closed(self) -> None:
        with patch.object(pc, "classify_text", side_effect=RuntimeError("boom")):
            self.assertIn("unavailable", pc.egress_refusal(PLAIN_TEXT))

    def test_stored_stamp_is_read(self) -> None:
        bad = self.add_event(SENSITIVE_TEXT)
        ok = self.add_event(PLAIN_TEXT, ts=NOW + 1)
        self.assertEqual(pc.event_egress_refusal(self.store, bad),
                         "privacy_class=sensitive")
        self.assertIsNone(pc.event_egress_refusal(self.store, ok))
        self.assertIsNone(pc.event_egress_refusal(self.store, 999_999))

    def test_insert_stamp_fails_closed(self) -> None:
        with patch("app.services.privacy_class.stamp_event",
                   side_effect=RuntimeError("classifier down")):
            eid = self.add_event(PLAIN_TEXT)
        self.assertEqual(pc.event_privacy_class(self.store, eid), "sensitive")


class McpEgressTests(_StoreBase):
    def setUp(self) -> None:
        super().setUp()
        from app.services import memory as mem
        self.mem = mem.memory
        self.pins = [patch.object(self.mem, "_store", self.store),
                     patch.object(self.mem, "_vectors", None),
                     patch.object(self.mem, "_semantic", False)]
        for p in self.pins:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.pins):
            p.stop()
        super().tearDown()

    def test_provenance_withholds_a_sensitive_event(self) -> None:
        from app.services import mcp_tools
        bad = self.add_event(SENSITIVE_TEXT)
        ok = self.add_event(PLAIN_TEXT, ts=NOW + 1)
        out = mcp_tools.call_tool("provenance", {"event_id": bad})
        self.assertFalse(out["ok"])
        self.assertIn("privacy_class=sensitive", out["error"])
        self.assertTrue(mcp_tools.call_tool("provenance", {"event_id": ok})["ok"])

    def test_memory_search_drops_sensitive_hits_and_carries_ids(self) -> None:
        from app.services import mcp_tools
        self.add_event(SENSITIVE_TEXT)
        ok = self.add_event(PLAIN_TEXT, ts=NOW + 1)
        out = mcp_tools.call_tool("memory_search", {"query": "Friday"})
        self.assertTrue(out["ok"])
        texts = [r["text"] for r in out["results"]]
        self.assertEqual(texts, [PLAIN_TEXT])
        self.assertEqual(out["results"][0]["event_id"], ok)

    def test_fact_citing_a_sensitive_event_is_withheld(self) -> None:
        from app.services import mcp_tools
        bad = self.add_event(SENSITIVE_TEXT)
        rows = [{"text": "Follow up on Friday", "kind": "task",
                 "source_event_id": bad}]
        self.assertEqual(mcp_tools.redact_result(rows), [])


class PeerEgressTests(unittest.TestCase):
    def row(self, cls: str | None) -> dict:
        return {"kind": "claim", "state": "active", "text":
                "Atlas migration cutover moved to October 16",
                "source_event_id": 7, "source_time": NOW,
                "event_source": "audio.whisper", "source_privacy_class": cls}

    def test_claim_from_a_sensitive_event_never_crosses(self) -> None:
        from app.services import peer_retrieval
        self.assertTrue(peer_retrieval._eligible(self.row("internal")))
        self.assertTrue(peer_retrieval._eligible(self.row(None)))
        self.assertFalse(peer_retrieval._eligible(self.row("sensitive")))
        self.assertFalse(peer_retrieval._eligible(self.row("never-send")))

    def test_fact_rows_carry_the_source_class(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = Store(db_path=Path(td) / "q.db", audio_dir=Path(td) / "a")
            try:
                eid = store.insert(Event(time=NOW, modality=Modality.AUDIO,
                                         raw=SENSITIVE_TEXT, source="t"))
                fid = store.add_claim("Atlas cutover moves", source_event_id=eid,
                                      source_span="Atlas cutover moves",
                                      confidence=0.9, extracted_at=NOW)
                row = store.facts_by_ids([fid])[fid]
                self.assertEqual(row["source_privacy_class"], "sensitive")
            finally:
                store.close()


class DigestEgressTests(unittest.TestCase):
    def test_sensitive_lines_never_reach_the_digest(self) -> None:
        from app.services import org_digest
        self.assertFalse(org_digest._shareable_fact(
            {"text": PLAIN_TEXT, "source_privacy_class": "sensitive"}))
        self.assertFalse(org_digest._shareable_fact({"text": SENSITIVE_TEXT}))
        self.assertTrue(org_digest._shareable_fact({"text": PLAIN_TEXT}))
        out = org_digest._scrub_digest({
            "summary": "Wire transfer to the brokerage account is late",
            "progress": [PLAIN_TEXT, SENSITIVE_TEXT], "blockers": [],
            "asks": [], "deps": []})
        self.assertEqual(out["progress"], [PLAIN_TEXT])
        self.assertTrue(out["summary"].startswith("(summary withheld"))


class FleetEgressTests(unittest.TestCase):
    def signal(self, **over) -> dict:
        sig = {"topic": "eng.status", "kind": "note", "subject": "Atlas",
               "summary": "Cutover slipped a week.", "body": {}}
        sig.update(over)
        return sig

    def test_content_check_refuses_sensitive_words(self) -> None:
        from app.services.fleet import router
        self.assertIsNone(router.content_refusal(self.signal()))
        self.assertEqual(
            router.content_refusal(self.signal(
                body={"note": "routing number on the wire transfer"})),
            "content_privacy_class=sensitive")
        self.assertEqual(
            router.content_refusal(self.signal(
                summary="key sk-ant-api03-" + "x" * 40)),
            "content_privacy_class=never-send")

    def test_egress_check_runs_the_content_check(self) -> None:
        from app.services.fleet import envelope as env
        from app.services.fleet import kinds, router
        with patch.object(env, "validate", return_value=None), \
                patch.object(kinds, "registry", return_value={}), \
                patch.object(kinds, "blocklist", return_value=env.Blocklist()):
            self.assertIsNone(router.egress_check(self.signal()))
            self.assertEqual(router.egress_check(self.signal(
                summary=SENSITIVE_TEXT)), "content_privacy_class=sensitive")


class CloudGateFallbackTests(unittest.TestCase):
    def test_refuses_when_redaction_is_unavailable(self) -> None:
        from app.services import model_router
        with patch.dict(sys.modules, {"app.services.redact": None}):
            with self.assertRaises(RuntimeError):
                model_router._redact_or_refuse("sys", [{"role": "user",
                                                        "content": "hi"}])


# --- 2. bus isolation -----------------------------------------------------------
class BusIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.seen: list[str] = []

        def boom(_ev):
            raise RuntimeError("sqlite is locked")

        async def later(ev):
            self.seen.append(ev.raw)

        self.bus.subscribe(boom)
        self.bus.subscribe(lambda ev: self.seen.append("sync:" + ev.raw))
        self.bus.subscribe(later)
        self.ev = Event(time=NOW, modality=Modality.SYSTEM, raw="x",
                        source="fleet.signal")

    def test_async_publish_reaches_every_later_subscriber(self) -> None:
        asyncio.run(self.bus.publish(self.ev))
        self.assertEqual(self.seen, ["sync:x", "x"])

    def test_nowait_without_a_loop_keeps_going(self) -> None:
        self.bus.publish_nowait(self.ev)
        self.assertEqual(self.seen, ["sync:x"])  # coroutines are closed here

    def test_nowait_from_a_thread_onto_the_loop(self) -> None:
        async def main():
            self.bus.bind_loop(asyncio.get_running_loop())
            await asyncio.to_thread(self.bus.publish_nowait, self.ev)
            for _ in range(50):
                if len(self.seen) == 2:
                    break
                await asyncio.sleep(0.01)
        asyncio.run(main())
        self.assertEqual(self.seen, ["sync:x", "x"])


# --- 3. delete and expire -----------------------------------------------------
class _FakeVectors:
    def __init__(self) -> None:
        self.deleted: list[int] = []
        self.added: list[tuple[int, str]] = []

    def delete_ids(self, ids):
        self.deleted.extend(int(i) for i in ids)
        return len(ids)

    def add(self, id, time_, modality, text, vector):
        self.added.append((int(id), text))

    def list_ids(self):
        return []


class DeletePathTests(_StoreBase):
    def setUp(self) -> None:
        super().setUp()
        from app.services.memory import MemoryEngine
        self.engine = MemoryEngine(store=self.store)
        self.vectors = _FakeVectors()
        self.engine._vectors = self.vectors
        self.engine._semantic = True
        self.engine._embed = lambda text: [0.0] * 8
        add_delete_hook(self.engine._on_store_change)

    def tearDown(self) -> None:
        remove_delete_hook(self.engine._on_store_change)
        super().tearDown()

    def belief(self, eid: int) -> tuple[int, int, int]:
        p = self.store.resolve_person("Ada", ts=NOW)
        e = self.store.resolve_entity("Acme", "org", ts=NOW)
        self.store.add_relation("person", p, "works_at", "entity", e,
                                origin="asserted", source_event_id=eid,
                                confidence=0.9, ts=NOW,
                                quote="Ada works at Acme")
        with self.store._lock:
            pid = self.store._conn.execute(
                "SELECT id FROM kg_predicates WHERE subj_id=? AND obj_id=? "
                "AND predicate='works_at'", (p, e)).fetchone()["id"]
        return p, e, int(pid)

    def kg(self, pid: int) -> tuple[str, int]:
        with self.store._lock:
            status = self.store._conn.execute(
                "SELECT status FROM kg_predicates WHERE id=?", (pid,)
            ).fetchone()["status"]
            n = self.store._conn.execute(
                "SELECT COUNT(*) FROM kg_evidence WHERE predicate_id=?", (pid,)
            ).fetchone()[0]
        return status, int(n)

    def test_erase_removes_the_evidence_quote_and_the_copies(self) -> None:
        eid = self.add_event("Ada said she works at Acme now")
        self.engine._events = [e for _, e in self.store.all_with_ids()]
        _, _, pid = self.belief(eid)
        self.assertEqual(self.kg(pid), ("active", 1))
        out = self.store.erase_event(eid)
        self.assertEqual(out["kg_evidence"], 1)
        self.assertEqual(self.kg(pid), ("unsupported", 0))
        self.assertIn(eid, self.vectors.deleted)
        self.assertEqual(self.engine._events, [])

    def test_purge_reaches_lance_and_the_timeline(self) -> None:
        keep = self.add_event("kept", source="chat")
        gone = self.add_event("scanned readme", source="documents.scan",
                              ts=NOW + 1)
        self.engine._events = [e for _, e in self.store.all_with_ids()]
        self.store.purge_source("documents.scan")
        self.assertEqual([e.raw for e in self.engine._events], ["kept"])
        self.assertIn(gone, self.vectors.deleted)
        self.assertNotIn(keep, self.vectors.deleted)

    def test_window_erasure_runs_the_hook(self) -> None:
        eid = self.add_event("desktop frame text", source="desktop.screen")
        self.store.erase_events_window(NOW - 1, NOW + 1, vacuum=False)
        self.assertIn(eid, self.vectors.deleted)

    def test_compaction_rewrites_the_mirror_and_the_raw_index_row(self) -> None:
        eid = self.add_event("the full original transcript")
        self.engine._events = [e for _, e in self.store.all_with_ids()]
        self.assertTrue(self.store.compact_event(eid, "[compacted]", NOW + 5))
        self.assertEqual(self.engine._events[0].raw, "[compacted]")
        self.assertEqual(self.vectors.added, [(eid, "[compacted]")])
        self.assertTrue(self.store.restore_event(eid))
        self.assertEqual(self.engine._events[0].raw,
                         "the full original transcript")

    def test_a_temp_store_never_touches_the_live_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            other = Store(db_path=Path(td) / "o.db", audio_dir=Path(td) / "a")
            try:
                eid = other.insert(Event(time=NOW, modality=Modality.TEXT,
                                         raw="x", source="t"))
                other.erase_event(eid)
            finally:
                other.close()
        self.assertEqual(self.vectors.deleted, [])

    def test_v1_edge_delete_retracts_the_v2_belief(self) -> None:
        eid = self.add_event("Ada works at Acme")
        p, e, pid = self.belief(eid)
        self.assertTrue(self.store.delete_relation("person", p, "works_at",
                                                   "entity", e))
        self.assertEqual(self.kg(pid)[0], "retracted")
        rows = self.store.list_kg_predicates(
            obj_type="entity", obj_id=e, statuses=("active", "superseded"))
        self.assertEqual(rows, [])

    def test_deleting_a_person_retracts_their_beliefs(self) -> None:
        eid = self.add_event("Ada works at Acme")
        p, _, pid = self.belief(eid)
        self.store.delete_person(p)
        self.assertEqual(self.kg(pid)[0], "retracted")


class CommitmentStateTests(_StoreBase):
    def test_done_task_reclassified_stays_done(self) -> None:
        fid = self.store.add_task("Send Ada the deck", extracted_at=NOW)
        self.store.set_fact_status(fid, "done")
        self.assertTrue(self.store.reclassify_fact_kind(fid, "commitment"))
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT status, state FROM commitments WHERE fact_id=?",
                (fid,)).fetchone()
        self.assertEqual((row["status"], row["state"]), ("done", "completed"))


class SignalExpiryTests(_StoreBase):
    def test_expired_signals_drop_out_of_search(self) -> None:
        from app.services.memory import MemoryEngine
        engine = MemoryEngine(store=self.store)
        engine._semantic = False
        now = time.time()
        for i, exp in enumerate((now - 60, now + 600)):
            self.store.insert(Event(
                time=now - 10 + i, modality=Modality.SYSTEM,
                raw=f"agent:pm (note) on Atlas: signal {i}",
                source="fleet.signal",
                meta={"signal": {"expires_at": exp}}))
        hits = engine.search("Atlas")
        self.assertEqual([h["raw"][-8:] for h in hits], ["signal 1"])
        engine._events = [e for _, e in self.store.all_with_ids()]
        self.assertEqual([h["raw"][-8:] for h in engine.search("")],
                         ["signal 1"])


if __name__ == "__main__":
    unittest.main()
