"""Capture consent + kill-switch toggle tests (privacy hardening)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _patch_data_dir(tmp: str):
    """Redirect consent/kill-switch file paths into tmp without touching frozen settings."""
    consent_p = patch("app.services.capture_consent._path",
                      lambda: Path(tmp) / "capture_consent.json")
    kill_p = patch("app.services.hardening._overrides_path",
                   lambda: Path(tmp) / "kill_switches.json")
    return consent_p, kill_p


class CaptureConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cp, kp = _patch_data_dir(self.tmp)
        cp.start(); kp.start()
        self.addCleanup(cp.stop)
        self.addCleanup(kp.stop)
        import app.services.capture_consent as cc
        cc._cached = None

    def test_default_off(self):
        from app.services import capture_consent as cc
        st = cc.load(force=True)
        self.assertFalse(st["consented"])
        self.assertFalse(cc.allows("mic"))
        self.assertFalse(cc.any_recording_source())

    def test_save_and_allows(self):
        from app.services import capture_consent as cc
        st = cc.save({"mic": True, "webcam": False, "screen": True,
                      "system_audio": False, "save_audio": True})
        self.assertTrue(st["consented"])
        self.assertTrue(cc.allows("mic"))
        self.assertFalse(cc.allows("webcam"))
        self.assertTrue(cc.allows("screen"))
        self.assertTrue(cc.any_recording_source())
        path = Path(self.tmp) / "capture_consent.json"
        self.assertTrue(path.is_file())
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(raw["sources"]["mic"])

    def test_revoke(self):
        from app.services import capture_consent as cc
        cc.save({"mic": True})
        st = cc.save(consented=False)
        self.assertFalse(st["consented"])
        self.assertFalse(cc.allows("mic"))

    def test_clicks_source_and_default_off(self):
        from app.services import capture_consent as cc
        from app.config import settings
        self.assertIn("clicks", cc.SOURCES)
        # Config default is off (env may override in live .env — assert blank).
        self.assertFalse(cc.load(force=True)["sources"].get("clicks"))
        st = cc.save({"screen": True, "clicks": False})
        self.assertTrue(st["sources"]["screen"])
        self.assertFalse(st["sources"]["clicks"])
        # Capability patch: screen alone enables desktop, clicks stays off.
        cc.apply_saved_to_runtime()
        self.assertTrue(settings.desktop_capture.enabled)
        self.assertTrue(settings.desktop_capture.screen)
        self.assertFalse(settings.desktop_capture.clicks)


class KillSwitchToggleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cp, kp = _patch_data_dir(self.tmp)
        cp.start(); kp.start()
        self.addCleanup(cp.stop)
        self.addCleanup(kp.stop)
        import app.services.hardening as h
        h._overrides.clear()
        h._overrides_loaded = False

    def test_set_and_persist(self):
        from app.services import hardening
        from app.config import settings
        before = bool(settings.attention.wm)
        row = hardening.set_kill_switch("QUILL_WM", not before)
        self.assertEqual(row["on"], not before)
        self.assertEqual(bool(settings.attention.wm), not before)
        self.assertEqual(os.environ.get("QUILL_WM"),
                         "1" if not before else "0")
        path = Path(self.tmp) / "kill_switches.json"
        self.assertTrue(path.is_file())
        hardening.set_kill_switch("QUILL_WM", before)

    def test_unknown_env_raises(self):
        from app.services import hardening
        with self.assertRaises(KeyError):
            hardening.set_kill_switch("QUILL_NOT_A_SWITCH", True)

    def test_audit_includes_overridden(self):
        from app.services import hardening
        hardening.set_kill_switch("QUILL_ANTICIPATE", True)
        rows = hardening.kill_switches()
        anti = next(r for r in rows if r["env"] == "QUILL_ANTICIPATE")
        self.assertTrue(anti["on"])
        self.assertTrue(anti["overridden"])
        hardening.set_kill_switch("QUILL_ANTICIPATE", False)


class CaptureEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cp, kp = _patch_data_dir(self.tmp)
        cp.start(); kp.start()
        self.addCleanup(cp.stop)
        self.addCleanup(kp.stop)
        import app.services.capture_consent as cc
        cc._cached = None
        import app.services.hardening as h
        h._overrides.clear()
        h._overrides_loaded = False

    def test_consent_required_for_audio_start(self):
        r = self.client.post("/audio/start")
        self.assertEqual(r.status_code, 403)

    def test_consent_flow(self):
        r = self.client.get("/capture/status")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["consent"]["consented"])
        r = self.client.post("/capture/consent",
                             json={"mic": False, "webcam": False,
                                   "screen": False, "system_audio": False,
                                   "save_audio": False, "consented": True})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["consent"]["consented"])
        r = self.client.post("/audio/start")
        self.assertEqual(r.status_code, 403)
        import app.api.routes as routes_mod
        with patch.object(routes_mod, "_audio") as audio_mock, \
             patch.object(routes_mod, "_vision") as vision_mock, \
             patch.object(routes_mod, "_desktop_capture") as desk_mock:
            audio_mock.start = lambda: None
            audio_mock.stop = lambda: None
            vision_mock.start = lambda: None
            vision_mock.stop = lambda: None
            desk_mock.start = lambda: None
            desk_mock.stop = lambda: None
            desk_mock.running = lambda: False
            r = self.client.post("/capture/consent", json={"mic": True})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["consent"]["sources"]["mic"])
            r = self.client.post("/capture/pause", json={"source": "mic"})
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json()["running"]["mic"])

    def test_screen_resume_windows_only_returns_503(self):
        """Linux / non-Windows: Privacy screen toggle must not ASGI-crash."""
        self.client.post("/capture/consent",
                         json={"screen": True, "consented": True})
        import app.api.routes as routes_mod
        with patch.object(routes_mod, "_desktop_capture") as desk_mock, \
             patch.object(routes_mod, "_desktop_capture_running", False):
            desk_mock.start.side_effect = RuntimeError(
                "desktop capture is currently Windows-only")
            r = self.client.post("/capture/resume", json={"source": "screen"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("Windows-only", r.json().get("detail", ""))

    def test_kill_switch_endpoint(self):
        r = self.client.post("/console/hardening/kill-switch",
                             json={"env": "QUILL_REASONERS", "on": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["switch"]["on"])
        self.client.post("/console/hardening/kill-switch",
                         json={"env": "QUILL_REASONERS", "on": True})


class DefaultOffTests(unittest.TestCase):
    def test_vision_and_save_audio_ship_off(self):
        text = Path("app/config.py").read_text(encoding="utf-8")
        self.assertIn('QUILL_VISION", "0"', text)
        self.assertIn('QUILL_SAVE_AUDIO", "0"', text)


if __name__ == "__main__":
    unittest.main()
