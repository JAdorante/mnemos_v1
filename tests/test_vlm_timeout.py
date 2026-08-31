"""Local VLM timeout default + cooldown after errors."""
from __future__ import annotations

import time
import unittest
from pathlib import Path

from app.services.vlm import VLMRouter


def _res(content_type: str = "none", confidence: float = 0.9) -> dict:
    return {
        "description": "x", "ocr_text": "", "people_count": 0,
        "objects": [], "scene_type": "", "content_type": content_type,
        "title": "", "items": [], "item_confidences": [],
        "confidence": confidence,
    }


class _FakeProvider:
    model = "fake-model"

    def __init__(self, result: dict | None = None, *, exc: Exception | None = None):
        self.result = result or _res()
        self.exc = exc
        self.calls = 0

    def describe(self, jpeg_bytes: bytes) -> dict:
        self.calls += 1
        if self.exc:
            raise self.exc
        return dict(self.result)

    def available(self) -> bool:
        return True


class VisionTimeoutConfigTests(unittest.TestCase):
    def test_shipped_default_timeout_is_25s(self) -> None:
        # 25s rides out CPU contention (Whisper x2 + embeddings warm) instead
        # of shunting whole cooldown windows of frames to paid Claude.
        cfg_path = Path(__file__).resolve().parents[1] / "app" / "config.py"
        text = cfg_path.read_text(encoding="utf-8")
        self.assertIn('QUILL_VISION_LOCAL_TIMEOUT_S", "25"', text)
        self.assertIn('QUILL_VISION_LOCAL_COOLDOWN_S", "120"', text)


class LocalCooldownTests(unittest.TestCase):
    @staticmethod
    def _set_cloud(flag: bool) -> None:
        from app.config import settings
        object.__setattr__(settings.vision, "cloud_when_local_down", flag)

    def setUp(self) -> None:
        # Pin the outage policy per-test — the repo .env may set either mode.
        from app.config import settings
        saved = settings.vision.cloud_when_local_down
        self.addCleanup(self._set_cloud, saved)

    def test_timeout_trips_cooldown_and_skips_local(self) -> None:
        self._set_cloud(True)
        r = VLMRouter()
        local = _FakeProvider(exc=TimeoutError("timed out"))
        lite = _FakeProvider(_res("notes", 0.9))
        accurate = _FakeProvider(_res("notes", 0.9))
        r.local = local
        r.claude = accurate
        r.claude_lite = lite
        r._local_ok = True

        # First error: escalates the frame, but does NOT trip the cooldown —
        # a one-off timeout under CPU contention gets a second chance instead
        # of buying a whole window of paid frames.
        out1 = r.describe(b"jpg")
        self.assertEqual(out1["_provider"], "claude")
        self.assertEqual(local.calls, 1)
        self.assertLessEqual(r._local_cool_until, time.time())

        # Second consecutive error: the streak trips the cooldown.
        out2 = r.describe(b"jpg")
        self.assertEqual(out2["_provider"], "claude")
        self.assertEqual(local.calls, 2)
        self.assertGreater(r._local_cool_until, time.time())

        # While cooling, local must not be called again — and outage traffic
        # goes to the cheap tier, never the accurate reader.
        out3 = r.describe(b"jpg")
        self.assertEqual(out3["_provider"], "claude")
        self.assertEqual(out3.get("_route", {}).get("reason"), "local_cooldown")
        self.assertEqual(local.calls, 2)  # unchanged
        self.assertEqual(lite.calls, 3)
        self.assertEqual(accurate.calls, 0)

    def test_success_resets_error_streak(self) -> None:
        self._set_cloud(True)
        r = VLMRouter()
        local = _FakeProvider(_res("notes", 0.9), exc=TimeoutError("timed out"))
        lite = _FakeProvider(_res("notes", 0.9))
        r.local = local
        r.claude = _FakeProvider(_res("notes", 0.9))
        r.claude_lite = lite
        r._local_ok = True

        r.describe(b"jpg")                       # error #1 — no cooldown
        local.exc = None
        out = r.describe(b"jpg")                 # success resets the streak
        self.assertEqual(out["_provider"], "ollama")
        local.exc = TimeoutError("timed out")
        r.describe(b"jpg")                       # error #1 again, not #2
        self.assertLessEqual(r._local_cool_until, time.time())

    def test_cloud_off_skips_frames_during_outage(self) -> None:
        self._set_cloud(False)
        r = VLMRouter()
        local = _FakeProvider(exc=TimeoutError("timed out"))
        lite = _FakeProvider(_res("notes", 0.9))
        r.local = local
        r.claude_lite = lite
        r._local_ok = True

        out1 = r.describe(b"jpg")  # error #1 -> skip, no Claude spend
        self.assertEqual(out1["_provider"], "none")
        self.assertEqual(out1["_route"]["reason"], "local_error_cloud_off")
        out2 = r.describe(b"jpg")  # error #2 -> cooldown trips, still no spend
        self.assertEqual(out2["_provider"], "none")
        self.assertEqual(out2["_route"]["reason"], "local_error_cloud_off")
        out3 = r.describe(b"jpg")  # cooling -> still skip, still no spend
        self.assertEqual(out3["_provider"], "none")
        self.assertEqual(out3["_route"]["reason"], "local_cooldown_cloud_off")
        self.assertEqual(lite.calls, 0)
        # local recovers after cooldown -> frames flow again
        r._local_cool_until = 0.0
        local.exc = None
        out4 = r.describe(b"jpg")
        self.assertEqual(out4["_provider"], "ollama")


if __name__ == "__main__":
    unittest.main()
