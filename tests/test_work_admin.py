"""Tasks tab — the /work board: add, list, due, done/reopen, dismiss, and
the storage due-setter."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import self_profile
from app.storage import Store

NOW = 1_000_000_000.0


class SetDueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def test_set_and_clear_due_bumps_updated_at(self):
        fid = self.store.add_task("ship it", extracted_at=100.0)
        self.assertTrue(self.store.set_fact_due(fid, "Friday", 200.0))
        f = self.store.get_fact(fid)
        self.assertEqual((f["due"], f["updated_at"]), ("Friday", 200.0))
        self.assertTrue(self.store.set_fact_due(fid, "", 300.0))
        self.assertIsNone(self.store.get_fact(fid)["due"])

    def test_claims_have_no_due(self):
        fid = self.store.add_claim("just a fact", extracted_at=100.0)
        self.assertFalse(self.store.set_fact_due(fid, "Friday", 200.0))


class WorkEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")
        import app.api.routes as routes_mod
        for p in (patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store),
                  patch.object(routes_mod.memory, "index_fact",
                               lambda *a, **k: None),
                  patch("app.services.identity.user_identity",
                        return_value={"name": "Test Person",
                                      "source": "profile"})):
            p.start()
            self.addCleanup(p.stop)

    def test_add_task_with_self_owner(self):
        r = self.client.post("/work/add", json={
            "kind": "task", "text": "send the term sheet",
            "due": "Friday", "owner": "me"}).json()
        self.assertTrue(r["ok"])
        d = self.client.get("/work/list").json()
        self.assertEqual(len(d["open"]), 1)
        item = d["open"][0]
        self.assertEqual(item["text"], "send the term sheet")
        self.assertEqual(item["due"], "Friday")
        self.assertEqual(item["owner"], "Test Person")   # "me" -> self node
        self.assertEqual(item["review"], "approved")     # human add = gold

    def test_add_commitment_names_the_counterparty(self):
        self.client.post("/work/add", json={
            "kind": "commitment", "text": "send the revised pricing",
            "owner": "Marchetti"})
        item = self.client.get("/work/list").json()["open"][0]
        self.assertEqual(item["kind"], "commitment")
        self.assertEqual(item["from_person"], "Test Person")
        self.assertEqual(item["to_person"], "Marchetti")

    def test_bad_kind_and_empty_text_400(self):
        self.assertEqual(self.client.post(
            "/work/add", json={"kind": "wish", "text": "x"}).status_code, 400)
        self.assertEqual(self.client.post(
            "/work/add", json={"kind": "task", "text": " "}).status_code, 400)

    def test_done_reopen_cycle(self):
        fid = self.client.post("/work/add", json={
            "kind": "task", "text": "book the room"}).json()["fact_id"]
        self.client.post(f"/facts/{fid}/done")
        d = self.client.get("/work/list").json()
        self.assertEqual(d["open"], [])
        self.assertEqual(d["closed"][0]["fact_id"], fid)
        self.client.post(f"/facts/{fid}/reopen")
        d = self.client.get("/work/list").json()
        self.assertEqual(d["open"][0]["fact_id"], fid)
        self.assertEqual(d["closed"], [])

    def test_due_endpoint_and_dismiss_leaves_board_entirely(self):
        fid = self.client.post("/work/add", json={
            "kind": "task", "text": "draft the memo"}).json()["fact_id"]
        self.client.post(f"/facts/{fid}/due", json={"due": "Monday"})
        self.assertEqual(
            self.client.get("/work/list").json()["open"][0]["due"], "Monday")
        self.client.post(f"/facts/{fid}/dismiss")
        d = self.client.get("/work/list").json()
        self.assertEqual((d["open"], d["closed"]), ([], []))   # not even closed

    def test_bulk_done_and_dismiss(self):
        ids = []
        for text in ("a", "b", "c"):
            ids.append(self.client.post("/work/add", json={
                "kind": "task", "text": text}).json()["fact_id"])
        r = self.client.post("/work/bulk",
                             json={"ids": ids[:2], "action": "done"}).json()
        self.assertEqual(r["updated"], 2)
        d = self.client.get("/work/list").json()
        self.assertEqual([x["fact_id"] for x in d["open"]], [ids[2]])
        self.assertEqual({x["fact_id"] for x in d["closed"]}, set(ids[:2]))
        r = self.client.post("/work/bulk",
                             json={"ids": ids, "action": "dismiss"}).json()
        self.assertEqual(r["updated"], 3)
        d = self.client.get("/work/list").json()
        self.assertEqual((d["open"], d["closed"]), ([], []))

    def test_bulk_edit_and_due(self):
        ids = [
            self.client.post("/work/add", json={
                "kind": "task", "text": "old one"}).json()["fact_id"],
            self.client.post("/work/add", json={
                "kind": "commitment", "text": "old two",
                "owner": "Pat"}).json()["fact_id"],
        ]
        self.client.post("/work/bulk",
                         json={"ids": ids, "action": "edit",
                               "text": "shared rewrite"})
        self.client.post("/work/bulk",
                         json={"ids": ids, "action": "due", "due": "Friday"})
        open_items = self.client.get("/work/list").json()["open"]
        self.assertEqual(len(open_items), 2)
        for item in open_items:
            self.assertEqual(item["text"], "shared rewrite")
            self.assertEqual(item["due"], "Friday")


if __name__ == "__main__":
    unittest.main()
