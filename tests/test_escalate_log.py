"""Tests for the escalation distillation trail (Part 1 of the retrain pipe).

Two layers:
  * EscalateLog — append-only JSONL writer: row shape, disabled flag, crash-safe
    writes, restart-safe counts, stats.
  * VLMRouter._distill wiring — a distill row is written exactly when the parent
    (Claude) actually runs: hard type / low confidence / weak capture /
    local error / local disabled. Never on escalate=False, never on a plain
    scene, never when the parent call itself fails.

Providers are faked; no network, no real model calls.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services.escalate_log import EscalateLog, escalate_log
from app.services.vlm import VLMRouter


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _res(content_type="none", confidence=0.9, **kw) -> dict:
    return {"description": "a scene", "ocr_text": "", "people_count": 0,
            "objects": [], "scene_type": "desk", "content_type": content_type,
            "title": "", "items": [], "item_confidences": [],
            "confidence": confidence, **kw}


class _FakeProvider:
    def __init__(self, res=None, exc: Exception | None = None, model="fake-model"):
        self.model = model
        self._res = res or _res()
        self._exc = exc
        self.calls = 0

    def describe(self, jpeg_bytes: bytes) -> dict:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return dict(self._res)


class _TempTrailMixin:
    """Point the module singleton at a temp file for the duration of a test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_distill_"))
        self.trail = self.tmp / "escalate_distill.jsonl"
        self._orig = (escalate_log._path, escalate_log._counts, escalate_log._total)
        escalate_log._path = self.trail
        from collections import Counter
        escalate_log._counts = Counter()
        escalate_log._total = 0

    def tearDown(self) -> None:
        escalate_log._path, escalate_log._counts, escalate_log._total = self._orig


class EscalateLogTests(_TempTrailMixin, unittest.TestCase):
    def test_record_appends_one_clean_row(self) -> None:
        local = {**_res("todo_list", 0.4), "_provider": "ollama", "_route": {"x": 1}}
        parent = {**_res("todo_list", 0.95, items=["buy milk"]), "_provider": "claude"}
        row = escalate_log.record(
            task="vision.describe", reason="hard_type", local=local, parent=parent,
            local_model="minicpm-v", parent_model="claude-opus-4-8",
            capture_quality=0.7, frame_path="data/frames/x.jpg",
            source="desktop.screen", modality="vision")
        self.assertIsNotNone(row)
        rows = _rows(self.trail)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["task"], "vision.describe")
        self.assertEqual(r["reason"], "hard_type")
        self.assertEqual(r["frame_path"], "data/frames/x.jpg")
        self.assertEqual(r["source"], "desktop.screen")
        self.assertEqual(r["user_outcome"], "unknown")
        # Both structured payloads survive, with router-internal tags stripped.
        self.assertEqual(r["local"]["content_type"], "todo_list")
        self.assertEqual(r["parent"]["items"], ["buy milk"])
        self.assertNotIn("_provider", r["local"])
        self.assertNotIn("_route", r["local"])
        self.assertNotIn("_provider", r["parent"])

    def test_rows_reference_frames_not_image_bytes(self) -> None:
        escalate_log.record(task="vision.describe", reason="low_confidence",
                            local=_res(), parent=_res(),
                            frame_path="data/frames/y.jpg")
        text = self.trail.read_text(encoding="utf-8")
        self.assertLess(len(text), 4000)   # a pointer row, not an embedded image
        self.assertNotIn("base64", text)

    def test_disabled_writes_nothing(self) -> None:
        with mock.patch.object(EscalateLog, "enabled", return_value=False):
            out = escalate_log.record(task="vision.describe", reason="hard_type",
                                      local=_res(), parent=_res())
        self.assertIsNone(out)
        self.assertFalse(self.trail.exists())

    def test_write_failure_is_swallowed(self) -> None:
        # Parent "directory" is a FILE, so mkdir/open must fail — record() should
        # warn and return None, never raise into the vision path.
        blocker = self.tmp / "blocker"
        blocker.write_text("x", encoding="utf-8")
        escalate_log._path = blocker / "trail.jsonl"
        out = escalate_log.record(task="vision.describe", reason="hard_type",
                                  local=_res(), parent=_res())
        self.assertIsNone(out)

    def test_counts_reload_from_existing_trail(self) -> None:
        escalate_log.record(task="vision.describe", reason="hard_type",
                            local=_res(), parent=_res())
        escalate_log.record(task="vision.describe", reason="hard_type",
                            local=_res(), parent=_res())
        escalate_log.record(task="vision.describe", reason="local_error",
                            local=None, parent=_res(), local_error="boom")
        fresh = EscalateLog.__new__(EscalateLog)  # bypass settings path
        import threading
        from collections import Counter
        fresh._lock = threading.Lock()
        fresh._path = self.trail
        fresh._counts = Counter()
        fresh._total = 0
        fresh._load_counts()
        self.assertEqual(fresh._total, 3)
        self.assertEqual(fresh._counts["hard_type"], 2)
        self.assertEqual(fresh._counts["local_error"], 1)

    def test_stats_shape(self) -> None:
        escalate_log.record(task="vision.describe", reason="weak_capture",
                            local=_res(), parent=_res())
        s = escalate_log.stats(recent=5)
        self.assertEqual(s["total"], 1)
        self.assertEqual(s["by_reason"], {"weak_capture": 1})
        self.assertEqual(len(s["recent"]), 1)
        self.assertTrue(s["path"].endswith("escalate_distill.jsonl"))


class RouterDistillTests(_TempTrailMixin, unittest.TestCase):
    def _router(self, local: _FakeProvider, claude: _FakeProvider,
                local_ok: bool = True,
                lite: _FakeProvider | None = None) -> VLMRouter:
        r = VLMRouter()
        r.local = local
        r.claude = claude
        # Cheap tier defaults to the same fake so single-parent tests keep
        # working; pass `lite` explicitly to assert tier selection.
        r.claude_lite = lite if lite is not None else claude
        r._local_ok = local_ok   # skip the availability probe
        return r

    def test_hard_type_escalates_and_logs_pair(self) -> None:
        local = _FakeProvider(_res("todo_list", 0.9))
        claude = _FakeProvider(_res("todo_list", 0.97))
        out = self._router(local, claude).describe(b"jpg")
        self.assertEqual(out["_provider"], "claude")
        self.assertEqual(claude.calls, 1)
        rows = _rows(self.trail)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "hard_type")
        self.assertIsNotNone(rows[0]["local"])
        self.assertIsNotNone(rows[0]["parent"])

    def test_low_confidence_reason(self) -> None:
        local = _FakeProvider(_res("notes", 0.2))
        claude = _FakeProvider(_res("notes", 0.9))
        self._router(local, claude).describe(b"jpg")
        self.assertEqual(_rows(self.trail)[0]["reason"], "low_confidence")

    def test_weak_capture_reason(self) -> None:
        local = _FakeProvider(_res("notes", 0.95))
        claude = _FakeProvider(_res("notes", 0.9))
        self._router(local, claude).describe(b"jpg", capture_quality=0.2)
        row = _rows(self.trail)[0]
        self.assertEqual(row["reason"], "weak_capture")
        self.assertEqual(row["capture_quality"], 0.2)

    def test_plain_confident_scene_stays_local_no_row(self) -> None:
        local = _FakeProvider(_res("none", 0.9))
        claude = _FakeProvider()
        out = self._router(local, claude).describe(b"jpg")
        self.assertEqual(out["_provider"], "ollama")
        self.assertEqual(claude.calls, 0)
        self.assertEqual(_rows(self.trail), [])

    def test_escalate_false_never_calls_parent_or_logs(self) -> None:
        # Low-confidence hard type — would escalate, but the caller (click crop)
        # said no. The cost pass depends on this staying local-only.
        local = _FakeProvider(_res("todo_list", 0.1))
        claude = _FakeProvider()
        out = self._router(local, claude).describe(b"jpg", escalate=False)
        self.assertEqual(out["_provider"], "ollama")
        self.assertEqual(claude.calls, 0)
        self.assertEqual(_rows(self.trail), [])

    def test_escalate_false_local_error_logs_nothing(self) -> None:
        local = _FakeProvider(exc=RuntimeError("gpu gone"))
        claude = _FakeProvider()
        out = self._router(local, claude).describe(b"jpg", escalate=False)
        self.assertEqual(out["_provider"], "none")
        self.assertEqual(claude.calls, 0)
        self.assertEqual(_rows(self.trail), [])

    def test_local_error_falls_back_and_logs(self) -> None:
        local = _FakeProvider(exc=RuntimeError("gpu gone"))
        claude = _FakeProvider(_res("notes", 0.9))
        out = self._router(local, claude).describe(
            b"jpg", context={"frame_path": "data/frames/z.jpg",
                             "source": "vision.webcam", "modality": "vision"})
        self.assertEqual(out["_provider"], "claude")
        row = _rows(self.trail)[0]
        self.assertEqual(row["reason"], "local_error")
        self.assertIsNone(row["local"])
        self.assertIn("gpu gone", row["local_error"])
        self.assertEqual(row["frame_path"], "data/frames/z.jpg")
        self.assertEqual(row["source"], "vision.webcam")

    def test_local_disabled_logs_parent_only_row(self) -> None:
        local = _FakeProvider()
        claude = _FakeProvider(_res("notes", 0.9))
        out = self._router(local, claude, local_ok=False).describe(b"jpg")
        self.assertEqual(out["_provider"], "claude")
        row = _rows(self.trail)[0]
        self.assertEqual(row["reason"], "local_disabled")
        self.assertIsNone(row["local"])
        self.assertIsNotNone(row["parent"])

    def test_tier_selection_accurate_vs_lite(self) -> None:
        # hard_type earns the accurate reader; a merely-unsure local read and
        # local outages go to the cheap tier. The distill row records which.
        local = _FakeProvider(_res("todo_list", 0.9))
        accurate = _FakeProvider(_res("todo_list", 0.97), model="acc")
        lite = _FakeProvider(_res("todo_list", 0.9), model="lite")
        self._router(local, accurate, lite=lite).describe(b"jpg")
        self.assertEqual((accurate.calls, lite.calls), (1, 0))
        self.assertEqual(_rows(self.trail)[-1]["parent_model"], "acc")

        local = _FakeProvider(_res("notes", 0.2))
        accurate = _FakeProvider(model="acc")
        lite = _FakeProvider(_res("notes", 0.9), model="lite")
        self._router(local, accurate, lite=lite).describe(b"jpg")
        self.assertEqual((accurate.calls, lite.calls), (0, 1))
        self.assertEqual(_rows(self.trail)[-1]["parent_model"], "lite")

        local = _FakeProvider(exc=RuntimeError("gpu gone"))
        accurate = _FakeProvider(model="acc")
        lite = _FakeProvider(_res("notes", 0.9), model="lite")
        self._router(local, accurate, lite=lite).describe(b"jpg")
        self.assertEqual((accurate.calls, lite.calls), (0, 1))
        self.assertEqual(_rows(self.trail)[-1]["parent_model"], "lite")

    def test_parent_failure_keeps_local_and_logs_nothing(self) -> None:
        local = _FakeProvider(_res("todo_list", 0.9))
        claude = _FakeProvider(exc=RuntimeError("api down"))
        out = self._router(local, claude).describe(b"jpg")
        self.assertEqual(out["_provider"], "ollama")
        self.assertEqual(_rows(self.trail), [])


if __name__ == "__main__":
    unittest.main()
