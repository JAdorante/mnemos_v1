"""Org living brief route smoke + org-people payload shape."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

NOW = time.time()


def _mk(tmp: str):
    from app.storage import Store
    return Store(db_path=Path(tmp) / "quill.db")


class OrgBriefTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="org_brief_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.eid = self.store.resolve_entity("Acme Robotics", "org", ts=NOW)
        self.pid = self.store.resolve_person("Ada Lovelace", ts=NOW)
        self.store.add_relation(
            "person", self.pid, "works_at", "entity", self.eid,
            origin="user", ts=NOW, quote="Ada works at Acme",
            source_class="user")

    def test_org_html_smoke(self) -> None:
        r = self.client.get(f"/org/{self.eid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Living brief", r.text)
        self.assertIn("text/html", r.headers.get("content-type", ""))

    def test_org_data_shape(self) -> None:
        r = self.client.get(f"/org/{self.eid}/data")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["entity"]["id"], self.eid)
        self.assertEqual(body["entity"]["name"], "Acme Robotics")
        self.assertIn("people", body)
        self.assertIn("facts", body)
        self.assertIn("work", body)
        self.assertIn("details", body)
        self.assertIn("org_people", body)
        op = body["org_people"]
        self.assertTrue(op.get("found"))
        self.assertEqual(op.get("entity_id"), self.eid)

    def test_org_people_api(self) -> None:
        r = self.client.get("/graph/org-people",
                            params={"name": "Acme Robotics"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("found"))
        self.assertIsInstance(body.get("people"), list)

    def test_missing_org_404(self) -> None:
        r = self.client.get("/org/999999/data")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
