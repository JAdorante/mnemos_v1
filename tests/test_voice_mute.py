"""Runtime mute for AI voice (TTS) — persist + skip speak without a restart."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _patch_prefs(tmp: str):
    return patch("app.services.voice._prefs_path",
                 lambda: Path(tmp) / "voice_prefs.json")


class VoiceMuteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        p = _patch_prefs(self.tmp)
        p.start()
        self.addCleanup(p.stop)
        from app.services import voice
        voice.speaker._muted = False
        voice.speaker._cancel.clear()
        voice.speaker.stop()
        self.addCleanup(self._reset)

    def _reset(self):
        from app.services import voice
        voice.speaker._muted = False
        voice.speaker._cancel.clear()
        voice.speaker.stop()

    def test_default_unmuted(self):
        from app.services import voice
        st = voice.status()
        self.assertFalse(st["muted"])
        self.assertIn("enabled", st)

    def test_mute_blocks_speak(self):
        from app.services import voice
        st = voice.set_muted(True)
        self.assertTrue(st["muted"])
        out = voice.speak("hello there")
        self.assertFalse(out["spoken"])
        self.assertEqual(out["reason"], "voice muted")

    def test_maybe_speak_reply_respects_mute(self):
        from app.services import voice
        with patch.object(voice.speaker, "speak") as sp:
            voice.set_muted(True)
            voice.maybe_speak_reply("result", "hi")
            sp.assert_not_called()
            voice.set_muted(False)
            voice.maybe_speak_reply("result", "hi")
            sp.assert_called_once()

    def test_mute_persists(self):
        from app.services import voice
        voice.set_muted(True)
        path = Path(self.tmp) / "voice_prefs.json"
        self.assertTrue(path.is_file())
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(raw["muted"])
        voice.speaker._muted = False
        voice.speaker._muted = voice._load_muted()
        self.assertTrue(voice.speaker._muted)

    def test_unmute_restores(self):
        from app.services import voice
        voice.set_muted(True)
        st = voice.set_muted(False)
        self.assertFalse(st["muted"])
        self.assertFalse(voice._load_muted())


class VoiceMuteRouteTests(unittest.TestCase):
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
        p = _patch_prefs(self.tmp)
        p.start()
        self.addCleanup(p.stop)
        from app.services import voice
        voice.speaker._muted = False
        voice.speaker._cancel.clear()
        self.addCleanup(self._reset)

    def _reset(self):
        from app.services import voice
        voice.speaker._muted = False
        voice.speaker._cancel.clear()

    def test_status_and_mute_roundtrip(self):
        r = self.client.get("/speak/status")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["muted"])
        r = self.client.post("/speak/mute", json={"muted": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["muted"])
        r = self.client.get("/speak/status")
        self.assertTrue(r.json()["muted"])
        r = self.client.post("/speak", json={"text": "should stay quiet"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["spoken"])
        r = self.client.post("/speak/mute", json={"muted": False})
        self.assertFalse(r.json()["muted"])


class VoiceMuteUiTests(unittest.TestCase):
    def test_ui_exposes_voice_toggle(self):
        from app.api.mnemos_ui import UI_JS
        self.assertIn("/speak/mute", UI_JS)
        self.assertIn("Speak replies aloud", UI_JS)
        self.assertIn("recVoice", UI_JS)
        self.assertIn("toggleVoice", UI_JS)


if __name__ == "__main__":
    unittest.main()
