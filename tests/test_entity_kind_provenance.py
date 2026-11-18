"""Entity model, Phase 0b: every kind records who set it.

`entities.kind_source` is one of model < rule < filing < user. A write changes
the kind only when its source outranks the one on the row, so the extractor
cannot relabel what a filing decided and a cleanup rule cannot undo a kind the
user chose. `entities.updated_at` moves on every edit that does not change a
count, so the Orgs & tools tab and the map refetch.

Every test runs against a real Store on a temp db.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`: a module
# first imported under that patch would keep the stand-in for good.
import app.api.routes  # noqa: F401
import app.services.extractor  # noqa: F401
import app.services.exhaust_ingest  # noqa: F401
import app.services.onboarding  # noqa: F401
import app.services.people_pipeline  # noqa: F401
import app.services.person_adjudicator  # noqa: F401
from app.services.resolution import Resolver
from app.storage import Store

NOW = 1_700_000_000.0
NEW_COLS = ("kind_source", "kind_confidence", "updated_at",
            "parent_id", "parent_source")


def _mk(tmp: str, **kw) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio",
                 **kw)


def _make_legacy(tmp: str) -> None:
    """Turn the db in `tmp` back into one from before this phase: no
    provenance columns, no parent index, no tombstone table."""
    raw = sqlite3.connect(Path(tmp) / "t.db")
    raw.execute("DROP INDEX IF EXISTS idx_entities_parent")
    for col in NEW_COLS:
        raw.execute(f"ALTER TABLE entities DROP COLUMN {col}")
    raw.execute("DROP TABLE IF EXISTS entity_tombstones")
    raw.commit()
    raw.close()


def _cols(store: Store) -> set[str]:
    with store._lock:
        return {r["name"] for r in store._conn.execute(
            "PRAGMA table_info(entities)").fetchall()}


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_kind_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)

    def kind(self, eid: int) -> tuple:
        row = self.store.get_entity(eid)
        return (row["kind"], row["kind_source"])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_kind_mig_")
        store = _mk(self.tmp)
        self.eid = store.resolve_entity("Dell Capital", "org", ts=NOW)
        store.close()
        _make_legacy(self.tmp)

    def test_fresh_db_has_the_columns_index_and_table(self):
        tmp = tempfile.mkdtemp(prefix="quill_kind_fresh_")
        store = _mk(tmp)
        try:
            self.assertTrue(set(NEW_COLS) <= _cols(store))
            with store._lock:
                names = {r["name"] for r in store._conn.execute(
                    "SELECT name FROM sqlite_master").fetchall()}
            self.assertIn("idx_entities_parent", names)
            self.assertIn("entity_tombstones", names)
        finally:
            store.close()

    def test_legacy_db_is_migrated_in_place_and_twice_is_harmless(self):
        raw = sqlite3.connect(Path(self.tmp) / "t.db")
        before = {r[1] for r in raw.execute("PRAGMA table_info(entities)")}
        raw.close()
        self.assertFalse(set(NEW_COLS) & before, "fixture is not legacy")

        for _ in range(2):          # second open: nothing left to add
            store = _mk(self.tmp)
            try:
                self.assertTrue(set(NEW_COLS) <= _cols(store))
                row = store.get_entity(self.eid)
                self.assertEqual((row["name"], row["kind"]),
                                 ("Dell Capital", "org"))
                for col in ("kind_source", "kind_confidence", "parent_id",
                            "parent_source"):
                    self.assertIsNone(row[col], col)
                self.assertEqual(store.entity_tombstone_count(), 0)
            finally:
                store.close()

    def test_legacy_row_ranks_as_model(self):
        store = _mk(self.tmp)
        try:
            # Equal rank (model on a NULL row) does not change the kind...
            store.resolve_entity("Dell Capital", "tool", ts=NOW,
                                 kind_source="model")
            self.assertEqual(store.get_entity(self.eid)["kind"], "org")
            self.assertIsNone(store.get_entity(self.eid)["kind_source"])
            # ...the next rank up does.
            store.resolve_entity("Dell Capital", "tool", ts=NOW,
                                 kind_source="rule")
            row = store.get_entity(self.eid)
            self.assertEqual((row["kind"], row["kind_source"]),
                             ("tool", "rule"))
        finally:
            store.close()

    def test_readonly_store_on_a_legacy_db_still_reads(self):
        ro = _mk(self.tmp, readonly=True)
        try:
            self.assertFalse(set(NEW_COLS) & _cols(ro))
            row = ro.get_entity(self.eid)
            self.assertEqual((row["name"], row["kind"]),
                             ("Dell Capital", "org"))
            for col in ("kind_source", "kind_confidence", "parent_id",
                        "parent_source"):
                self.assertIn(col, row)
                self.assertIsNone(row[col], col)
            listed = ro.all_entities()
            self.assertEqual([e["name"] for e in listed], ["Dell Capital"])
            for col in ("kind_source", "kind_confidence", "parent_id",
                        "parent_source"):
                self.assertIn(col, listed[0])
                self.assertIsNone(listed[0][col], col)
            self.assertTrue(ro.memory_version())
            self.assertFalse(ro.is_entity_tombstoned("Dell Capital"))
        finally:
            ro.close()


# The `entities` table as the commit before this phase created it, and the
# two columns its own migration added later (the shape live seats have).
_BASE_ENTITIES_DDL = """
    CREATE TABLE entities (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_name TEXT    NOT NULL,
        kind           TEXT,
        aliases        TEXT,
        embedding      BLOB,
        first_seen     REAL,
        last_seen      REAL
    )
"""


def _make_base_db(tmp: str, *, migrated_once: bool) -> None:
    """A database written before this phase, built by hand rather than by
    un-migrating a new one: the old `entities` table with three rows (one
    hidden when the table has the column) and nothing else."""
    raw = sqlite3.connect(Path(tmp) / "t.db")
    raw.execute(_BASE_ENTITIES_DDL)
    raw.execute("CREATE UNIQUE INDEX idx_entities_name "
                "ON entities(canonical_name)")
    if migrated_once:
        raw.execute("ALTER TABLE entities ADD COLUMN canonical_id TEXT")
        raw.execute("ALTER TABLE entities ADD COLUMN "
                    "hidden INTEGER NOT NULL DEFAULT 0")
    for name, kind in (("Project Falcon", "project"), ("Acme Corp", "org"),
                       ("Zed", "tool")):
        raw.execute(
            "INSERT INTO entities (canonical_name, kind, aliases, "
            "first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
            (name, kind, '["%s"]' % name, NOW, NOW))
    if migrated_once:
        raw.execute("UPDATE entities SET hidden = 1 "
                    "WHERE canonical_name = 'Zed'")
    raw.commit()
    raw.close()


class BaseSchemaMigrationTests(unittest.TestCase):
    def _open_and_check(self, tmp: str, *, hidden_zed: bool) -> None:
        for _ in range(2):          # the second open has nothing to add
            store = _mk(tmp)
            try:
                self.assertTrue(set(NEW_COLS) <= _cols(store))
                with store._lock:
                    names = {r["name"] for r in store._conn.execute(
                        "SELECT name FROM sqlite_master").fetchall()}
                self.assertIn("idx_entities_parent", names)
                self.assertIn("entity_tombstones", names)
                rows = store.all_entities(include_hidden=True)
                self.assertEqual(
                    [(e["id"], e["name"], e["kind"]) for e in rows],
                    [(1, "Project Falcon", "project"), (2, "Acme Corp", "org"),
                     (3, "Zed", "tool")])
                for e in rows:
                    for col in ("kind_source", "kind_confidence", "parent_id",
                                "parent_source"):
                        self.assertIsNone(e[col], (e["name"], col))
                self.assertEqual(
                    [e["name"] for e in rows if e["hidden"]],
                    ["Zed"] if hidden_zed else [])
                self.assertEqual(store.entity_tombstone_count(), 0)
                self.assertTrue(store.memory_version())
            finally:
                store.close()

    def test_database_from_before_this_phase_is_migrated_with_its_rows(self):
        tmp = tempfile.mkdtemp(prefix="quill_kind_base_")
        _make_base_db(tmp, migrated_once=True)
        self._open_and_check(tmp, hidden_zed=True)

    def test_oldest_table_shape_is_migrated_too(self):
        tmp = tempfile.mkdtemp(prefix="quill_kind_base0_")
        _make_base_db(tmp, migrated_once=False)
        self._open_and_check(tmp, hidden_zed=False)

    def test_old_rows_work_with_the_new_rules_after_migration(self):
        tmp = tempfile.mkdtemp(prefix="quill_kind_base_")
        _make_base_db(tmp, migrated_once=True)
        store = _mk(tmp)
        try:
            # A row from before the columns ranks as a model guess...
            self.assertEqual(store.resolve_entity(
                "acme corp", "tool", ts=NOW, kind_source="model"), 2)
            self.assertEqual(store.get_entity(2)["kind"], "org")
            # ...a filing outranks it, and a forget is lasting.
            self.assertEqual(store.resolve_entity(
                "Zed", "project", ts=NOW, kind_source="filing"), 3)
            row = store.get_entity(3)
            self.assertEqual((row["kind"], row["kind_source"]),
                             ("project", "filing"))
            self.assertTrue(store.set_entity_kind(1, "idea"))
            self.assertEqual(store.get_entity(1)["kind_source"], "user")
            store.delete_entity(2, tombstone=True, reason="forget")
            self.assertEqual(store.resolve_entity("Acme Corp", "org"), 0)
            new = store.resolve_entity("Northwind", "org", ts=NOW,
                                       kind_source="rule")
            self.assertEqual(store.get_entity(new)["kind_source"], "rule")
        finally:
            store.close()

    def test_readonly_open_of_an_unmigrated_database_reads_and_adds_nothing(
            self):
        tmp = tempfile.mkdtemp(prefix="quill_kind_base_ro_")
        _make_base_db(tmp, migrated_once=True)
        # A read-only store needs the other tables to exist: open once
        # writable, then take the new columns and the table away again.
        _mk(tmp).close()
        _make_legacy(tmp)
        ro = _mk(tmp, readonly=True)
        try:
            self.assertFalse(set(NEW_COLS) & _cols(ro))
            self.assertEqual([e["name"] for e in ro.all_entities()],
                             ["Project Falcon", "Acme Corp"])
            self.assertEqual(len(ro.all_entities(include_hidden=True)), 3)
            self.assertIsNone(ro.get_entity(2)["kind_source"])
            self.assertEqual(ro.find_entity_exact("zed"), 3)
            self.assertFalse(ro.is_entity_tombstoned("Zed"))
            self.assertEqual(ro.entity_tombstone_count(), 0)
            self.assertTrue(ro.memory_version())
        finally:
            ro.close()
        raw = sqlite3.connect(Path(tmp) / "t.db")
        try:
            after = {r[1] for r in raw.execute("PRAGMA table_info(entities)")}
            tables = {r[0] for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            raw.close()
        self.assertFalse(set(NEW_COLS) & after)
        self.assertNotIn("entity_tombstones", tables)


class RankRuleTests(_StoreCase):
    def test_new_row_stores_source_and_confidence(self):
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW,
                                        kind_source="model",
                                        kind_confidence=0.62)
        row = self.store.get_entity(eid)
        self.assertEqual(row["kind"], "org")
        self.assertEqual(row["kind_source"], "model")
        self.assertAlmostEqual(row["kind_confidence"], 0.62)
        self.assertIsNone(row["parent_id"])
        self.assertIsNone(row["parent_source"])
        listed = {e["id"]: e for e in self.store.all_entities()}[eid]
        self.assertEqual(listed["kind_source"], "model")
        self.assertAlmostEqual(listed["kind_confidence"], 0.62)
        self.assertIn("parent_id", listed)
        self.assertIn("parent_source", listed)

    def test_unsourced_mint_is_stored_unsourced_and_ranks_lowest(self):
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.assertEqual(self.kind(eid), ("org", None))
        self.assertEqual(Store.kind_source_rank(None), 1)
        self.assertEqual(Store.kind_source_rank("nonsense"), 1)
        self.assertEqual(
            [Store.kind_source_rank(s)
             for s in ("model", "rule", "filing", "user")], [1, 2, 3, 4])

    def test_each_higher_rank_changes_the_kind(self):
        eid = self.store.resolve_entity("Atlas", "idea", ts=NOW,
                                        kind_source="model")
        for kind, source in (("tool", "rule"), ("project", "filing"),
                             ("org", "user")):
            self.assertEqual(
                self.store.resolve_entity("Atlas", kind, ts=NOW,
                                          kind_source=source), eid)
            self.assertEqual(self.kind(eid), (kind, source))

    def test_equal_or_lower_rank_never_changes_the_kind(self):
        for held in ("model", "rule", "filing", "user"):
            name = f"Thing {held}"
            eid = self.store.resolve_entity(name, "org", ts=NOW,
                                            kind_source=held,
                                            kind_confidence=0.9)
            held_rank = Store.kind_source_rank(held)
            for source in (None, "model", "rule", "filing", "user"):
                if Store.kind_source_rank(source) > held_rank:
                    continue
                self.assertEqual(
                    self.store.resolve_entity(name, "tool", ts=NOW + 5,
                                              kind_source=source,
                                              kind_confidence=0.1), eid)
                row = self.store.get_entity(eid)
                self.assertEqual((row["kind"], row["kind_source"]),
                                 ("org", held), (held, source))
                self.assertAlmostEqual(row["kind_confidence"], 0.9)

    def test_same_kind_from_a_higher_rank_upgrades_the_source_only(self):
        eid = self.store.resolve_entity("Atlas", "project", ts=NOW,
                                        kind_source="model",
                                        kind_confidence=0.4)
        self.store.resolve_entity("Atlas", "project", ts=NOW,
                                  kind_source="filing", kind_confidence=1.0)
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"]),
                         ("project", "filing"))
        self.assertAlmostEqual(row["kind_confidence"], 1.0)
        # And now the model cannot relabel it.
        self.store.resolve_entity("Atlas", "tool", ts=NOW,
                                  kind_source="model")
        self.assertEqual(self.kind(eid), ("project", "filing"))

    def test_a_call_without_a_kind_never_changes_kind_or_source(self):
        eid = self.store.resolve_entity("Atlas", "project", ts=NOW,
                                        kind_source="model")
        for kind in (None, "", "   "):
            self.assertEqual(
                self.store.resolve_entity("Atlas", kind, ts=NOW + 9,
                                          kind_source="user"), eid)
            self.assertEqual(self.kind(eid), ("project", "model"))
        # last_seen still moves: the call is a sighting.
        self.assertEqual(self.store.get_entity(eid)["last_seen"], NOW + 9)

    def test_a_label_that_is_not_a_kind_is_no_vote_on_an_existing_row(self):
        # normalize_entity_kind turns any unknown label into "idea". At a
        # high rank that would overwrite a good kind and lock the row.
        org = self.store.resolve_entity("Northwind", "org", ts=NOW,
                                        kind_source="rule")
        proj = self.store.resolve_entity("Sparrow 1.0", "project", ts=NOW,
                                         kind_source="filing")
        for label in ("client", "other", "?", "unknown", "none", "wizard"):
            self.assertEqual(self.store.resolve_entity(
                "Northwind", label, ts=NOW, kind_source="user"), org, label)
            self.assertEqual(self.kind(org), ("org", "rule"), label)
            self.assertEqual(Resolver(store=self.store).resolve_entity(
                "Sparrow 1.0", label, ts=NOW, kind_source="user"), proj, label)
            self.assertEqual(self.kind(proj), ("project", "filing"), label)
        # A label that maps onto a kind is a vote ("company" -> org), and
        # so is an explicit "idea".
        self.store.resolve_entity("Sparrow 1.0", "company", ts=NOW,
                                  kind_source="user")
        self.assertEqual(self.kind(proj), ("org", "user"))
        self.store.resolve_entity("Northwind", "idea", ts=NOW,
                                  kind_source="filing")
        self.assertEqual(self.kind(org), ("idea", "filing"))
        # A new row is still minted as an idea under such a label.
        new = self.store.resolve_entity("Glossary", "client", ts=NOW,
                                        kind_source="user")
        self.assertEqual(self.kind(new), ("idea", "user"))

    def test_unknown_source_and_unreadable_confidence_are_stored_as_none(self):
        eid = self.store.resolve_entity("Zed", "tool", ts=NOW,
                                        kind_source="wizard",
                                        kind_confidence="high")
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"],
                          row["kind_confidence"]), ("tool", None, None))
        # Case and spacing in a source are forgiven; it still ranks.
        self.store.resolve_entity("Zed", "org", ts=NOW, kind_source=" Rule ")
        self.assertEqual(self.kind(eid), ("org", "rule"))
        # An unknown source ranks as a model: it does not beat the rule.
        self.store.resolve_entity("Zed", "place", ts=NOW,
                                  kind_source="wizard")
        self.assertEqual(self.kind(eid), ("org", "rule"))
        self.assertFalse(self.store.set_entity_kind(eid, "place",
                                                    source="wizard"))
        self.assertEqual(self.kind(eid), ("org", "rule"))

    def test_set_entity_kind_on_an_unknown_entity_or_a_non_kind(self):
        self.assertFalse(self.store.set_entity_kind(424242, "org"))
        eid = self.store.resolve_entity("Zed", "tool", ts=NOW)
        self.assertFalse(self.store.set_entity_kind(eid, "wizard"))
        self.assertEqual(self.kind(eid), ("tool", None))
        self.assertEqual(self.store.all_entities(include_hidden=True)[0]["id"],
                         eid)

    def test_set_entity_kind_defaults_to_the_user_and_refuses_lower_ranks(self):
        eid = self.store.resolve_entity("Atlas", "idea", ts=NOW,
                                        kind_source="filing")
        self.assertFalse(self.store.set_entity_kind(eid, "tool",
                                                    source="model"))
        self.assertFalse(self.store.set_entity_kind(eid, "tool",
                                                    source="rule"))
        self.assertEqual(self.kind(eid), ("idea", "filing"))
        # Equal rank may write (a second filing decision replaces the first).
        self.assertTrue(self.store.set_entity_kind(eid, "project",
                                                   source="filing"))
        self.assertEqual(self.kind(eid), ("project", "filing"))
        self.assertTrue(self.store.set_entity_kind(eid, "org"))
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "user"))
        self.assertAlmostEqual(row["kind_confidence"], 1.0)
        for source in ("model", "rule", "filing"):
            self.assertFalse(self.store.set_entity_kind(eid, "tool",
                                                        source=source))
        self.assertEqual(self.kind(eid), ("org", "user"))
        self.assertFalse(self.store.set_entity_kind(eid, "wizard"))
        self.assertFalse(self.store.set_entity_kind(99999, "org"))

    def test_resolver_applies_the_same_rank_rule(self):
        resolver = Resolver(store=self.store)
        eid = resolver.resolve_entity("Northwind", "org", ts=NOW,
                                      kind_source="model",
                                      kind_confidence=0.7)
        self.assertTrue(eid)
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "model"))
        self.assertAlmostEqual(row["kind_confidence"], 0.7)
        # Same rank: the second model reading does not win.
        self.assertEqual(
            resolver.resolve_entity("Northwind", "tool", ts=NOW,
                                    kind_source="model"), eid)
        self.assertEqual(self.kind(eid), ("org", "model"))
        # A rule outranks it.
        self.assertEqual(
            resolver.resolve_entity("northwind", "tool", ts=NOW,
                                    kind_source="rule"), eid)
        self.assertEqual(self.kind(eid), ("tool", "rule"))
        # And the model cannot take it back.
        self.assertEqual(
            resolver.resolve_entity("Northwind", "org", ts=NOW,
                                    kind_source="model"), eid)
        self.assertEqual(self.kind(eid), ("tool", "rule"))


class CallerSourceTests(_StoreCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {
            "QUILL_HEADS": "off", "QUILL_USAGE_LEDGER": "0",
            "QUILL_PEOPLE_V2": "1"})
        env.start()
        self.addCleanup(env.stop)
        from app.services import resolution
        for p in (patch.object(resolution, "resolver",
                               Resolver(store=self.store)),
                  patch("app.storage.get_store", return_value=self.store)):
            p.start()
            self.addCleanup(p.stop)

    def test_filing_upgrades_a_model_row_but_not_a_user_row(self):
        from app.services import projects
        guessed = self.store.resolve_entity("Atlas", "tool", ts=NOW,
                                            kind_source="model")
        chosen = self.store.resolve_entity("Borealis", "org", ts=NOW,
                                           kind_source="user")
        self.assertEqual(
            projects.ensure_filed("Atlas", store=self.store, now=NOW), guessed)
        self.assertEqual(
            projects.ensure_filed("Borealis", store=self.store, now=NOW),
            chosen)
        self.assertEqual(self.kind(guessed), ("project", "filing"))
        self.assertEqual(self.kind(chosen), ("org", "user"))

    def test_extractor_cannot_relabel_a_filed_project(self):
        from app.services import projects
        from app.services.extractor import Extractor
        eid = projects.ensure_filed("Atlas", store=self.store, now=NOW)
        Extractor(store=self.store)._persist_entities(
            {"entities": [{"name": "Atlas", "kind": "tool",
                           "confidence": 0.95}], "relations": []},
            None, NOW, event_source="chat.user", text="we use Atlas")
        self.assertEqual(self.kind(eid), ("project", "filing"))

    def test_extractor_mints_at_model_rank_with_its_confidence(self):
        from app.services.extractor import Extractor
        Extractor(store=self.store)._persist_entities(
            {"entities": [{"name": "Northwind", "kind": "org",
                           "confidence": 0.71}], "relations": []},
            None, NOW, event_source="chat.user",
            text="Northwind signed the pilot")
        eid = self.store.find_entity_exact("Northwind")
        self.assertTrue(eid)
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "model"))
        self.assertAlmostEqual(row["kind_confidence"], 0.71)

    def test_email_domain_org_is_a_rule(self):
        from app.services import people_pipeline as pp
        sample = (Path(__file__).resolve().parent / "fixtures"
                  / "email_capture_sample.txt").read_text(encoding="utf-8")
        out = pp.ingest_email_network(
            sample, store=self.store, event_id=99,
            event_source="desktop.screen",
            window="Inbox - Jane - Outlook", now=NOW)
        self.assertTrue(out.get("ok"), out)
        eid = self.store.find_entity_exact("Acme Robotics")
        self.assertTrue(eid)
        self.assertEqual(self.kind(eid), ("org", "rule"))

    def test_adjudicator_reroute_is_a_model_guess_with_confidence(self):
        from app.services import person_adjudicator
        pid = self.store.insert_person("Initech", ts=NOW)
        person_adjudicator._apply(
            self.store, {"id": pid, "name": "Initech"},
            {"kind": "org", "confidence": 0.93, "reason": "a company"})
        eid = self.store.find_entity_exact("Initech")
        self.assertTrue(eid)
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "model"))
        self.assertAlmostEqual(row["kind_confidence"], 0.93)

    def test_cleanup_rule_cannot_override_the_user_or_a_filing(self):
        from app.services import ambient_cleanup
        ids = {src: self.store.resolve_entity(f"Soft {src}", "project",
                                              ts=NOW, kind_source=src)
               for src in ("model", "rule", "filing", "user")}
        ids["legacy"] = self.store.resolve_entity("Soft legacy", "project",
                                                  ts=NOW)
        plan = {"people": [], "entities": [
            {"id": eid, "name": f"Soft {src}", "kind": "project",
             "action": "reclassify", "to_kind": "tool"}
            for src, eid in ids.items()]}
        out = ambient_cleanup.apply(self.store, plan)
        applied = {int(e["id"]) for e in out["entities"]}
        self.assertEqual(applied,
                         {ids["model"], ids["rule"], ids["legacy"]})
        for src in ("model", "rule", "legacy"):
            self.assertEqual(self.kind(ids[src]), ("tool", "rule"), src)
        self.assertEqual(self.kind(ids["filing"]), ("project", "filing"))
        self.assertEqual(self.kind(ids["user"]), ("project", "user"))

    def test_map_reclassify_is_the_user(self):
        from app.services import graph
        eid = self.store.resolve_entity("Atlas", "project", ts=NOW,
                                        kind_source="filing")
        out = graph.reclassify_constellation_node(
            self.store, f"entity:{eid}", "tool")
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(self.kind(eid), ("tool", "user"))

    def test_onboarding_answer_is_the_user(self):
        from app.services.onboarding import _Ingestor
        eid = self.store.resolve_entity("Atlas", "tool", ts=NOW,
                                        kind_source="filing")
        got = _Ingestor(self.store, set(), NOW).entity("Atlas", "project", [])
        self.assertEqual(got, eid)
        self.assertEqual(self.kind(eid), ("project", "user"))

    def test_person_moved_to_an_entity_by_hand_is_the_user(self):
        pid = self.store.insert_person("Initech", ts=NOW)
        out = self.store.convert_person_to_entity(pid, "org")
        self.assertIsNotNone(out)
        self.assertEqual(self.kind(out["entity_id"]), ("org", "user"))

    def test_person_team_keeps_a_filed_project_and_org_is_the_user(self):
        from app.api import routes
        pid = self.store.resolve_person("Ada Lovelace", ts=NOW)
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW,
                                          kind_source="filing")
        # "team: Atlas" on a person names a project that already exists; it
        # must not turn the project into an org.
        routes._add_detail_side_effects(self.store, pid, "team", "Atlas", NOW)
        self.assertEqual(self.kind(atlas), ("project", "filing"))
        self.assertIn(("member_of", atlas), {
            (e["predicate"], int(e["obj_id"]))
            for e in self.store.relations_of("person", pid)["out"]})
        # A filing or user kind also survives the org field.
        routes._add_detail_side_effects(self.store, pid, "org", "Atlas", NOW)
        self.assertEqual(self.kind(atlas), ("project", "filing"))
        # An org the user types that the model had guessed wrong is fixed.
        guessed = self.store.resolve_entity("Northwind", "tool", ts=NOW,
                                            kind_source="model")
        routes._add_detail_side_effects(self.store, pid, "org", "Northwind",
                                        NOW)
        self.assertEqual(self.kind(guessed), ("org", "user"))
        # A new org is minted at user rank.
        routes._add_detail_side_effects(self.store, pid, "org", "Initech", NOW)
        self.assertEqual(
            self.kind(self.store.find_entity_exact("Initech")),
            ("org", "user"))


class MemoryVersionTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.other = self.store.resolve_entity("Atlas", "project", ts=NOW)

    def moved(self, action) -> bool:
        before = self.store.memory_version()
        time.sleep(0.002)       # updated_at is wall-clock
        action()
        return self.store.memory_version() != before

    def test_idle_store_keeps_its_version(self):
        v = self.store.memory_version()
        self.store.get_entity(self.eid)
        self.store.all_entities(include_hidden=True)
        self.store.find_entity_exact("Dell Capital")
        self.store.is_entity_tombstoned("Dell Capital")
        self.assertEqual(self.store.memory_version(), v)

    def test_rename_moves_the_version(self):
        self.assertTrue(self.moved(
            lambda: self.store.rename_entity(self.eid, "Dell Capital LLC")))

    def test_hide_and_unhide_move_the_version(self):
        self.assertTrue(self.moved(
            lambda: self.store.set_entity_hidden(self.eid, hidden=True)))
        self.assertTrue(self.moved(
            lambda: self.store.set_entity_hidden(self.eid, hidden=False)))

    def test_alias_edits_move_the_version(self):
        # No ts: last_seen does not move, so only updated_at can.
        self.assertTrue(self.moved(
            lambda: self.store.touch_entity(self.eid, None, alias="DellCap")))
        aid = self.store.upsert_entity_alias(
            self.eid, "Dull Capital", "dull capital", source="embedding",
            confirmed=False, ts=NOW)["id"]
        self.assertTrue(self.moved(
            lambda: self.store.confirm_entity_alias(aid, True)))
        self.assertTrue(self.moved(
            lambda: self.store.delete_entity_alias(aid)))

    def test_kind_changes_move_the_version(self):
        self.assertTrue(self.moved(
            lambda: self.store.set_entity_kind(self.eid, "tool")))
        self.assertTrue(self.moved(
            lambda: self.store.resolve_entity("Atlas", "org",
                                              kind_source="user")))

    def test_a_refused_kind_write_leaves_the_version_alone(self):
        self.store.set_entity_kind(self.eid, "tool")          # user
        self.assertFalse(self.moved(
            lambda: self.store.set_entity_kind(self.eid, "org",
                                               source="rule")))
        self.assertFalse(self.moved(
            lambda: self.store.resolve_entity("Dell Capital", "org",
                                              kind_source="model")))


class KindRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_kind_route_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_route_returns_the_stored_kind_and_stamps_the_user(self):
        eid = self.store.resolve_entity("Atlas", "project", ts=NOW,
                                        kind_source="filing")
        r = self.client.post(f"/entities/{eid}/kind",
                             json={"kind": "Software"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        # What is stored ("tool"), not what was typed ("Software").
        self.assertEqual(body["kind"], "tool")
        self.assertEqual(body["kind_source"], "user")
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"]), ("tool", "user"))
        listed = self.client.get("/entities/list").json()["entities"]
        self.assertEqual(
            {e["name"]: e["kind"] for e in listed}["Atlas"], "tool")
        # Automation cannot take it back.
        self.store.resolve_entity("Atlas", "project", ts=NOW,
                                  kind_source="filing")
        self.assertEqual(self.store.get_entity(eid)["kind"], "tool")

    def test_route_rejects_a_label_that_is_not_a_kind(self):
        eid = self.store.resolve_entity("Atlas", "project", ts=NOW)
        r = self.client.post(f"/entities/{eid}/kind", json={"kind": "wizard"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.store.get_entity(eid)["kind"], "project")
        self.assertEqual(
            self.client.post("/entities/99999/kind",
                             json={"kind": "org"}).status_code, 404)

    def test_route_reports_a_refused_write_with_the_stored_kind(self):
        # The route writes at user rank, which nothing outranks today, so
        # the store's refusal is forced here to pin what the route answers
        # when a write does not take: never {"ok": true, "kind": <asked>}.
        eid = self.store.resolve_entity("Atlas", "project", ts=NOW,
                                        kind_source="user")
        with patch.object(self.store, "set_entity_kind", return_value=False):
            r = self.client.post(f"/entities/{eid}/kind",
                                 json={"kind": "tool"})
        self.assertEqual(r.status_code, 409)
        detail = r.json()["detail"]
        self.assertTrue(detail["refused"])
        self.assertEqual(detail["kind"], "project")
        self.assertEqual(detail["kind_source"], "user")
        self.assertEqual(self.store.get_entity(eid)["kind"], "project")

    def test_detail_route_carries_the_provenance_fields(self):
        eid = self.store.resolve_entity("Atlas", "project", ts=NOW,
                                        kind_source="filing",
                                        kind_confidence=1.0)
        body = self.client.get(f"/entities/{eid}").json()
        self.assertEqual(body["kind_source"], "filing")
        self.assertEqual(body["kind_confidence"], 1.0)
        self.assertIn("parent_id", body)
        self.assertIn("parent_source", body)


if __name__ == "__main__":
    unittest.main()
