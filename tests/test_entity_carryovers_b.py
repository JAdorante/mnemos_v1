"""Entity model, Phase 4 part A: carry-overs from the Phase 3 check.

B1  `context/surfaces.py` holds generic words only. One seat's own heading
    junk ("this seat's news brand") and containment stop names live in that
    seat's vocabulary (`heading_junk_name`, `containment_stop_name`), read by
    the surface index; the seed script takes and reports them. A multi-user
    slot label used as a name ("User 3") is a placeholder by its shape.
B2  is the repair rule for person names stored as entities: see
    `tests/test_entity_repair.py` (`PersonNameTests`).
B3  a repeated "Not a person…" on a name already learned still offers Undo;
    setting the parent a row already has puts a missing mirror edge back.

Every test runs against a real Store on a temp db.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes as routes_mod
from app import storage as storage_mod
from app.services.context import surfaces as sf
from app.storage import Store

NOW = 1_700_000_000.0
ROOT = Path(__file__).resolve().parents[1]


def _mk(tmp: str, **kw) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio",
                 **kw)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_carry_b_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)
        shared = patch.object(storage_mod, "_store", None)
        shared.start()
        self.addCleanup(shared.stop)


# -------------------------------------------------------------------- B1 --
class SurfacesVocabularyTests(_Case):
    def setUp(self):
        super().setUp()
        now = time.time()
        self.brand = self.store.resolve_entity("Harborlight News", "org",
                                               ts=now)
        self.pen = self.store.resolve_entity("The Quill", "tool", ts=now)

    def test_no_seat_names_are_left_in_code(self):
        # The four headings and the containment name one seat had moved out.
        for n in ("project x", "up and down the org", "google news",
                  "fox news"):
            self.assertNotIn(n, sf._VLM_JUNK_NAMES)
        self.assertNotIn("the pen", sf._NO_CONTAINMENT)
        src = (ROOT / "app/services/context/surfaces.py").read_text()
        for n in ("Project X", "Fox", "Boostrun", "Justin", "Karos"):
            self.assertNotIn(n, src)
        # Generic furniture stayed.
        for n in ("your team", "top stories", "sparrow", "workspace"):
            self.assertIn(n, sf._VLM_JUNK_NAMES)

    def test_a_seat_heading_name_never_names_a_node_there(self):
        idx = sf.SurfaceIndex(self.store)
        self.assertEqual([h.node_id for h in
                          idx.from_heading("Harborlight News")], [self.brand])
        self.assertTrue(self.store.add_seat_vocab(
            "Harborlight  News", "heading_junk_name", source="migration"))
        self.assertTrue(sf.is_heading_junk("harborlight news",
                                           store=self.store))
        idx = sf.SurfaceIndex(self.store)
        self.assertEqual(idx.from_heading("Harborlight News"), [])
        # Another seat (another store) still names it.
        other = _mk(tempfile.mkdtemp(prefix="quill_carry_b2_"))
        self.addCleanup(other.close)
        oid = other.resolve_entity("Harborlight News", "org", ts=NOW)
        self.assertEqual([h.node_id for h in sf.SurfaceIndex(other)
                          .from_heading("Harborlight News")], [oid])

    def test_a_seat_containment_name_is_only_matched_whole(self):
        seg = "Notes on The Quill rollout"
        # The whole-segment binder (no embedding model in a unit test).
        exact = patch("app.services.entity_alias.resolve",
                      side_effect=lambda s, **kw: self.pen
                      if s.strip().lower() == "the quill" else None)
        exact.start()
        self.addCleanup(exact.stop)
        idx = sf.SurfaceIndex(self.store)
        self.assertIn(self.pen, [h.node_id for h in idx.resolve(seg)])
        self.store.add_seat_vocab("the quill", "containment_stop_name",
                                  source="migration")
        self.assertFalse(sf.may_contain("The Quill", store=self.store))
        idx = sf.SurfaceIndex(self.store)
        self.assertNotIn(self.pen, [h.node_id for h in idx.resolve(seg)])
        # An exact whole segment still names it.
        self.assertEqual([h.node_id for h in idx.resolve("The Quill")],
                         [self.pen])

    def test_a_slot_label_is_a_placeholder_by_its_shape(self):
        for n in ("User 2", "user 7", "User12", "new user 3"):
            self.assertTrue(sf.is_heading_junk(n), n)
        for n in ("User Research", "Userbase 2", "Power User 2"):
            self.assertFalse(sf.is_heading_junk(n), n)

    def test_the_generic_lists_still_decide_without_a_store(self):
        self.assertTrue(sf.is_heading_junk("Your team"))
        self.assertFalse(sf.may_contain("CEO"))
        self.assertFalse(sf.may_contain("demo"))
        self.assertTrue(sf.may_contain("Harborlight News"))
        ro = _mk(self.tmp, readonly=True)
        try:
            self.assertTrue(sf.may_contain("The Quill", store=ro))
        finally:
            ro.close()

    def test_the_seed_script_takes_and_reports_them(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "seed_seat_vocab_b1", ROOT / "scripts" / "seed_seat_vocab.py")
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
        terms = seed.parse_terms(
            "heading_junk_name Harborlight News\n"
            "containment_stop_name the quill\n")
        self.assertEqual(terms, [("heading_junk_name", "harborlight news"),
                                 ("containment_stop_name", "the quill")])
        with self.assertRaises(ValueError):
            seed.parse_terms("heading_junk_name " + "w " * 7)
        rep = seed.plan(self.store.db_path, terms)
        changed = {(c["name"], tuple(c["before"]), tuple(c["after"]))
                   for c in rep["changed"]}
        self.assertIn(("Harborlight News",
                       (False, False, True, False, False, True),
                       (False, False, True, False, True, True)), changed)
        self.assertIn(("The Quill", (False, False, True, False, False, True),
                       (False, False, True, False, False, False)), changed)
        self.assertEqual(self.store.list_seat_vocab(), [])    # dry run


# -------------------------------------------------------------------- B3 --
class RepeatNotAPersonTests(_Case):
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(routes_mod.router)
        self.client = TestClient(app)
        p = patch.object(routes_mod.memory, "_ensure_store",
                         return_value=self.store)
        p.start()
        self.addCleanup(p.stop)

    def test_a_second_not_a_person_on_a_learned_name_still_offers_undo(self):
        a = self.store.insert_person("Quenby Marsh", ts=NOW)
        b = self.store.insert_person("Quenby  Marsh", ts=NOW)
        r1 = self.client.post(f"/people/{a}/forget",
                              json={"not_a_person": True}).json()
        self.assertEqual((r1["learned"], r1["already_learned"]), (True, False))
        r2 = self.client.post(f"/people/{b}/forget",
                              json={"not_a_person": True}).json()
        self.assertEqual((r2["learned"], r2["already_learned"]), (True, True))
        self.assertEqual(r2["deleted"], "Quenby  Marsh")
        # A plain forget learns nothing.
        c = self.store.insert_person("Odile Brack", ts=NOW)
        r3 = self.client.post(f"/people/{c}/forget", json={}).json()
        self.assertEqual((r3["learned"], r3["already_learned"]),
                         (False, False))
        # The page shows the notice on `learned`.
        from app.api import profile_page
        self.assertIn("if(d.learned&&d.deleted) notPersonNotice(d.deleted);",
                      profile_page.PROFILE_PAGE)


class SameParentMirrorTests(_Case):
    def mirrors(self, eid):
        with self.store._lock:
            return [int(r["obj_id"]) for r in self.store._conn.execute(
                "SELECT obj_id FROM relations WHERE subj_type='entity' AND "
                "subj_id=? AND predicate='part_of' AND origin='context'",
                (eid,))]

    def test_setting_the_same_parent_again_restores_a_missing_mirror(self):
        p = self.store.resolve_entity("Harbor Line", "project", ts=NOW,
                                      kind_source="user")
        x = self.store.resolve_entity("Wave 4", "label", ts=NOW)
        self.assertTrue(self.store.set_entity_parent(x, p, source="rule"))
        self.assertEqual(self.mirrors(x), [p])
        with self.store._lock:
            self.store._conn.execute(
                "DELETE FROM relations WHERE predicate='part_of'")
            self.store._conn.commit()
        before = self.store.get_entity(x)
        with self.store._lock:
            ub = self.store._conn.execute(
                "SELECT updated_at FROM entities WHERE id=?", (x,)).fetchone()[0]
        self.assertTrue(self.store.set_entity_parent(x, p, source="rule"))
        self.assertEqual(self.mirrors(x), [p])
        after = self.store.get_entity(x)
        self.assertEqual((before["parent_id"], before["parent_source"]),
                         (after["parent_id"], after["parent_source"]))
        with self.store._lock:
            ua = self.store._conn.execute(
                "SELECT updated_at FROM entities WHERE id=?", (x,)).fetchone()[0]
        self.assertEqual(ua, ub)          # only the edge was written

    def test_the_same_parent_with_its_mirror_writes_nothing(self):
        p = self.store.resolve_entity("Harbor Line", "project", ts=NOW,
                                      kind_source="user")
        x = self.store.resolve_entity("Wave 4", "label", ts=NOW)
        self.assertTrue(self.store.set_entity_parent(x, p, source="rule"))
        n = self.store._conn.total_changes
        self.assertTrue(self.store.set_entity_parent(x, p, source="rule"))
        self.assertEqual(self.store._conn.total_changes, n)
        self.assertEqual(self.mirrors(x), [p])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
