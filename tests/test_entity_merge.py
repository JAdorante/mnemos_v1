"""Entity model, Phase 4: the audited entity merge.

`Store.merge_entities(survivor, absorbed)` re-points everything that named
the absorbed row (relations on both sides, beliefs, node keys and
attributes, details, aliases, ledger sightings, pooled relations, children,
and every other table that references a node by type and id, found by
walking the schema), deletes the row, and writes an `entity_merges` audit
row. The absorbed names become confirmed aliases of the survivor, and
`resolve_entity` / `find_entity_exact` match a confirmed alias before they
mint, so the old name keeps resolving to the survivor. The route and the
"Merge into…" picker sit on top.

Every test runs against a real Store on a temp db.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes as routes_mod  # noqa: F401
from app.storage import Store

NOW = 1_700_000_000.0


def _mk(tmp: str, **kw) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio",
                 **kw)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_merge_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)

    def ent(self, name, kind="org", source="model"):
        return self.store.resolve_entity(name, kind, ts=NOW,
                                         kind_source=source)

    def fact(self, text="x"):
        with self.store._lock:
            cur = self.store._conn.execute(
                "INSERT INTO facts (kind, text, extracted_at) "
                "VALUES ('claim', ?, ?)", (text, NOW))
            self.store._conn.commit()
            return int(cur.lastrowid)

    def q(self, sql, args=()):
        with self.store._lock:
            return [dict(r) for r in self.store._conn.execute(sql, args)]

    def refs_to(self, eid: int) -> dict:
        """Every row anywhere that still names entity `eid`."""
        out = {}
        with self.store._lock:
            typed, plain = self.store._merge_ref_columns_unlocked()
            for t, ct, ci in typed + [("relations", "subj_type", "subj_id"),
                                      ("relations", "obj_type", "obj_id"),
                                      ("kg_predicates", "subj_type", "subj_id"),
                                      ("kg_predicates", "obj_type", "obj_id")]:
                n = self.store._conn.execute(
                    f'SELECT COUNT(*) FROM "{t}" WHERE {ct} = \'entity\' '
                    f"AND {ci} = ?", (eid,)).fetchone()[0]
                if n:
                    out[f"{t}.{ci}"] = n
            for t, ci in plain + [("entity_aliases", "entity_id")]:
                n = self.store._conn.execute(
                    f'SELECT COUNT(*) FROM "{t}" WHERE {ci} = ?',
                    (eid,)).fetchone()[0]
                if n:
                    out[f"{t}.{ci}"] = n
            n = self.store._conn.execute(
                "SELECT COUNT(*) FROM entities WHERE id = ? OR parent_id = ?",
                (eid, eid)).fetchone()[0]
            if n:
                out["entities"] = n
        return out


# ---------------------------------------------------------- re-pointing --
class RepointTests(_Case):
    def test_relations_on_both_sides_move_and_clashes_keep_the_stronger(self):
        s, a = self.ent("Acme"), self.ent("ACME Corp.")
        p = self.store.resolve_person("Dana Whitlock", ts=NOW)
        f1, f2 = self.fact(), self.fact()
        other = self.ent("Harborlight")
        # a fact about both; the absorbed edge is the stronger (asserted)
        self.store.add_relation("fact", f1, "about", "entity", s,
                                origin="derived", weight=1.0)
        self.store.add_relation("fact", f1, "about", "entity", a,
                                origin="asserted", weight=3.0)
        self.store.add_relation("fact", f2, "about", "entity", a,
                                origin="derived")
        self.store.add_relation("person", p, "works_at", "entity", a,
                                origin="asserted")
        self.store.add_relation("entity", a, "partner_of", "entity", other,
                                origin="asserted")
        # an edge between the two becomes a self-loop and goes
        self.store.add_relation("entity", a, "related_to", "entity", s,
                                origin="asserted")
        res = self.store.merge_entities(s, a)
        self.assertTrue(res)
        self.assertEqual(self.refs_to(a), {})
        about = self.q("SELECT origin, weight FROM relations WHERE "
                       "subj_type='fact' AND subj_id=? AND obj_id=?", (f1, s))
        self.assertEqual(about, [{"origin": "asserted", "weight": 3.0}])
        self.assertTrue(self.q("SELECT 1 FROM relations WHERE subj_type='fact'"
                               " AND subj_id=? AND obj_id=?", (f2, s)))
        self.assertTrue(self.q("SELECT 1 FROM relations WHERE "
                               "subj_type='person' AND predicate='works_at' "
                               "AND obj_id=?", (s,)))
        self.assertTrue(self.q("SELECT 1 FROM relations WHERE subj_type="
                               "'entity' AND subj_id=? AND obj_id=?",
                               (s, other)))
        self.assertFalse(self.q("SELECT 1 FROM relations WHERE subj_type="
                                "'entity' AND subj_id=obj_id AND obj_type="
                                "'entity'"))
        self.assertEqual(res["counts"].get("relations.self_loop_dropped"), 1)
        self.assertEqual(res["counts"].get("relations.duplicate_folded"), 1)

    def test_beliefs_fold_duplicates_and_retract_self_loops(self):
        s, a = self.ent("Northwind"), self.ent("Northwind Co")
        p = self.store.resolve_person("Ana Brightfield", ts=NOW)
        # The same belief about each row (asserted edges dual-write it).
        for eid, ev in ((s, 11), (a, 12)):
            self.store.add_relation("person", p, "works_at", "entity", eid,
                                    origin="asserted", source_event_id=ev,
                                    quote=f"q{ev}", ts=NOW)
        self.store.add_relation("entity", a, "part_of", "entity", s,
                                origin="asserted", source_event_id=13,
                                quote="q13", ts=NOW)
        preds = self.q("SELECT id, subj_id, obj_id, status FROM kg_predicates")
        self.assertGreaterEqual(len(preds), 2)
        res = self.store.merge_entities(s, a)
        active = self.q("SELECT id, predicate FROM kg_predicates "
                        "WHERE status = 'active'")
        self.assertEqual([r["predicate"] for r in active], ["works_at"])
        keep = active[0]["id"]
        ev = self.q("SELECT predicate_id FROM kg_evidence")
        self.assertTrue(all(r["predicate_id"] == keep for r in ev
                            if r["predicate_id"] in {keep}))
        moved = self.q("SELECT COUNT(*) AS n FROM kg_evidence "
                       "WHERE predicate_id = ?", (keep,))[0]["n"]
        self.assertGreaterEqual(moved, 2)
        retracted = self.q("SELECT superseded_by FROM kg_predicates "
                           "WHERE status = 'retracted'")
        self.assertEqual(len(retracted), 2)    # the copy and the self-loop
        self.assertIn(keep, [r["superseded_by"] for r in retracted])
        self.assertEqual(self.refs_to(a), {})
        self.assertEqual(res["counts"].get("kg_predicates.self_loop_retracted"),
                         1)

    def test_keys_attrs_details_survivor_wins_and_nothing_is_orphaned(self):
        s, a = self.ent("Cobalt Line"), self.ent("Cobaltline")
        self.store.set_entity_attr(s, "status", "active", None, NOW)
        self.store.set_entity_attr(a, "status", "paused", None, NOW)
        self.store.set_entity_attr(a, "url", "https://cobalt.example", None,
                                   NOW)
        with self.store._lock:
            c = self.store._conn
            c.execute("INSERT INTO kg_node_attrs (node_type, node_id, key, "
                      "value, created_at, updated_at) VALUES "
                      "('entity', ?, 'hq', 'Leeds', 1, 1)", (a,))
            c.execute("INSERT INTO node_dynamics (node_type, node_id, V) "
                      "VALUES ('entity', ?, 0.4)", (s,))
            c.execute("INSERT INTO node_dynamics (node_type, node_id, V) "
                      "VALUES ('entity', ?, 0.9)", (a,))
            c.execute("INSERT INTO brain_items (subject_type, subject_id, "
                      "section_key, item_type, item_id, text, review_state, "
                      "updated_at) VALUES ('entity', ?, 's', 'fact', '7', "
                      "'t', 'auto', 1)", (a,))
            c.commit()
        self.store.merge_entities(s, a)
        attrs = self.store.entity_attrs(s)
        self.assertEqual(attrs["status"]["value"], "active")   # survivor wins
        self.assertEqual(attrs["url"]["value"], "https://cobalt.example")
        self.assertEqual(self.q("SELECT V FROM node_dynamics WHERE "
                                "node_type='entity' AND node_id=?", (s,)),
                         [{"V": 0.4}])
        self.assertTrue(self.q("SELECT 1 FROM kg_node_attrs WHERE "
                               "node_id = ? AND key = 'hq'", (s,)))
        self.assertTrue(self.q("SELECT 1 FROM brain_items WHERE "
                               "subject_id = ?", (s,)))
        keys = {r["key_value"] for r in self.store.list_node_keys("entity", s)}
        self.assertIn("cobaltline", keys)        # the absorbed name's key
        self.assertEqual(self.refs_to(a), {})

    def test_a_table_added_later_is_found_by_the_schema_walk(self):
        s, a = self.ent("Tidemark"), self.ent("Tide Mark")
        with self.store._lock:
            self.store._conn.execute(
                "CREATE TABLE later_refs (id INTEGER PRIMARY KEY, "
                "target_type TEXT, target_id INTEGER)")
            self.store._conn.execute(
                "INSERT INTO later_refs (target_type, target_id) "
                "VALUES ('entity', ?), ('person', ?)", (a, a))
            self.store._conn.commit()
        res = self.store.merge_entities(s, a)
        rows = self.q("SELECT target_type, target_id FROM later_refs "
                      "ORDER BY id")
        self.assertEqual(rows, [{"target_type": "entity", "target_id": s},
                                {"target_type": "person", "target_id": a}])
        self.assertEqual(res["counts"].get("later_refs.target_id"), 1)

    def test_ledger_and_pooled_relations_follow(self):
        s, a = self.ent("Quarry Bay"), self.ent("Quarry-Bay")
        self.store.record_entity_mention("Quarry-Bay", proposed_kind="org",
                                         event_id=5, entity_id=a,
                                         observed_at=NOW)
        self.store.add_pending_entity_relation(
            subj_kind="entity", subj_name="Quarry-Bay", subj_id=a,
            predicate="partner_of", obj_kind="entity", obj_name="Vesper",
            source_event_id=5, ts=NOW)
        self.store.merge_entities(s, a)
        self.assertEqual([m["entity_id"] for m in
                          self.store.entity_mentions(entity_id=s)], [s])
        rel = self.store.pending_entity_relations()
        self.assertEqual([r["subj_id"] for r in rel], [s])


# --------------------------------------------------- names and identity --
class AliasTests(_Case):
    def test_absorbed_names_become_confirmed_aliases_and_resolve(self):
        s = self.ent("Ledgerleaf", "org")
        a = self.ent("Ledgerleaf Payroll", "project")
        self.store.touch_entity(a, NOW, alias="LL Payroll")
        self.store.upsert_entity_alias(a, "Leaf Pay", "leaf pay",
                                       source="embedding", confirmed=False)
        self.store.merge_entities(s, a)
        conf = {r["alias"] for r in self.store.list_entity_aliases(
            entity_id=s, confirmed=True)}
        self.assertEqual(conf, {"Ledgerleaf Payroll", "LL Payroll"})
        unconf = {r["alias"] for r in self.store.list_entity_aliases(
            entity_id=s, confirmed=False)}
        self.assertEqual(unconf, {"Leaf Pay"})   # a guess stays a guess
        row = self.store.get_entity(s)
        self.assertIn("Ledgerleaf Payroll", row["aliases"])
        self.assertIn("LL Payroll", row["aliases"])
        n_before = len(self.store.all_entities(include_hidden=True))
        self.assertEqual(self.store.resolve_entity(
            "ledgerleaf payroll", "project", ts=NOW, kind_source="model"), s)
        self.assertEqual(self.store.find_entity_exact("LL PAYROLL"), s)
        self.assertEqual(len(self.store.all_entities(include_hidden=True)),
                         n_before)
        self.assertEqual(self.store.get_entity(s)["kind"], "org")
        # An unconfirmed guess still mints its own row.
        self.assertNotEqual(self.store.resolve_entity("Leaf Pay", "org",
                                                      ts=NOW), s)

    def test_a_live_alias_wins_over_an_old_tombstone(self):
        s, a = self.ent("Brightline"), self.ent("Brightline Partners")
        gone = self.ent("Brightline Partners Ltd")
        self.store.delete_entity(gone, tombstone=True)
        self.store.merge_entities(s, a)
        # The tombstone is on another name; the alias of a live row binds.
        self.assertEqual(self.store.resolve_entity(
            "Brightline Partners", "org", ts=NOW), s)
        self.assertEqual(self.store.resolve_entity(
            "Brightline Partners Ltd", "org", ts=NOW), 0)

    def test_a_forgotten_spelling_never_becomes_a_confirmed_alias(self):
        s, a = self.ent("Quayline"), self.ent("Quayline Freight")
        self.store.touch_entity(a, NOW, alias="QL Freight")
        gone = self.ent("QL Freight")
        self.store.delete_entity(gone, tombstone=True)   # the user forgot it
        res = self.store.merge_entities(s, a)
        conf = {r["alias"] for r in self.store.list_entity_aliases(
            entity_id=s, confirmed=True)}
        self.assertEqual(conf, {"Quayline Freight"})
        self.assertEqual(res["counts"].get("entity_aliases.tombstoned_skipped"),
                         1)
        # Still forgotten: the merge did not bring the name back.
        self.assertEqual(self.store.resolve_entity("QL Freight", "org",
                                                   ts=NOW), 0)
        self.assertIsNone(self.store.find_entity_exact("QL Freight"))
        self.assertTrue(self.store.is_entity_tombstoned("QL Freight"))
        keys = {r["key_value"] for r in self.store.list_node_keys("entity", s)}
        self.assertNotIn("ql freight", keys)

    def test_a_hidden_survivor_binds_extraction_but_not_the_user(self):
        s = self.ent("Lumen", "project", None)
        self.store.set_entity_hidden(s, hidden=True)
        a = self.ent("Ana Ruiz's Lumen", "project", None)
        self.store.merge_entities(s, a)
        # A sighting binds the hidden survivor, and it stays hidden.
        self.assertEqual(self.store.resolve_entity(
            "Ana Ruiz's Lumen", "project", ts=NOW, kind_source="model"), s)
        self.assertTrue(self.store.entity_is_hidden(s))
        # The user filing under the old name gets a row of that name; the
        # survivor the user never named stays hidden.
        new = self.store.resolve_entity("Ana Ruiz's Lumen", "project",
                                        ts=NOW, kind_source="filing")
        self.assertNotIn(new, (0, s))
        self.assertTrue(self.store.entity_is_hidden(s))
        self.assertEqual(self.store.get_entity(new)["name"], "Ana Ruiz's Lumen")
        self.assertEqual(self.store.find_entity_exact("Ana Ruiz's Lumen"), new)

    def test_the_alias_binding_respects_the_kind_rank(self):
        s = self.ent("Heron", "project", "filing")
        a = self.ent("Project Heron", "idea")
        self.store.merge_entities(s, a)
        self.store.resolve_entity("Project Heron", "tool", ts=NOW,
                                  kind_source="model")
        self.assertEqual(self.store.get_entity(s)["kind"], "project")
        self.store.resolve_entity("Project Heron", "org", ts=NOW,
                                  kind_source="user")
        self.assertEqual(self.store.get_entity(s)["kind"], "org")


# --------------------------------------------------- kind, parent, audit --
class RowTests(_Case):
    def test_a_user_kind_on_the_absorbed_row_moves_to_the_survivor(self):
        s = self.ent("Wavecrest", "project", "model")
        a = self.ent("Wave Crest", "org", "user")
        self.store.merge_entities(s, a)
        e = self.store.get_entity(s)
        self.assertEqual((e["kind"], e["kind_source"]), ("org", "user"))

    def test_the_survivors_own_higher_kind_stays(self):
        s = self.ent("Wavecrest", "project", "filing")
        a = self.ent("Wave Crest", "org", "model")
        self.store.merge_entities(s, a)
        e = self.store.get_entity(s)
        self.assertEqual((e["kind"], e["kind_source"]), ("project", "filing"))

    def test_children_follow_and_parents_stay_consistent(self):
        proj = self.ent("Harbor Line", "project", "user")
        other = self.ent("Kestrel Rollout", "project", "user")
        s = self.ent("Wave 4", "label")
        a = self.ent("Wave Four", "label")
        kid = self.ent("Sprint 9", "label")
        self.assertTrue(self.store.set_entity_parent(a, proj, source="rule"))
        self.assertTrue(self.store.set_entity_parent(kid, a, source="rule"))
        self.store.merge_entities(s, a)
        self.assertEqual(self.store.get_entity(kid)["parent_id"], s)
        e = self.store.get_entity(s)
        self.assertEqual((e["parent_id"], e["parent_source"]), (proj, "rule"))
        mirrors = self.q("SELECT subj_id, obj_id FROM relations WHERE "
                         "predicate='part_of' AND origin='context' "
                         "ORDER BY subj_id")
        self.assertEqual(sorted((m["subj_id"], m["obj_id"]) for m in mirrors),
                         sorted([(s, proj), (kid, s)]))
        # The survivor's own parent wins over the absorbed row's.
        a2 = self.ent("Wave IV", "label")
        self.assertTrue(self.store.set_entity_parent(a2, other, source="rule"))
        self.store.merge_entities(s, a2)
        self.assertEqual(self.store.get_entity(s)["parent_id"], proj)
        self.assertFalse(self.q("SELECT 1 FROM relations WHERE subj_id = ? "
                                "AND predicate='part_of' AND obj_id = ?",
                                (s, other)))

    def test_a_merge_never_makes_a_parent_cycle(self):
        # The survivor sits inside a child of the absorbed row: S < K < A.
        a = self.ent("Harrow Program", "project")
        k = self.ent("Harrow Phase", "label")
        s = self.ent("Harrow", "project")
        self.assertTrue(self.store.set_entity_parent(k, a, source="rule"))
        self.assertTrue(self.store.set_entity_parent(s, k, source="rule"))
        res = self.store.merge_entities(s, a)
        self.assertEqual(res["counts"].get("entities.parent_cycle_cleared"), 1)
        self.assertEqual(self.store.get_entity(k)["parent_id"], s)
        self.assertIsNone(self.store.get_entity(s)["parent_id"])
        mirrors = self.q("SELECT subj_id, obj_id FROM relations WHERE "
                         "predicate='part_of' AND origin='context'")
        self.assertEqual([(m["subj_id"], m["obj_id"]) for m in mirrors],
                         [(k, s)])

    def test_plain_entity_id_columns_follow(self):
        s, a = self.ent("Fenwick"), self.ent("Fenwick Ltd")
        with self.store._lock:
            c = self.store._conn
            # A column the schema documents only in a comment, and a table
            # added later with a declared foreign key.
            c.execute("INSERT INTO agent_runs (goal, project_id, started_at) "
                      "VALUES ('g', ?, 1)", (a,))
            c.execute("CREATE TABLE later_fk (id INTEGER PRIMARY KEY, "
                      "owner INTEGER REFERENCES entities(id))")
            c.execute("INSERT INTO later_fk (owner) VALUES (?)", (a,))
            c.commit()
        res = self.store.merge_entities(s, a)
        self.assertEqual(self.q("SELECT project_id FROM agent_runs"),
                         [{"project_id": s}])
        self.assertEqual(self.q("SELECT owner FROM later_fk"), [{"owner": s}])
        self.assertEqual(res["counts"].get("agent_runs.project_id"), 1)
        with self.store._lock:
            _typed, plain = self.store._merge_ref_columns_unlocked()
        self.assertIn(("later_fk", "owner"), plain)
        self.assertNotIn(("entity_aliases", "entity_id"), plain)

    def test_a_survivor_inside_the_absorbed_row_comes_out(self):
        a = self.ent("Topo", "project")
        s = self.ent("Topo map", "label")
        self.assertTrue(self.store.set_entity_parent(s, a, source="rule"))
        self.store.merge_entities(s, a)
        self.assertIsNone(self.store.get_entity(s)["parent_id"])
        self.assertFalse(self.q("SELECT 1 FROM relations WHERE "
                                "predicate='part_of'"))

    def test_an_audit_row_records_who_whom_and_what_moved(self):
        s, a = self.ent("Corvane"), self.ent("Corvane Labs", "tool")
        self.store.touch_entity(a, NOW, alias="Corvane Lab")
        f = self.fact()
        self.store.add_relation("fact", f, "about", "entity", a,
                                origin="derived")
        before = self.store.memory_version()
        res = self.store.merge_entities(s, a, reason="duplicate",
                                        actor="user")
        rows = self.store.entity_merges()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["survivor_id"], r["absorbed_id"],
                          r["absorbed_name"], r["absorbed_kind"],
                          r["actor"], r["reason"]),
                         (s, a, "Corvane Labs", "tool", "user", "duplicate"))
        self.assertIn("Corvane Lab", r["absorbed_aliases"])
        self.assertEqual(r["counts"]["relations"], 1)
        self.assertEqual(res["merge_id"], r["id"])
        self.assertNotEqual(self.store.memory_version(), before)
        self.assertIn(("entity", s), self.store.graph_dirty_nodes())
        self.assertIsNone(self.store.get_entity(a))

    def test_refused_cases_write_nothing(self):
        s = self.ent("Solo")
        self.assertEqual(self.store.merge_entities(s, s), {})
        self.assertEqual(self.store.merge_entities(s, 999), {})
        self.assertEqual(self.store.merge_entities(999, s), {})
        self.assertEqual(self.store.entity_merges(), [])
        ro = _mk(self.tmp, readonly=True)
        try:
            other = self.ent("Solo Two")
            self.assertEqual(ro.merge_entities(s, other), {})
        finally:
            ro.close()
        self.assertIsNotNone(self.store.get_entity(s))


# ------------------------------------------------------- route and page --
class MergeRouteTests(_Case):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(routes_mod.router)
        p = patch.object(routes_mod.memory, "_ensure_store",
                         return_value=self.store)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(app)

    def test_merge_route(self):
        s, a = self.ent("Fernbrook Print"), self.ent("Fernbrook Printing")
        r = self.client.post(f"/entities/{a}/merge", json={"into": s})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["survivor"]["id"], d["absorbed"]["id"]), (s, a))
        self.assertIn("Fernbrook Printing", d["survivor"]["aliases"])
        self.assertEqual(self.store.entity_merges()[0]["actor"], "user")
        self.assertEqual(self.client.post(f"/entities/{a}/merge",
                                          json={"into": s}).status_code, 404)
        self.assertEqual(self.client.post(f"/entities/{s}/merge",
                                          json={"into": s}).status_code, 400)

    def test_the_detail_panel_has_a_searchable_picker(self):
        from app.api import profile_page
        html = profile_page.PROFILE_PAGE
        for needle in ('id="eMergeQ"', 'id="eMergeList"', "data-emerge",
                       "'/entities/'+eid+'/merge'",
                       "function mergeChoices(eid, q){",
                       "mergeChoices(eid, q.value)",
                       "JSON.stringify({into})"):
            self.assertIn(needle, html)
        self.assertNotIn("prompt('Merge", html)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
