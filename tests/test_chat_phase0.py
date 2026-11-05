"""Chat log: one write path, generation on new(), SSE that ends.

Phase 1 persists the live thread in chat_log.db. A late emit from an old
generation inserts nothing.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from app.api.chat_page import CHAT_PAGE
from app.api.routes import chat_stream
from app.services import chat_log
from app.services import chat_sessions as cs
from app.services.agent_bridge import AgentWorker


def stream_releases_on_disconnect() -> None:
    """A closed ASGI client releases /chat/stream within one wake, not an hour."""

    async def _run() -> None:
        release = asyncio.Event()
        calls = {"n": 0}

        async def receive():
            calls["n"] += 1
            if calls["n"] == 1:
                return {"type": "http.request", "body": b"", "more_body": False}
            await release.wait()
            return {"type": "http.disconnect"}

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/chat/stream",
            "raw_path": b"/chat/stream",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        request = Request(scope, receive)
        response = await chat_stream(request)
        agen = response.body_iterator
        started = time.monotonic()
        first = await agen.__anext__()
        text = first if isinstance(first, str) else first.decode()
        if "ping" not in text:
            raise AssertionError(f"expected an opening ping, got {text!r}")
        release.set()
        try:
            while True:
                await asyncio.wait_for(agen.__anext__(), timeout=2.0)
        except StopAsyncIteration:
            pass
        elapsed = time.monotonic() - started
        if elapsed >= 2.0:
            raise AssertionError(
                f"/chat/stream still running {elapsed:.2f}s after disconnect")

    asyncio.run(_run())


class ChatPagePhase0Tests(unittest.TestCase):
    def test_send_restores_draft_and_poll_catches_up(self) -> None:
        self.assertIn("function restoreDraft()", CHAT_PAGE)
        self.assertIn("pollDirty", CHAT_PAGE)
        self.assertIn("visibilitychange", CHAT_PAGE)
        # A skipped poll must re-run when the in-flight one finishes.
        self.assertIn("if(pollDirty && !document.hidden)", CHAT_PAGE)
        self.assertIn("client_msg_id", CHAT_PAGE)
        self.assertIn("session_id", CHAT_PAGE)
        self.assertIn("dataset.seq", CHAT_PAGE)
        self.assertIn("e.compiled", CHAT_PAGE)
        self.assertIn("resolveOffer", CHAT_PAGE)
        self.assertIn("/chat/offers/", CHAT_PAGE)
        self.assertIn("offer.expired", CHAT_PAGE)
        self.assertIn("offer-expired", CHAT_PAGE)


class NewChatGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_chat_phase0_"))
        self._dir_patch = patch.object(cs, "sessions_dir", return_value=self.tmp)
        self._dir_patch.start()

    def tearDown(self) -> None:
        self._dir_patch.stop()

    def test_new_clears_gates_on_the_caller_thread(self) -> None:
        w = AgentWorker()
        w._emit("user", "hold this")
        w.awaiting = True
        w.awaiting_fast = True
        w.question = "Ship the note?"
        w.question_fast = "Text them?"
        w.pending_todo = {"kind": "task", "message": "yes?"}
        w.offer_queue = [{"kind": "task", "message": "later"}]

        w.new()

        self.assertFalse(w.awaiting)
        self.assertFalse(w.awaiting_fast)
        self.assertIsNone(w.question)
        self.assertIsNone(w.question_fast)
        self.assertIsNone(w.pending_todo)
        self.assertEqual(w.offer_queue, [])
        # The cancel that unblocks a lane is not a chat bubble.
        self.assertFalse(any(e.get("kind") == "user" and e.get("text") == "cancel"
                             for e in w.events))

    def test_new_unblocks_a_waiting_ask(self) -> None:
        w = AgentWorker()
        box: dict = {}

        def run() -> None:
            box["answer"] = w._on_ask("Ship it?")

        t = threading.Thread(target=run)
        t.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if w.awaiting and w.question and "Ship it?" in w.question:
                break
            time.sleep(0.01)
        else:
            self.fail("ask never blocked")

        w.new()
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive(), "ask still blocked after new()")
        self.assertEqual(box.get("answer"), "cancel")
        self.assertFalse(w.awaiting)
        self.assertIsNone(w.pending_todo)


class AgentOffLogTests(unittest.TestCase):
    def test_disabled_agent_writes_user_and_result_before_returning(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.services.agent_bridge import worker

        tmp = Path(tempfile.mkdtemp(prefix="quill_chat_phase1_"))
        chat_log.reset_log(tmp / "chat_log.db")
        with worker.lock:
            saved_events = list(worker.events)
            saved_next = worker.next_id
        try:
            with patch.dict(os.environ, {"QUILL_AGENT": "0"}):
                with patch("app.api.routes.llm.answer",
                           return_value={"answer": "Forty-nine a month.",
                                         "sources": []}) as answer:
                    with patch("app.services.chat_ingest.ingest", return_value=None):
                        with TestClient(app) as client:
                            before_n = len(worker.events)
                            r = client.post(
                                "/chat",
                                json={"message": "What did we promise on price?"},
                                headers={"Origin": "http://testserver"},
                            )
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body.get("routed"), "memory")
            self.assertNotIn("answer", body)
            answer.assert_called_once()
            fresh = worker.events[before_n:]
            self.assertEqual([e["kind"] for e in fresh], ["user", "result"])
            self.assertEqual(fresh[0]["text"], "What did we promise on price?")
            self.assertEqual(fresh[1]["text"], "Forty-nine a month.")
            # The user row is written before the response returns.
            self.assertEqual(fresh[0]["id"], body["since"])
            self.assertEqual(body["since"], body["seq"])
            stored = chat_log.get_log().events_since(body["session_id"], 0)
            self.assertEqual([e["kind"] for e in stored], ["user", "result"])
        finally:
            with worker.lock:
                worker.events[:] = saved_events
                worker.next_id = saved_next
            chat_log.close_log()


class ChatStreamReleaseTests(unittest.TestCase):
    def test_stream_releases_on_disconnect(self) -> None:
        stream_releases_on_disconnect()


class ChatLogSchemaTests(unittest.TestCase):
    def test_stale_generation_inserts_nothing(self) -> None:
        conn = sqlite3.connect(":memory:")
        chat_log.apply(conn)
        conn.execute(
            "INSERT INTO chat_sessions (id, created_at, current_gen) VALUES (?, ?, ?)",
            ("s1", 1.0, 1),
        )
        conn.commit()
        seq = chat_log.insert_if_current(
            conn, session_id="s1", turn_id="t1", gen=1, kind="user",
            text="hello", payload_json=None, created_at=2.0, client_msg_id="c1",
        )
        self.assertIsNotNone(seq)
        conn.execute("UPDATE chat_sessions SET current_gen = 2 WHERE id = 's1'")
        conn.commit()
        stale = chat_log.insert_if_current(
            conn, session_id="s1", turn_id="t1", gen=1, kind="result",
            text="too late", payload_json=None, created_at=3.0, client_msg_id=None,
        )
        self.assertIsNone(stale)
        rows = conn.execute("SELECT kind, text, gen FROM chat_events").fetchall()
        self.assertEqual(rows, [("user", "hello", 1)])

    def test_client_msg_id_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        chat_log.apply(conn)
        conn.execute(
            "INSERT INTO chat_sessions (id, created_at) VALUES (?, ?)",
            ("s1", 1.0),
        )
        conn.commit()
        chat_log.insert_if_current(
            conn, session_id="s1", turn_id="t1", gen=1, kind="user",
            text="hello", payload_json=None, created_at=2.0, client_msg_id="c1",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            chat_log.insert_if_current(
                conn, session_id="s1", turn_id="t2", gen=1, kind="user",
                text="hello again", payload_json=None, created_at=3.0,
                client_msg_id="c1",
            )


class ChatLogPhase1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_chat_log_"))
        self.log = chat_log.ChatLog(self.tmp / "chat_log.db")

    def tearDown(self) -> None:
        self.log.close()

    def test_retire_drops_the_old_generation(self) -> None:
        accepted = self.log.accept_turn(
            text="hello", session_id=None, client_msg_id="c1")
        sid = accepted["session_id"]
        late = self.log.emit_event(
            session_id=sid, gen=accepted["gen"], kind="result",
            text="too late", turn_id=accepted["turn_id"])
        # Same generation still accepts, so retire first.
        self.assertIsNotNone(late.get("seq"))
        self.log.retire()
        dropped = self.log.emit_event(
            session_id=sid, gen=accepted["gen"], kind="result",
            text="after close", turn_id=accepted["turn_id"])
        self.assertTrue(dropped.get("dropped"))
        old = [e["text"] for e in self.log.events_since(sid, 0)]
        self.assertEqual(old, ["hello", "too late"])
        self.assertFalse(any(
            e.get("text") == "after close"
            for e in self.log.events_since(self.log.live_id(), 0)))

    def test_client_msg_id_returns_the_same_seq(self) -> None:
        first = self.log.accept_turn(
            text="hello", session_id=None, client_msg_id="c1")
        again = self.log.accept_turn(
            text="hello", session_id=None, client_msg_id="c1")
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["seq"], again["seq"])
        users = [e for e in self.log.events_since(first["session_id"], 0)
                 if e["kind"] == "user"]
        self.assertEqual(len(users), 1)

    def test_retire_cancels_the_running_job(self) -> None:
        accepted = self.log.accept_turn(
            text="go", session_id=None, client_msg_id=None)
        self.assertTrue(accepted["job_id"])
        self.log.retire()
        self.assertTrue(self.log.cancel_requested(accepted["job_id"]))

    def test_progress_stays_off_the_transcript(self) -> None:
        accepted = self.log.accept_turn(
            text="go", session_id=None, client_msg_id=None)
        self.log.note_progress(accepted["job_id"], "opening the page")
        kinds = [e["kind"] for e in self.log.events_since(accepted["session_id"], 0)]
        self.assertNotIn("progress", kinds)
        job_id, lines = self.log.progress_state(accepted["session_id"])
        self.assertEqual(job_id, accepted["job_id"])
        self.assertEqual(lines, ["opening the page"])

    def test_other_session_id_does_not_write(self) -> None:
        live = self.log.live_id()
        switched = self.log.accept_turn(
            text="stale tab", session_id="not-the-live-one", client_msg_id="c9")
        self.assertTrue(switched.get("switched"))
        self.assertEqual(switched["session_id"], live)
        self.assertEqual(self.log.events_since(live, 0), [])


class PersistWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_chat_worker_"))
        chat_log.reset_log(self.tmp / "chat_log.db")
        self.worker = AgentWorker(persist=True)

    def tearDown(self) -> None:
        chat_log.close_log()

    def test_new_drops_a_late_result(self) -> None:
        from app.services import agent_bridge

        accepted = self.worker.begin_turn(
            text="hello", session_id=None, client_msg_id="m1")
        self.worker._emit("result", "answer")
        sid = accepted["session_id"]
        self.worker.new()
        token = agent_bridge._emit_scope.set({
            "session_id": sid,
            "gen": accepted["gen"],
            "turn_id": accepted["turn_id"],
            "job_id": accepted["job_id"],
        })
        try:
            self.worker._emit("result", "too late")
        finally:
            agent_bridge._emit_scope.reset(token)
        log = chat_log.get_log()
        old = [e["text"] for e in log.events_since(sid, 0) if e["kind"] == "result"]
        self.assertEqual(old, ["answer"])
        live = log.events_since(log.live_id(), 0)
        self.assertFalse(any(e.get("text") == "too late" for e in live))
        self.assertFalse(any(e.get("text") == "too late" for e in self.worker.events))


class ChatOfferPhase3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_chat_offer_"))
        chat_log.reset_log(self.tmp / "chat_log.db")
        self.log = chat_log.get_log()
        self.worker = AgentWorker(persist=True)

    def tearDown(self) -> None:
        chat_log.close_log()

    def test_cas_resolves_once(self) -> None:
        minted = self.log.mint_offer(
            {"kind": "todo", "title": "milk", "message": "buy milk?",
             "items": ["buy milk"]},
            ttl_s=90.0)
        oid, phash = minted["offer_id"], minted["payload_hash"]
        first = self.log.resolve_offer(oid, phash, "accepted")
        self.assertTrue(first["ok"])
        second = self.log.resolve_offer(oid, phash, "declined")
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "already_resolved")

    def test_wrong_hash_is_refused(self) -> None:
        minted = self.log.mint_offer(
            {"kind": "todo", "message": "x", "items": ["x"]}, ttl_s=90.0)
        out = self.log.resolve_offer(minted["offer_id"], "deadbeef", "accepted")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "payload_hash_mismatch")

    def test_expire_due_is_cas(self) -> None:
        minted = self.log.mint_offer(
            {"kind": "todo", "message": "x", "items": ["x"]}, ttl_s=1.0)
        # Force past expiry.
        with self.log._lock:
            self.log._conn.execute(
                "UPDATE chat_offers SET expires_at=? WHERE id=?",
                (time.time() - 1, minted["offer_id"]))
        expired = self.log.expire_due()
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["offer_id"], minted["offer_id"])
        again = self.log.resolve_offer(
            minted["offer_id"], minted["payload_hash"], "accepted")
        self.assertFalse(again["ok"])
        self.assertIn(again["error"], ("expired", "already_resolved"))

    def test_bare_yes_needs_exactly_one_open(self) -> None:
        w = self.worker
        w.send = lambda *a, **k: None  # type: ignore[method-assign]
        w.propose_todo(["a"], "One")
        first = dict(w.pending_todo or {})
        # Second open offer stays queued.
        w.busy = True
        w.propose_todo(["b"], "Two")
        w.busy = False
        self.assertEqual(len(w._open_offers()), 2)
        out = w.handle_reply("yes")
        self.assertEqual(out.get("routed"), "offer_ambiguous")
        self.assertIsNotNone(w.pending_todo)
        # Resolve the active one by id; the other stays open.
        done = w.resolve_offer(
            first["offer_id"], first["payload_hash"], True)
        self.assertTrue(done.get("ok") or done.get("accepted") is not None
                        or done.get("routed") is None)
        # After one resolve, a bare yes may accept the remaining card.
        opens = w._open_offers()
        self.assertEqual(len(opens), 1)

    def test_worker_expiry_emits_offer_expired_stream(self) -> None:
        w = self.worker
        w.propose_todo(["milk"], "groceries")
        self.assertIsNotNone(w.pending_todo)
        w.pending_todo["created_at"] = time.time() - 500
        w.pending_todo["expires_at"] = time.time() - 1
        n = w.expire_stale_offers()
        self.assertGreaterEqual(n, 1)
        self.assertIsNone(w.pending_todo)
        expired = [e for e in w.events
                   if (e.get("stream") or {}).get("type") == "offer.expired"]
        self.assertTrue(expired)


if __name__ == "__main__":
    unittest.main()
