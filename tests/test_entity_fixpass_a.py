"""Entity model, final-review fix pass A: data safety, forget, trail,
metrics, repair.

One test class per fix (A1-A18 of the fix list): a multi-statement write
that fails part way leaves nothing behind; a forget redacts only what is the
forgotten row's (and whole forgotten names), in every record and report
that keeps text, tombstones only the names the user tied to the row and
leaves another row's sightings alone; the repair protects whatever the user
touched, merges a dressed duplicate, weighs what the user typed and asks
about a given name; the ledger is bounded; a mint after a concurrent merge
or delete never binds a dead id; the repair run guard is atomic; Inbox drops
are hides; backups are bounded; the metrics count rows; no-op edits record
nothing; a deleted person's lookups go; reports never overwrite.

Every name here is invented. Every test runs against a real Store on a temp
db.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes as routes_mod
from app import storage as storage_mod
from app.events import Event, Modality
from app.services import entity_candidates as cand
from app.services import entity_metrics as em
from app.services import entity_repair as er
from app.services import entity_typing
from app.services import inbox
from app.services import projects
from app.services.kg_keys import entity_name_key
from app.services.resolution import Resolver
from app.storage import Store

NOW = 1_700_000_000.0
DAY = 86400.0
ROOT = Path(__file__).resolve().parents[1]
MAIL = "google.mail"
NOTE = "assistant.claude"
CHAT = "chat.user"


def _mk(tmp, **kw) -> Store:
    return Store(db_path=Path(tmp) / "quill.db",
                 audio_dir=Path(tmp) / "audio", **kw)


def _script():
    spec = importlib.util.spec_from_file_location(
        "entity_repair_script_fixa", ROOT / "scripts" / "entity_repair.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_fixa_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp,
                                      "QUILL_ENTITY_CANDIDATES": "1",
                                      "QUILL_HEADS": "off"})
        env.start()
        self.addCleanup(env.stop)
        for var in ("QUILL_ENTITY_REPAIR_AUTO",
                    "QUILL_ENTITY_REPAIR_AUTO_MIN_CONF",
                    "QUILL_ENTITY_REPAIR_NOTE_ONLY",
                    "QUILL_ENTITY_REPAIR_BACKUPS_KEEP",
                    "QUILL_ENTITY_REPAIR_BACKUP_RETRY_S",
                    "QUILL_ENTITY_MENTION_CAP",
                    "QUILL_ENTITY_CANDIDATE_TTL_DAYS",
                    "QUILL_ENTITY_CANDIDATE_SESSIONS"):
            os.environ.pop(var, None)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(routes_mod.router)
        self.client = TestClient(app)
        for p in (patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store),
                  patch.object(routes_mod.memory, "index_fact",
                               lambda *a, **k: None),
                  patch("app.storage.get_store", return_value=self.store),
                  patch.object(storage_mod, "_store", None),
                  patch.object(projects, "_scopes", lambda: []),
                  patch("app.services.identity.user_identity",
                        return_value={})):
            p.start()
            self.addCleanup(p.stop)
        projects.invalidate_filed_keys()
        self.addCleanup(projects.invalidate_filed_keys)
        self._t = NOW

    # -- the world -----------------------------------------------------
    def ent(self, name, kind="org", source="model", ts=NOW) -> int:
        return self.store.resolve_entity(name, kind, ts=ts,
                                         kind_source=source)

    def event(self, source=MAIL, raw="r", gap=600.0, ts=None) -> int:
        if ts is None:
            self._t += gap
            ts = self._t
        return int(self.store.insert(Event(
            time=ts, modality=Modality.TEXT, raw=raw, source=source,
            meta={})))

    def minted_by(self, eid, source=NOTE) -> int:
        ev = self.event(source)
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE events SET extracted_at = ? WHERE id = ?",
                (self._t + 0.5, ev))
            self.store._conn.execute(
                "UPDATE entities SET first_seen = ? WHERE id = ?",
                (self._t + 0.5, eid))
            self.store._conn.commit()
        return ev

    def sight(self, name, ts, *, entity_id=None, kind="org", span=None,
              source=MAIL, cls="connector_mail") -> int:
        return self.store.record_entity_mention(
            name, proposed_kind=kind, confidence=0.8, source_class=cls,
            event_source=source, event_id=self.event(source, ts=ts),
            observed_at=ts, entity_id=entity_id, span=span)

    def count(self, sql, *args) -> int:
        with self.store._lock:
            return int(self.store._conn.execute(sql, args).fetchone()[0])

    def raw_table(self, table) -> str:
        with self.store._lock:
            rows = self.store._conn.execute(f"SELECT * FROM {table}").fetchall()
        return json.dumps([dict(r) for r in rows], default=str).lower()

    def plan(self, **kw) -> dict:
        return er.plan(self.store, **kw)

    def acts(self, p, eid) -> list[dict]:
        return [a for a in p["actions"] if a["entity_id"] == eid]

    def one(self, p, eid, action=None) -> dict:
        rows = [a for a in self.acts(p, eid)
                if action is None or a["action"] == action]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def trigger(self, sql: str) -> None:
        """A temporary trigger on this store's connection (removed after
        the test), to make one statement of a write fail."""
        self.store._conn.execute(sql)
        name = sql.split()[3]
        self.addCleanup(lambda: self.store._conn.execute(
            f"DROP TRIGGER IF EXISTS {name}"))


# ------------------------------------------------------------------ A1 --
class RollbackTests(_Case):
    """A failure part way through a multi-statement write rolls the whole
    write back; the next unrelated commit persists nothing of it."""

    def test_a_failed_forget_leaves_the_store_as_it_was(self):
        a = self.ent("Harrowgate Tiles")
        b = self.ent("Quenby Analytics")
        self.store.add_relation("entity", a, "uses", "entity", b, ts=NOW)
        self.sight("Harrowgate Tiles", NOW, entity_id=a)
        before = (self.store.entity_tombstone_count(),
                  self.count("SELECT COUNT(*) FROM relations"),
                  self.count("SELECT COUNT(*) FROM entity_mentions"),
                  self.count("SELECT COUNT(*) FROM kg_node_keys"))
        self.trigger("CREATE TEMP TRIGGER boom_del BEFORE DELETE ON "
                     "main.entities BEGIN SELECT RAISE(ABORT, 'boom'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.delete_entity(a, tombstone=True, reason="forget")
        self.assertFalse(self.store._conn.in_transaction)
        self.store._conn.execute("DROP TRIGGER boom_del")
        self.store.touch_entity(b, NOW + 1)      # an unrelated commit
        self.assertIsNotNone(self.store.get_entity(a))
        self.assertEqual((self.store.entity_tombstone_count(),
                          self.count("SELECT COUNT(*) FROM relations"),
                          self.count("SELECT COUNT(*) FROM entity_mentions"),
                          self.count("SELECT COUNT(*) FROM kg_node_keys")),
                         before)

    def test_a_failed_dismissal_writes_no_tombstone(self):
        self.sight("Quillfern Mills", NOW)
        self.trigger("CREATE TEMP TRIGGER boom_men BEFORE DELETE ON "
                     "main.entity_mentions BEGIN SELECT RAISE(ABORT, 'x'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.tombstone_entity_name("Quillfern Mills",
                                             reason="candidate_dismiss")
        self.store._conn.execute("DROP TRIGGER boom_men")
        self.ent("Unrelated Row")                # an unrelated commit
        self.assertFalse(self.store.is_entity_tombstoned("Quillfern Mills"))
        self.assertEqual(len(self.store.entity_mentions(status="pending")), 1)

    def test_a_failed_adoption_binds_nothing(self):
        self.sight("Orrin Freight", NOW)
        self.store.add_pending_entity_relation(
            subj_kind="entity", subj_name="Orrin Freight", predicate="uses",
            obj_kind="entity", obj_name="Kestle Ledger", ts=NOW)
        eid = self.ent("Orrin Freight Hold", "org")
        for kind, body in (("abort", "SELECT RAISE(ABORT, 'x')"),
                           ("missing", "SELECT * FROM no_such_table")):
            self.trigger(f"CREATE TEMP TRIGGER boom_{kind} BEFORE UPDATE ON "
                         f"main.entity_pending_relations BEGIN {body}; END")
            try:
                self.store.adopt_entity_candidates(
                    entity_name_key("Orrin Freight"), eid, ts=NOW)
            except sqlite3.Error:
                pass
            self.assertFalse(self.store._conn.in_transaction, kind)
            self.store._conn.execute(f"DROP TRIGGER boom_{kind}")
            self.ent(f"Unrelated {kind}")        # an unrelated commit
            self.assertEqual(
                [r["status"] for r in self.store.entity_mentions(
                    name_key=entity_name_key("Orrin Freight"))],
                ["pending"], kind)

    def test_a_failed_mint_keeps_the_tombstone(self):
        eid = self.ent("Pellam Works")
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        self.trigger("CREATE TEMP TRIGGER boom_ins BEFORE INSERT ON "
                     "main.entities BEGIN SELECT RAISE(ABORT, 'x'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.resolve_entity("Pellam Works", "org",
                                      kind_source="user")
        self.store._conn.execute("DROP TRIGGER boom_ins")
        self.ent("Unrelated Row")
        self.assertTrue(self.store.is_entity_tombstoned("Pellam Works"))

    def test_a_merge_that_fails_on_any_error_rolls_back(self):
        a = self.ent("Tarnside Labs")
        b = self.ent("Tarnside Laboratories")
        self.store.add_relation("entity", b, "uses", "entity", a, ts=NOW)
        n = self.count("SELECT COUNT(*) FROM relations")
        with patch.object(Store, "_merge_ref_columns_unlocked",
                          side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.store.merge_entities(a, b)
        self.assertFalse(self.store._conn.in_transaction)
        self.ent("Unrelated Row")
        self.assertIsNotNone(self.store.get_entity(b))
        self.assertEqual(self.count("SELECT COUNT(*) FROM relations"), n)
        self.assertEqual(self.count("SELECT COUNT(*) FROM entity_merges"), 0)


# ------------------------------------------------------------------ A2 --
class TrailRedactionTests(_Case):
    def trail(self) -> list[dict]:
        return list(reversed(self.store.list_decisions(limit=500)))

    def test_forgetting_a_short_name_leaves_other_rows_names(self):
        a = self.ent("Acme AI", "org", "user")
        b = self.ent("Cloud", "idea")
        self.client.post(f"/entities/{a}/rename", json={"name": "Acme AI Cloud"})
        self.client.post(f"/entities/{a}/kind", json={"kind": "tool"})
        self.client.post(f"/entities/{b}/forget")
        rows = {d["surface"]: d for d in self.trail()
                if d["subject"] == f"entity:{a}"}
        self.assertEqual(rows["entities.rename"]["edit"],
                         {"name": "Acme AI Cloud"})
        self.assertEqual(rows["entities.rename"]["shown"]["name"], "Acme AI")
        self.assertEqual(rows["entities.kind"]["shown"]["name"],
                         "Acme AI Cloud")

    def test_the_forgotten_rows_own_verdicts_and_whole_names_are_hashed(self):
        a = self.ent("Osprey", "org")
        b = self.ent("Osprey Tidepool Study", "project", "user")
        c = self.ent("Fenwick Hollow", "org")
        self.client.post(f"/entities/{b}/rename",
                         json={"name": "Osprey Tidepool Survey"})
        self.client.post(f"/entities/{a}/detail",
                         json={"key": "url", "value": "https://osprey.example"})
        self.client.post(f"/entities/{c}/merge", json={"into": a})
        self.client.post(f"/entities/{a}/forget")
        raw = json.dumps(self.store.list_decisions(limit=500)).lower()
        # The other row's names stand, whole.
        self.assertIn("osprey tidepool survey", raw)
        self.assertIn("osprey tidepool study", raw)
        # The forgotten row's own verdicts keep no text about it, and the
        # merge into it keeps the survivor's name as a hash only.
        own = [d for d in self.trail() if d["subject"] == f"entity:{a}"]
        self.assertNotIn("osprey.example", json.dumps(own).lower())
        merge = next(d for d in self.trail()
                     if d["surface"] == "entities.merge")
        self.assertIsNone(merge["shown"]["into"]["name"])
        self.assertEqual(merge["shown"]["into"]["name_hash"],
                         self.store.entity_tombstone_hash("Osprey"))
        # The user merged "Fenwick Hollow" into it: that name is the
        # forgotten row's too (tombstoned with it), so it is hashed as well.
        self.assertTrue(self.store.is_entity_tombstoned("Fenwick Hollow"))
        self.assertIsNone(merge["shown"]["name"])
        self.assertEqual(merge["shown"]["name_hash"],
                         self.store.entity_tombstone_hash("Fenwick Hollow"))

    def test_free_text_quoting_a_forgotten_name_is_cleared(self):
        keys = {entity_name_key("Brindlemoor")}
        got = self.store._redact_value(
            {"name": "Brindlemoor Works", "reason": "the same as Brindlemoor",
             "into_name": "Brindlemoor", "kind": "org",
             "aliases": ["Brindlemoor", "Brindlemoor Ltd"]}, keys)
        self.assertEqual(got["name"], "Brindlemoor Works")
        self.assertIsNone(got["reason"])
        self.assertIsNone(got["into_name"])
        self.assertEqual(got["into_name_hash"],
                         self.store.entity_tombstone_hash("Brindlemoor"))
        self.assertEqual(got["aliases"][1], "Brindlemoor Ltd")
        self.assertEqual(got["aliases"][0],
                         self.store.entity_tombstone_hash("Brindlemoor"))


# ------------------------------------------------------------------ A3 --
class ForgetEverywhereTests(_Case):
    def test_merges_repair_log_proposals_and_reports_lose_the_name(self):
        a = self.ent("Marrowfield Supply", "org")
        b = self.ent("Marrowfield", "idea")
        keep = self.ent("Quayside Haulage", "org")
        # A merge into the row, a repair change and a proposal about it, a
        # proposal about another row naming it, and a report on disk.
        self.store.merge_entities(a, b, reason="test", actor="user")
        self.store.entity_repair_start_run("r1", mode="auto")
        self.store.entity_repair_log_change(
            "r1", entity_id=a, canonical_id=None, name="Marrowfield Supply",
            action="rekind", rule="legal_form", field="kind",
            before={"kind": "idea"}, after={"kind": "org"})
        self.store.entity_repair_log_change(
            "r1", entity_id=keep, canonical_id=None, name="Quayside Haulage",
            action="rekind", rule="x", field="kind", before={}, after={})
        self.store.entity_repair_finish_run("r1", {})
        mine = self.store.entity_repair_propose(
            run_id="r1", entity_id=a, canonical_id=None, action="rekind",
            rule="x", dedupe_key="k1",
            payload={"entity_id": a, "name": "Marrowfield Supply",
                     "reason": "a company: Marrowfield Supply"})
        other = self.store.entity_repair_propose(
            run_id="r1", entity_id=keep, canonical_id=None, action="merge",
            rule="merge_same_name", dedupe_key="k2",
            payload={"entity_id": keep, "name": "Quayside Haulage",
                     "into": a, "into_name": "Marrowfield Supply"})
        report = Path(self.tmp) / "entity_repair_20990101T000000Z.json"
        report.write_text(json.dumps({"plan": {"actions": [
            {"entity_id": a, "name": "Marrowfield Supply",
             "reason": "Marrowfield Supply is an org"},
            {"entity_id": keep, "name": "Quayside Haulage",
             "into_name": "Marrowfield Supply"}]}}))
        self.client.post(f"/entities/{a}/forget")
        for table in ("entity_merges", "entity_repair_log",
                      "entity_repair_proposals"):
            raw = self.raw_table(table)
            self.assertNotIn("marrowfield", raw, table)
        self.assertIn("quayside haulage", self.raw_table("entity_repair_log"))
        self.assertIn("quayside haulage",
                      self.raw_table("entity_repair_proposals"))
        props = {p["id"]: p for p in self.store.entity_repair_proposals(
            status=None)}
        self.assertEqual(props[mine]["status"], "stale")
        self.assertEqual(props[other]["status"], "stale")
        text = report.read_text().lower()
        self.assertNotIn("marrowfield", text)
        self.assertIn("quayside haulage", text)


# ------------------------------------------------------------------ A4 --
class UserTouchTests(_Case):
    def note_only(self, name) -> int:
        eid = self.ent(name, "project", None)
        self.minted_by(eid, NOTE)
        return eid

    def test_a_rename_or_typed_alias_protects_a_note_only_row(self):
        a = self.note_only("Larkspur Deck")
        b = self.note_only("Kelvin Ridge Plan")
        c = self.note_only("Thornbury Loop")
        self.assertEqual(self.one(self.plan(), c)["rule"], "note_only")
        self.client.post(f"/entities/{a}/rename",
                         json={"name": "Larkspur Decking"})
        self.client.post(f"/entities/{b}/alias", json={"alias": "KR Plan"})
        p = self.plan()
        for eid, why in ((a, "rename"), (b, "alias")):
            x = self.one(p, eid)
            self.assertEqual((x["action"], x["rule"]),
                             ("keep", "user_touched"), x)
            self.assertIn(why, x["evidence"]["user_touched"])

    def test_a_pin_or_graph_hide_protects_a_row(self):
        a = self.note_only("Pennant Yard")
        b = self.note_only("Gorse Hill Survey")
        self.store.set_constellation_pin("entity", a, True)
        self.store.set_constellation_hidden("entity", b, True)
        p = self.plan()
        for eid in (a, b):
            self.assertEqual(self.one(p, eid)["rule"], "user_touched")

    def test_a_repair_hide_the_user_undid_is_not_hidden_again(self):
        a = self.note_only("Saltmarsh Beacon")
        res = er.auto_run(self.store, force=True)
        self.assertTrue(res["ran"], res)
        self.assertTrue(self.store.entity_is_hidden(a))
        change = next(c for c in self.store.entity_repair_log(
            run_id=res["run_id"]) if c["entity_id"] == a)
        er.undo(self.store, change_id=change["id"])
        self.assertFalse(self.store.entity_is_hidden(a))
        x = self.one(self.plan(), a)
        self.assertEqual((x["action"], x["rule"]), ("keep", "user_touched"))
        self.assertIn("unhide", x["evidence"]["user_touched"])

    def test_priors_do_not_rekind_a_legacy_row_the_user_worked_on(self):
        a = self.ent("Brightwell", "tool", None)
        b = self.ent("Cobblestone", "tool", None)
        for who, addr in (("Ana Brightwell", "ana@brightwell.example"),
                          ("Ben Cobble", "ben@cobblestone.example")):
            pid = self.store.resolve_person(who, ts=NOW)
            self.store.upsert_contact_point(
                person_id=pid, type_="email", value_display=addr,
                value_normalized=addr, confidence=0.9,
                attribution_method="email_header",
                verification_status="attributed", source_event_id=None,
                evidence_quote=None, discourse_role=None, ts=NOW,
                created_by="system", pipeline_version="t")
        self.client.post(f"/entities/{a}/alias", json={"alias": "Brightwell HQ"})
        va = entity_typing.vote(self.store, entity_id=a)
        vb = entity_typing.vote(self.store, entity_id=b)
        self.assertEqual((va.kind, va.breakdown["reason"]),
                         ("tool", "legacy_user_touched"))
        self.assertEqual((vb.kind, vb.breakdown["reason"]),
                         ("org", "email_domain"))


# ------------------------------------------------------------------ A5 --
class LedgerBoundTests(_Case):
    def test_each_entity_keeps_its_newest_sightings(self):
        eid = self.ent("Wexley Marine")
        cap = cand.bound_cap()
        self.assertEqual(cap, entity_typing.MAX_MENTIONS)
        for i in range(cap + 12):
            self.sight("Wexley Marine", NOW + i * 60, entity_id=eid)
        rows = self.store.entity_mentions(entity_id=eid)
        self.assertEqual(len(rows), cap)
        self.assertEqual(min(r["observed_at"] for r in rows), NOW + 12 * 60)
        # A cap below what the vote reads is raised to it.
        with patch.dict(os.environ, {"QUILL_ENTITY_MENTION_CAP": "10"}):
            self.assertEqual(cand.bound_cap(), entity_typing.MAX_MENTIONS)
        with patch.dict(os.environ, {"QUILL_ENTITY_MENTION_CAP": "900"}):
            self.assertEqual(cand.bound_cap(), 900)

    def test_the_graph_sweep_bounds_old_spans_and_dead_rows(self):
        eid = self.ent("Hollin Spar")
        gone = self.ent("Garnet Row")
        m1 = self.sight("Hollin Spar", NOW, entity_id=eid,
                        span="Hollin Spar shipped the order")
        m2 = self.sight("Hollin Spar", NOW + DAY, entity_id=eid,
                        span="Hollin Spar again")
        self.sight("Garnet Row", NOW, entity_id=gone, span="x")
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE entity_mentions SET created_at = ? WHERE id = ?",
                (time.time() - 40 * DAY, m1))
            # A sighting left bound to a row that is gone (written before
            # this fix, or by a path that skipped the check).
            self.store._conn.execute("DELETE FROM entities WHERE id = ?",
                                     (gone,))
            self.store._conn.commit()
        out = cand.sweep_expired(self.store, bound=True)
        self.assertEqual(out["bound"]["orphans"], 1)
        self.assertEqual(out["bound"]["spans"], 1)
        rows = {r["id"]: r for r in self.store.entity_mentions()}
        self.assertIsNone(rows[m1]["span"])
        self.assertEqual(rows[m1]["name_key"], "hollin spar")
        self.assertEqual(rows[m2]["span"], "Hollin Spar again")
        # The sweep on every record does not do it.
        self.assertNotIn("bound", cand.sweep_expired(self.store))

    def test_the_entity_time_index_exists(self):
        with self.store._lock:
            idx = {r["name"] for r in self.store._conn.execute(
                "PRAGMA index_list(entity_mentions)").fetchall()}
            plan_ = " ".join(str(tuple(r)) for r in self.store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM entity_mentions "
                "WHERE entity_id = 1 ORDER BY observed_at DESC, id DESC "
                "LIMIT 500").fetchall())
        self.assertIn("idx_entity_mentions_entity_seen", idx)
        self.assertIn("idx_entity_mentions_span_age", idx)
        self.assertNotIn("TEMP B-TREE", plan_.upper())


# ------------------------------------------------------------------ A6 --
class ConcurrentMintTests(_Case):
    def test_a_mint_after_a_delete_or_merge_never_binds_a_dead_id(self):
        r = Resolver(store=self.store)
        a = self.ent("Penhallow Freight", "org")
        m = r.match_entity("Penhallow Freight", "org")
        self.assertEqual((m.status, m.entity_id), ("existing", a))
        self.store.delete_entity(a)              # a request thread
        got = r.mint_entity(m, "org", ts=NOW, kind_source="model")
        self.assertTrue(got)
        self.assertNotEqual(got, a)
        self.assertIsNotNone(self.store.get_entity(got))
        # Forgotten in between: nothing at all.
        m = r.match_entity("Penhallow Freight", "org")
        self.store.delete_entity(got, tombstone=True, reason="forget")
        self.assertEqual(r.mint_entity(m, "org", ts=NOW,
                                       kind_source="model"), 0)
        # Merged in between: the survivor answers.
        s = self.ent("Ravelin Holdings", "org")
        b = self.ent("Ravelin Hold", "org")
        m = r.match_entity("Ravelin Hold", "org")
        self.store.merge_entities(s, b, actor="user")
        self.assertEqual(r.mint_entity(m, "org", ts=NOW,
                                       kind_source="model"), s)

    def test_the_ledger_never_binds_a_dead_id(self):
        a = self.ent("Calder Works", "org")
        self.sight("Calder Works", NOW)          # pending under the name
        self.store.delete_entity(a)
        mid = self.sight("Calder Works", NOW + DAY, entity_id=a)
        row = next(r for r in self.store.entity_mentions() if r["id"] == mid)
        self.assertEqual((row["status"], row["entity_id"]), ("pending", None))
        out = self.store.adopt_entity_candidates("calder works", a, ts=NOW)
        self.assertEqual(out["adopted"], 0)
        self.assertEqual({r["status"] for r in self.store.entity_mentions()},
                         {"pending"})
        b = self.ent("Dunmore Cut", "org")
        self.store.delete_entity(b, tombstone=True, reason="forget")
        self.assertEqual(self.sight("Dunmore Cut", NOW, entity_id=b), 0)


# ------------------------------------------------------------------ A7 --
class RunGuardTests(_Case):
    def test_a_second_process_cannot_start_a_run_beside_the_first(self):
        other = _mk(self.tmp)
        self.addCleanup(other.close)
        self.assertTrue(self.store.entity_repair_start_run(
            "auto-1", mode="auto", exclusive_s=3600))
        self.assertFalse(other.entity_repair_start_run(
            "auto-2", mode="auto", exclusive_s=3600))
        self.store.entity_repair_finish_run("auto-1", {})
        self.assertTrue(other.entity_repair_start_run(
            "auto-3", mode="auto", exclusive_s=3600))

    def test_apply_refuses_when_another_run_started_after_its_check(self):
        e = self.ent("Ashgrove Pump", "project", None)
        self.minted_by(e, NOTE)
        p = self.plan()
        other = _mk(self.tmp)
        self.addCleanup(other.close)
        # The other process started between this one's check and its insert.
        real = self.store.entity_repair_active_run
        calls = []

        def racing(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                other.entity_repair_start_run("reviewed-x", mode="reviewed")
                return None
            return real(*a, **k)
        with patch.object(self.store, "entity_repair_active_run", racing):
            with self.assertRaises(er.PlanRefused) as cm:
                er.apply(self.store, p, mode="auto")
        self.assertIn("in progress", cm.exception.reason)
        self.assertFalse(self.store.entity_is_hidden(e))
        self.assertEqual(self.store.entity_repair_log(), [])


# ------------------------------------------------------------------ A8 --
class InboxTests(_Case):
    def proposal(self, a) -> str:
        pid = er.propose(self.store, a, run_id="t")
        return f"entityfix:{pid}"

    def test_an_inbox_drop_is_an_undoable_hide(self):
        e = self.ent("Fallowmere Kiln", "org")
        a = {"entity_id": e, "name": "Fallowmere Kiln", "kind": "org",
             "action": "drop", "rule": "common_noun", "tombstone": True,
             "route": "review", "state": er._snapshot(
                 {**self.store.get_entity(e), "hidden": False})}
        item = self.proposal(a)
        title = next(i for i in inbox._entity_repair_items(self.store, NOW)
                     if i["id"] == item)["title"]
        self.assertIn("Hide", title)
        self.assertIn("undo", title)
        with patch.object(er, "check", return_value=[]):
            out = inbox.act(self.store, item, "yes")
        self.assertTrue(out["ok"], out)
        self.assertIsNotNone(self.store.get_entity(e))
        self.assertTrue(self.store.entity_is_hidden(e))
        self.assertFalse(self.store.is_entity_tombstoned("Fallowmere Kiln"))
        self.assertIn("undo_token", out)
        inbox.undo(out["undo_token"])
        self.assertFalse(self.store.entity_is_hidden(e))
        # An Inbox hide is the user's call: real sightings do not lift it.
        self.assertIsNone(self.store.entity_repair_hold(e))

    def test_merges_say_they_cannot_be_undone_and_back_up_first(self):
        s = self.ent("Tidewell Rigging", "org")
        b = self.ent("Tidewell Rig", "org")
        a = {"entity_id": b, "name": "Tidewell Rig", "kind": "org",
             "action": "merge", "rule": "merge_same_name", "into": s,
             "into_name": "Tidewell Rigging", "route": "review",
             "state": er._snapshot({**self.store.get_entity(b),
                                    "hidden": False}),
             "into_state": er._snapshot({**self.store.get_entity(s),
                                         "hidden": False})}
        item = self.proposal(a)
        title = next(i for i in inbox._entity_repair_items(self.store, NOW)
                     if i["id"] == item)["title"]
        self.assertIn("cannot be undone", title)
        with patch.object(er, "check", return_value=[]):
            out = inbox.act(self.store, item, "yes")
        self.assertTrue(out["ok"], out)
        self.assertIsNone(self.store.get_entity(b))
        baks = list(Path(self.tmp).glob("quill.db.bak-entity-repair-inbox-*"))
        self.assertEqual(len(baks), 1)
        self.assertNotIn("undo_token", out)
        page = (ROOT / "app" / "api" / "profile_page.py").read_text()
        self.assertIn("A merge cannot be undone.", page)


# ------------------------------------------------------------------ A9 --
class BackupRetentionTests(_Case):
    def test_only_the_newest_backups_of_each_kind_are_kept(self):
        db = Path(self.store.db_path)
        made = []
        for i in range(5):
            for tag in ("bak-entity-repair-auto", "bak-seat-vocab"):
                f = db.with_name(f"{db.name}.{tag}-2026010{i}T000000Z")
                f.write_bytes(b"x")
                os.utime(f, (NOW + i, NOW + i))
                made.append(f)
        wal = db.with_name(f"{db.name}.bak-entity-repair-auto-x-wal")
        wal.write_bytes(b"x")
        removed = er.prune_backups(db)
        self.assertEqual(len(removed), 4)
        left = sorted(p.name for p in db.parent.glob(f"{db.name}.bak-*"))
        self.assertEqual(len([n for n in left if "seat-vocab" in n]), 3)
        self.assertEqual(len([n for n in left if "entity-repair" in n
                              and not n.endswith("-wal")]), 3)
        self.assertIn(f"{db.name}.bak-seat-vocab-20260104T000000Z", left)
        self.assertNotIn(f"{db.name}.bak-seat-vocab-20260100T000000Z", left)
        self.assertTrue(wal.exists())
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_BACKUPS_KEEP": "1"}):
            er.prune_backups(db)
        self.assertEqual(len(list(db.parent.glob(f"{db.name}.bak-seat-*"))),
                         1)

    def test_a_refused_automatic_run_does_not_copy_again(self):
        e = self.ent("Brackenholt Survey", "project", None)
        self.minted_by(e, NOTE)
        db = Path(self.store.db_path)
        problem = [{"entity_id": e, "problem": "stale", "fields": ["kind"]}]
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}), \
                patch.object(er, "check", return_value=problem):
            first = er.auto_run(self.store)
            self.assertEqual(first["why"], "refused")
            self.assertEqual(list(db.parent.glob(f"{db.name}.bak-*")), [])
            with patch.object(self.store, "backup_to",
                              side_effect=AssertionError("copied again")):
                again = er.auto_run(self.store)
        self.assertEqual(again["why"], "refused_recently")
        self.assertIsNone(er.last_auto_run(self.store))
        # After the wait it tries again (and, the plan fitting, runs).
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1",
                                     "QUILL_ENTITY_REPAIR_BACKUP_RETRY_S": "0"}):
            later = er.auto_run(self.store)
        self.assertTrue(later["ran"], later)


# ----------------------------------------------------------------- A10 --
class ForgetScopeTests(_Case):
    def test_a_users_merge_ties_the_absorbed_name_and_nothing_else(self):
        s = self.ent("Oakhurst Analytics")
        b = self.ent("Oakhurst")
        self.store.touch_entity(b, NOW, alias="Oakhirst")     # a fuzzy fold
        self.store.merge_entities(s, b, actor="user")
        by = {r["alias"]: r for r in self.store.list_entity_aliases(
            entity_id=s)}
        self.assertEqual(by["Oakhurst"]["confirmed"], True)
        with self.store._lock:
            marks = {r["alias"]: r["confirmed_by"] for r in
                     self.store._conn.execute(
                         "SELECT alias, confirmed_by FROM entity_aliases "
                         "WHERE entity_id = ?", (s,)).fetchall()}
        self.assertEqual(marks, {"Oakhurst": "user", "Oakhirst": "auto"})
        self.store.delete_entity(s, tombstone=True, reason="forget")
        self.assertTrue(self.store.is_entity_tombstoned("Oakhurst"))
        self.assertFalse(self.store.is_entity_tombstoned("Oakhirst"))

    def test_existing_confirmed_aliases_count_as_auto_unless_the_user_said(self):
        e = self.ent("Hesketh Mills")
        rows = {}
        for alias, src in (("HM", "normalized"), ("Hesketh", "context"),
                           ("H Mills", "user")):
            rows[alias] = self.store.upsert_entity_alias(
                e, alias, alias.lower(), source=src, confirmed=True, ts=NOW)
        self.store.add_decision({
            "id": "d1", "decided_at": NOW, "surface": "entities.alias",
            "subject": f"entity:{e}", "verdict": "approved", "actor": "user",
            "meta": {"alias_id": rows["Hesketh"]["id"]}})
        self.store.close()
        con = sqlite3.connect(Path(self.tmp) / "quill.db")
        con.execute("ALTER TABLE entity_aliases DROP COLUMN confirmed_by")
        con.commit()
        con.close()
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        with self.store._lock:
            marks = {r["alias"]: r["confirmed_by"] for r in
                     self.store._conn.execute(
                         "SELECT alias, confirmed_by FROM entity_aliases"
                     ).fetchall()}
        self.assertEqual(marks, {"HM": "auto", "Hesketh": "user",
                                 "H Mills": "user"})

    def test_a_forget_leaves_another_rows_sightings_under_the_same_key(self):
        a = self.ent("Dellmont-Capital")
        b = self.ent("Dellmont Capital")
        self.sight("Dellmont-Capital", NOW, entity_id=a)
        self.sight("Dellmont Capital", NOW + DAY, entity_id=b)
        key = entity_name_key("Dellmont Capital")
        self.assertEqual(entity_name_key("Dellmont-Capital"), key)
        with self.store._lock:
            self.store._conn.execute(
                "INSERT INTO entity_candidate_summaries (name_hash, mentions, "
                "sessions, updated_at) VALUES (?, 3, 2, ?)",
                (self.store.entity_key_hash(key), time.time()))
            self.store._conn.commit()
        self.store.delete_entity(a, tombstone=True, reason="forget")
        self.assertEqual(len(self.store.entity_mentions(entity_id=b)), 1)
        self.assertIsNotNone(self.store.entity_candidate_summary(key))


# ----------------------------------------------------------------- A11 --
class CandidateListTests(_Case):
    def test_the_list_takes_a_limit_and_says_how_many_wait(self):
        for i in range(260):
            self.sight(f"Zephyrine Holding {i:03d}", NOW + i)
        totals: dict = {}
        rows = cand.list_candidates(self.store, limit=200, totals=totals)
        self.assertEqual((len(rows), totals["pending"]), (200, 260))
        self.assertEqual(rows[0]["name"], "Zephyrine Holding 259")
        r = self.client.get("/entities/candidates").json()
        self.assertEqual((len(r["candidates"]), r["pending"], r["limit"]),
                         (200, 260, 200))
        r = self.client.get("/entities/candidates", params={"limit": 5}).json()
        self.assertEqual(len(r["candidates"]), 5)
        self.assertEqual(len(cand.list_candidates(self.store, limit=None)), 260)


# ----------------------------------------------------------------- A12 --
class MetricsTests(_Case):
    def test_one_forget_is_one_row_and_stays_counted(self):
        e = self.ent("Kelp Line Tours")
        for a in ("Kelp Line", "KLT", "Kelpline"):
            self.client.post(f"/entities/{e}/alias", json={"alias": a})
        self.ent("Harbor Line")
        self.client.post(f"/entities/{e}/forget")
        self.assertEqual(self.store.entity_tombstone_count(), 4)
        m = em.compute(self.store)
        self.assertEqual(m["forget"]["forgotten"], 1)
        self.assertEqual(m["forget"]["rate"], 0.5)
        self.client.post("/entities", json={"name": "Kelp Line Tours",
                                            "kind": "org"})
        self.assertEqual(em.compute(self.store)["forget"]["forgotten"], 1)

    def test_repair_drops_count_rows(self):
        a = self.ent("Glimmer Patch")
        b = self.ent("Slate Notch")
        for al in ("GP", "Glimmer"):
            self.store.upsert_entity_alias(a, al, al.lower(), source="user",
                                           confirmed=True, ts=NOW)
        self.store.delete_entity(a, tombstone=True, reason="repair:x",
                                 actor="repair")
        self.assertEqual(self.store.entity_tombstone_count(), 3)
        self.store.delete_entity(b, tombstone=False, reason="repair:note_only",
                                 actor="repair")
        self.store.delete_entity(self.ent("Plain Delete"))
        m = em.compute(self.store)
        self.assertEqual((m["forget"]["forgotten"],
                          m["forget"]["repair_drops"]), (0, 2))

    def test_promoted_counts_the_gate_only(self):
        # A filing and a manual add adopt what was pending, and are no
        # promotion; only the gate's own mint is.
        self.sight("Filed Or Added", NOW - DAY)
        self.ent("Filed Or Added", "project", "filing")
        self.sight("Hand Added", NOW - DAY)
        cand.add_manual(self.store, "Hand Added", "org")
        self.assertEqual(em.compute(self.store)["candidates"]["promoted"], 0)


# ----------------------------------------------------------------- A13 --
class NoOpTests(_Case):
    def test_an_edit_that_changes_nothing_records_nothing(self):
        e = self.ent("Osprey Analytics")
        self.client.post(f"/entities/{e}/alias", json={"alias": "Osprey"})
        n = len(self.store.list_decisions(limit=500))
        for path, body in (("alias", {"alias": "Osprey"}),
                           ("alias", {"alias": "osprey"}),
                           ("alias", {"alias": "Osprey Analytics"}),
                           ("rename", {"name": "Osprey Analytics"}),
                           ("detail", {"key": "url", "value": ""})):
            r = self.client.post(f"/entities/{e}/{path}", json=body)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json().get("unchanged"), (path, body))
        self.assertEqual(len(self.store.list_decisions(limit=500)), n)
        # A real change still records.
        self.client.post(f"/entities/{e}/rename", json={"name": "Osprey Labs"})
        self.assertEqual(len(self.store.list_decisions(limit=500)), n + 1)


# ----------------------------------------------------------------- A14 --
class DeletePersonTests(_Case):
    def test_a_deleted_person_is_never_found_again(self):
        pid = self.store.resolve_person("Tomas Varga", ts=NOW)
        self.store.upsert_contact_point(
            person_id=pid, type_="email", value_display="tv@saltreef.example",
            value_normalized="tv@saltreef.example", confidence=0.9,
            attribution_method="email_header",
            verification_status="attributed", source_event_id=None,
            evidence_quote=None, discourse_role=None, ts=NOW,
            created_by="system", pipeline_version="t")
        self.assertEqual(self.store.find_person_by_contact(
            "email", "tv@saltreef.example"), pid)
        self.store.delete_person(pid)
        self.assertIsNone(self.store.find_person_by_contact(
            "email", "tv@saltreef.example"))
        self.assertEqual(self.count(
            "SELECT COUNT(*) FROM person_contact_points WHERE person_id = ?",
            pid), 0)
        self.assertEqual(self.count(
            "SELECT COUNT(*) FROM kg_node_keys WHERE node_type = 'person' "
            "AND node_id = ?", pid), 0)


# ----------------------------------------------------------------- A15 --
class DressedDuplicateTests(_Case):
    def test_a_dressed_twin_is_merged_not_dropped(self):
        plain = self.ent("Dockside Refit", "project", None)
        dressed = self.ent("Dockside Refit (project)", "project", None)
        lone = self.ent("Gullwing Ramp (project)", "project", None)
        p = self.plan()
        x = self.one(p, dressed)
        self.assertEqual((x["action"], x["into"], x["route"]),
                         ("merge", plain, "review"))
        y = self.one(p, lone)
        self.assertEqual((y["action"], y["rule"]), ("drop", "implausible"))


# ----------------------------------------------------------------- A16 --
class TypedEvidenceTests(_Case):
    def chat(self, text, day) -> int:
        return self.event(CHAT, raw=text, ts=NOW + day * DAY)

    def test_typed_names_count_on_a_store_from_before_the_ledger(self):
        twice = self.ent("dock fenders", "project", None)
        once = self.ent("bilge pumps", "project", None)
        never = self.ent("stern lights", "project", None)
        self.chat("order more dock fenders for slip 3", 2)
        self.chat("the dock fenders came in", 4)
        self.chat("check the bilge pumps", 2)
        p = self.plan()
        # Typed in two sessions: never dropped (its lowercase "project" kind
        # is still a question for rule 8).
        self.assertNotIn("drop", {a["action"] for a in self.acts(p, twice)})
        x = self.one(p, once)
        self.assertEqual((x["action"], x["rule"], x["tombstone"]),
                         ("drop", "common_noun", False))
        self.assertEqual(x["evidence"]["typed_events"], 1)
        y = self.one(p, never)
        self.assertEqual((y["action"], y["tombstone"]), ("drop", True))

    def test_an_automatic_hide_lifts_when_the_user_types_it_twice(self):
        e = self.ent("mooring lines", "project", None)
        res = er.auto_run(self.store, force=True)
        self.assertTrue(res["ran"], res)
        self.assertTrue(self.store.entity_is_hidden(e))
        self.assertEqual(self.store.entity_repair_hold(e)["rule"],
                         "common_noun")
        for day in (10, 12):
            ev = self.chat(f"order more mooring lines, day {day}", day)
            ctx = cand.context(self.store, anchor=ev, now=NOW + day * DAY,
                               event_source=CHAT,
                               source_class="direct_message", mint_ok=True,
                               text=f"order more mooring lines, day {day}")
            cand.record_bound(self.store, "mooring lines", e, ctx,
                              kind="project")
        self.assertFalse(self.store.entity_is_hidden(e))

    def test_a_value_hide_is_no_hold(self):
        e = self.ent("$4,900", "idea", None)
        res = er.auto_run(self.store, force=True)
        self.assertTrue(res["ran"], res)
        self.assertTrue(self.store.entity_is_hidden(e))
        self.assertIsNone(self.store.entity_repair_hold(e))


# ----------------------------------------------------------------- A17 --
class ReportNameTests(_Case):
    def test_reports_never_overwrite_and_undo_has_its_own(self):
        script = _script()
        e = self.ent("Quarrel Point", "project", None)
        self.minted_by(e, NOTE)
        db = str(self.store.db_path)
        self.store.close()
        out = io.StringIO()
        with patch.object(script.time, "strftime",
                          lambda *a, **k: "20990101T000000Z"), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(script.main(["--db", db]), 0)
            self.assertEqual(script.main(["--db", db]), 0)
            self.assertEqual(script.main(["--db", db, "--auto"]), 0)
            auto = json.loads(sorted(Path(self.tmp).glob(
                "entity_repair_auto_*.json"))[0].read_text())["auto"]
            self.assertEqual(script.main(["--db", db, "--undo",
                                          auto["run_id"]]), 0)
        names = sorted(p.name for p in Path(self.tmp).glob("entity_repair_*"))
        self.assertEqual(names, ["entity_repair_20990101T000000Z-1.json",
                                 "entity_repair_20990101T000000Z.json",
                                 "entity_repair_auto_20990101T000000Z.json",
                                 "entity_repair_undo_20990101T000000Z.json"])
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)


# ----------------------------------------------------------------- A18 --
class GivenNameTests(_Case):
    def test_a_given_name_is_asked_and_never_tombstoned(self):
        self.store.resolve_person("Ruby Chen", ts=NOW)
        e = self.ent("Ruby", "idea")
        full = self.ent("Ruby Chen", "idea")
        p = self.plan()
        x = self.one(p, e)
        self.assertEqual((x["action"], x["rule"], x["route"], x["tombstone"]),
                         ("drop", "person_given_name", "review", False))
        y = self.one(p, full)
        self.assertEqual((y["rule"], y["tombstone"]), ("person_name", True))


if __name__ == "__main__":
    unittest.main()
