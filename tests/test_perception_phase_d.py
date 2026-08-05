"""Phase D perception L3 semantics / corpus tests.

Extract idempotency, activity_blocks segmentation, L3⇔screen_extract
cutover wiring, Parquet incremental export, VLM fallback skip mocks.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.perception.schemas import Capture, MetaEvent, OcrLine, now_ms


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
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PerceptionStoreMixin:
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="perc_d_"))
        self.ps = _fresh_pstore(self.tmp)
        self.addCleanup(_reset_pstore)
        self.assertEqual(self.ps.user_version(), 3)

    def _seed_capture(self, cid: str, text: str, *,
                      ts: int | None = None,
                      novel: int = 5,
                      thumb: str | None = None) -> None:
        ts = ts if ts is not None else now_ms()
        self.ps.insert_capture(Capture(
            capture_id=cid, ts_utc=ts, window_id="w1", kind="full",
            trigger="test", novel_line_count=novel, total_line_count=1,
            thumb_sha256=thumb))
        line = OcrLine(
            line_hash=_sha(text), window_id="w1",
            first_capture_id=cid, text=text, conf=0.9)
        self.ps.upsert_ocr_lines([line])
        self.ps.set_frame_line_map(cid, [line.line_hash])


# ---------------------------------------------------------------------------
# Extract idempotency
# ---------------------------------------------------------------------------
class ExtractIdempotencyTests(PerceptionStoreMixin, unittest.TestCase):
    def test_rerun_no_duplicate_extractions(self) -> None:
        from app.perception import l3_workers
        cid = "cap_idem_1"
        self._seed_capture(
            cid,
            "TODO: ship the Phase D extract path for Mnemos tomorrow.")
        facts = {
            "tasks": [{
                "text": "ship the Phase D extract path",
                "source_span": "ship the Phase D extract path",
                "confidence": 0.9,
            }],
            "commitments": [], "claims": [], "entities": [], "relations": [],
        }
        with patch.object(l3_workers, "_run_llm_extract", return_value=facts), \
             patch.object(l3_workers, "_find_source_event_id", return_value=None), \
             patch.object(l3_workers, "run_vlm_fallback", return_value={"ok": False}):
            a = l3_workers.run_extract({"capture_id": cid}, store=self.ps)
            b = l3_workers.run_extract({"capture_id": cid}, store=self.ps)
        self.assertGreater(a["extractions"], 0)
        self.assertEqual(b["extractions"], 0)  # has_extractions skip
        rows = self.ps.list_extractions(cid)
        types = [r["type"] for r in rows]
        self.assertEqual(types.count("task"), 1)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------
class SegmentTests(PerceptionStoreMixin, unittest.TestCase):
    def test_idle_gap_splits_blocks(self) -> None:
        from app.perception.l3_workers import run_segment
        t0 = 1_700_000_000_000
        # Contiguous Code.exe stretch, then 6 min idle, then Slack.
        metas = [
            MetaEvent(session_id="S", seq=1, ts_utc=t0,
                      app_name="Code.exe", window_id="1"),
            MetaEvent(session_id="S", seq=2, ts_utc=t0 + 60_000,
                      app_name="Code.exe", window_id="1", key_count=10),
            MetaEvent(session_id="S", seq=3, ts_utc=t0 + 120_000,
                      app_name="Code.exe", window_id="1"),
            MetaEvent(session_id="S", seq=4, ts_utc=t0 + 120_000 + 6 * 60_000,
                      app_name="Slack.exe", window_id="2"),
            MetaEvent(session_id="S", seq=5,
                      ts_utc=t0 + 120_000 + 6 * 60_000 + 30_000,
                      app_name="Slack.exe", window_id="2"),
        ]
        self.ps.insert_meta_batch(metas)
        self.ps.insert_capture(Capture(
            capture_id="c1", ts_utc=t0 + 30_000, window_id="1", kind="full"))
        self.ps.insert_capture(Capture(
            capture_id="c2",
            ts_utc=t0 + 120_000 + 6 * 60_000 + 10_000,
            window_id="2", kind="full"))
        with patch("app.config.settings") as cfg:
            cfg.perception.l3_idle_gap_s = 300
            cfg.perception.l3_switch_gap_s = 180
            res = run_segment({"lookback_ms": 10 ** 12}, store=self.ps)
        self.assertGreaterEqual(res["blocks"], 2)
        blocks = self.ps.list_activity_blocks(since_ms=t0 - 1)
        apps = {b["dominant_app"] for b in blocks}
        self.assertIn("Code.exe", apps)
        self.assertIn("Slack.exe", apps)
        # Idempotent re-run does not mint duplicate (ts_start, app) rows.
        n_before = len(blocks)
        with patch("app.config.settings") as cfg:
            cfg.perception.l3_idle_gap_s = 300
            cfg.perception.l3_switch_gap_s = 180
            run_segment({"lookback_ms": 10 ** 12}, store=self.ps)
        self.assertEqual(len(self.ps.list_activity_blocks(since_ms=t0 - 1)),
                         n_before)


# ---------------------------------------------------------------------------
# Cutover wiring
# ---------------------------------------------------------------------------
class CutoverTests(unittest.TestCase):
    def test_l3_on_disables_screen_extract(self) -> None:
        from app.perception.l3_workers import l3_cutover_plan
        on = l3_cutover_plan(l3_enabled=True, extract_on=True)
        self.assertTrue(on["register_l3"])
        self.assertFalse(on["register_screen_extract"])
        self.assertFalse(on["chain_screen_extract_from_activity"])
        self.assertTrue(on["enqueue_l3_from_captures"])

    def test_l3_off_keeps_legacy(self) -> None:
        from app.perception.l3_workers import l3_cutover_plan
        off = l3_cutover_plan(l3_enabled=False, extract_on=True)
        self.assertFalse(off["register_l3"])
        self.assertTrue(off["register_screen_extract"])

    def test_register_l3_jobs_kinds(self) -> None:
        from app.perception.l3_workers import register_l3_jobs

        class FakeWorker:
            def __init__(self):
                self.handlers = {}
                self.enqueued = []

            def register(self, kind, fn):
                self.handlers[kind] = fn

            def enqueue(self, kind, payload=None, *, unique=False):
                self.enqueued.append((kind, payload, unique))

        w = FakeWorker()
        with patch("app.perception.export_parquet.run_export", return_value={}):
            out = register_l3_jobs(w)
        self.assertTrue(out["screen_extract_disabled"])
        for kind in ("l3_segment", "l3_extract", "l3_vlm_fallback",
                     "perception_export"):
            self.assertIn(kind, w.handlers)
        self.assertNotIn("screen_extract", w.handlers)


# ---------------------------------------------------------------------------
# Parquet export
# ---------------------------------------------------------------------------
class ParquetExportTests(PerceptionStoreMixin, unittest.TestCase):
    def test_incremental_partitions(self) -> None:
        from app.perception.export_parquet import run_export
        t0 = 1_700_000_100_000
        self.ps.insert_meta_batch([
            MetaEvent(session_id="S", seq=1, ts_utc=t0, app_name="A"),
            MetaEvent(session_id="S", seq=2, ts_utc=t0 + 1000, app_name="B"),
        ])
        root = self.tmp / "export"
        m1 = run_export(store=self.ps, root=root)
        self.assertEqual(m1["tables"]["meta_events"]["rows"], 2)
        parts = list((root / "meta_events").rglob("*.parquet"))
        self.assertEqual(len(parts), 1)
        # Second run with no new rows → incremental empty.
        m2 = run_export(store=self.ps, root=root)
        self.assertEqual(m2["tables"]["meta_events"]["rows"], 0)
        self.assertEqual(len(list((root / "meta_events").rglob("*.parquet"))), 1)
        # New row advances watermark and writes another part.
        self.ps.insert_meta_batch([
            MetaEvent(session_id="S", seq=3, ts_utc=t0 + 2000, app_name="C"),
        ])
        m3 = run_export(store=self.ps, root=root)
        self.assertEqual(m3["tables"]["meta_events"]["rows"], 1)
        self.assertEqual(len(list((root / "meta_events").rglob("*.parquet"))), 2)


# ---------------------------------------------------------------------------
# VLM fallback skips
# ---------------------------------------------------------------------------
class VlmFallbackTests(PerceptionStoreMixin, unittest.TestCase):
    def test_skipped_when_ocr_long(self) -> None:
        from app.perception.l3_workers import run_vlm_fallback
        cid = "cap_vlm_long"
        self._seed_capture(cid, "x" * 80, thumb="abc")
        with patch("app.config.settings") as cfg:
            cfg.perception.l3_vlm_ocr_chars = 40
            res = run_vlm_fallback({"capture_id": cid}, store=self.ps)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "ocr_sufficient")

    def test_skipped_when_budget_exhausted(self) -> None:
        from app.perception.l3_workers import run_vlm_fallback
        cid = "cap_vlm_budget"
        self._seed_capture(cid, "short", thumb="deadbeef" * 8)
        thumb_path = self.tmp / "frames" / "de" / ("deadbeef" * 8 + ".webp")
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.write_bytes(b"RIFF....WEBP")
        with patch("app.config.settings") as cfg, \
             patch("app.perception.l2_frames.path_for", return_value=thumb_path), \
             patch("app.perception.spend_cap.spend_cap") as cap:
            cfg.perception.l3_vlm_ocr_chars = 40
            cap.allow.return_value = False
            res = run_vlm_fallback({"capture_id": cid}, store=self.ps)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "budget_exhausted")


if __name__ == "__main__":
    unittest.main()
