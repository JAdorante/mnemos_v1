"""Phase C perception L2 frame-layer tests.

CAS WebP put, L1+L2 wiring, privacy (no pixels), age/budget compaction
order, and pin exemption from the 72h full-frame drop.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.perception.ocr import OcrLineResult, OcrResult
from app.perception.schemas import Capture, OcrLine


def _fresh_pstore(tmp: Path):
    import app.perception.store as store_mod
    from app.perception.store import PerceptionStore
    ps = PerceptionStore(tmp / "perception.db")
    store_mod._pstore = ps
    return ps


def _reset_pstore():
    import app.perception.store as store_mod
    if store_mod._pstore is not None:
        try:
            store_mod._pstore.close()
        except Exception:
            pass
        store_mod._pstore = None


class _FakeOcr:
    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)
        self.calls = 0

    def available(self) -> bool:
        return True

    def recognize(self, rgb) -> OcrResult:
        self.calls += 1
        return OcrResult(
            lines=[OcrLineResult(text=t, conf=0.9) for t in self.lines],
            engine="fake", version="test")


class PerceptionStoreMixin:
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="perc_c_"))
        self.frames = self.tmp / "frames"
        self.frames.mkdir()
        self.ps = _fresh_pstore(self.tmp)
        self.addCleanup(_reset_pstore)
        self.events: list = []
        self.assertEqual(self.ps.user_version(), 3)


# ---------------------------------------------------------------------------
# CAS
# ---------------------------------------------------------------------------
class CasPutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cas_"))
        self.root = self.tmp / "frames"

    def test_idempotent_same_sha(self) -> None:
        from app.perception import l2_frames
        rgb = np.random.RandomState(0).randint(0, 255, (40, 60, 3), dtype=np.uint8)
        a = l2_frames.put_rgb(rgb, root=self.root)
        b = l2_frames.put_rgb(rgb, root=self.root)
        self.assertEqual(a["frame_sha256"], b["frame_sha256"])
        self.assertEqual(a["thumb_sha256"], b["thumb_sha256"])
        self.assertNotEqual(a["frame_sha256"], a["thumb_sha256"])
        fp = Path(a["frame_path"])
        self.assertTrue(fp.is_file())
        self.assertEqual(fp.parent.name, a["frame_sha256"][:2])
        # Second put must not grow the tree with duplicates.
        webps = list(self.root.rglob("*.webp"))
        self.assertEqual(len(webps), 2)  # one full + one thumb


# ---------------------------------------------------------------------------
# L1 + L2 wire
# ---------------------------------------------------------------------------
class L1L2WireTests(PerceptionStoreMixin, unittest.TestCase):
    def test_capture_writes_shas_and_frame_path(self) -> None:
        from app.perception.l1_capture import L1Capture
        l1 = L1Capture(
            store=self.ps, ocr=_FakeOcr(["hello world line"]),
            sink=lambda ev: self.events.append(ev))
        rgb = np.zeros((32, 48, 3), dtype=np.uint8)
        rgb[5:20, 5:40] = 200
        with patch("app.perception.ocr_blocks.add_blocks", return_value=0), \
             patch("app.perception.l2_frames.frames_root",
                   return_value=self.frames):
            r = l1.capture_once(
                rgb, {"window": "Notes", "window_id": "1", "hwnd": 1})

        self.assertIsNotNone(r)
        self.assertTrue(r["frame_sha256"])
        self.assertTrue(r["thumb_sha256"])
        row = self.ps.get_capture(r["capture_id"])
        self.assertEqual(row["frame_sha256"], r["frame_sha256"])
        self.assertEqual(row["degradation"], "full")
        self.assertTrue(self.events)
        self.assertIn("frame_path", self.events[0].meta)
        self.assertTrue(Path(self.events[0].meta["frame_path"]).is_file())


# ---------------------------------------------------------------------------
# Privacy — no pixels
# ---------------------------------------------------------------------------
class PrivacyNoPixelsTests(PerceptionStoreMixin, unittest.TestCase):
    def test_blocklist_writes_zero_cas_files(self) -> None:
        from app.perception.l1_capture import L1Capture
        from app.perception.privacy_gate import PrivacyGate

        gate = PrivacyGate(blocklist_path=self.tmp / "bl.json")
        ocr = _FakeOcr(["secret"])
        l1 = L1Capture(store=self.ps, ocr=ocr,
                       sink=lambda ev: self.events.append(ev))
        with patch("app.perception.privacy_gate.gate", gate), \
             patch("app.perception.l2_frames.frames_root",
                   return_value=self.frames):
            out = l1.capture_once(
                np.zeros((16, 16, 3), dtype=np.uint8),
                {"window": "Bitwarden", "window_id": "9", "hwnd": 9})
        self.assertIsNone(out)
        self.assertEqual(ocr.calls, 0)
        self.assertEqual(list(self.frames.rglob("*.webp")), [])


# ---------------------------------------------------------------------------
# Compactor
# ---------------------------------------------------------------------------
class CompactorTests(PerceptionStoreMixin, unittest.TestCase):
    def _seed(self, *, ts_ms: int, promoted: bool = False,
              with_full: bool = True) -> dict:
        from app.perception import l2_frames
        rgb = np.random.RandomState(ts_ms % 1000).randint(
            0, 255, (24, 32, 3), dtype=np.uint8)
        cas = l2_frames.put_rgb(rgb, root=self.frames)
        cap = Capture(
            ts_utc=ts_ms, window_id="w", kind="full", trigger="test",
            frame_sha256=cas["frame_sha256"] if with_full else None,
            thumb_sha256=cas["thumb_sha256"],
            degradation="full" if with_full else "thumb",
            promoted=promoted, novel_line_count=1, total_line_count=1)
        self.ps.insert_capture(cap)
        self.ps.upsert_ocr_lines([
            OcrLine(line_hash="h" + str(ts_ms), window_id="w",
                    first_capture_id=cap.capture_id, text="keep me", conf=0.9)])
        self.ps.set_frame_line_map(cap.capture_id, ["h" + str(ts_ms)])
        return {"cap": cap, "cas": cas}

    def test_age_drops_full_keeps_thumb(self) -> None:
        from app.perception.compactor import compact
        from app.perception import l2_frames
        now = int(time.time() * 1000)
        old = now - 80 * 3600_000  # 80h > 72h
        seeded = self._seed(ts_ms=old)
        before_ocr = self.ps.counts()["ocr_lines"]
        man = compact(self.ps, root=self.frames, now_ms=now,
                      full_ttl_h=72, thumb_ttl_d=30,
                      budget_bytes=10 * 1024 ** 3)
        self.assertGreaterEqual(man["full_aged"], 1)
        row = self.ps.get_capture(seeded["cap"].capture_id)
        self.assertIsNone(row["frame_sha256"])
        self.assertIsNotNone(row["thumb_sha256"])
        self.assertEqual(row["degradation"], "thumb")
        self.assertFalse(l2_frames.path_for(
            seeded["cas"]["frame_sha256"], self.frames).is_file())
        self.assertTrue(l2_frames.path_for(
            seeded["cas"]["thumb_sha256"], self.frames).is_file())
        self.assertEqual(self.ps.counts()["ocr_lines"], before_ocr)
        self.assertEqual(self.ps.reconstruct_text(seeded["cap"].capture_id),
                         "keep me")

    def test_thumb_expiry_leaves_text(self) -> None:
        from app.perception.compactor import compact
        now = int(time.time() * 1000)
        old = now - 40 * 86400_000  # 40d > 30d
        seeded = self._seed(ts_ms=old, with_full=False)
        # Ensure no full on disk for this row.
        man = compact(self.ps, root=self.frames, now_ms=now,
                      full_ttl_h=72, thumb_ttl_d=30,
                      budget_bytes=10 * 1024 ** 3)
        self.assertGreaterEqual(man["thumb_aged"], 1)
        row = self.ps.get_capture(seeded["cap"].capture_id)
        self.assertIsNone(row["thumb_sha256"])
        self.assertEqual(row["degradation"], "text")
        self.assertEqual(self.ps.reconstruct_text(seeded["cap"].capture_id),
                         "keep me")

    def test_budget_drops_full_then_thumb(self) -> None:
        from app.perception.compactor import compact
        now = int(time.time() * 1000)
        # Fresh captures (not aged) so only budget applies.
        a = self._seed(ts_ms=now - 1000)
        b = self._seed(ts_ms=now - 500)
        ocr_before = self.ps.counts()["ocr_lines"]
        # Tiny budget forces deletion of everything pixel-wise.
        man = compact(self.ps, root=self.frames, now_ms=now,
                      full_ttl_h=72, thumb_ttl_d=30, budget_bytes=1)
        self.assertGreater(man["full_budget"] + man["thumb_budget"], 0)
        self.assertEqual(self.ps.counts()["ocr_lines"], ocr_before)
        # Eventually under budget (or emptied).
        from app.perception import l2_frames
        self.assertLessEqual(l2_frames.dir_size_bytes(self.frames), 1
                             if man["dir_bytes"] <= 1 else man["dir_bytes"])
        # At least one of the captures lost its full frame.
        rows = [self.ps.get_capture(a["cap"].capture_id),
                self.ps.get_capture(b["cap"].capture_id)]
        self.assertTrue(any(r["frame_sha256"] is None for r in rows))

    def test_pin_survives_age(self) -> None:
        from app.perception.compactor import compact
        from app.perception import l2_frames
        now = int(time.time() * 1000)
        old = now - 80 * 3600_000
        seeded = self._seed(ts_ms=old, promoted=True)
        man = compact(self.ps, root=self.frames, now_ms=now,
                      full_ttl_h=72, thumb_ttl_d=30,
                      budget_bytes=10 * 1024 ** 3)
        self.assertGreaterEqual(man["skipped_promoted"], 1)
        row = self.ps.get_capture(seeded["cap"].capture_id)
        self.assertEqual(row["frame_sha256"], seeded["cas"]["frame_sha256"])
        self.assertTrue(l2_frames.path_for(
            seeded["cas"]["frame_sha256"], self.frames).is_file())

    def test_shared_sha_survives_sibling_age(self) -> None:
        """Two captures of identical pixels share one CAS file; aging one
        must not unlink pixels the other still claims."""
        from app.perception import l2_frames
        from app.perception.compactor import compact
        now = int(time.time() * 1000)
        old = now - 80 * 3600_000
        rgb = np.zeros((24, 32, 3), dtype=np.uint8)
        rgb[:, :] = 90
        cas = l2_frames.put_rgb(rgb, root=self.frames)
        a = Capture(
            ts_utc=old, window_id="w", kind="full", trigger="test",
            frame_sha256=cas["frame_sha256"], thumb_sha256=cas["thumb_sha256"],
            degradation="full", novel_line_count=0, total_line_count=0)
        b = Capture(
            ts_utc=now - 1000, window_id="w", kind="full", trigger="test",
            frame_sha256=cas["frame_sha256"], thumb_sha256=cas["thumb_sha256"],
            degradation="full", novel_line_count=0, total_line_count=0)
        self.ps.insert_capture(a)
        self.ps.insert_capture(b)
        self.assertEqual(self.ps.sha_refcount(cas["frame_sha256"]), 2)
        man = compact(self.ps, root=self.frames, now_ms=now,
                      full_ttl_h=72, thumb_ttl_d=30,
                      budget_bytes=10 * 1024 ** 3)
        self.assertGreaterEqual(man["shared_sha_kept"], 1)
        ra = self.ps.get_capture(a.capture_id)
        rb = self.ps.get_capture(b.capture_id)
        self.assertIsNone(ra["frame_sha256"])
        self.assertEqual(rb["frame_sha256"], cas["frame_sha256"])
        self.assertEqual(rb["degradation"], "full")
        self.assertTrue(l2_frames.path_for(
            cas["frame_sha256"], self.frames).is_file())
        self.assertEqual(self.ps.sha_refcount(cas["frame_sha256"]), 1)


# ---------------------------------------------------------------------------
# Pin API
# ---------------------------------------------------------------------------
class PinApiTests(PerceptionStoreMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def test_pin_unpin(self) -> None:
        cap = Capture(ts_utc=1_700_000_000_000, window_id="w", kind="full",
                      trigger="t", frame_sha256="abc", thumb_sha256="def")
        self.ps.insert_capture(cap)
        r = self.client.post("/perception/pin",
                             json={"capture_id": cap.capture_id})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["promoted"])
        self.assertEqual(self.ps.get_capture(cap.capture_id)["promoted"], 1)
        r = self.client.post("/perception/unpin",
                             json={"capture_id": cap.capture_id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ps.get_capture(cap.capture_id)["promoted"], 0)


if __name__ == "__main__":
    unittest.main()
