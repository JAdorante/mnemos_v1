"""Entity model, checks after the final fix passes: defects the independent
verification of passes A and B found, each with a test that fails without
its fix.

- The ledger cap keeps the newest sightings of each kind of evidence, so a
  flood of one kind (the name on screen all day, a stream of notes) cannot
  evict the mail that made a row, and the repair and the scope rule decide
  what they decided before the cap.
- A forget leaves another live row's own name alone in the decision trail
  and the repair reports, also when that row carries the forgotten name as
  its alias; adding a row again as it already is records nothing.
- A repair change the log cannot record is put back (an automatic change
  nobody could undo must not stand); a deletion that happened is reported
  as applied.
- The candidate sweep never deletes sightings it could not fold into their
  summary.
- Seeding a seat's vocabulary is one transaction.

Every name here is invented. Every test runs against a real Store on a temp
db.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes  # noqa: F401
from app.services import entity_candidates as cand
from app.services import entity_repair as er
from app.services import entity_scope
from app.services import entity_typing as et
from app.services import projects
from app.services.kg_keys import entity_name_key
from tests.test_entity_decision_trail import _Case as _TrailCase
from tests.test_entity_decisions import DAY, HOUR, MIN, NOW, _StoreCase

SCREEN = "desktop.screen"
NOTE = "assistant.claude"


def _plan_for(store, name):
    return [(a["action"], a["rule"]) for a in er.plan(store)["actions"]
            if a["name"] == name and a["action"] != "keep"]


# ------------------------------------------------------- the ledger cap --
class CapKeepsEvidenceTests(_StoreCase):
    """Beyond the newest N, the newest sightings of each kind stay."""

    def setUp(self):
        super().setUp()
        self.ev = 0

    def sight(self, *a, **kw):
        # One event per sighting: the repair and the scope rule count
        # events.
        self.ev += 1
        return super().sight(*a, event_id=self.ev, **kw)

    def flood(self, eid, name, source, cls, n=600, kind="org"):
        for i in range(n):
            self.sight(name, kind, cls=cls, source=source,
                       ts=NOW + 3 * DAY + i * MIN, entity_id=eid)

    def test_a_screen_flood_keeps_the_mail_that_made_the_row(self):
        eid = self.store.resolve_entity("Gannet Freight", "org", ts=NOW,
                                        kind_source="model")
        for d in (0, 2):
            self.sight("Gannet Freight", "org", ts=NOW + d * DAY,
                       entity_id=eid)
        self.flood(eid, "Gannet Freight", SCREEN, "unknown")
        rows = self.store.entity_mentions(entity_id=eid)
        self.assertEqual(sum(r["event_source"] == "google.mail"
                             for r in rows), 2)
        self.assertEqual(len(rows), cand.bound_cap() + 2)
        self.assertEqual(_plan_for(self.store, "Gannet Freight"), [])

    def test_a_note_flood_keeps_the_mail_too(self):
        eid = self.store.resolve_entity("Corvel Analytics", "org", ts=NOW,
                                        kind_source="model")
        self.sight("Corvel Analytics", "org", ts=NOW, entity_id=eid)
        self.flood(eid, "Corvel Analytics", NOTE, "assistant_capture")
        cand.sweep_expired(self.store, now=NOW + 90 * DAY, bound=True)
        self.assertEqual(_plan_for(self.store, "Corvel Analytics"), [])

    def test_the_scope_rule_still_sees_the_other_project(self):
        for p in ("Larkfen Rollout", "Odile Survey"):
            projects.ensure_filed(p, store=self.store, now=NOW,
                                  user_accept=True)
        eid = self.store.resolve_entity("QA-B", "label", ts=NOW,
                                        kind_source="rule")
        for d in (0, 2, 4):
            self.sight("QA-B", "label", ts=NOW + d * DAY, entity_id=eid,
                       filing_project="Odile Survey")
        for i in range(600):
            self.sight("QA-B", "label", ts=NOW + 10 * DAY + i * HOUR,
                       entity_id=eid, filing_project="Larkfen Rollout")
        row = next(r for r in entity_scope.compute(self.store)
                   if r["entity_id"] == eid)
        self.assertNotEqual(row["action"], "set")
        self.assertIsNone(row["parent_id"])

    def test_the_vote_reads_what_it_read_before(self):
        eid = self.store.resolve_entity("Gannet Freight", "org", ts=NOW,
                                        kind_source="model")
        self.sight("Gannet Freight", "tool", ts=NOW, entity_id=eid)
        self.flood(eid, "Gannet Freight", SCREEN, "unknown")
        newest = self.store.entity_mentions(
            entity_id=eid, limit=et.MAX_MENTIONS, newest_first=True)
        self.assertTrue(all(r["event_source"] == SCREEN for r in newest))
        self.assertEqual(et.vote(self.store, entity_id=eid).breakdown[
            "mentions"], et.MAX_MENTIONS)


# ------------------------------------------------- the trail's precision --
class ForgetKeepsLiveNamesTests(_TrailCase):
    """A name another live row carries is that row's, forgotten or not."""

    def test_another_rows_alias_decision_stays_in_clear(self):
        topic = self.ent("HFP", "idea")
        org = self.ent("Harbor Freight Partners", "org", "user")
        self.client.post(f"/entities/{org}/alias", json={"alias": "HFP"})
        self.client.post(f"/entities/{topic}/forget")
        alias = self.last("entities.alias")
        self.assertEqual(alias["subject"], f"entity:{org}")
        self.assertEqual(alias["edit"], {"alias": "HFP"})
        forget = self.last("entities.forget")
        self.assertNotIn("HFP", json.dumps(forget))

    def test_forgetting_the_row_itself_still_hashes_its_alias(self):
        org = self.ent("Harbor Freight Partners", "org", "user")
        self.client.post(f"/entities/{org}/alias", json={"alias": "HFP"})
        self.client.post(f"/entities/{org}/forget")
        blob = json.dumps(self.trail())
        self.assertNotIn("HFP", blob)
        self.assertNotIn("Harbor Freight", blob)


class AddRecordsOnlyChangesTests(_TrailCase):
    """Adding a row again as it already is records nothing."""

    def test_adding_a_row_again_as_it_is_records_nothing(self):
        r1 = self.client.post("/entities",
                              json={"name": "Velmont Sailworks", "kind": "org"})
        r2 = self.client.post("/entities",
                              json={"name": "Velmont Sailworks", "kind": "org"})
        self.assertEqual(r1.json()["id"], r2.json()["id"])
        self.assertTrue(r2.json().get("unchanged"))
        self.assertEqual(len(self.trail("entities.add")), 1)
        # Adding a row extraction made takes its kind for the user: a
        # change, recorded.
        self.ent("Larkfen Ledger", "tool", "model")
        r3 = self.client.post("/entities",
                              json={"name": "Larkfen Ledger", "kind": "tool"})
        self.assertFalse(r3.json().get("unchanged"))
        self.assertEqual(r3.json()["kind_source"], "user")
        self.assertEqual(len(self.trail("entities.add")), 2)


# ------------------------------------------------- the repair log writes --
class RepairLogFailureTests(_StoreCase):
    """A change with no log entry could not be undone."""

    def test_an_automatic_change_the_log_cannot_record_is_put_back(self):
        hid = self.store.resolve_entity("net 60", "idea", ts=NOW,
                                        kind_source="model")
        lab = self.store.resolve_entity("k9", "project", ts=NOW,
                                        kind_source="model")
        before = {i: (self.store.get_entity(i)["kind"],
                      self.store.get_entity(i)["kind_source"],
                      self.store.entity_is_hidden(i)) for i in (hid, lab)}
        plan = er.plan(self.store)
        with patch.object(self.store, "entity_repair_log_change",
                          side_effect=sqlite3.OperationalError("disk full")):
            out = er.apply(self.store, plan, mode="auto")
        self.assertEqual(out["applied"], [])
        self.assertTrue(out["skipped"])
        after = {i: (self.store.get_entity(i)["kind"],
                     self.store.get_entity(i)["kind_source"],
                     self.store.entity_is_hidden(i)) for i in (hid, lab)}
        self.assertEqual(after, before)

    def test_a_deletion_that_happened_is_reported_as_applied(self):
        eid = self.store.resolve_entity("notes.md", "tool", ts=NOW,
                                        kind_source="model")
        plan = er.plan(self.store)
        self.assertIn(("drop", "implausible"),
                      _plan_for(self.store, "notes.md"))
        with patch.object(self.store, "entity_repair_log_change",
                          side_effect=sqlite3.OperationalError("disk full")):
            out = er.apply(self.store, plan, mode="reviewed")
        self.assertIsNone(self.store.get_entity(eid))
        done = [a for a in out["applied"] if a["entity_id"] == eid]
        self.assertEqual(len(done), 1)
        self.assertIn("log_error", done[0])


# ------------------------------------------------ the candidate sweep --
class SweepFoldFailureTests(_StoreCase):
    """Sightings that could not be folded are not deleted."""

    def test_a_failed_fold_deletes_nothing_and_the_next_sweep_folds(self):
        self.sight("Osprey Chandlers", "org", ts=NOW)
        key = entity_name_key("Osprey Chandlers")
        # Expiry counts from when a sighting was written.
        later = float(self.store.entity_mentions(
            name_key=key)[0]["created_at"]) + 40 * DAY
        with patch.object(self.store, "fold_entity_candidates",
                          side_effect=sqlite3.OperationalError("locked")):
            cand.sweep_expired(self.store, now=later)
        self.assertEqual(len(self.store.entity_mentions(
            name_key=key, status="pending")), 1)
        self.assertIsNone(self.store.entity_candidate_summary(key))
        cand.sweep_expired(self.store, now=later)
        self.assertEqual(self.store.entity_mentions(
            name_key=key, status="pending"), [])
        self.assertEqual(
            self.store.entity_candidate_summary(key)["mentions"], 1)


# ------------------------------------------------- seeding a seat --
class SeedIsOneTransactionTests(_StoreCase):
    def test_a_failure_part_way_seeds_nothing(self):
        self.store._conn.execute(
            "CREATE TEMP TRIGGER boom BEFORE INSERT ON main.seat_vocab "
            "WHEN NEW.term = 'zzbar' BEGIN SELECT RAISE(ABORT, 'boom'); END")
        terms = [("nonperson_token", "zzfoo"), ("nonperson_token", "zzbar"),
                 ("corp_word", "zzbaz")]
        with self.assertRaises(sqlite3.Error):
            self.store.add_seat_vocab_many(terms, source="migration")
        self.assertFalse(self.store._conn.in_transaction)
        self.assertEqual(self.store.list_seat_vocab(), [])
        self.store._conn.execute("DROP TRIGGER boom")
        self.assertEqual(
            self.store.add_seat_vocab_many(terms, source="migration"), 3)
        self.assertEqual(
            self.store.add_seat_vocab_many(terms, source="migration"), 0)


if __name__ == "__main__":
    unittest.main()
