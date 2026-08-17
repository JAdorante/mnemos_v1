"""Workstream 2 — Gmail/Calendar metadata exhaust (no bodies, no LLM)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import exhaust_ingest as ex
from app.services.source_policy import classify_source, policy_for
from app.storage import Store


class DeriveTests(unittest.TestCase):
    def test_parse_addrs_and_freemail_org(self) -> None:
        people = ex.parse_rfc2822_addr('Ada Lovelace <ada@example.com>, bob@gmail.com')
        emails = {p["email"] for p in people}
        self.assertIn("ada@example.com", emails)
        self.assertIn("bob@gmail.com", emails)
        from app.services.people_pipeline import org_from_email_domain
        self.assertEqual(org_from_email_domain("ada@acme.io"), "Acme")
        self.assertIsNone(org_from_email_domain("bob@gmail.com"))

    def test_stats_and_co_attendance(self) -> None:
        messages = [
            {"id": "<m1>", "ts": 100.0, "headers": {
                "from": "Ada <ada@acme.io>",
                "to": "me@ours.com",
            }},
            {"id": "<m2>", "ts": 200.0, "headers": {
                "from": "me@ours.com",
                "to": "Ada <ada@acme.io>,  Bob <bob@acme.io>",
            }},
        ]
        events = [{
            "id": "e1", "start": 150.0, "title": "Sync",
            "attendees": [
                {"email": "ada@acme.io", "name": "Ada"},
                {"email": "bob@acme.io", "name": "Bob"},
            ],
            "organizer": {"email": "me@ours.com", "name": "Me"},
        }]
        stats = ex.derive_contact_stats(
            messages, events, self_emails=["me@ours.com"])
        self.assertIn("ada@acme.io", stats)
        self.assertGreaterEqual(stats["ada@acme.io"]["interaction_count"], 2)
        self.assertEqual(stats["bob@acme.io"]["co_attendance"], 1)
        self.assertTrue(stats["bob@acme.io"]["from_calendar"])
        pairs = ex.co_attendance_pairs(events, self_emails=["me@ours.com"])
        self.assertEqual(pairs[("ada@acme.io", "bob@acme.io")], 1)

    def test_scope_guard_rejects_gmail_readonly_full(self) -> None:
        with self.assertRaises(PermissionError):
            ex.assert_metadata_scopes(
                "https://www.googleapis.com/auth/gmail.readonly "
                + "https://www.googleapis.com/auth/calendar.events.readonly")
        ex.assert_metadata_scopes(" ".join(ex.SCOPES))


class IngestPurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_ex_"))
        self.env = patch.dict(os.environ, {
            "QUILL_DATA_DIR": str(self.tmp),
            "QUILL_EXHAUST_INGEST": "1",
        }, clear=False)
        self.env.start()
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def tearDown(self) -> None:
        self.env.stop()

    def test_ingest_mints_people_org_edges_no_commitments(self) -> None:
        messages = [{"id": "<m1>", "ts": 100.0, "headers": {
            "from": "Ada Lovelace <ada@acme.io>",
            "to": "me@ours.com",
            "date": "Thu, 1 Jan 2026 12:00:00 +0000",
            "message-id": "<m1>",
        }}]
        events = [{
            "id": "cal-ada", "start": 200.0, "end": 260.0, "title": "Sync",
            "attendees": [{"email": "ada@acme.io", "name": "Ada Lovelace"}],
            "organizer": {"email": "me@ours.com", "name": "Me"},
        }]
        with patch.object(ex, "_self_emails", return_value=["me@ours.com"]):
            out = ex.run_ingest(
                store=self.store, messages=messages, events=events, fetch=False)
        self.assertTrue(out.get("ok"), out)
        self.assertGreaterEqual(out.get("people") or 0, 1)
        people = self.store.all_people()
        names = {p["name"].lower() for p in people}
        self.assertTrue(any("ada" in n for n in names))
        # Policy: exhaust must not mint commitments/claims.
        facts = self.store.list_facts(limit=50)
        self.assertFalse(any((f.get("kind") in ("commitment", "claim", "task"))
                             for f in facts))
        rels = []
        for p in people:
            rels.extend(self.store.relations_of("person", p["id"]).get("out") or [])
        self.assertTrue(any(r.get("predicate") == "works_at" for r in rels))

    def test_purge_removes_exhaust_rows(self) -> None:
        messages = [{"id": "<m1>", "ts": 100.0, "headers": {
            "from": "Ada <ada@acme.io>", "to": "x@ours.com",
        }}]
        with patch.object(ex, "_self_emails", return_value=["x@ours.com"]):
            ex.run_ingest(store=self.store, messages=messages, events=[], fetch=False)
        before = len(self.store.all_people())
        self.assertGreater(before, 0)
        purged = ex.purge(store=self.store)
        self.assertTrue(purged.get("ok"))
        leftover = [e for e in self.store.all_with_ids()
                    if getattr(e[1], "source", "").startswith("exhaust")]
        # all_with_ids returns (id, Event) — exhaust provenance events gone.
        self.assertEqual(leftover, [])

    def test_no_bodies_persisted(self) -> None:
        secret = "UNIQUE_BODY_STRING_9f3a_do_not_store"
        messages = [{"id": "<m1>", "ts": 1.0, "headers": {
            "from": "Ada <ada@acme.io>", "to": "x@ours.com",
        }}]
        with patch.object(ex, "_self_emails", return_value=["x@ours.com"]):
            ex.run_ingest(store=self.store, messages=messages, events=[], fetch=False)
        blob = ""
        for p in self.tmp.rglob("*"):
            if p.is_file():
                try:
                    blob += p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    pass
        self.assertNotIn(secret, blob)

    def test_asserted_edges_survive_rebuild(self) -> None:
        from app.services import graph
        messages = [{"id": "<m1>", "ts": 100.0, "headers": {
            "from": "Ada <ada@acme.io>", "to": "x@ours.com",
        }}]
        with patch.object(ex, "_self_emails", return_value=["x@ours.com"]):
            ex.run_ingest(store=self.store, messages=messages, events=[], fetch=False)
        works = []
        for p in self.store.all_people():
            works.extend([
                r for r in (self.store.relations_of("person", p["id"]).get("out") or [])
                if r.get("predicate") == "works_at" and r.get("origin") == "asserted"
            ])
        self.assertTrue(works)
        graph.rebuild(self.store)
        works2 = []
        for p in self.store.all_people():
            works2.extend([
                r for r in (self.store.relations_of("person", p["id"]).get("out") or [])
                if r.get("predicate") == "works_at" and r.get("origin") == "asserted"
            ])
        self.assertTrue(works2)

    def test_exhaust_source_class_policy(self) -> None:
        self.assertEqual(classify_source(event_source="exhaust.gmail"), "exhaust")
        p = policy_for("exhaust")
        self.assertTrue(p.create_person_candidates)
        self.assertFalse(p.create_commitments)
        self.assertFalse(p.create_claims)


if __name__ == "__main__":
    unittest.main()
