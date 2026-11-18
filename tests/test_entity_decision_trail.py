"""Entity model, Phase 5: every entity route that changes a row records the
user's verdict in the shared decision trail (`decisions.record`).

Surfaces `entities.rename|alias|kind|parent|note|detail|merge|add|forget`
(subject `entity:<id>`; a dismissed candidate is `entities.forget` with
subject `candidate:<name hash>`), `entities.repair` (an Inbox proposal
accepted or dismissed, a run or a change undone, a reviewed plan applied by
the operator) and `people.not_a_person` (and its undo). A name the user
forgot is never kept: a forget keeps the kind and the name's hash.

Every test runs against a real Store on a temp db, through the routes.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes as routes_mod
from app import storage as storage_mod
from app.events import Event, Modality
from app.services import entity_repair as er
from app.services import inbox
from app.services import projects
from app.storage import Store

NOW = 1_700_000_000.0
DAY = 86400.0
ROOT = Path(__file__).resolve().parents[1]


def _mk(tmp: str, **kw) -> Store:
    return Store(db_path=Path(tmp) / "quill.db",
                 audio_dir=Path(tmp) / "audio", **kw)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_trail_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp,
                                      "QUILL_ENTITY_CANDIDATES": "1"})
        env.start()
        self.addCleanup(env.stop)
        for var in ("QUILL_ENTITY_REPAIR_AUTO", "QUILL_ENTITY_REPAIR_NOTE_ONLY"):
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

    def ent(self, name, kind="org", source="model") -> int:
        return self.store.resolve_entity(name, kind, ts=NOW,
                                         kind_source=source)

    def trail(self, surface=None) -> list[dict]:
        rows = self.store.list_decisions(surface=surface, limit=500)
        return list(reversed(rows))          # oldest first

    def last(self, surface) -> dict:
        rows = self.trail(surface)
        self.assertTrue(rows, f"no {surface} decision")
        return rows[-1]

    def raw_trail(self) -> str:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT * FROM decisions").fetchall()
        return json.dumps([list(r) for r in rows])


# ------------------------------------------------------- the entity rows --
class EntityRouteTrailTests(_Case):
    def test_rename(self):
        e = self.ent("Harbor Lne", "project")
        r = self.client.post(f"/entities/{e}/rename", json={"name": "Harbor Line"})
        self.assertEqual(r.status_code, 200, r.text)
        d = self.last("entities.rename")
        self.assertEqual((d["subject"], d["verdict"], d["actor"], d["via"]),
                         (f"entity:{e}", "edited", "user", "route"))
        self.assertEqual(d["shown"], {"name": "Harbor Lne", "kind": "project"})
        self.assertEqual(d["edit"], {"name": "Harbor Line"})

    def test_kind_change_is_an_edit_and_a_confirmation_is_an_approval(self):
        e = self.ent("Wave 4", "project")
        self.client.post(f"/entities/{e}/kind", json={"kind": "label"})
        d = self.last("entities.kind")
        self.assertEqual(d["verdict"], "edited")
        self.assertEqual(d["shown"]["kind"], "project")
        self.assertEqual(d["shown"]["kind_source"], "model")
        self.assertEqual(d["edit"], {"kind": "label"})
        # The same kind again on the row the user already kinded changes
        # nothing, so it is no verdict.
        self.client.post(f"/entities/{e}/kind", json={"kind": "label"})
        self.assertEqual(len(self.trail("entities.kind")), 1)
        # The kind a model row already had, confirmed by the user: only its
        # source moves.
        f = self.ent("Wave 5", "label")
        self.client.post(f"/entities/{f}/kind", json={"kind": "label"})
        d = self.last("entities.kind")
        self.assertEqual((d["subject"], d["verdict"], d["edit"]),
                         (f"entity:{f}", "approved", None))

    def test_a_refused_or_invalid_kind_records_nothing(self):
        e = self.ent("Wave 4", "project")
        self.assertEqual(self.client.post(f"/entities/{e}/kind",
                                          json={"kind": "wizard"}).status_code,
                         400)
        with patch.object(Store, "set_entity_kind", return_value=False):
            self.assertEqual(self.client.post(
                f"/entities/{e}/kind", json={"kind": "label"}).status_code, 409)
        self.assertEqual(self.client.post("/entities/424242/kind",
                                          json={"kind": "label"}).status_code,
                         404)
        self.assertEqual(self.trail("entities.kind"), [])

    def test_the_map_reclassify_is_a_kind_decision_too(self):
        e = self.ent("Quay Ledger", "tool")
        r = self.client.post("/graph/constellation/reclassify",
                             json={"id": f"entity:{e}", "kind": "org"})
        self.assertEqual(r.status_code, 200, r.text)
        d = self.last("entities.kind")
        self.assertEqual((d["subject"], d["verdict"], d["via"]),
                         (f"entity:{e}", "edited", "constellation"))
        self.assertEqual(d["edit"], {"kind": "org"})

    def test_parent_set_and_removed(self):
        p = self.ent("Harbor Line", "project", "user")
        x = self.ent("Wave 4", "label")
        self.client.post(f"/entities/{x}/parent", json={"parent_id": p})
        d = self.last("entities.parent")
        self.assertEqual(d["verdict"], "edited")
        self.assertIsNone(d["shown"]["parent"])
        self.assertEqual(d["edit"], {"parent": {"id": p, "name": "Harbor Line"}})
        # The same parent again: nothing moved, no verdict.
        self.client.post(f"/entities/{x}/parent", json={"parent_id": p})
        self.assertEqual(len(self.trail("entities.parent")), 1)
        self.client.post(f"/entities/{x}/parent", json={"parent_id": None})
        d = self.last("entities.parent")
        self.assertEqual(d["shown"]["parent"], {"id": p, "name": "Harbor Line"})
        self.assertEqual(d["edit"], {"parent": None})

    def test_alias_note_and_detail(self):
        e = self.ent("Harbor Line", "project", "user")
        self.client.post(f"/entities/{e}/alias", json={"alias": "the Line"})
        d = self.last("entities.alias")
        self.assertEqual((d["verdict"], d["edit"]), ("edited",
                                                     {"alias": "the Line"}))
        r = self.client.post(f"/entities/{e}/note",
                             json={"text": "Harbor Line ships in March."})
        d = self.last("entities.note")
        self.assertEqual(d["meta"]["fact_id"], r.json()["fact_id"])
        self.client.post(f"/entities/{e}/detail",
                         json={"key": "status", "value": "active"})
        d = self.last("entities.detail")
        self.assertEqual((d["verdict"], d["edit"]),
                         ("edited", {"status": "active"}))
        self.client.post(f"/entities/{e}/detail",
                         json={"key": "status", "value": ""})
        self.assertEqual(self.last("entities.detail")["edit"],
                         {"status": None})

    def test_alias_proposals_confirmed_or_rejected(self):
        from app.services.kg_keys import norm_name
        e = self.ent("Harbor Line", "project", "user")
        a = self.store.upsert_entity_alias(e, "Harbour Line",
                                           norm_name("Harbour Line"),
                                           source="embedding")
        b = self.store.upsert_entity_alias(e, "Harbor Lyne",
                                           norm_name("Harbor Lyne"),
                                           source="embedding")
        self.client.post(f"/entities/aliases/{a['id']}/confirm")
        self.client.post(f"/entities/aliases/{b['id']}/reject")
        rows = self.trail("entities.alias")
        self.assertEqual([(r["verdict"], r["shown"]["alias"]) for r in rows],
                         [("approved", "Harbour Line"),
                          ("rejected", "Harbor Lyne")])
        self.assertEqual(rows[0]["shown"]["proposed_by"], "embedding")
        # The Inbox's answers trail the same way, and its undo too.
        c = self.store.upsert_entity_alias(e, "Harbr Line",
                                           norm_name("Harbr Line"),
                                           source="embedding")
        out = inbox.act(self.store, f"alias:{c['id']}", "yes")
        self.assertTrue(out["ok"], out)
        d = self.last("entities.alias")
        self.assertEqual((d["verdict"], d["via"]), ("approved", "inbox"))
        inbox.undo(out["undo_token"])
        d = self.last("entities.alias")
        self.assertEqual((d["verdict"], d["via"]), ("undone", "inbox"))

    def test_merge_and_manual_add(self):
        a = self.ent("Vesper Logistics", "org")
        b = self.ent("Vesper-Logistics", "org")
        r = self.client.post(f"/entities/{b}/merge", json={"into": a})
        self.assertEqual(r.status_code, 200, r.text)
        d = self.last("entities.merge")
        self.assertEqual((d["subject"], d["verdict"]), (f"entity:{b}",
                                                         "approved"))
        self.assertEqual(d["shown"]["name"], "Vesper-Logistics")
        self.assertEqual(d["shown"]["into"]["id"], a)
        self.assertEqual(d["meta"]["merge_id"], r.json()["merge_id"])
        r = self.client.post("/entities", json={"name": "Osprey Yard",
                                                "kind": "place"})
        d = self.last("entities.add")
        self.assertEqual((d["subject"], d["verdict"]),
                         (f"entity:{r.json()['id']}", "approved"))
        self.assertEqual(d["shown"], {"name": "Osprey Yard", "kind": "place"})
        self.assertTrue(d["meta"]["created"])

    def test_a_forget_keeps_the_kind_and_the_hash_not_the_name(self):
        e = self.ent("Quillmoor Analytics", "org")
        self.client.post(f"/entities/{e}/forget")
        d = self.last("entities.forget")
        self.assertEqual((d["subject"], d["verdict"]), (f"entity:{e}",
                                                         "dismissed"))
        self.assertEqual(d["shown"], {
            "kind": "org",
            "name_hash": self.store.entity_tombstone_hash(
                "Quillmoor Analytics")})
        self.assertNotIn("quillmoor", self.raw_trail().lower())

    def test_a_forget_takes_the_name_out_of_earlier_verdicts_too(self):
        e = self.ent("Quillmoor Analytics", "org")
        p = self.ent("Harbor Line", "project", "user")
        child = self.ent("Wave 4", "label")
        self.client.post(f"/entities/{e}/kind", json={"kind": "project"})
        self.client.post(f"/entities/{e}/alias", json={"alias": "Quillmoor AI"})
        self.client.post(f"/entities/{e}/detail",
                         json={"key": "url", "value": "quillmoor ai docs"})
        self.client.post(f"/entities/{e}/rename",
                         json={"name": "Quillmoor Labs"})
        # Another row's verdict that names it (put inside it).
        self.client.post(f"/entities/{child}/parent", json={"parent_id": e})
        self.client.post(f"/entities/{p}/rename", json={"name": "Harbor Lane"})
        self.assertIn("quillmoor", self.raw_trail().lower())
        self.client.post(f"/entities/{e}/forget")
        raw = self.raw_trail().lower()
        self.assertNotIn("quillmoor", raw)
        # What it kept: the kinds, the hashes, and the unrelated rows.
        k = self.trail("entities.kind")[0]
        self.assertEqual((k["shown"]["kind"], k["edit"]),
                         ("org", {"kind": "project"}))
        self.assertEqual(k["shown"]["name_hash"],
                         self.store.entity_tombstone_hash("Quillmoor Analytics"))
        d = self.last("entities.parent")
        self.assertEqual(d["shown"]["name"], "Wave 4")
        self.assertEqual(d["edit"]["parent"], {
            "id": e, "name": None,
            "name_hash": self.store.entity_tombstone_hash("Quillmoor Labs")})
        self.assertIn("harbor lane", raw)

    def test_a_dismissed_candidate_is_a_forget_without_its_name(self):
        for i, day in enumerate((1, 1)):
            ev = int(self.store.insert(Event(
                time=NOW + i * 60, modality=Modality.TEXT, raw="r",
                source="google.mail", meta={})))
            self.store.record_entity_mention(
                "Quillmoor Analytics", proposed_kind="org", confidence=0.8,
                source_class="connector_mail", event_source="google.mail",
                event_id=ev, observed_at=NOW + i * 60)
        r = self.client.post("/entities/candidates/dismiss",
                             json={"name": "Quillmoor Analytics"})
        self.assertEqual(r.status_code, 200, r.text)
        h = self.store.entity_tombstone_hash("Quillmoor Analytics")
        d = self.last("entities.forget")
        self.assertEqual((d["subject"], d["verdict"]), (f"candidate:{h}",
                                                         "dismissed"))
        self.assertEqual(d["shown"], {"kind": "org", "name_hash": h})
        self.assertTrue(d["meta"]["candidate"])
        self.assertNotIn("quillmoor", self.raw_trail().lower())

    def test_a_route_that_changes_nothing_records_nothing(self):
        e = self.ent("Harbor Line", "project")
        self.assertEqual(self.client.post("/entities/424242/rename",
                                          json={"name": "X"}).status_code, 400)
        self.assertEqual(self.client.post("/entities/424242/forget"
                                          ).status_code, 404)
        self.assertEqual(self.client.post(f"/entities/{e}/merge",
                                          json={"into": e}).status_code, 400)
        self.assertEqual(self.client.post("/entities/candidates/dismiss",
                                          json={"name": "nothing"}
                                          ).status_code, 404)
        self.assertEqual(self.trail(), [])


# ---------------------------------------------------------------- repair --
class RepairTrailTests(_Case):
    def setUp(self):
        super().setUp()
        self.a = self.ent("Vesper Logistics", "org")
        self.b = self.ent("Vesper-Logistics", "org")
        self.c = self.ent("coastal survey", "project", None)
        self.code = self.ent("Sprint 9", "project", None)   # automatic
        for _ in range(2):
            ev = int(self.store.insert(Event(
                time=NOW, modality=Modality.TEXT, raw="r",
                source="google.mail", meta={})))
            with self.store._lock:
                cur = self.store._conn.execute(
                    "INSERT INTO facts (kind, text, source_event_id, "
                    "extracted_at) VALUES ('claim', 'x', ?, ?)", (ev, NOW))
                fid = int(cur.lastrowid)
                self.store._conn.commit()
            self.store.add_relation("fact", fid, "about", "entity", self.c,
                                    origin="derived", ts=NOW)
        with patch.dict(os.environ, {"QUILL_ENTITY_REPAIR_AUTO": "1"}):
            self.run = er.auto_run(self.store)

    def items(self) -> dict:
        data = inbox.collect(self.store)
        return {i["id"]: i for i in data["needs_you"] + data["can_wait"]
                if i["id"].startswith("entityfix:")}

    def test_inbox_answers_and_their_undo(self):
        items = self.items()
        merge = next(i for i in items.values() if i["kind"] == "confirm")
        rek = next(i for i in items.values() if i["kind"] == "review")
        self.assertTrue(inbox.act(self.store, merge["id"], "no")["ok"])
        d = self.last("entities.repair")
        self.assertEqual((d["subject"], d["verdict"], d["via"]),
                         (f"repair_proposal:{merge['id'].split(':')[1]}",
                          "rejected", "inbox"))
        self.assertEqual(d["shown"]["action"], "merge")
        out = inbox.act(self.store, rek["id"], "yes")
        self.assertTrue(out["ok"], out)
        d = self.last("entities.repair")
        self.assertEqual((d["verdict"], d["meta"]["status"]),
                         ("approved", "accepted"))
        self.assertEqual(d["shown"]["name"], "coastal survey")
        inbox.undo(out["undo_token"])
        d = self.last("entities.repair")
        self.assertEqual((d["subject"], d["verdict"]),
                         (f"repair_proposal:{rek['id'].split(':')[1]}",
                          "undone"))
        self.assertIsNotNone(d["meta"].get("undoes"))

    def test_an_accepted_drop_is_a_hide_that_forgets_nothing(self):
        # Fix pass A8: an Inbox "remove" hides the row (undoable); nothing
        # is deleted or tombstoned, so the trail keeps the name it showed.
        e = self.ent("Quillmoor Analytics", "org")
        a = {"entity_id": e, "name": "Quillmoor Analytics", "kind": "org",
             "action": "drop", "rule": "common_noun", "tombstone": True,
             "route": "review", "state": er._snapshot(
                 {**self.store.get_entity(e), "hidden": False})}
        with patch.object(er, "check", return_value=[]):
            pid = er.propose(self.store, a, run_id="t")
            res = er.decide(self.store, pid, "accept", via="inbox")
        self.assertTrue(res["ok"], res)
        self.assertTrue(self.store.entity_is_hidden(e))
        self.assertFalse(self.store.is_entity_tombstoned("Quillmoor Analytics"))
        d = self.last("entities.repair")
        self.assertEqual(d["shown"]["name"], "Quillmoor Analytics")
        self.assertNotIn("name_hash", d["shown"])

    def test_the_undo_route(self):
        r = self.client.post("/entities/repair/undo",
                             json={"run_id": self.run["run_id"]})
        self.assertEqual(r.status_code, 200, r.text)
        d = self.last("entities.repair")
        self.assertEqual((d["subject"], d["verdict"], d["via"]),
                         (f"repair_run:{self.run['run_id']}", "undone",
                          "route"))
        self.assertEqual(d["meta"]["undone"], len(r.json()["undone"]))
        # Nothing to undo there: no verdict recorded.
        n = len(self.trail("entities.repair"))
        self.client.post("/entities/repair/undo", json={"run_id": "nope"})
        self.assertEqual(len(self.trail("entities.repair")), n)
        # Every change refused (the user changed the field since): nothing
        # undone, so no verdict either.
        refused = {"undone": [], "refused": [{"change_id": 1}],
                   "withdrawn": 0}
        with patch.object(er, "undo", return_value=refused):
            r = self.client.post("/entities/repair/undo",
                                 json={"run_id": self.run["run_id"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.trail("entities.repair")), n)

    def test_a_stale_answer_records_nothing(self):
        rek = next(i for i in self.items().values() if i["kind"] == "review")
        pid = int(rek["id"].split(":")[1])
        with patch.object(er, "check", return_value=["it moved"]):
            res = er.decide(self.store, pid, "accept", via="inbox")
        self.assertEqual(res["status"], "stale")
        self.assertEqual([d for d in self.trail("entities.repair")
                          if d["subject"] == f"repair_proposal:{pid}"], [])

    def test_the_operator_script(self):
        spec = importlib.util.spec_from_file_location(
            "entity_repair_trail", ROOT / "scripts" / "entity_repair.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        db = self.store.db_path
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(script.main(["--db", str(db), "--apply"]), 0)
        st = _mk(self.tmp)
        try:
            d = [x for x in st.list_decisions(surface="entities.repair")
                 if x["actor"] == "operator"][0]
            self.assertEqual((d["verdict"], d["via"]), ("approved", "script"))
            self.assertEqual(d["meta"]["mode"], "reviewed")
            run_id = d["subject"].split(":", 1)[1]
        finally:
            st.close()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(script.main(["--db", str(db), "--undo",
                                          run_id]), 0)
        st = _mk(self.tmp)
        try:
            d = st.list_decisions(surface="entities.repair", limit=1)[0]
            self.assertEqual((d["subject"], d["verdict"], d["actor"]),
                             (f"repair_run:{run_id}", "undone", "operator"))
        finally:
            st.close()


# ------------------------------------------------------- not a person --
class NotAPersonTrailTests(_Case):
    def test_not_a_person_and_its_undo_keep_only_the_hash(self):
        p = self.store.insert_person("Quenby Marsh", ts=NOW)
        r = self.client.post(f"/people/{p}/forget", json={"not_a_person": True})
        self.assertTrue(r.json()["learned"])
        h = self.store.entity_tombstone_hash("Quenby Marsh")
        d = self.last("people.not_a_person")
        self.assertEqual((d["subject"], d["verdict"]), (f"person_name:{h}",
                                                         "rejected"))
        self.assertEqual(d["shown"], {"name_hash": h})
        r = self.client.post("/people/not-a-person/undo",
                             json={"name": "Quenby Marsh"})
        self.assertTrue(r.json()["unlearned"])
        d = self.last("people.not_a_person")
        self.assertEqual((d["subject"], d["verdict"]), (f"person_name:{h}",
                                                         "undone"))
        self.assertEqual(d["meta"]["undoes"],
                         self.trail("people.not_a_person")[0]["id"])
        self.assertNotIn("quenby", self.raw_trail().lower())
        # An undo of a name nothing learned takes nothing back.
        r = self.client.post("/people/not-a-person/undo",
                             json={"name": "Never Learned"})
        self.assertFalse(r.json()["unlearned"])
        self.assertEqual(len(self.trail("people.not_a_person")), 2)

    def test_a_plain_person_forget_is_not_a_not_a_person_verdict(self):
        p = self.store.insert_person("Odile Brack", ts=NOW)
        self.client.post(f"/people/{p}/forget", json={})
        self.assertEqual(self.trail("people.not_a_person"), [])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
