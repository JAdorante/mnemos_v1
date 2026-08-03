"""Ghost browser: frame relay, API endpoints, and headless frame publishing."""
from __future__ import annotations

import unittest
from unittest import mock

from browser_agent import ghost


class GhostRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        ghost.clear()

    def tearDown(self) -> None:
        ghost.clear()

    def test_empty_relay(self) -> None:
        self.assertIsNone(ghost.latest())
        m = ghost.meta()
        self.assertFalse(m["has_frame"])
        self.assertFalse(m["fresh"])

    def test_publish_latest_meta(self) -> None:
        ghost.publish(b"png-bytes", url="https://x.com", title="Home / X")
        fr = ghost.latest()
        self.assertIsNotNone(fr)
        assert fr is not None
        png, meta = fr
        self.assertEqual(png, b"png-bytes")
        self.assertEqual(meta["url"], "https://x.com")
        m = ghost.meta()
        self.assertTrue(m["fresh"])
        self.assertEqual(m["title"], "Home / X")

    def test_empty_frame_ignored(self) -> None:
        ghost.publish(b"", url="https://x.com")
        self.assertIsNone(ghost.latest())

    def test_stale_frame_not_fresh(self) -> None:
        ghost.publish(b"png", url="u")
        with ghost._lock:
            ghost._meta["ts"] -= ghost.FRESH_S + 5
        m = ghost.meta()
        self.assertTrue(m["has_frame"])
        self.assertFalse(m["fresh"])


class GhostEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router

        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self) -> None:
        ghost.clear()

    def test_frame_204_then_png(self) -> None:
        r = self.client.get("/agent/ghost/frame")
        self.assertEqual(r.status_code, 204)
        ghost.publish(b"\x89PNG-fake", url="https://a.b", title="T")
        r = self.client.get("/agent/ghost/frame")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"\x89PNG-fake")
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_status_shape(self) -> None:
        j = self.client.get("/agent/ghost/status").json()
        self.assertIn("mode", j)
        self.assertFalse(j["fresh"])
        ghost.publish(b"png", url="https://a.b", title="T")
        j = self.client.get("/agent/ghost/status").json()
        self.assertTrue(j["fresh"])
        self.assertEqual(j["title"], "T")

    def test_chat_page_has_ghost_pane(self) -> None:
        html = self.client.get("/chat").text
        self.assertIn('id="ghost"', html)
        self.assertIn("/agent/ghost/status", html)
        self.assertIn("/agent/ghost/frame", html)


class HeadlessFramePublishTests(unittest.TestCase):
    """A real headless browser publishes frames on scan and on actions."""

    def test_scan_and_click_publish_frames(self) -> None:
        from browser_agent import config as bcfg
        from browser_agent.browser import BrowserDriver

        ghost.clear()
        with mock.patch.object(bcfg, "GHOST_MODE", "headless"):
            d = BrowserDriver(headless=True)
            d.start()
            try:
                self.assertEqual(d._ghost, "headless")
                d.page.set_content("<button id='b'>Go</button><a href='#z'>z</a>")
                s = d.scan()
                fr = ghost.latest()
                self.assertIsNotNone(fr)
                assert fr is not None
                png1, _ = fr
                self.assertTrue(png1.startswith(b"\x89PNG"))
                bid = next(e["id"] for e in s["elements"] if e["name"] == "Go")
                r = d.execute("click", {"element_id": bid})
                self.assertTrue(r["ok"])
                self.assertIsNotNone(ghost.latest())
            finally:
                d.close()
        ghost.clear()

    def test_ghost_off_publishes_nothing(self) -> None:
        from browser_agent import config as bcfg
        from browser_agent.browser import BrowserDriver

        ghost.clear()
        with mock.patch.object(bcfg, "GHOST_MODE", "off"):
            d = BrowserDriver(headless=True)
            d.start()
            try:
                d.page.set_content("<button>Go</button>")
                d.scan()
                self.assertIsNone(ghost.latest())
            finally:
                d.close()


if __name__ == "__main__":
    unittest.main()
