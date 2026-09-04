"""Web "Start meeting" — manual session start/end + routes."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))


def _store(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db", audio_dir=Path(td) / "audio")


class ManualStartTests(unittest.TestCase):
    def setUp(self):
        from app.services import meeting_session as ms
        ms.reset()
        self.td = tempfile.TemporaryDirectory()
        self.store = _store(self.td.name)

    def tearDown(self):
        from app.services import meeting_session as ms
        ms.end(store=self.store)
        ms.reset()
        try:
            self.store.close()
        except Exception:
            pass
        try:
            self.td.cleanup()
        except (OSError, PermissionError):
            pass  # Windows: lingering sqlite handle in a temp dir

    def test_start_manual_activates_with_consent(self):
        from app.services import meeting_session as ms
        with patch("app.services.meeting_session.get_store",
                   return_value=self.store):
            out = ms.start_manual(title="Web standup",
                                  consent=ms.CONSENT_TRANSCRIPT,
                                  store=self.store)
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("started"))
        st = ms.current()
        self.assertEqual(st["status"], ms.STATUS_ACTIVE)
        self.assertEqual(st["consent"], ms.CONSENT_TRANSCRIPT)
        self.assertEqual(st["source"], ms.SOURCE_MANUAL)
        self.assertEqual(st["title"], "Web standup")
        # Ingest is open while active with recording consent.
        self.assertTrue(ms.should_ingest("audio.web_mic"))
        self.assertTrue(ms.should_ingest("audio.web_tab"))

    def test_start_manual_rejects_bad_consent(self):
        from app.services import meeting_session as ms
        out = ms.start_manual(consent="skip", store=self.store)
        self.assertFalse(out.get("ok"))

    def test_double_start_refused(self):
        from app.services import meeting_session as ms
        with patch("app.services.meeting_session.get_store",
                   return_value=self.store):
            first = ms.start_manual(title="A", store=self.store)
            self.assertTrue(first.get("ok"))
            second = ms.start_manual(title="B", store=self.store)
        self.assertFalse(second.get("ok"))
        self.assertIn("already live", second.get("error") or "")
        self.assertEqual(ms.current()["title"], "A")

    def test_end_closes_session(self):
        from app.services import meeting_session as ms
        with patch("app.services.meeting_session.get_store",
                   return_value=self.store):
            ms.start_manual(title="A", store=self.store)
            out = ms.end(store=self.store)
        self.assertTrue(out.get("ok"))
        self.assertIsNone(ms.current())

    def test_default_title_and_duration_clamp(self):
        from app.services import meeting_session as ms
        with patch("app.services.meeting_session.get_store",
                   return_value=self.store):
            out = ms.start_manual(title="  ", duration_min=99999,
                                  store=self.store)
        self.assertTrue(out.get("ok"), out)
        st = ms.current()
        self.assertEqual(st["title"], "Meeting")
        self.assertLessEqual(st["t_end"] - st["t_start"], 600 * 60 + 1)


class ManualStartRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)
        cls.client.get("/auth/status")  # prime the CSRF cookie

    def _post(self, url, json=None):
        csrf = self.client.get("/auth/status").json()["csrf_token"]
        return self.client.post(url, json=json or {},
                                headers={"x-csrf-token": csrf})

    def setUp(self):
        from app.services import meeting_session as ms
        ms.reset()

    def tearDown(self):
        from app.services import meeting_session as ms
        try:
            ms.end()
        except Exception:
            pass
        ms.reset()

    def test_status_start_end_roundtrip(self):
        r = self.client.get("/meeting/session/status")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["active"])

        r = self._post("/meeting/session/start",
                       json={"title": "Route meeting",
                             "consent": "transcript_only"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("ok"), r.json())

        r = self.client.get("/meeting/session/status")
        self.assertTrue(r.json()["active"])
        self.assertEqual(r.json()["title"], "Route meeting")

        r = self._post("/meeting/session/end")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/meeting/session/status")
        self.assertFalse(r.json()["active"])

    def test_bad_consent_400(self):
        r = self._post("/meeting/session/start", json={"consent": "nope"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
