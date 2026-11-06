"""iMessage connector: reads a chat.db-shaped fixture read-only, lands texts
through the scheduler stamped sensitive, keeps out codes / reactions / business
senders, and stays unavailable off a Mac.

The fixture follows the Messages schema; no real chat.db was available on the
build machine, so the attributedBody blobs are hand-built to the typedstream
shape.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import connectors  # noqa: E402
from app.services.connectors import imessage as im  # noqa: E402
from app.services.connectors import imessage_reader as reader  # noqa: E402
from app.services.connectors import scheduler, session  # noqa: E402

NOW = 1_790_000_000.0


def _blob(text: str) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) < 0x80:
        size = bytes([len(raw)])
    elif len(raw) < 0x10000:
        size = b"\x81" + len(raw).to_bytes(2, "little")
    else:
        size = b"\x82" + len(raw).to_bytes(4, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12"
            b"NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84"
            b"\x08NSString\x01\x94\x84\x01+" + size + raw + b"\x86\x84\x02iI\x01")


def _mac_ns(ts: float) -> int:
    return int((ts - reader.MAC_EPOCH) * 1e9)


class _Db:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
          CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
          CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, display_name TEXT);
          CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
          CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
          CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT,
            text TEXT, attributedBody BLOB, handle_id INTEGER, date INTEGER,
            is_from_me INTEGER DEFAULT 0, service TEXT DEFAULT 'iMessage',
            associated_message_type INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0);
        """)
        self._n = 0

    def handle(self, value: str) -> int:
        return self.conn.execute("INSERT INTO handle (id) VALUES (?)", (value,)).lastrowid

    def chat(self, guid: str, handles: list[int], name: str = "") -> int:
        cid = self.conn.execute("INSERT INTO chat (guid, display_name) VALUES (?, ?)",
                                (guid, name)).lastrowid
        for h in handles:
            self.conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (cid, h))
        return cid

    def msg(self, chat_id, handle_id, text=None, *, ts=NOW - 60, me=0, body=None,
            assoc=0, item=0) -> str:
        self._n += 1
        guid = f"guid-{self._n}"
        rid = self.conn.execute(
            "INSERT INTO message (guid, text, attributedBody, handle_id, date, "
            "is_from_me, associated_message_type, item_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guid, text, body, handle_id, _mac_ns(ts), me, assoc, item)).lastrowid
        self.conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_id, rid))
        self.conn.commit()
        return guid


class _Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.td = Path(td.name)
        self.db = _Db(self.td / "chat.db")
        self.addCleanup(self.db.conn.close)
        self.conn = im.imessage
        for p in (
            mock.patch.dict(os.environ, {"QUILL_IMESSAGE_DB": str(self.db.path)}),
            mock.patch.object(im, "_state_path", lambda: self.td / "state.json"),
            mock.patch.object(reader, "contact_names", lambda: {"5551234567": "Sam Lee"}),
            # Purging and rating must never reach the real data dir from a test.
            mock.patch("app.services.exemplar_store.delete_for_pair"),
            mock.patch("app.services.exemplar_store.ingest_pair"),
            mock.patch("app.services.escalate_log.escalate_log.purge_containing",
                       return_value=0),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.sam = self.db.handle("+15551234567")
        self.sam_chat = self.db.chat("iMessage;-;+15551234567", [self.sam])

    def fetch(self, cursor=None):
        return self.conn.fetch_items(cursor=cursor, now=NOW)


class RegistryTests(unittest.TestCase):
    def test_registered_as_a_ready_directory_connector(self):
        c = connectors.get("imessage")
        self.assertIsNotNone(c)
        self.assertEqual(c.availability, "ready")
        self.assertEqual(connectors.for_tool("iMessage").id, "imessage")
        row = next(r for r in connectors.directory()["connectors"]
                   if r["id"] == "imessage")
        self.assertEqual(row["category"], "messages")
        self.assertIn("messages",
                      [x["id"] for x in connectors.directory()["categories"]])

    def test_chat_toggle_blocks_its_sources(self):
        self.assertEqual(session.source_prefixes()["imessage"], ("imessage.",))

    def test_source_policy_treats_a_text_like_mail(self):
        from app.services import source_policy as sp
        self.assertEqual(sp.classify_source(event_source="imessage.message"),
                         "connector_mail")


class AvailabilityTests(unittest.TestCase):
    def test_off_a_mac_it_is_unavailable_and_says_why(self):
        with mock.patch.dict(os.environ, {"QUILL_IMESSAGE_DB": "", "QUILL_IMESSAGE_RELAY": ""}), \
                mock.patch.object(im.sys, "platform", "linux"):
            st = im.imessage.status()
            self.assertFalse(st["configured"])
            self.assertFalse(st["connected"])
            self.assertIn("Mac", st["setup_hint"])
            r = im.imessage.begin_connect()
            self.assertFalse(r["ok"])
            self.assertIn("Mac", r["error"])

    def test_on_a_mac_a_blocked_database_stays_connectable(self):
        # Without Full Disk Access the stat fails too, so this can't hang off
        # the file existing, or the user never sees the steps.
        with mock.patch.dict(os.environ, {"QUILL_IMESSAGE_DB": "", "QUILL_IMESSAGE_RELAY": ""}), \
                mock.patch.object(im.sys, "platform", "darwin"):
            self.assertTrue(im.imessage.configured())


class ConnectTests(_Base):
    def test_connect_marks_connected_and_disconnect_clears(self):
        self.assertFalse(self.conn.connected())
        r = self.conn.begin_connect()
        self.assertTrue(r["ok"])
        self.assertEqual(r["mode"], "device")
        self.assertTrue(self.conn.connected())
        self.assertTrue(self.conn.disconnect()["ok"])
        self.assertFalse(self.conn.connected())

    def test_unreadable_database_returns_the_full_disk_access_steps(self):
        with mock.patch.dict(os.environ, {"QUILL_IMESSAGE_DB": str(self.td / "nope.db")}):
            r = self.conn.begin_connect()
        self.assertFalse(r["ok"])
        self.assertTrue(r["needs_permission"])
        self.assertIn("Full Disk Access", r["error"])
        self.assertFalse(self.conn.connected())

    def test_connection_cannot_write_to_messages(self):
        conn = reader.open_messages()
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM message")


class ReadTests(_Base):
    def test_received_and_sent_text_shape(self):
        g1 = self.db.msg(self.sam_chat, self.sam, "Can you send the deck?")
        self.db.msg(self.sam_chat, self.sam, "Sending it now", me=1)
        items, _ = self.fetch()
        a, b = items
        self.assertEqual(a["external_id"], g1)
        self.assertEqual(a["kind"], "message")
        self.assertEqual(a["thread_id"], "iMessage;-;+15551234567")
        self.assertEqual(a["title"], "From Sam Lee: Can you send the deck?")
        self.assertEqual(a["body"], "From Sam Lee: Can you send the deck?")
        self.assertEqual(a["people"], ["Sam Lee"])
        self.assertEqual(a["direction"], "received")
        self.assertAlmostEqual(a["ts"], NOW - 60, delta=1)
        self.assertEqual(b["title"], "To Sam Lee: Sending it now")
        self.assertEqual(b["direction"], "sent")
        self.assertTrue(b["from_self"])
        self.assertEqual(b["people"], ["Sam Lee"])

    def test_unknown_number_is_shown_but_never_minted_as_a_person(self):
        h = self.db.handle("+15559990000")
        c = self.db.chat("c2", [h])
        self.db.msg(c, h, "hey it's me")
        item = self.fetch()[0][0]
        self.assertEqual(item["people"], [])
        self.assertIn("+15559990000", item["title"])

    def test_group_chat_lists_people_and_name(self):
        h = self.db.handle("kim@example.com")
        c = self.db.chat("g1", [self.sam, h], "Family")
        self.db.msg(c, self.sam, "dinner at 7?")
        with mock.patch.object(reader, "contact_names",
                               lambda: {"5551234567": "Sam Lee",
                                        "kim@example.com": "Kim Ro"}):
            item = self.fetch()[0][0]
        self.assertEqual(item["title"], "From Sam Lee in Family: dinner at 7?")
        self.assertEqual(item["people"], ["Sam Lee", "Kim Ro"])
        self.assertTrue(item["meta"]["group"])

    def test_reads_attributed_body_when_text_is_null(self):
        self.db.msg(self.sam_chat, self.sam, None, body=_blob("Only in the archive"))
        self.assertIn("Only in the archive", self.fetch()[0][0]["body"])

    def test_decoder_handles_each_length_form(self):
        for text in ("short", "x" * 200, "é" * 40000):
            self.assertEqual(reader.decode_attributed_body(_blob(text)), text)
        self.assertEqual(reader.decode_attributed_body(None), "")
        self.assertEqual(reader.decode_attributed_body(b"not an archive"), "")
        # A length running past the end of the blob is corrupt, not text.
        self.assertEqual(
            reader.decode_attributed_body(b"NSString\x01\x94\x84\x01+\x50abc"), "")

    def test_keeps_out_what_should_never_land(self):
        short = self.db.handle("72345")
        short_chat = self.db.chat("s1", [short])
        self.db.msg(short_chat, short, "Your package arrives today")
        self.db.msg(short_chat, short, "STOP", me=1)
        sender = self.db.handle("AMAZON")
        self.db.msg(self.db.chat("s2", [sender]), sender, "Your order shipped")
        self.db.msg(self.sam_chat, self.sam, "Your verification code is 482913")
        self.db.msg(self.sam_chat, self.sam, "Loved “see you”", assoc=2000)
        self.db.msg(self.sam_chat, self.sam, "renamed", item=2)
        self.db.msg(self.sam_chat, self.sam, "￼")
        self.db.msg(self.sam_chat, self.sam, "Lunch tomorrow?")
        items, cur = self.fetch()
        self.assertEqual([i["body"] for i in items], ["From Sam Lee: Lunch tomorrow?"])
        # Dropped rows still move the cursor, so they aren't re-read forever.
        self.assertEqual(self.fetch(cur)[0], [])

    def test_first_run_is_bounded_and_late_old_history_stays_out(self):
        self.db.msg(self.sam_chat, self.sam, "ancient", ts=NOW - 40 * 86400)
        self.db.msg(self.sam_chat, self.sam, "recent")
        items, cur = self.fetch()
        self.assertEqual(len(items), 1)
        self.assertIn("recent", items[0]["body"])
        # Messages in iCloud can insert old rows with new rowids.
        self.db.msg(self.sam_chat, self.sam, "old but late", ts=NOW - 39 * 86400)
        self.db.msg(self.sam_chat, self.sam, "new")
        items, _ = self.fetch(cur)
        self.assertEqual([i["body"] for i in items], ["From Sam Lee: new"])

    def test_large_backlog_drains_across_passes_without_a_gap(self):
        for i in range(5):
            self.db.msg(self.sam_chat, self.sam, f"note {i}")
        seen, cur = [], None
        with mock.patch.object(scheduler, "MAX_ITEMS_PER_SYNC", 2):
            for _ in range(4):
                items, cur = self.fetch(cur)
                seen += [i["body"] for i in items]
        self.assertEqual(seen, [f"From Sam Lee: note {i}" for i in range(5)])


class PurgeTests(_Base):
    def test_purge_disconnects_deletes_and_forgets_the_cursor(self):
        from app.storage import Store
        store = Store(self.td / "t.db")
        self.conn.begin_connect()
        self.db.msg(self.sam_chat, self.sam, "keep me until asked")
        scheduler.sync_connector(self.conn, store=store, now=NOW, force=True)
        self.assertEqual(len(store.recent_events(source_substr="imessage", limit=5)), 1)
        r = self.conn.purge(store=store)
        self.assertEqual(r["deleted"], 1)
        self.assertFalse(self.conn.connected())
        self.assertEqual(store.recent_events(source_substr="imessage", limit=5), [])
        st = store.connector_sync_get("imessage")
        self.assertEqual(st["items_landed"], 0)
        self.assertEqual(st["cursor"], {})
        # Reconnecting can bring the same texts back: the ledger was cleared.
        again = scheduler.sync_connector(self.conn, store=store, now=NOW, force=True)
        self.assertEqual(again["landed"], 1)


class LandingTests(_Base):
    def setUp(self):
        super().setUp()
        from app.storage import Store
        self.store = Store(self.td / "t.db")

    def test_lands_sensitive_observed_and_never_authorizing_once(self):
        self.db.msg(self.sam_chat, self.sam, "Can you send the deck?")
        first = scheduler.sync_connector(self.conn, store=self.store, now=NOW, force=True)
        self.assertEqual(first["landed"], 1)
        again = scheduler.sync_connector(self.conn, store=self.store, now=NOW, force=True)
        self.assertEqual(again["landed"], 0)
        rows = self.store.recent_events(source_substr="imessage", limit=10)
        self.assertEqual(len(rows), 1)
        ev = rows[0]
        self.assertEqual(ev["source"], "imessage.message")
        self.assertEqual(ev["meta"]["connector_id"], "imessage")
        self.assertEqual(ev["meta"]["privacy_class"], "sensitive")
        self.assertTrue(ev["meta"]["never_authorizes"])
        self.assertEqual(ev["meta"]["direction"], "received")

    def test_chat_line_carries_the_words_and_the_toggle_can_match_it(self):
        from app.services import grounding
        self.db.msg(self.sam_chat, self.sam, "Budget review moved to Thursday")
        scheduler.sync_connector(self.conn, store=self.store, now=NOW, force=True)
        ev = self.store.recent_events(source_substr="imessage", limit=1)[0]
        line = grounding._dedup_hit_lines([ev])[0]
        self.assertIn("Budget review moved to Thursday", line)
        # agent_bridge drops a memory line when it contains a blocked prefix.
        with mock.patch.object(session, "active_ids", return_value=set()):
            blocked = session.blocked_source_prefixes()
        self.assertTrue(any(p in line for p in blocked if p == "imessage."))

    def test_sensitive_stamp_blocks_egress(self):
        from app.services import privacy_class as pc
        self.db.msg(self.sam_chat, self.sam, "personal news")
        scheduler.sync_connector(self.conn, store=self.store, now=NOW, force=True)
        eid = self.store.recent_events(source_substr="imessage", limit=1)[0]["id"]
        self.assertIn("sensitive", pc.event_egress_refusal(self.store, eid))

    def test_revoked_access_is_recorded_not_raised(self):
        with mock.patch.dict(os.environ, {"QUILL_IMESSAGE_DB": str(self.td / "gone.db")}):
            r = scheduler.sync_connector(self.conn, store=self.store, now=NOW, force=True)
        self.assertFalse(r["ok"])
        self.assertIn("Full Disk Access",
                      self.store.connector_sync_get("imessage")["last_error"])


class ContactNameTests(unittest.TestCase):
    def test_reads_names_from_the_address_book(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "Sources" / "abc"
            src.mkdir(parents=True)
            c = sqlite3.connect(src / "AddressBook-v22.abcddb")
            c.executescript("""
              CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT,
                ZLASTNAME TEXT, ZORGANIZATION TEXT);
              CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
              CREATE TABLE ZABCDEMAILADDRESS (ZOWNER INTEGER, ZADDRESS TEXT);
              INSERT INTO ZABCDRECORD VALUES (1, 'Sam', 'Lee', NULL), (2, NULL, NULL, 'Acme');
              INSERT INTO ZABCDPHONENUMBER VALUES (1, '(555) 123-4567');
              INSERT INTO ZABCDEMAILADDRESS VALUES (2, 'Hi@Acme.com');
            """)
            c.commit()
            c.close()
            with mock.patch.object(reader, "contacts_root", lambda: root):
                names = reader.contact_names()
        self.assertEqual(names["5551234567"], "Sam Lee")
        self.assertEqual(names["hi@acme.com"], "Acme")

    def test_missing_address_book_is_empty_not_an_error(self):
        with mock.patch.object(reader, "contacts_root", lambda: Path("/nonexistent/x")):
            self.assertEqual(reader.contact_names(), {})


class TrainingTests(_Base):
    """Texts feed this user's own model and cross to no one else's."""

    def setUp(self):
        super().setUp()
        from app.storage import Store
        self.store = Store(self.td / "t.db")
        p = mock.patch("app.storage.get_store", lambda: self.store)
        p.start()
        self.addCleanup(p.stop)

    def sync(self):
        return scheduler.sync_connector(self.conn, store=self.store, now=NOW, force=True)

    def pairs(self):
        return self.store.list_learning_pairs(task_type="imessage.reply", limit=50)

    def test_a_reply_becomes_a_pair_quarantined_and_sensitive(self):
        self.db.msg(self.sam_chat, self.sam, "Can you send the deck?", ts=NOW - 300)
        self.db.msg(self.sam_chat, self.sam, "By Friday please", ts=NOW - 200)
        self.db.msg(self.sam_chat, self.sam, "Sending it over tonight", ts=NOW - 100, me=1)
        self.sync()
        p = self.pairs()[0]
        self.assertEqual(p["input_text"],
                         "From Sam Lee: Can you send the deck?\nFrom Sam Lee: By Friday please")
        self.assertEqual(p["final_target"], "Sending it over tonight")
        self.assertFalse(p["human_confirmed"])
        self.assertEqual(p["privacy_class"], "sensitive")
        self.assertFalse(p["shadow_eligible"])

    def test_what_is_not_a_reply_teaches_nothing(self):
        kim = self.db.handle("kim@example.com")
        group = self.db.chat("g1", [self.sam, kim], "Family")
        self.db.msg(group, self.sam, "Dinner at seven?", ts=NOW - 500)
        self.db.msg(group, None, "Yes that works for me", ts=NOW - 490, me=1)
        self.db.msg(self.sam_chat, None, "Starting the conversation here", ts=NOW - 400, me=1)
        self.db.msg(self.sam_chat, None, "And a second bubble after it", ts=NOW - 390, me=1)
        self.db.msg(self.sam_chat, self.sam, "ok?", ts=NOW - 300)
        self.db.msg(self.sam_chat, None, "ok", ts=NOW - 290, me=1)
        other = self.db.handle("+15550001111")
        late = self.db.chat("c9", [other])
        self.db.msg(late, other, "Are you free?", ts=NOW - 9 * 3600)
        self.db.msg(late, None, "Sorry only just saw this", ts=NOW - 60, me=1)
        self.sync()
        self.assertEqual(self.pairs(), [])

    def test_the_off_switch_works(self):
        self.db.msg(self.sam_chat, self.sam, "Can you send the deck?", ts=NOW - 300)
        self.db.msg(self.sam_chat, self.sam, "Sending it over tonight", ts=NOW - 100, me=1)
        with mock.patch.dict(os.environ, {"QUILL_IMESSAGE_LEARN": "0"}):
            self.sync()
        self.assertEqual(self.pairs(), [])

    def test_replies_reach_training_but_never_the_gate(self):
        self.db.msg(self.sam_chat, self.sam, "Can you send the deck?", ts=NOW - 300)
        self.db.msg(self.sam_chat, self.sam, "Sending it over tonight", ts=NOW - 100, me=1)
        self.sync()
        import importlib
        sys.path.insert(0, "scripts")
        dc = importlib.import_module("distill_curate")
        sys.path.pop(0)
        replies = dc.load_text_replies(store=self.store)
        self.assertEqual(len(replies), 1)
        self.assertEqual(dc.load_from_learning_pairs(store=self.store), [])
        stats = dc.curate([], holdout_pct=34, dedupe_sim=1.0, reply_rows=replies)
        self.assertEqual(stats["reply_pairs"], 1)
        self.assertEqual(stats["holdout_n"], 0)
        self.assertEqual(stats["train_pairs"], 0)
        ex = stats["weighted"][0]
        self.assertIn("own voice", ex["system"])
        self.assertIn("Sending it over tonight", ex["target"])

    def test_replies_are_never_offered_to_teammates_or_stored_as_exemplars(self):
        from app.services import exemplar_store
        from app.services.fleet import exemplar_share
        self.db.msg(self.sam_chat, self.sam, "Can you send the deck?", ts=NOW - 300)
        self.db.msg(self.sam_chat, self.sam, "Sending it over tonight", ts=NOW - 100, me=1)
        with mock.patch.object(exemplar_share, "offer_pair") as offer, \
                mock.patch.object(exemplar_store, "ingest_pair") as ingest:
            self.sync()
        self.assertEqual(len(self.pairs()), 1)
        offer.assert_not_called()
        ingest.assert_not_called()

    def test_a_rated_answer_that_quoted_a_text_trains_here_and_cannot_cross(self):
        from app.services import learning_store
        from app.services.fleet import exemplar_share
        row = {"id": "d1", "modality": "text", "task": "chat", "time": NOW,
               "local": {"text": ""},
               "parent": {"text": "Sam asked you to send the deck by Friday."},
               "meta": {"system": 'MEMORIES:\n- [document] "[imessage.message] From Sam Lee: send it"',
                        "messages": [{"role": "user", "text": "what did Sam want?"}]}}
        pid = learning_store.record_from_distill(row, "accepted", verdict_source="backfill",
                                                 store=self.store)
        pair = self.store.get_learning_pair(pid)
        self.assertEqual(pair["privacy_class"], "sensitive")
        self.assertTrue(pair["source_refs"]["quotes_imessage"])
        self.assertIn("sensitive", exemplar_share.privacy_refusal(
            pair["input_text"], source="fleet.exemplar",
            declared_class=pair["privacy_class"]))

    def test_saturation_gate_ignores_replies(self):
        from app.services import idle_trainer
        self.assertFalse(idle_trainer.lora_saturation({}, [], min_type_pairs=1,
                                                      plateau_eps=0.1, truncating=True)[0])
        self.db.msg(self.sam_chat, self.sam, "Can you send the deck?", ts=NOW - 300)
        self.db.msg(self.sam_chat, self.sam, "Sending it over tonight", ts=NOW - 100, me=1)
        self.sync()
        with mock.patch.dict(os.environ, {"QUILL_LORA_SATURATION_MIN_PAIRS": "1"}):
            seen = {}
            real = idle_trainer.lora_saturation

            def spy(counts, *a, **k):
                seen.update(counts)
                return real(counts, *a, **k)

            with mock.patch.object(idle_trainer, "lora_saturation", spy):
                idle_trainer.lora_saturated_probe()
        self.assertNotIn("imessage.reply", seen)

    def test_the_router_trainer_does_not_learn_from_replies(self):
        from app.services import router_train
        self.db.msg(self.sam_chat, self.sam, "Can you send the deck?", ts=NOW - 300)
        self.db.msg(self.sam_chat, self.sam, "Sending it over tonight", ts=NOW - 100, me=1)
        self.sync()
        self.assertIsNone(router_train._pair_label(self.pairs()[0]))

    def test_purge_takes_the_training_data_too(self):
        from app.services import learning_store
        from app.services.escalate_log import EscalateLog
        log = EscalateLog()
        log._path = self.td / "distill.jsonl"
        log._path.write_text(
            '{"id":"keep","reason":"a","meta":{"system":"nothing private"}}\n'
            '{"id":"drop","reason":"b","meta":{"system":"[imessage.message] From Sam"}}\n',
            encoding="utf-8")
        self.db.msg(self.sam_chat, self.sam, "Can you send the deck?", ts=NOW - 300)
        self.db.msg(self.sam_chat, self.sam, "Sending it over tonight", ts=NOW - 100, me=1)
        self.sync()
        other = learning_store.record(task_type="extraction.task", input_text="unrelated",
                                      verdict="accepted", verdict_source="t",
                                      final_target="do the thing", store=self.store)
        with mock.patch("app.services.escalate_log.escalate_log", log):
            r = self.conn.purge(store=self.store)
        self.assertEqual(r["training_deleted"], 2)
        self.assertEqual(self.pairs(), [])
        self.assertIsNotNone(self.store.get_learning_pair(other))
        lines = log._path.read_text("utf-8")
        self.assertIn('"keep"', lines)
        self.assertNotIn('"drop"', lines)


class QuotedTextPrivacyTests(unittest.TestCase):
    TEXT = 'MEMORIES:\n- [document] "[imessage.message] From Sam Lee: I got the test results back"'

    def test_the_tag_keeps_a_quoted_text_sensitive_whatever_its_words(self):
        from app.services import privacy_class as pc
        self.assertEqual(pc.classify_text(self.TEXT), pc.SENSITIVE)
        self.assertEqual(pc.classify_text("From Sam Lee: I got the test results back"), pc.INTERNAL)
        self.assertIn("sensitive", pc.egress_refusal(self.TEXT))

    def test_chat_can_still_read_it(self):
        from app.services import privacy_class as pc
        _, _, cls, action = pc.gate_cloud(self.TEXT, [{"role": "user", "content": "what did Sam say?"}])
        self.assertEqual((cls, action), (pc.SENSITIVE, "redact"))

    def test_a_secret_in_the_same_prompt_still_refuses(self):
        from app.services import privacy_class as pc
        with self.assertRaises(pc.PrivacyRefuse):
            pc.gate_cloud(self.TEXT + " sk-ant-api03-" + "x" * 40, [])


if __name__ == "__main__":
    unittest.main()
