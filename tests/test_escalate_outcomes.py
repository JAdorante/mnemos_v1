"""Tests for user_outcome labeling on the escalation distill trail (Task 2).

Three layers:
  * EscalateLog.set_user_outcome — matching (row_id / frame_path / source+time
    window), most-recent-wins, allowed values, and the crash-safe in-place
    rewrite (temp file + os.replace) that keeps the trail a single canonical
    JSONL file.
  * stats().by_outcome — the accepted/rejected/edited/unknown histogram.
  * The offer-flow hook — AgentWorker.resolve_todo threads a vision-born offer's
    frame_path back onto the trail as accepted/rejected (unit-level: a faked
    pending offer, no browser/agent spin-up).

No network, no real model calls.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from app.services.escalate_log import escalate_log


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


class _TempTrailMixin:
    """Point the module singleton at a temp file for the duration of a test.

    Mirrors tests/test_escalate_log.py — kept local so each test module stands
    alone under unittest discovery."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_distill_"))
        self.trail = self.tmp / "escalate_distill.jsonl"
        self._orig = (escalate_log._path, escalate_log._counts, escalate_log._total)
        escalate_log._path = self.trail
        escalate_log._counts = Counter()
        escalate_log._total = 0

    def tearDown(self) -> None:
        escalate_log._path, escalate_log._counts, escalate_log._total = self._orig

    def _record(self, frame_path="data/frames/a.jpg", source="vision.webcam",
                reason="low_confidence", **kw) -> dict:
        row = escalate_log.record(
            task="vision.describe", reason=reason,
            local={"description": "x", "confidence": 0.4},
            parent={"description": "y", "confidence": 0.9},
            frame_path=frame_path, source=source, modality="vision", **kw)
        assert row is not None
        return row


class SetUserOutcomeTests(_TempTrailMixin, unittest.TestCase):
    def test_frame_path_match_flips_exactly_that_row(self) -> None:
        self._record(frame_path="data/frames/a.jpg")
        self._record(frame_path="data/frames/b.jpg")
        self._record(frame_path="data/frames/c.jpg")
        ok = escalate_log.set_user_outcome("accepted",
                                           frame_path="data/frames/b.jpg")
        self.assertTrue(ok)
        rows = _rows(self.trail)          # every line still parses (valid JSONL)
        self.assertEqual(len(rows), 3)
        by_fp = {r["frame_path"]: r["user_outcome"] for r in rows}
        self.assertEqual(by_fp["data/frames/b.jpg"], "accepted")
        self.assertEqual(by_fp["data/frames/a.jpg"], "unknown")
        self.assertEqual(by_fp["data/frames/c.jpg"], "unknown")

    def test_rejected_and_edited_values(self) -> None:
        self._record(frame_path="data/frames/a.jpg")
        self._record(frame_path="data/frames/b.jpg")
        self.assertTrue(escalate_log.set_user_outcome(
            "rejected", frame_path="data/frames/a.jpg"))
        self.assertTrue(escalate_log.set_user_outcome(
            "edited", frame_path="data/frames/b.jpg"))
        by_fp = {r["frame_path"]: r["user_outcome"] for r in _rows(self.trail)}
        self.assertEqual(by_fp["data/frames/a.jpg"], "rejected")
        self.assertEqual(by_fp["data/frames/b.jpg"], "edited")

    def test_invalid_value_raises_and_leaves_trail_alone(self) -> None:
        self._record()
        with self.assertRaises(ValueError):
            escalate_log.set_user_outcome("approved", frame_path="data/frames/a.jpg")
        self.assertEqual(_rows(self.trail)[0]["user_outcome"], "unknown")

    def test_row_id_match(self) -> None:
        r1 = self._record(frame_path="data/frames/a.jpg")
        self._record(frame_path="data/frames/a.jpg")   # same frame, newer
        ok = escalate_log.set_user_outcome("accepted", row_id=r1["id"])
        self.assertTrue(ok)
        rows = _rows(self.trail)
        self.assertEqual(rows[0]["user_outcome"], "accepted")   # the id'd row
        self.assertEqual(rows[1]["user_outcome"], "unknown")

    def test_legacy_rows_without_id_still_match_by_frame_path(self) -> None:
        legacy = {"time": 100.0, "task": "vision.describe", "reason": "hard_type",
                  "source": "vision.webcam", "frame_path": "data/frames/old.jpg",
                  "user_outcome": "unknown"}
        self.trail.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        ok = escalate_log.set_user_outcome("accepted",
                                           frame_path="data/frames/old.jpg")
        self.assertTrue(ok)
        self.assertEqual(_rows(self.trail)[0]["user_outcome"], "accepted")

    def test_time_window_fallback_matches_same_source(self) -> None:
        row = self._record(frame_path="", source="vision.webcam")
        ok = escalate_log.set_user_outcome(
            "accepted", source="vision.webcam", time=row["time"] + 30)
        self.assertTrue(ok)
        self.assertEqual(_rows(self.trail)[0]["user_outcome"], "accepted")

    def test_outside_window_or_wrong_source_is_a_quiet_no_op(self) -> None:
        row = self._record(frame_path="", source="vision.webcam")
        # Outside the 120s window.
        self.assertFalse(escalate_log.set_user_outcome(
            "accepted", source="vision.webcam", time=row["time"] + 500))
        # Different source, inside the window.
        self.assertFalse(escalate_log.set_user_outcome(
            "accepted", source="desktop.screen", time=row["time"] + 30))
        # Unknown frame_path, no time fallback provided.
        self.assertFalse(escalate_log.set_user_outcome(
            "accepted", frame_path="data/frames/nope.jpg"))
        # No keys at all.
        self.assertFalse(escalate_log.set_user_outcome("accepted"))
        self.assertEqual(_rows(self.trail)[0]["user_outcome"], "unknown")

    def test_missing_file_returns_false(self) -> None:
        self.assertFalse(escalate_log.set_user_outcome(
            "accepted", frame_path="data/frames/a.jpg"))

    def test_multiple_matches_update_most_recent_only(self) -> None:
        # The same page escalated three times; the user judged the NEWEST parent
        # output, so only that row earns the label.
        for _ in range(3):
            self._record(frame_path="data/frames/same.jpg")
        ok = escalate_log.set_user_outcome("accepted",
                                           frame_path="data/frames/same.jpg")
        self.assertTrue(ok)
        rows = _rows(self.trail)
        self.assertEqual([r["user_outcome"] for r in rows],
                         ["unknown", "unknown", "accepted"])

    def test_rewrite_preserves_row_count_and_content(self) -> None:
        originals = [self._record(frame_path=f"data/frames/{i}.jpg")
                     for i in range(5)]
        escalate_log.set_user_outcome("rejected", frame_path="data/frames/2.jpg")
        rows = _rows(self.trail)
        self.assertEqual(len(rows), 5)
        for orig, got in zip(originals, rows):
            self.assertEqual(got["id"], orig["id"])
            self.assertEqual(got["frame_path"], orig["frame_path"])
            self.assertEqual(got["parent"], orig["parent"])

    def test_concurrent_record_and_label_do_not_corrupt(self) -> None:
        for i in range(3):
            self._record(frame_path=f"data/frames/seed{i}.jpg")

        def writer(k: int) -> None:
            for i in range(10):
                self._record(frame_path=f"data/frames/w{k}_{i}.jpg")

        def labeler() -> None:
            for i in range(3):
                escalate_log.set_user_outcome(
                    "accepted", frame_path=f"data/frames/seed{i}.jpg")

        threads = [threading.Thread(target=writer, args=(k,)) for k in range(3)]
        threads += [threading.Thread(target=labeler) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = _rows(self.trail)          # raises if any line is corrupt
        self.assertEqual(len(rows), 3 + 3 * 10)
        labeled = {r["frame_path"] for r in rows if r["user_outcome"] == "accepted"}
        self.assertEqual(labeled, {f"data/frames/seed{i}.jpg" for i in range(3)})

    def test_stats_includes_by_outcome(self) -> None:
        self._record(frame_path="data/frames/a.jpg")
        self._record(frame_path="data/frames/b.jpg")
        self._record(frame_path="data/frames/c.jpg")
        escalate_log.set_user_outcome("accepted", frame_path="data/frames/a.jpg")
        escalate_log.set_user_outcome("rejected", frame_path="data/frames/b.jpg")
        s = escalate_log.stats(recent=5)
        self.assertEqual(s["by_outcome"],
                         {"accepted": 1, "rejected": 1, "unknown": 1})


class OfferFlowLabelTests(_TempTrailMixin, unittest.TestCase):
    """resolve_todo threads the offer's frame_path back onto the distill trail."""

    def _worker_with_offer(self, frame_path: str, event_time: float):
        from app.services.agent_bridge import AgentWorker

        w = AgentWorker()
        # send() would spin up the real agent thread — stub it out; this test is
        # only about the labeling hook, not dispatch.
        w.send = mock.Mock()
        w.propose_todo(["buy milk"], "groceries",
                       frame_path=frame_path, event_time=event_time)
        self.assertIsNotNone(w.pending_todo)
        self.assertEqual(w.pending_todo.get("frame_path"), frame_path)
        return w

    def test_yes_labels_accepted(self) -> None:
        row = self._record(frame_path="data/frames/offer.jpg")
        w = self._worker_with_offer("data/frames/offer.jpg", row["time"])
        out = w.resolve_todo(True)
        self.assertTrue(out["ok"])
        self.assertEqual(_rows(self.trail)[0]["user_outcome"], "accepted")
        w.send.assert_called()   # the accepted items were still dispatched

    def test_no_labels_rejected(self) -> None:
        row = self._record(frame_path="data/frames/offer.jpg")
        w = self._worker_with_offer("data/frames/offer.jpg", row["time"])
        out = w.resolve_todo(False)
        self.assertTrue(out["ok"])
        self.assertEqual(_rows(self.trail)[0]["user_outcome"], "rejected")

    def test_offer_without_frame_path_skips_labeling(self) -> None:
        from app.services.agent_bridge import AgentWorker

        self._record(frame_path="data/frames/other.jpg")
        w = AgentWorker()
        w.send = mock.Mock()
        w.propose_todo(["buy milk"])          # no frame_path — nothing to label
        w.resolve_todo(True)
        self.assertEqual(_rows(self.trail)[0]["user_outcome"], "unknown")


if __name__ == "__main__":
    unittest.main()
