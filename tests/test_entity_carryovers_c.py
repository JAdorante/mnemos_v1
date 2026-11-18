"""Entity model, Phase 5 part A: carry-overs from the Phase 4 check.

C1  An exact name beats a fuzzy neighbour. A sighting of a name a hidden row
    carries (its canonical name or a confirmed alias) binds that row, which
    stays hidden unless a filing or the user shows it again, instead of
    folding into a similar visible entity. So the repair's "hidden until a
    real sighting" sees the sighting and can lift. Forgotten names behave as
    before.
C2  Who hid a row is recorded (`entities.hidden_source`: user, repair, rule),
    so the repair's undo lifts only its own hide: a row the user showed again
    and someone hid again later stays hidden.

Every test runs against a real Store on a temp db; sightings go through the
extractor's own persist path.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes  # noqa: F401
import app.services.extractor  # noqa: F401
from app import storage as storage_mod
from app.events import Event, Modality
from app.services import entity_repair as er
from app.services import projects
from app.services.resolution import Resolver
from app.storage import Store

NOW = 1_700_000_000.0
DAY = 86400.0
MAIL = "google.mail"
NOTE = "assistant.claude"


def _mk(tmp: str, **kw) -> Store:
    return Store(db_path=Path(tmp) / "quill.db",
                 audio_dir=Path(tmp) / "audio", **kw)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_carry_c_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {
            "QUILL_DATA_DIR": self.tmp, "QUILL_HEADS": "off",
            "QUILL_USAGE_LEDGER": "0", "QUILL_ENTITY_CANDIDATES": "1"})
        env.start()
        self.addCleanup(env.stop)
        for var in ("QUILL_ENTITY_REPAIR_AUTO", "QUILL_ENTITY_REPAIR_NOTE_ONLY",
                    "QUILL_ENTITY_CANDIDATE_SESSIONS",
                    "QUILL_ENTITY_CANDIDATE_SESSION_GAP_S"):
            os.environ.pop(var, None)
        from app.services import resolution
        self.resolver = Resolver(store=self.store)
        for p in (patch.object(resolution, "resolver", self.resolver),
                  patch("app.storage.get_store", return_value=self.store),
                  patch.object(storage_mod, "_store", None),
                  patch.object(projects, "_scopes", lambda: []),
                  patch("app.services.identity.user_identity",
                        return_value={}),
                  patch("app.services.extractor._index_fact",
                        lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)
        projects.invalidate_filed_keys()
        self.addCleanup(projects.invalidate_filed_keys)
        from app.services.extractor import Extractor
        self.ex = Extractor(store=self.store)
        self._t = NOW

    def ent(self, name, kind, source="model") -> int:
        return self.store.resolve_entity(name, kind, ts=NOW,
                                         kind_source=source)

    def event(self, source=MAIL, gap=600.0) -> int:
        self._t += gap
        return int(self.store.insert(Event(
            time=self._t, modality=Modality.TEXT, raw="r", source=source,
            meta={})))

    def see(self, name, kind="org", *, source=MAIL, gap=DAY) -> int:
        """One extracted event in which the model read `name`."""
        ev = self.event(source, gap=gap)
        text = f"A note that mentions {name}."
        self.ex._persist_entities(
            {"entities": [{"name": name, "kind": kind, "confidence": 0.9,
                           "source_span": text}], "relations": []},
            ev, self._t, event_source=source, text=text)
        return ev

    def fact(self, eid, source=NOTE) -> None:
        ev = self.event(source)
        with self.store._lock:
            cur = self.store._conn.execute(
                "INSERT INTO facts (kind, text, source_event_id, "
                "extracted_at) VALUES ('claim', 'x', ?, ?)", (ev, self._t))
            fid = int(cur.lastrowid)
            self.store._conn.commit()
        self.store.add_relation("fact", fid, "about", "entity", eid,
                                origin="derived", ts=self._t)

    def hidden(self, eid) -> bool:
        return self.store.entity_is_hidden(eid)

    def hidden_source(self, eid):
        with self.store._lock:
            return self.store._conn.execute(
                "SELECT hidden_source FROM entities WHERE id = ?",
                (eid,)).fetchone()[0]

    def aliases(self, eid) -> list[str]:
        return [a.lower() for a in (self.store.get_entity(eid) or {})
                .get("aliases") or []]

    def bound_to(self, eid) -> int:
        return len(self.store.entity_mentions(entity_id=eid))


# -------------------------------------------------------------------- C1 --
class ExactBeforeFuzzyTests(_Case):
    def test_a_hidden_name_binds_its_own_row_not_a_fuzzy_neighbour(self):
        near = self.ent("Brindlemoor", "org")
        hid = self.ent("Brindlemore", "org")
        self.store.set_entity_hidden(hid, hidden=True, source="user")
        # The corrector alone would fold the spelling into the neighbour.
        from app.services.entity_correction import corrector
        self.assertEqual(int(corrector.match(
            "Brindlemore", self.store.all_entities()).ref), near)
        m = self.resolver.match_entity("Brindlemore", "org")
        self.assertEqual((m.status, m.via, m.entity_id),
                         ("existing", "hidden", hid))
        self.see("Brindlemore")
        self.assertEqual(self.bound_to(hid), 1)
        self.assertEqual(self.bound_to(near), 0)
        self.assertNotIn("brindlemore", self.aliases(near))
        self.assertTrue(self.hidden(hid))         # still hidden

    def test_a_confirmed_alias_on_a_hidden_row_wins_too(self):
        near = self.ent("Fenwold Works", "org")
        hid = self.ent("Fenwold Group", "org")
        from app.services.kg_keys import norm_name
        self.store.upsert_entity_alias(hid, "Fenwold Werks",
                                       norm_name("Fenwold Werks"),
                                       source="merge", confirmed=True)
        self.store.set_entity_hidden(hid, hidden=True, source="repair")
        m = self.resolver.match_entity("Fenwold Werks", "org")
        self.assertEqual((m.status, m.via, m.entity_id),
                         ("existing", "hidden", hid))
        self.assertNotEqual(m.entity_id, near)

    def test_a_note_only_hide_lifts_on_real_sightings_of_its_name(self):
        # The automatic repair hid a row only assistant notes named. A real
        # sighting of exactly that name must reach it, even with a visible
        # near-twin on the tab.
        twin = self.ent("Ravel Pilot Programs", "project")
        row = self.ent("Ravel Pilot Program", "project", None)
        self.fact(row, source=NOTE)
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            er.auto_run(self.store)
        self.assertTrue(self.hidden(row))
        self.assertFalse(self.hidden(twin))
        self.see("Ravel Pilot Program", "project", gap=DAY)
        self.assertTrue(self.hidden(row))          # one session is not enough
        self.see("Ravel Pilot Program", "project", gap=2 * DAY)
        self.assertFalse(self.hidden(row))         # lifted
        self.assertEqual(self.bound_to(row), 2)
        self.assertEqual(self.bound_to(twin), 0)

    def test_the_user_shows_the_hidden_row_not_the_neighbour(self):
        near = self.ent("Brindlemoor", "org")
        hid = self.ent("Brindlemore", "org")
        self.store.set_entity_hidden(hid, hidden=True, source="rule")
        eid = self.resolver.resolve_entity("Brindlemore", "org", ts=NOW,
                                           kind_source="user")
        self.assertEqual(eid, hid)
        self.assertFalse(self.hidden(hid))
        self.assertIsNone(self.hidden_source(hid))
        self.assertNotIn("brindlemore", self.aliases(near))

    def test_a_visible_confirmed_alias_binds_its_row(self):
        row = self.ent("Fenwold Works", "org")
        from app.services.kg_keys import norm_name
        self.store.upsert_entity_alias(row, "Fenwold Werks",
                                       norm_name("Fenwold Werks"),
                                       source="user", confirmed=True)
        m = self.resolver.match_entity("Fenwold Werks", "org")
        self.assertEqual((m.status, m.via, m.entity_id),
                         ("existing", "alias", row))
        self.see("Fenwold Werks")
        self.assertEqual(self.bound_to(row), 1)


class ForgottenNamesKeepTheirRuleTests(_Case):
    def test_a_forgotten_name_with_no_row_is_never_folded(self):
        near = self.ent("Brindlemoor", "org")
        gone = self.ent("Brindlemore", "org")
        self.store.delete_entity(gone, tombstone=True, reason="forget")
        m = self.resolver.match_entity("Brindlemore", "org")
        self.assertEqual((m.status, m.forgotten), ("new", True))
        self.see("Brindlemore")
        self.see("Brindlemore", gap=2 * DAY)
        self.assertNotIn("brindlemore", self.aliases(near))
        self.assertIsNone(self.store.find_entity_exact("Brindlemore"))
        self.assertEqual(self.bound_to(near), 0)

    def test_a_forgotten_name_a_hidden_row_carries_binds_that_row(self):
        self.ent("Brindlemoor", "org")
        hid = self.ent("Brindlemore", "org")
        self.store.set_entity_hidden(hid, hidden=True, source="rule")
        # The same name was forgotten earlier (a dismissed candidate).
        self.store.tombstone_entity_name("Brindlemore", reason="test")
        self.assertTrue(self.store.is_entity_tombstoned("Brindlemore"))
        m = self.resolver.match_entity("Brindlemore", "org")
        self.assertEqual((m.status, m.via, m.entity_id, m.forgotten),
                         ("existing", "hidden", hid, True))

    def test_a_known_persons_name_on_a_hidden_row_is_still_refused(self):
        self.store.resolve_person("Odile Brack", ts=NOW)
        hid = self.ent("Odile Brack", "tool", None)
        self.store.set_entity_hidden(hid, hidden=True, source="repair")
        m = self.resolver.match_entity("Odile Brack", "tool")
        self.assertEqual((m.status, m.via), ("rejected", "known_person"))
        # An org may share a person's name (a founder's firm), as before.
        m = self.resolver.match_entity("Odile Brack", "org")
        self.assertEqual((m.status, m.entity_id), ("existing", hid))


# -------------------------------------------------------------------- C2 --
class HideSourceTests(_Case):
    def note_only_row(self) -> tuple[int, dict]:
        row = self.ent("Ravel Pilot Program", "project", None)
        self.fact(row, source=NOTE)
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            res = er.auto_run(self.store)
        self.assertTrue(self.hidden(row))
        return row, res

    def test_every_hide_says_who_made_it(self):
        row, _ = self.note_only_row()
        self.assertEqual(self.hidden_source(row), "repair")
        value = self.store.resolve_entity("$49", "thing", ts=NOW,
                                          kind_source="rule", hidden=True)
        self.assertEqual(self.hidden_source(value), "rule")
        x = self.ent("Wave 4", "label")
        self.store.set_entity_hidden(x, hidden=True, source="user")
        self.assertEqual(self.hidden_source(x), "user")
        self.store.set_entity_hidden(x, hidden=True, source="somebody")
        self.assertIsNone(self.hidden_source(x))        # not a known source
        self.store.set_entity_hidden(x, hidden=True, source="rule")
        self.store.set_entity_hidden(x, hidden=False)
        self.assertIsNone(self.hidden_source(x))        # shown: cleared
        # Cleanup hides by rule.
        from app.services import ambient_cleanup as ac
        y = self.ent("Sprint 9", "project")
        ac.apply(self.store, {"people": [], "entities": [
            {"id": y, "action": "hide", "name": "Sprint 9"}]})
        self.assertEqual(self.hidden_source(y), "rule")
        # A filing or the user showing a row clears it too.
        self.store.resolve_entity("Sprint 9", "project", ts=NOW,
                                  kind_source="user")
        self.assertFalse(self.hidden(y))
        self.assertIsNone(self.hidden_source(y))

    def test_undo_lifts_the_repairs_own_hide(self):
        row, res = self.note_only_row()
        out = er.undo(self.store, run_id=res["run_id"])
        self.assertEqual(out["refused"], [])
        self.assertFalse(self.hidden(row))
        self.assertIsNone(self.hidden_source(row))

    def test_undo_leaves_a_later_hide_alone(self):
        row, res = self.note_only_row()
        # The user shows it again (adds it by hand), then cleanup hides it
        # by rule: by value the row looks as the repair left it.
        self.store.resolve_entity("Ravel Pilot Program", "project", ts=NOW,
                                  kind_source="user")
        self.assertFalse(self.hidden(row))
        self.store.set_entity_hidden(row, hidden=True, source="rule")
        out = er.undo(self.store, run_id=res["run_id"])
        self.assertEqual([r["why"] for r in out["refused"]], ["changed"])
        self.assertTrue(self.hidden(row))
        self.assertEqual(self.hidden_source(row), "rule")

    def test_undo_refuses_a_hide_the_user_made(self):
        row, res = self.note_only_row()
        self.store.set_entity_hidden(row, hidden=False)
        self.store.set_entity_hidden(row, hidden=True, source="user")
        out = er.undo(self.store, run_id=res["run_id"])
        self.assertEqual([r["why"] for r in out["refused"]],
                         ["changed_by_user"])
        self.assertTrue(self.hidden(row))

    def test_a_real_sighting_does_not_lift_someone_elses_hide(self):
        row, _ = self.note_only_row()
        self.store.set_entity_hidden(row, hidden=False)
        self.store.set_entity_hidden(row, hidden=True, source="user")
        self.see("Ravel Pilot Program", "project", gap=DAY)
        self.see("Ravel Pilot Program", "project", gap=2 * DAY)
        self.assertTrue(self.hidden(row))

    def test_a_hide_from_before_the_column_is_undone_as_before(self):
        row, res = self.note_only_row()
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE entities SET hidden_source = NULL WHERE id = ?",
                (row,))
            self.store._conn.commit()
        out = er.undo(self.store, run_id=res["run_id"])
        self.assertEqual(out["refused"], [])
        self.assertFalse(self.hidden(row))


class HideSourceMigrationTests(unittest.TestCase):
    def test_the_column_is_added_once_and_old_stores_still_read(self):
        tmp = tempfile.mkdtemp(prefix="quill_carry_c_mig_")
        db = Path(tmp) / "quill.db"
        st = _mk(tmp)
        eid = st.resolve_entity("Harbor Line", "project", ts=NOW,
                                kind_source="user")
        st.close()
        # A store migrated before Phase 5: the column is not there.
        con = sqlite3.connect(db)
        con.execute("ALTER TABLE entities DROP COLUMN hidden_source")
        con.commit()
        con.close()
        ro = _mk(tmp, readonly=True)
        try:
            self.assertTrue(ro.get_entity(eid)["kind_source"] == "user")
            self.assertEqual(ro.entity_repair_restore(
                eid, "hidden", before={"hidden": False},
                expect={"hidden": True}), (False, "changed_by_user"))
        finally:
            ro.close()
        for _ in range(2):
            st = _mk(tmp)
            st.set_entity_hidden(eid, hidden=True, source="repair")
            st.close()
        con = sqlite3.connect(db)
        cols = [r[1] for r in con.execute("PRAGMA table_info(entities)")]
        src = con.execute("SELECT hidden_source FROM entities WHERE id = ?",
                          (eid,)).fetchone()[0]
        con.close()
        self.assertEqual(cols.count("hidden_source"), 1)
        self.assertEqual(src, "repair")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
