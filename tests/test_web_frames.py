"""Web Perceive screen frames — feed_web_frame gating + POST /ingest/frame.

The heavy tail (privacy gate, VLM, filters, event emit) is _analyze_screen,
already exercised by the desktop tests; here we prove the web entry point
gates and routes into it correctly, and that the endpoint enforces consent.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app.services.desktop_capture import DesktopCapturePipeline


def _rgb(seed: int, w: int = 64, h: int = 48) -> np.ndarray:
    """Textured frame — a uniform fill scores quality='dead' (analyzable
    False), which is correct behavior but not what these tests probe."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (h, w, 3), dtype=np.int64).astype(np.uint8)


def _jpeg(seed: int = 1) -> bytes:
    ok, buf = cv2.imencode(".jpg", _rgb(seed))
    assert ok
    return buf.tobytes()


class FeedWebFrameTests(unittest.TestCase):
    def _pipeline(self):
        return DesktopCapturePipeline(sink=lambda ev: None)

    def test_first_frame_analyzed_with_web_window_context(self):
        p = self._pipeline()
        calls = []
        with patch.object(
                p, "_analyze_screen",
                side_effect=lambda rgb, motion, ts, fq, win=None:
                    calls.append(win)), \
             patch.object(p.cfg.__class__, "min_interval_s", 0.0):
            out = p.feed_web_frame(_rgb(200), ts=123.0, title="Meet — weekly",
                                   wait=True)
        self.assertTrue(out["accepted"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["window"], "Meet — weekly")
        self.assertEqual(calls[0]["surface"], "web_share")

    def test_unchanged_frame_is_dropped(self):
        p = self._pipeline()
        with patch.object(p, "_analyze_screen"), \
             patch.object(p.cfg.__class__, "min_interval_s", 0.0), \
             patch.object(p.cfg.__class__, "max_interval_s", 9999.0):
            first = p.feed_web_frame(_rgb(200), ts=1.0, wait=True)
            second = p.feed_web_frame(_rgb(200), ts=2.0, wait=True)
        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])
        self.assertEqual(second["reason"], "unchanged")

    def test_unanalyzable_frame_is_dropped(self):
        p = self._pipeline()
        with patch.object(p, "_analyze_screen") as analyze, \
             patch("app.services.desktop_capture.frame_quality.score",
                   return_value={"analyzable": False}):
            out = p.feed_web_frame(_rgb(200), ts=1.0, wait=True)
        self.assertFalse(out["accepted"])
        self.assertEqual(out["reason"], "quality")
        analyze.assert_not_called()


class IngestFrameEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.web_ingest import router
        # Bare app (no CSRF middleware) — the established endpoint-test shape.
        bare = FastAPI()
        bare.include_router(router)
        cls.client = TestClient(bare)

    def test_consent_required(self):
        with patch("app.services.capture_consent.allows",
                   return_value=False):
            r = self.client.post("/ingest/frame", content=_jpeg())
        self.assertEqual(r.status_code, 403)

    def test_bad_body_rejected(self):
        with patch("app.services.capture_consent.allows", return_value=True):
            r = self.client.post("/ingest/frame", content=b"not a jpeg")
        self.assertEqual(r.status_code, 400)

    def test_frame_reaches_pipeline_with_title(self):
        from app.api import routes as routes_mod
        seen = {}

        def fake_feed(rgb, ts, title):
            seen.update(shape=rgb.shape, ts=ts, title=title)
            return {"accepted": True, "motion": 255.0}

        with patch("app.services.capture_consent.allows", return_value=True), \
             patch.object(routes_mod._desktop_capture, "feed_web_frame",
                          side_effect=fake_feed):
            r = self.client.post(
                "/ingest/frame?ts=42.5&title=Docs%20tab", content=_jpeg())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["accepted"])
        self.assertEqual(seen["ts"], 42.5)
        self.assertEqual(seen["title"], "Docs tab")
        self.assertEqual(seen["shape"], (48, 64, 3))


if __name__ == "__main__":
    unittest.main()
