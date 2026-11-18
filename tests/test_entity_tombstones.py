"""Entity model, Phase 0a: forgetting an entity is lasting.

"Forget" on the Orgs & tools tab writes a tombstone (a hash of the name key,
never the name). Extraction, cleanup rules and background jobs cannot mint the
name again, cannot fold it into a similar-sounding neighbour, and never turn
the refusal (id 0) into a relation endpoint. Only the user brings it back.

Every test runs against a real Store on a temp db.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import kg_keys
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


def _mk(tmp: str) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio")


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_tomb_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)

    def forget(self, name: str, kind: str = "org") -> int:
        eid = self.store.resolve_entity(name, kind, ts=NOW)
        self.assertTrue(eid)
        gone = self.store.delete_entity(eid, tombstone=True, reason="forget")
        self.assertEqual(gone["canonical_name"], name)
        return eid

    def tombstone_rows(self) -> list[dict]:
        with self.store._lock:
            return [dict(r) for r in self.store._conn.execute(
                "SELECT * FROM entity_tombstones").fetchall()]

    def entity_edges_to_zero(self) -> int:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT COUNT(*) AS n FROM relations WHERE "
                "(subj_type='entity' AND subj_id=0) OR "
                "(obj_type='entity' AND obj_id=0)").fetchone()
        return int(row["n"])


class NameKeyTests(unittest.TestCase):
    def test_key_folds_case_punctuation_spacing_and_diacritics(self):
        want = "dell capital"
        for name in ("Dell Capital", "DELL-CAPITAL", "  dell   capital ",
                     "Déll Capital", "Dell.Capital"):
            self.assertEqual(kg_keys.entity_name_key(name), want, name)

    def test_names_without_ascii_letters_keep_a_distinct_key(self):
        # norm_name alone maps both to "", which would make one forgotten
        # Japanese name block every other one.
        a = kg_keys.entity_name_key("東京")
        b = kg_keys.entity_name_key("大阪")
        self.assertTrue(a)
        self.assertTrue(b)
        self.assertNotEqual(a, b)
        self.assertEqual(kg_keys.entity_name_key(""), "")
        self.assertEqual(kg_keys.entity_name_key("  "), "")


class TombstoneStoreTests(_StoreCase):
    def test_forget_blocks_remint_for_every_automatic_source(self):
        self.forget("Dell Capital")
        for source in (None, "model", "rule", "filing"):
            self.assertEqual(
                self.store.resolve_entity("Dell Capital", "org", ts=NOW,
                                          kind_source=source), 0, source)
        self.assertIsNone(self.store.find_entity_exact("Dell Capital"))
        self.assertEqual(self.store.all_entities(include_hidden=True), [])

    def test_only_the_hash_is_stored(self):
        self.forget("Dell Capital")
        rows = self.tombstone_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            row["name_hash"],
            hashlib.sha256(b"dell capital").hexdigest())
        self.assertEqual(row["kind"], "org")
        self.assertEqual(row["reason"], "forget")
        self.assertEqual(row["actor"], "user")
        self.assertIsNotNone(row["created_at"])
        for value in row.values():
            self.assertNotIn("dell", str(value).lower())

    def test_spelling_variants_of_a_forgotten_name_are_blocked(self):
        self.forget("Dell Capital")
        for variant in ("dell capital", "DELL-CAPITAL", "Déll  Capital",
                        " Dell.Capital "):
            self.assertTrue(self.store.is_entity_tombstoned(variant), variant)
            self.assertEqual(
                self.store.resolve_entity(variant, "org", ts=NOW), 0, variant)
        # A different name is not caught by the same key.
        self.assertFalse(
            self.store.is_entity_tombstoned("Dell Capital Partners"))
        self.assertTrue(
            self.store.resolve_entity("Dell Capital Partners", "org", ts=NOW))

    def test_user_aliases_are_tombstoned_but_folds_and_guesses_are_not(self):
        # Fix pass A10: only names the USER tied to the row are forgotten
        # with it. A spelling the resolver folded in ("DellCap") and an
        # alias recurrence confirmed are not the user's word.
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.store.touch_entity(eid, NOW, alias="DellCap")
        self.store.upsert_entity_alias(eid, "DC Fund", "dc fund",
                                       source="user", confirmed=True, ts=NOW)
        self.store.upsert_entity_alias(eid, "Dull Capital", "dull capital",
                                       source="embedding", confirmed=False,
                                       ts=NOW)
        self.store.upsert_entity_alias(eid, "D Capital", "d capital",
                                       source="normalized", confirmed=True,
                                       ts=NOW)
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        self.assertTrue(self.store.is_entity_tombstoned("Dell Capital"))
        self.assertFalse(self.store.is_entity_tombstoned("dellcap"))
        self.assertTrue(self.store.is_entity_tombstoned("DC Fund"))
        self.assertFalse(self.store.is_entity_tombstoned("Dull Capital"))
        self.assertFalse(self.store.is_entity_tombstoned("D Capital"))
        self.assertEqual(self.store.entity_tombstone_count(), 2)
        self.assertEqual(self.store.resolve_entity("DC Fund", "org", ts=NOW), 0)
        self.assertTrue(self.store.resolve_entity("DellCap", "org", ts=NOW))

    def test_plain_delete_leaves_no_tombstone(self):
        # scripts/graph_cleanup.py deletes junk this way: if the thing is
        # real it must be free to come back.
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.store.delete_entity(eid)
        self.assertEqual(self.store.entity_tombstone_count(), 0)
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capital"))
        self.assertTrue(self.store.resolve_entity("Dell Capital", "org", ts=NOW))

    def test_user_source_lifts_the_tombstone_and_mints(self):
        old = self.forget("Dell Capital")
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW,
                                        kind_source="user")
        self.assertTrue(eid)
        self.assertNotEqual(eid, old)
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capital"))
        self.assertEqual(self.store.entity_tombstone_count(), 0)
        self.assertEqual(self.store.get_entity(eid)["kind_source"], "user")
        # Now that it is back, automation binds to it again.
        self.assertEqual(
            self.store.resolve_entity("dell capital", "org", ts=NOW,
                                      kind_source="model"), eid)

    def test_clear_and_count(self):
        self.assertEqual(self.store.entity_tombstone_count(), 0)
        self.assertFalse(self.store.clear_entity_tombstone("Dell Capital"))
        self.forget("Dell Capital")
        self.forget("Acme")
        self.assertEqual(self.store.entity_tombstone_count(), 2)
        self.assertTrue(self.store.clear_entity_tombstone("DELL capital"))
        self.assertFalse(self.store.clear_entity_tombstone("Dell Capital"))
        self.assertEqual(self.store.entity_tombstone_count(), 1)
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capital"))
        self.assertTrue(self.store.is_entity_tombstoned("Acme"))
        self.assertFalse(self.store.is_entity_tombstoned(""))
        self.assertFalse(self.store.clear_entity_tombstone(""))

    def test_tombstones_survive_a_reopen(self):
        self.forget("Dell Capital")
        self.store.close()
        again = _mk(self.tmp)
        try:
            self.assertEqual(again.entity_tombstone_count(), 1)
            self.assertEqual(again.resolve_entity("Dell Capital", "org"), 0)
        finally:
            again.close()

    def test_delete_removes_what_used_to_be_orphaned(self):
        parent = self.store.resolve_entity("Atlas", "project", ts=NOW)
        child = self.store.resolve_entity("M3", "idea", ts=NOW)
        other = self.store.resolve_entity("Linear", "tool", ts=NOW)
        self.store.upsert_entity_alias(parent, "Atlas rollout", "atlas rollout",
                                       confirmed=True, ts=NOW)
        self.store.upsert_entity_alias(other, "Linear app", "linear app",
                                       confirmed=True, ts=NOW)
        self.store.set_node_attr("entity", parent, "status", "active", ts=NOW)
        self.store.set_node_attr("entity", other, "status", "active", ts=NOW)
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE entities SET parent_id = ?, parent_source = 'rule' "
                "WHERE id = ?", (parent, child))
            self.store._conn.commit()

        def count(sql: str, *args) -> int:
            with self.store._lock:
                return int(self.store._conn.execute(sql, args).fetchone()[0])

        keys = "SELECT COUNT(*) FROM kg_node_keys WHERE node_type='entity' AND node_id=?"
        attrs = "SELECT COUNT(*) FROM kg_node_attrs WHERE node_type='entity' AND node_id=?"
        aliases = "SELECT COUNT(*) FROM entity_aliases WHERE entity_id=?"
        self.assertGreater(count(keys, parent), 0)
        self.assertEqual(count(attrs, parent), 1)
        self.assertEqual(count(aliases, parent), 1)
        self.assertEqual(self.store.get_entity(child)["parent_id"], parent)
        v0 = self.store.memory_version()

        self.store.delete_entity(parent)

        self.assertEqual(count(keys, parent), 0)
        self.assertEqual(count(attrs, parent), 0)
        self.assertEqual(count(aliases, parent), 0)
        kid = self.store.get_entity(child)
        self.assertIsNone(kid["parent_id"])
        self.assertIsNone(kid["parent_source"])
        self.assertNotEqual(self.store.memory_version(), v0)
        # Another entity's rows are untouched.
        self.assertGreater(count(keys, other), 0)
        self.assertEqual(count(attrs, other), 1)
        self.assertEqual(count(aliases, other), 1)

    def test_rename_onto_a_forgotten_name_lifts_its_tombstone(self):
        self.forget("Dell Capital")
        eid = self.store.resolve_entity("Dell Capitol", "org", ts=NOW)
        self.assertTrue(self.store.rename_entity(eid, "Dell Capital"))
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capital"))
        self.assertEqual(self.store.find_entity_exact("Dell Capital"), eid)

    def test_readonly_store_from_before_the_table_reads_as_not_forgotten(self):
        self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.store.close()
        raw = sqlite3.connect(Path(self.tmp) / "t.db")
        raw.execute("DROP TABLE entity_tombstones")
        raw.commit()
        raw.close()
        ro = Store(db_path=Path(self.tmp) / "t.db",
                   audio_dir=Path(self.tmp) / "audio", readonly=True)
        try:
            self.assertFalse(ro.is_entity_tombstoned("Dell Capital"))
            self.assertEqual(ro.entity_tombstone_count(), 0)
        finally:
            ro.close()


class TombstoneEdgeCaseTests(_StoreCase):
    def test_empty_names_are_never_minted_forgotten_or_cleared(self):
        self.forget("Dell Capital")
        for name in ("", "   ", None):
            self.assertEqual(self.store.resolve_entity(name, "org"), 0, name)
            self.assertEqual(
                self.store.resolve_entity(name, "org", kind_source="user"),
                0, name)
            self.assertIsNone(Store.entity_tombstone_hash(name), name)
            self.assertFalse(self.store.is_entity_tombstoned(name), name)
            self.assertFalse(self.store.clear_entity_tombstone(name), name)
            self.assertIsNone(self.store.find_entity_exact(name), name)
        self.assertEqual(self.store.all_entities(include_hidden=True), [])
        self.assertEqual(self.store.entity_tombstone_count(), 1)

    def test_a_row_with_an_empty_alias_leaves_no_empty_tombstone(self):
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE entities SET aliases = ? WHERE id = ?",
                ('["Dell Capital", "", "   ", null]', eid))
            self.store._conn.commit()
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        self.assertEqual(self.store.entity_tombstone_count(), 1)
        self.assertNotIn(hashlib.sha256(b"").hexdigest(),
                         {r["name_hash"] for r in self.tombstone_rows()})

    def test_names_with_no_letters_or_digits_do_not_block_each_other(self):
        # norm_name maps "%" and "$" to "", and both would share one hash.
        self.forget("%", kind="thing")
        self.assertTrue(self.store.is_entity_tombstoned("%"))
        self.assertFalse(self.store.is_entity_tombstoned("$"))
        self.assertTrue(self.store.resolve_entity("$", "thing", ts=NOW))
        # A forgotten price blocks that price, not the next one.
        self.forget("$49", kind="thing")
        self.assertEqual(self.store.resolve_entity("$49", "thing", ts=NOW), 0)
        self.assertTrue(self.store.resolve_entity("$55", "thing", ts=NOW))

    def test_hidden_row_is_bound_as_it_is_and_can_be_forgotten(self):
        eid = self.store.resolve_entity("$49", "thing", ts=NOW,
                                        kind_source="rule", hidden=True)
        self.assertEqual(self.store.all_entities(), [])
        # Seen again, by any caller: the same row, still hidden.
        self.assertEqual(self.store.resolve_entity("$49", "thing", ts=NOW),
                         eid)
        self.assertEqual(self.store.find_entity_exact("$49"), eid)
        row = self.store.all_entities(include_hidden=True)[0]
        self.assertEqual((row["id"], row["hidden"]), (eid, True))
        # `hidden` is for a new row only: it never hides a row that exists.
        seen = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.assertEqual(self.store.resolve_entity(
            "Dell Capital", "org", ts=NOW, hidden=True), seen)
        self.assertEqual([e["name"] for e in self.store.all_entities()],
                         ["Dell Capital"])

        self.store.delete_entity(eid, tombstone=True, reason="forget")
        self.assertEqual(self.store.resolve_entity(
            "$49", "thing", ts=NOW, kind_source="rule", hidden=True), 0)
        self.assertIsNone(self.store.find_entity_exact("$49"))

    def test_live_row_that_shares_a_forgotten_alias_still_resolves(self):
        holdings = self.store.resolve_entity("Acme Holdings", "org", ts=NOW)
        acme = self.store.resolve_entity("Acme", "org", ts=NOW)
        # An alias the user tied to the row (A10: only those are forgotten).
        self.store.upsert_entity_alias(holdings, "Acme", "acme",
                                       source="user", confirmed=True, ts=NOW)
        self.store.delete_entity(holdings, tombstone=True, reason="forget")
        # The alias was tombstoned with its row, but a live row owns the
        # name: it is found, never refused and never duplicated.
        self.assertTrue(self.store.is_entity_tombstoned("Acme"))
        self.assertEqual(self.store.resolve_entity("Acme", "org", ts=NOW),
                         acme)
        self.assertEqual(self.store.resolve_entity("ACME", ts=NOW), acme)
        self.assertEqual(
            Resolver(store=self.store).resolve_entity(
                "acme", "org", ts=NOW, kind_source="model"), acme)
        self.assertEqual(len(self.store.all_entities(include_hidden=True)), 1)
        self.assertEqual(self.store.resolve_entity("Acme Holdings", "org"), 0)

    def test_forgetting_an_unknown_id_or_the_same_row_twice_is_harmless(self):
        self.assertEqual(self.store.delete_entity(424242, tombstone=True), {})
        self.assertEqual(self.store.entity_tombstone_count(), 0)
        eid = self.forget("Dell Capital")
        self.assertEqual(self.store.delete_entity(eid, tombstone=True), {})
        self.assertEqual(self.store.entity_tombstone_count(), 1)

    def test_forgetting_the_same_name_again_keeps_one_tombstone(self):
        self.forget("Dell Capital")
        back = self.store.resolve_entity("Dell Capital", "org", ts=NOW,
                                         kind_source="user")
        self.assertTrue(back)
        self.assertEqual(self.store.entity_tombstone_count(), 0)
        self.store.delete_entity(back, tombstone=True, reason="forget")
        self.assertEqual(self.store.entity_tombstone_count(), 1)
        self.assertEqual(self.store.resolve_entity("Dell Capital", "org"), 0)

    def test_concurrent_callers_cannot_remint_a_forgotten_name(self):
        import threading
        self.forget("Dell Capital")
        got: list[int] = []
        start = threading.Barrier(16)

        def call(i: int) -> None:
            start.wait()
            got.append(self.store.resolve_entity(
                ("Dell Capital", "dell capital", "DELL-CAPITAL")[i % 3],
                "org", ts=NOW, kind_source=("model", "rule", "filing")[i % 3]))

        threads = [threading.Thread(target=call, args=(i,)) for i in range(16)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(got, [0] * 16)
        self.assertEqual(self.store.all_entities(include_hidden=True), [])
        self.assertTrue(self.store.is_entity_tombstoned("Dell Capital"))

    def test_concurrent_user_and_automatic_callers_end_with_one_row(self):
        import threading
        self.forget("Dell Capital")
        got: list[tuple[str, int]] = []
        start = threading.Barrier(16)

        def call(i: int) -> None:
            src = "user" if i % 4 == 0 else "model"
            start.wait()
            got.append((src, self.store.resolve_entity(
                "Dell Capital", "org", ts=NOW, kind_source=src)))

        threads = [threading.Thread(target=call, args=(i,)) for i in range(16)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual(len(got), 16)
        rows = self.store.all_entities(include_hidden=True)
        self.assertEqual([e["name"] for e in rows], ["Dell Capital"])
        # Every user call got the row; an automatic call got it or 0 (when it
        # ran before the user lifted the tombstone), never a second row.
        self.assertTrue(all(eid == rows[0]["id"]
                            for src, eid in got if src == "user"), got)
        self.assertTrue(all(eid in (0, rows[0]["id"]) for _, eid in got), got)
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capital"))
        self.assertEqual(self.entity_edges_to_zero(), 0)


class ResolverTombstoneTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.resolver = Resolver(store=self.store)

    def test_resolver_returns_zero_for_a_forgotten_name(self):
        self.forget("Dell Capital")
        self.assertEqual(
            self.resolver.resolve_entity("Dell Capital", "org", ts=NOW), 0)
        self.assertEqual(
            self.resolver.resolve_entity("dell capital", "org", ts=NOW,
                                         kind_source="model"), 0)
        self.assertIsNone(self.store.find_entity_exact("Dell Capital"))

    def test_forgotten_name_is_not_folded_into_a_fuzzy_neighbour(self):
        keep = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        # Control: without a tombstone the corrector folds the near-twin
        # into the surviving row. That is the path a forgotten name must
        # not take.
        other_tmp = tempfile.mkdtemp(prefix="quill_tomb_ctl_")
        ctl = _mk(other_tmp)
        try:
            twin = ctl.resolve_entity("Dell Capital", "org", ts=NOW)
            gone = ctl.resolve_entity("Dell Capitol", "org", ts=NOW)
            ctl.delete_entity(gone)          # no tombstone
            self.assertEqual(
                Resolver(store=ctl).resolve_entity("Dell Capitol", "org",
                                                   ts=NOW), twin)
            self.assertIn("Dell Capitol", ctl.get_entity(twin)["aliases"])
        finally:
            ctl.close()

        self.forget("Dell Capitol")
        self.assertEqual(
            self.resolver.resolve_entity("Dell Capitol", "org", ts=NOW), 0)
        self.assertNotIn("Dell Capitol",
                         self.store.get_entity(keep)["aliases"])
        self.assertEqual(len(self.store.all_entities(include_hidden=True)), 1)

    def test_user_brings_a_forgotten_name_back_as_its_own_row(self):
        keep = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.forget("Dell Capitol")
        eid = self.resolver.resolve_entity("Dell Capitol", "org", ts=NOW,
                                           kind_source="user")
        self.assertTrue(eid)
        self.assertNotEqual(eid, keep)
        self.assertEqual(self.store.get_entity(eid)["name"], "Dell Capitol")
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capitol"))

    def test_resolver_blocks_spelling_variants_of_a_forgotten_name(self):
        self.forget("Café Müller")
        for name in ("Café Müller", "cafe muller", "CAFE-MULLER",
                     "  Cafe  Muller "):
            self.assertEqual(self.resolver.resolve_entity(
                name, "org", ts=NOW, kind_source="model"), 0, name)
        self.assertEqual(self.store.all_entities(include_hidden=True), [])

    def test_forgotten_name_on_a_hidden_row_binds_it_and_is_not_folded(self):
        # The forgotten row answered to "Dell Capitol"; a hidden row of
        # that exact name is still there, next to a near-twin. The name
        # must land on its own row, not become an alias of the twin.
        twin = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        hidden = self.store.resolve_entity("Dell Capitol", "org", ts=NOW)
        self.store.set_entity_hidden(hidden, hidden=True)
        group = self.store.resolve_entity("Round Rock Group", "org", ts=NOW)
        # An alias the user confirmed (A10: only those are forgotten).
        self.store.upsert_entity_alias(group, "Dell Capitol", "dell capitol",
                                       source="user", confirmed=True, ts=NOW)
        self.store.delete_entity(group, tombstone=True, reason="forget")
        self.assertTrue(self.store.is_entity_tombstoned("Dell Capitol"))

        got = self.resolver.resolve_entity("Dell Capitol", "org", ts=NOW,
                                           kind_source="model")
        self.assertEqual(got, hidden)
        self.assertNotIn("Dell Capitol",
                         self.store.get_entity(twin)["aliases"])
        self.assertEqual(len(self.store.all_entities(include_hidden=True)), 2)

    def test_alias_binder_does_not_propose_a_forgotten_name(self):
        from app.services import entity_alias as ea
        import numpy as np
        keep = self.store.resolve_entity("c3", "idea", ts=NOW)
        self.forget("c4", kind="idea")
        # Every string embeds to the same vector, so the embedding step
        # would propose any unknown name as an alias of "c3".
        vec = np.array([1.0, 0.0], dtype="float32")
        with patch.object(ea, "_embed", return_value=vec):
            # Control: an unknown, never-forgotten name gets a proposal row.
            self.assertIsNone(ea.resolve("c5", store=self.store, ts=NOW))
            self.assertIsNone(ea.resolve("c4", store=self.store, ts=NOW))
        proposed = {a["alias"] for a in
                    self.store.list_entity_aliases(entity_id=keep)}
        self.assertIn("c5", proposed)
        self.assertNotIn("c4", proposed)


class FilingTombstoneTests(_StoreCase):
    def test_extraction_job_cannot_bring_a_project_back_but_the_user_can(self):
        from app.services import projects
        first = projects.ensure_filed("Atlas", store=self.store, now=NOW)
        self.assertTrue(first)
        self.store.delete_entity(first, tombstone=True, reason="forget")

        self.assertEqual(
            projects.ensure_filed("Atlas", store=self.store, now=NOW), 0)
        self.assertEqual(
            projects.stamp_event_facts(self.store, 1, "Atlas"), 0)
        self.assertIsNone(self.store.find_entity_exact("Atlas"))
        self.assertTrue(self.store.is_entity_tombstoned("Atlas"))

        eid = projects.ensure_filed("Atlas", store=self.store, now=NOW,
                                    user_accept=True)
        self.assertTrue(eid)
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"]),
                         ("project", "filing"))
        self.assertEqual(self.store.entity_tombstone_count(), 0)


class CallerAuditTests(_StoreCase):
    """A refused mint is id 0. No caller may use it as a node."""

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
                  patch("app.storage.get_store", return_value=self.store),
                  # The hygiene gate's dedup probe reads the process-wide
                  # vector index, not this store.
                  patch("app.services.extractor._index_fact",
                        lambda *a, **k: None),
                  patch("app.services.fact_gate._similar_active",
                        return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def test_extractor_entity_and_relation_endpoint(self):
        from app.services.extractor import Extractor
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        facts = {
            "entities": [{"name": "Dell Capital", "kind": "org"}],
            "relations": [{
                "subject": "Atlas", "subject_kind": "project",
                "predicate": "part_of",
                "object": "Dell Capital", "object_kind": "org",
                "source_span": "Atlas is part of Dell Capital",
                "confidence": 0.9}],
        }
        ex = Extractor(store=self.store)
        # Control: a name nobody forgot is minted and linked.
        ex._persist_entities(facts, None, NOW, event_source="chat.user",
                             text="Atlas is part of Dell Capital")
        dell = self.store.find_entity_exact("Dell Capital")
        self.assertTrue(dell)
        out = self.store.relations_of("entity", atlas)["out"]
        self.assertIn(("part_of", dell),
                      {(e["predicate"], int(e["obj_id"])) for e in out})

        self.store.delete_entity(dell, tombstone=True, reason="forget")
        ex._persist_entities(facts, None, NOW + 60, event_source="chat.user",
                             text="Atlas is part of Dell Capital")
        self.assertIsNone(self.store.find_entity_exact("Dell Capital"))
        self.assertEqual(self.store.relations_of("entity", atlas)["out"], [])
        self.assertEqual(self.entity_edges_to_zero(), 0)

    def test_extracting_the_same_event_again_changes_nothing(self):
        from app.services.extractor import Extractor
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.forget("Dell Capital")
        facts = {
            "entities": [{"name": "Dell Capital", "kind": "org"},
                         {"name": "Northwind", "kind": "org",
                          "confidence": 0.8}],
            "relations": [
                {"subject": "Atlas", "subject_kind": "project",
                 "predicate": "part_of",
                 "object": "Dell Capital", "object_kind": "org",
                 "source_span": "Atlas is part of Dell Capital",
                 "confidence": 0.9},
                {"subject": "Atlas", "subject_kind": "project",
                 "predicate": "part_of",
                 "object": "Northwind", "object_kind": "org",
                 "source_span": "Atlas is part of Northwind",
                 "confidence": 0.9}],
        }
        text = "Atlas is part of Dell Capital. Atlas is part of Northwind."
        ex = Extractor(store=self.store)

        def snapshot():
            rows = [(e["id"], e["name"], e["kind"], e["kind_source"],
                     e["kind_confidence"], sorted(e["aliases"]))
                    for e in self.store.all_entities(include_hidden=True)]
            with self.store._lock:
                edges = [tuple(r) for r in self.store._conn.execute(
                    "SELECT subj_type, subj_id, predicate, obj_type, obj_id "
                    "FROM relations ORDER BY id").fetchall()]
            return rows, edges, self.store.entity_tombstone_count()

        ex._persist_entities(facts, None, NOW, event_source="chat.user",
                             text=text)
        first = snapshot()
        north = self.store.find_entity_exact("Northwind")
        self.assertTrue(north)
        self.assertEqual(self.store.get_entity(north)["kind_source"], "model")
        self.assertIn(("entity", atlas, "part_of", "entity", north), first[1])
        self.assertIsNone(self.store.find_entity_exact("Dell Capital"))

        for _ in range(2):
            ex._persist_entities(facts, None, NOW, event_source="chat.user",
                                 text=text)
        self.assertEqual(snapshot(), first)
        self.assertEqual(self.entity_edges_to_zero(), 0)

    def test_claim_about_a_forgotten_subject_writes_no_belief(self):
        from app.services.consolidation import Turn
        from app.services.extractor import Extractor
        self.forget("pilot plan", kind="idea")
        turn = Turn(start=NOW, end=NOW, speaker="Hugh",
                    text="the pilot plan is $49 a month",
                    event_ids=[], n_utterances=1)
        facts = {"tasks": [], "commitments": [], "entities": [],
                 "relations": [],
                 "claims": [{
                     "text": "the pilot plan is $49 a month",
                     "confidence": 0.9,
                     "source_span": "the pilot plan is $49 a month",
                     "assertion": "stated_by_other",
                     "subject": "pilot plan", "predicate": "costs",
                     "object": "$49"}]}
        Extractor(store=self.store)._persist(turn, facts, NOW)
        self.assertIsNone(self.store.find_entity_exact("pilot plan"))
        self.assertEqual(
            self.store.list_kg_predicates(predicate="costs", limit=20), [])
        # The flat claim is still kept; only the node stays forgotten.
        self.assertTrue(self.store.list_facts(kind="claim", limit=5))

    def test_email_domain_org_from_a_captured_email(self):
        from app.services import people_pipeline as pp
        sample = (Path(__file__).resolve().parent / "fixtures"
                  / "email_capture_sample.txt").read_text(encoding="utf-8")
        self.forget("Acme Robotics")
        out = pp.ingest_email_network(
            sample, store=self.store, event_id=99,
            event_source="desktop.screen",
            window="Inbox - Jane - Outlook", now=NOW)
        self.assertTrue(out.get("ok"), out)
        pid = self.store.find_person_by_contact(
            "email", "jordan.lee@acmerobotics.com")
        self.assertIsNotNone(pid, "the person is still captured")
        self.assertIsNone(self.store.find_entity_exact("Acme Robotics"))
        self.assertEqual(
            [e for e in self.store.relations_of("person", int(pid))["out"]
             if e["obj_type"] == "entity"], [])
        self.assertEqual(self.entity_edges_to_zero(), 0)
        with self.store._lock:
            n = self.store._conn.execute(
                "SELECT COUNT(*) FROM kg_predicates WHERE obj_type='entity' "
                "AND obj_id=0").fetchone()[0]
        self.assertEqual(int(n), 0)

    def test_email_domain_org_from_mail_exhaust(self):
        from app.services import exhaust_ingest as ex
        stats = {"ada@acme.io": {"name": "Ada Lovelace", "last_seen": NOW}}

        def run(store, tmp):
            with patch.object(ex, "_ledger_path",
                              return_value=Path(tmp) / "ledger.json"):
                ex.apply_stats(store, dict(stats), {}, messages=[], events=[],
                               now=NOW)

        # Control: the domain mints its org and a works_at edge.
        ctl_tmp = tempfile.mkdtemp(prefix="quill_tomb_ctl_")
        ctl = _mk(ctl_tmp)
        try:
            run(ctl, ctl_tmp)
            acme = ctl.find_entity_exact("Acme")
            self.assertTrue(acme)
            self.assertEqual(ctl.get_entity(acme)["kind_source"], "rule")
            ada = ctl.find_person_exact("Ada Lovelace")
            self.assertIn(("works_at", acme), {
                (e["predicate"], int(e["obj_id"]))
                for e in ctl.relations_of("person", ada)["out"]})
        finally:
            ctl.close()

        self.forget("Acme")
        run(self.store, self.tmp)
        ada = self.store.find_person_exact("Ada Lovelace")
        self.assertIsNotNone(ada, "the person is still captured")
        self.assertIsNone(self.store.find_entity_exact("Acme"))
        self.assertEqual(
            [e for e in self.store.relations_of("person", ada)["out"]
             if e["obj_type"] == "entity"], [])
        self.assertEqual(self.entity_edges_to_zero(), 0)

    def test_machine_scan_skips_a_forgotten_project_and_tool(self):
        from types import SimpleNamespace
        from app.services import onboarding_scan as scan
        cfg = SimpleNamespace(onboarding=SimpleNamespace(
            scan_enabled=True,
            scan_sources=frozenset({"projects", "bookmarks"})))
        self.forget("webapp", kind="project")
        self.forget("Linear", kind="tool")
        saved: dict = {}
        with patch.object(scan, "settings", cfg), \
                patch.object(scan, "dev_projects", return_value=[
                    {"name": "webapp", "path": "/home/u/webapp"},
                    {"name": "billing", "path": "/home/u/billing"}]), \
                patch.object(scan, "bookmark_tools",
                             return_value=["Linear", "GitHub"]), \
                patch.object(scan, "_load_scan_state", return_value={}), \
                patch.object(scan, "_save_scan_state", side_effect=saved.update), \
                patch.object(scan, "_seed_contact_points", return_value=0):
            out = scan.enrich(sources={"projects", "bookmarks"},
                              store=self.store)
        self.assertTrue(out["ok"], out)
        # Control and subject in one run: the two names nobody forgot are
        # minted by rule; the forgotten two are not, and are not counted.
        names = {e["name"]: e for e in self.store.all_entities()}
        self.assertEqual(set(names), {"billing", "GitHub"})
        self.assertEqual(names["billing"]["kind_source"], "rule")
        self.assertEqual(names["GitHub"]["kind_source"], "rule")
        self.assertEqual((out["projects"], out["tools"]), (1, 1))
        claims = " ".join(f["text"] for f in
                          self.store.list_facts(kind="claim", limit=20))
        self.assertIn("'billing'", claims)
        self.assertNotIn("'webapp'", claims)
        with self.store._lock:
            n = self.store._conn.execute(
                "SELECT COUNT(*) FROM kg_node_keys WHERE node_type='entity' "
                "AND node_id=0").fetchone()[0]
        self.assertEqual(int(n), 0)

    def test_onboarding_answer_is_the_user_and_brings_it_back(self):
        from app.services.onboarding import _Ingestor
        self.forget("Atlas", kind="project")
        ing = _Ingestor(self.store, set(), NOW)
        eid = ing.entity("Atlas", "project", ["Atlas rollout"])
        self.assertTrue(eid)
        row = self.store.get_entity(eid)
        self.assertEqual(row["kind_source"], "user")
        self.assertIn("Atlas rollout", row["aliases"])
        self.assertEqual(ing.counts["entities"], 1)

    def test_onboarding_alias_typed_by_the_user_is_no_longer_forgotten(self):
        from app.services.onboarding import _Ingestor
        self.forget("Northwind", kind="project")
        self.assertTrue(self.store.is_entity_tombstoned("Northwind"))
        ing = _Ingestor(self.store, set(), NOW)
        eid = ing.entity("Atlas", "project", ["Northwind"])
        self.assertIn("Northwind", self.store.get_entity(eid)["aliases"])
        self.assertFalse(self.store.is_entity_tombstoned("Northwind"))

    def test_person_org_typed_by_the_user_brings_it_back(self):
        from app.api import routes
        self.forget("Dell Capital")
        pid = self.store.resolve_person("Ada Lovelace", ts=NOW)
        routes._add_detail_side_effects(self.store, pid, "org",
                                        "Dell Capital", NOW)
        eid = self.store.find_entity_exact("Dell Capital")
        self.assertTrue(eid)
        self.assertEqual(self.store.get_entity(eid)["kind_source"], "user")
        self.assertIn(("works_at", eid), {
            (e["predicate"], int(e["obj_id"]))
            for e in self.store.relations_of("person", pid)["out"]})

    def test_person_reroute_by_the_adjudicator_stays_forgotten(self):
        from app.services import person_adjudicator
        self.forget("Initech")
        pid = self.store.insert_person("Initech", ts=NOW)
        action = person_adjudicator._apply(
            self.store, {"id": pid, "name": "Initech"},
            {"kind": "org", "confidence": 0.99, "reason": "a company"})
        self.assertEqual(action, "hidden_org")
        self.assertIsNone(self.store.find_entity_exact("Initech"))
        self.assertTrue(self.store.is_entity_tombstoned("Initech"))

    def test_moving_a_person_to_an_entity_by_hand_brings_it_back(self):
        self.forget("Initech")
        pid = self.store.insert_person("Initech", ts=NOW)
        out = self.store.convert_person_to_entity(pid, "org")
        self.assertIsNotNone(out)
        row = self.store.get_entity(out["entity_id"])
        self.assertEqual((row["name"], row["kind"], row["kind_source"]),
                         ("Initech", "org", "user"))

    def test_split_node_by_hand_brings_the_name_back(self):
        from app.services import kg_beliefs
        src = self.store.resolve_entity("Dell", "org", ts=NOW,
                                        kind_source="rule")
        self.forget("Dell Capital")
        out = kg_beliefs.manual_split(
            self.store, node_type="entity", node_id=src,
            new_name="Dell Capital", predicate_ids=[], now=NOW)
        self.assertTrue(out.get("ok"), out)
        eid = self.store.find_entity_exact("Dell Capital")
        self.assertTrue(eid)
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "rule"))


class ForgetRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_tomb_route_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_forget_route_tombstones_name_and_aliases(self):
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        # The alias the user typed is forgotten with the row (A10); a
        # spelling the resolver folded in is not.
        self.client.post(f"/entities/{eid}/alias", json={"alias": "DellCap"})
        self.store.touch_entity(eid, NOW, alias="Dell Capitol")
        r = self.client.post(f"/entities/{eid}/forget")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], "Dell Capital")
        self.assertTrue(self.store.is_entity_tombstoned("Dell Capital"))
        self.assertTrue(self.store.is_entity_tombstoned("DellCap"))
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capitol"))
        # The next extraction that sees the name mints nothing.
        self.assertEqual(
            Resolver(store=self.store).resolve_entity(
                "Dell Capital", "org", ts=NOW, kind_source="model"), 0)
        names = [e["name"] for e in
                 self.client.get("/entities/list").json()["entities"]]
        self.assertNotIn("Dell Capital", names)

    def test_alias_typed_by_the_user_lifts_a_stale_tombstone(self):
        gone = self.store.resolve_entity("DellCap", "org", ts=NOW)
        self.store.delete_entity(gone, tombstone=True, reason="forget")
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        r = self.client.post(f"/entities/{eid}/alias",
                             json={"alias": "DellCap"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.store.is_entity_tombstoned("DellCap"))
        self.assertIn("DellCap", self.store.get_entity(eid)["aliases"])


    def test_rename_route_onto_a_forgotten_name_lifts_its_tombstone(self):
        gone = self.store.resolve_entity("DellCap", "org", ts=NOW)
        self.store.delete_entity(gone, tombstone=True, reason="forget")
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        r = self.client.post(f"/entities/{eid}/rename",
                             json={"name": "DellCap"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.store.is_entity_tombstoned("DellCap"))
        # Renaming away from a name is not forgetting it.
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capital"))
        self.assertEqual(self.store.resolve_entity("Dell Capital", ts=NOW),
                         self.store.find_entity_exact("Dell Capital"))
        # Forgetting the renamed row forgets both spellings.
        self.client.post(f"/entities/{eid}/forget")
        self.assertTrue(self.store.is_entity_tombstoned("DellCap"))
        self.assertTrue(self.store.is_entity_tombstoned("Dell Capital"))

    def test_forget_route_on_an_unknown_entity_is_a_404_and_no_tombstone(self):
        r = self.client.post("/entities/424242/forget")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.store.entity_tombstone_count(), 0)


class CleanupScriptTests(unittest.TestCase):
    def test_graph_cleanup_deletes_without_a_tombstone(self):
        # The script must keep calling delete_entity with no tombstone flag.
        src = (Path(__file__).resolve().parent.parent / "scripts"
               / "graph_cleanup.py").read_text(encoding="utf-8")
        self.assertIn("delete_entity(", src)
        self.assertNotIn("tombstone", src)


if __name__ == "__main__":
    unittest.main()
