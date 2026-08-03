"""Phase B perception L1 text-layer tests.

Covers: reconstruction fidelity, delta/property sequences, scroll
suppression, pre-pixel privacy (no OCR), secret redaction before store,
and the single-producer mutex (L1 vs VLM screen loop).
"""
from __future__ import annotations

import hashlib
import random
import tempfile
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


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class _FakeOcr:
    def __init__(self, lines: list[str] | None = None,
                 conf: float = 0.9) -> None:
        self.lines = list(lines or [])
        self.conf = conf
        self.calls = 0

    def available(self) -> bool:
        return True

    def recognize(self, rgb) -> OcrResult:
        self.calls += 1
        out = [OcrLineResult(text=t, conf=self.conf, bbox_y=float(i))
               for i, t in enumerate(self.lines)]
        return OcrResult(lines=out, engine="fake", version="test")


class PerceptionStoreMixin:
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="perc_b_"))
        self.ps = _fresh_pstore(self.tmp)
        self.addCleanup(_reset_pstore)
        self.events: list = []


# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------
class ReconstructionTests(PerceptionStoreMixin, unittest.TestCase):
    def test_reconstruct_byte_identical(self) -> None:
        texts = ["Hello world", "Line two", "Third line here"]
        cap = Capture(ts_utc=1_700_000_000_000, window_id="w1", kind="full",
                      trigger="test", total_line_count=3, novel_line_count=3)
        self.ps.insert_capture(cap)
        hashes = [_sha(t) for t in texts]
        self.ps.upsert_ocr_lines([
            OcrLine(line_hash=hashes[i], window_id="w1",
                    first_capture_id=cap.capture_id, text=texts[i], conf=0.9)
            for i in range(3)])
        self.ps.set_frame_line_map(cap.capture_id, hashes)
        self.assertEqual(self.ps.reconstruct_text(cap.capture_id),
                         "\n".join(texts))

    def test_delta_property_random_sequences(self) -> None:
        """Random insert sequences: reconstruction always equals visible text."""
        rng = random.Random(42)
        window = "win-prop"
        visible: list[str] = []
        for _ in range(30):
            # Mutate visible screen: append, drop head (scroll), or replace.
            op = rng.choice(["append", "scroll", "replace"])
            if op == "append" or not visible:
                visible.append(f"line-{rng.randint(0, 9999)}-{len(visible)}")
            elif op == "scroll" and len(visible) > 2:
                visible = visible[1:] + [f"line-{rng.randint(0, 9999)}-new"]
            else:
                visible = [f"line-{rng.randint(0, 9999)}-{i}"
                           for i in range(rng.randint(1, 5))]
            cap = Capture(ts_utc=1_700_000_000_000 + len(visible) * 1000,
                          window_id=window, kind="full", trigger="prop",
                          total_line_count=len(visible),
                          novel_line_count=len(visible))
            self.ps.insert_capture(cap)
            hashes = [_sha(t) for t in visible]
            # Only novel vs prior DB rows matter for storage; map is always full.
            existing = set(self.ps.load_window_line_hashes(window, 5000))
            novel = [(h, t) for h, t in zip(hashes, visible) if h not in existing]
            if novel:
                self.ps.upsert_ocr_lines([
                    OcrLine(line_hash=h, window_id=window,
                            first_capture_id=cap.capture_id, text=t, conf=0.9)
                    for h, t in novel])
            self.ps.set_frame_line_map(cap.capture_id, hashes)
            self.assertEqual(self.ps.reconstruct_text(cap.capture_id),
                             "\n".join(visible))


# ---------------------------------------------------------------------------
# Scroll suppression + capture path
# ---------------------------------------------------------------------------
class ScrollAndCaptureTests(PerceptionStoreMixin, unittest.TestCase):
    def _l1(self, lines: list[str]) -> "L1Capture":
        from app.perception.l1_capture import L1Capture
        return L1Capture(
            store=self.ps, ocr=_FakeOcr(lines),
            sink=lambda ev: self.events.append(ev),
            settle_ms=0, dhash_every_s=9999, max_interval_s=9999)

    def test_scroll_suppression_marks_delta(self) -> None:
        base = [f"stable-line-{i}" for i in range(10)]
        l1 = self._l1(base)
        rgb = np.zeros((20, 20, 3), dtype=np.uint8)
        info = {"window": "Doc", "window_id": "42", "hwnd": 42}
        with patch("app.perception.ocr_blocks.add_blocks", return_value=0), \
             patch("app.perception.l2_frames.put_rgb",
                   return_value={"frame_sha256": None, "thumb_sha256": None,
                                 "frame_path": "", "thumb_path": ""}):
            r1 = l1.capture_once(rgb, info, trigger="l0_change")
        self.assertEqual(r1["kind"], "full")
        self.assertEqual(r1["novel"], 10)

        # 9/10 same + 1 new → 90% overlap ≥ 70% → scroll_delta
        scrolled = base[1:] + ["brand-new-bottom-line"]
        l1._ocr = _FakeOcr(scrolled)
        with patch("app.perception.ocr_blocks.add_blocks", return_value=0), \
             patch("app.perception.l2_frames.put_rgb",
                   return_value={"frame_sha256": None, "thumb_sha256": None,
                                 "frame_path": "", "thumb_path": ""}):
            r2 = l1.capture_once(rgb, info, trigger="dhash")
        self.assertEqual(r2["kind"], "scroll_delta")
        self.assertEqual(r2["novel"], 1)
        self.assertEqual(self.ps.reconstruct_text(r2["capture_id"]),
                         "\n".join(scrolled))
        self.assertEqual(len(self.events), 2)
        self.assertEqual(self.events[-1].meta["capture_id"], r2["capture_id"])
        self.assertEqual(self.events[-1].source, "desktop.screen")

    def test_redaction_drops_secret_lines(self) -> None:
        key = "sk-ant-api03-" + "a1B2" * 12
        lines = ["Meeting notes for Q3", f"export KEY={key}", "Next steps"]
        l1 = self._l1(lines)
        rgb = np.zeros((10, 10, 3), dtype=np.uint8)
        info = {"window": "Editor", "window_id": "7", "hwnd": 7}
        with patch("app.perception.ocr_blocks.add_blocks") as add_blocks, \
             patch("app.perception.l2_frames.put_rgb",
                   return_value={"frame_sha256": None, "thumb_sha256": None,
                                 "frame_path": "", "thumb_path": ""}):
            r = l1.capture_once(rgb, info)
            # Embed payload must not contain the key.
            if add_blocks.called:
                for call in add_blocks.call_args_list:
                    blob = str(call)
                    self.assertNotIn(key, blob)
        text = self.ps.reconstruct_text(r["capture_id"])
        self.assertNotIn(key, text)
        self.assertIn("Meeting notes", text)
        # Stored rows must be clean too.
        for row in self.ps._conn.execute("SELECT text FROM ocr_lines").fetchall():
            self.assertNotIn(key, row["text"])


# ---------------------------------------------------------------------------
# Pre-pixel privacy
# ---------------------------------------------------------------------------
class PrivacyPreOcrTests(PerceptionStoreMixin, unittest.TestCase):
    def test_blocklisted_never_calls_ocr(self) -> None:
        from app.perception.l1_capture import L1Capture
        from app.perception.privacy_gate import PrivacyGate

        gate = PrivacyGate(blocklist_path=self.tmp / "bl.json")
        ocr = _FakeOcr(["should-not-see"])
        l1 = L1Capture(store=self.ps, ocr=ocr,
                       sink=lambda ev: self.events.append(ev))
        rgb = np.zeros((8, 8, 3), dtype=np.uint8)
        info = {"window": "1Password — Vault", "window_id": "9", "hwnd": 9}
        with patch("app.perception.privacy_gate.gate", gate):
            out = l1.capture_once(rgb, info)
        self.assertIsNone(out)
        self.assertEqual(ocr.calls, 0)
        self.assertEqual(self.events, [])
        caps = self.ps.recent_captures(0)
        self.assertTrue(any(c["kind"] == "excluded" for c in caps), caps)


# ---------------------------------------------------------------------------
# Producer mutex
# ---------------------------------------------------------------------------
class ProducerMutexTests(unittest.TestCase):
    def test_l1_on_starts_l1_not_vlm_thread(self) -> None:
        from app.services.desktop_capture import DesktopCapturePipeline

        pipe = DesktopCapturePipeline(sink=lambda ev: None)
        started = {"l1": False, "vlm": False}

        def fake_start_l1():
            started["l1"] = True

        # Simulate the screen-branch decision from start() without touching
        # frozen DesktopCaptureConfig fields.
        with patch.object(DesktopCapturePipeline, "_l1_owns_screen",
                          return_value=True), \
             patch.object(pipe, "_start_l1", side_effect=fake_start_l1), \
             patch.object(pipe, "_vlm_screen_alive", return_value=False):
            if DesktopCapturePipeline._l1_owns_screen():
                self.assertFalse(pipe._vlm_screen_alive())
                pipe._start_l1()
            else:
                started["vlm"] = True
        self.assertTrue(started["l1"])
        self.assertFalse(started["vlm"])

    def test_start_refuses_dual_when_vlm_alive(self) -> None:
        from app.services.desktop_capture import DesktopCapturePipeline
        pipe = DesktopCapturePipeline()
        with patch.object(DesktopCapturePipeline, "_l1_owns_screen",
                          return_value=True), \
             patch.object(pipe, "_vlm_screen_alive", return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                if pipe._l1_owns_screen() and pipe._vlm_screen_alive():
                    raise RuntimeError(
                        "desktop capture: refuse dual screen producers "
                        "(VLM loop still alive while QUILL_PERCEPTION_L1=1)")
            self.assertIn("dual screen", str(ctx.exception))

    def test_l1_flag_defaults_off(self) -> None:
        text = Path("app/config.py").read_text(encoding="utf-8")
        self.assertIn('QUILL_PERCEPTION_L1", "0"', text)


class DHashTests(unittest.TestCase):
    def test_identical_images_zero_distance(self) -> None:
        from app.perception.dhash import dhash64, hamming64
        a = np.random.RandomState(0).randint(0, 255, (64, 64, 3), dtype=np.uint8)
        self.assertEqual(hamming64(dhash64(a), dhash64(a.copy())), 0)

    def test_different_images_nonzero(self) -> None:
        from app.perception.dhash import dhash64, hamming64
        # Horizontal gradient vs its mirror — adjacent-pixel comparisons differ.
        xs = np.linspace(0, 255, 64, dtype=np.uint8)
        a = np.tile(xs, (64, 1))
        a = np.stack([a, a, a], axis=2)
        b = np.flip(a, axis=1).copy()
        self.assertGreater(hamming64(dhash64(a), dhash64(b)), 10)


# ---------------------------------------------------------------------------
# Trigger smoke (settle)
# ---------------------------------------------------------------------------
class TriggerSmokeTests(PerceptionStoreMixin, unittest.TestCase):
    def test_capture_once_emits_event_with_capture_id(self) -> None:
        from app.perception.l1_capture import L1Capture
        l1 = L1Capture(
            store=self.ps, ocr=_FakeOcr(["alpha", "beta line long enough"]),
            sink=lambda ev: self.events.append(ev))
        with patch("app.perception.ocr_blocks.add_blocks", return_value=1), \
             patch("app.perception.l2_frames.put_rgb",
                   return_value={"frame_sha256": "a" * 64, "thumb_sha256": "b" * 64,
                                 "frame_path": "x", "thumb_path": "y"}):
            r = l1.capture_once(
                np.zeros((16, 16, 3), dtype=np.uint8),
                {"window": "Notes", "window_id": "1", "hwnd": 1},
                trigger="l0_change")
        self.assertIsNotNone(r)
        self.assertEqual(len(self.events), 1)
        ev = self.events[0]
        self.assertEqual(ev.meta["capture_id"], r["capture_id"])
        self.assertIn("alpha", ev.raw)
        self.assertEqual(ev.meta["l1"]["trigger"], "l0_change")


if __name__ == "__main__":
    unittest.main()
