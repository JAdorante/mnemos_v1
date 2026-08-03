"""Phase A perception safety-floor tests.

Acceptance criteria covered here (from PERCEPTION_IMPLEMENTATION_PROMPT):
  1. No unlabeled gaps (coverage audit over meta_events ∪ gaps)
  5. Privacy gate is pre-pixel (blocklist → no frame bytes, no OCR)
  6. Redaction is pre-egress and pre-log (planted sk-ant-… never appears)
  7. Spend cap holds (cloud denied once USD/day exhausted)
 11. Erasure is complete (SQLite + frames + distill + Parquet)
  4. Crash safety (WAL + dangling-gap reconcile + restart)

Providers/stores are faked or pointed at temp dirs; no network.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_ANT_KEY = "sk-ant-api03-" + "a1B2" * 12
_EMAIL = "alice.secret@example.com"
_PHONE = "+1 (555) 123-4567"


def _fresh_pstore(tmp: Path):
    """PerceptionStore on a temp DB, and patch the module singleton to it."""
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


class PerceptionStoreMixin:
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="perc_a_"))
        self.ps = _fresh_pstore(self.tmp)
        self.addCleanup(_reset_pstore)


# ---------------------------------------------------------------------------
# Criterion 1 — gap coverage
# ---------------------------------------------------------------------------
class GapCoverageTests(PerceptionStoreMixin, unittest.TestCase):
    def test_meta_and_gaps_cover_window(self) -> None:
        from app.perception.schemas import MetaEvent, SCHEMA_VERSION
        t0 = 1_700_000_000_000
        # Heartbeats every 60 s for 5 minutes (last at t0+300s).
        rows = [MetaEvent(session_id="S", seq=i + 1, ts_utc=t0 + i * 60_000,
                          schema_version=SCHEMA_VERSION)
                for i in range(6)]
        self.ps.insert_meta_batch(rows)
        # Sleep gap starts at the last heartbeat so meta∪gaps is contiguous.
        hole_start = t0 + 5 * 60_000
        hole_end = hole_start + 10 * 60_000
        self.ps.add_gap(hole_start, hole_end, "sleep")
        # Resume with more heartbeats.
        more = [MetaEvent(session_id="S", seq=i + 7,
                          ts_utc=hole_end + i * 60_000)
                for i in range(3)]
        self.ps.insert_meta_batch(more)
        end = hole_end + 2 * 60_000
        cov = self.ps.coverage(t0, end)
        self.assertEqual(cov["hole_ms"], 0, cov)
        self.assertEqual(cov["covered_pct"], 100.0, cov)
        self.assertEqual(cov["holes"], [])

    def test_unlabeled_hole_is_surfaced(self) -> None:
        from app.perception.schemas import MetaEvent
        t0 = 1_700_000_000_000
        # Two records 10 minutes apart with NO gap — beyond COVERAGE_MAX_STRIDE.
        self.ps.insert_meta_batch([
            MetaEvent(session_id="S", seq=1, ts_utc=t0),
            MetaEvent(session_id="S", seq=2, ts_utc=t0 + 600_000),
        ])
        cov = self.ps.coverage(t0, t0 + 600_000)
        self.assertGreater(cov["hole_ms"], 0)
        self.assertLess(cov["covered_pct"], 100.0)
        self.assertTrue(cov["holes"])

    def test_pause_writes_and_closes_user_pause_gap(self) -> None:
        from app.perception.l0_meta import L0Monitor
        mon = L0Monitor(store=self.ps, provider=lambda: {},
                        idle_age_fn=lambda: 0.0, use_input_hooks=False,
                        poll_s=0.05, debounce_ms=0, heartbeat_s=60,
                        batch_commit_s=0.1, gap_threshold_s=5.0,
                        audit_every_s=99999)
        mon.start()
        time.sleep(0.2)
        mon.pause()
        gaps = self.ps.list_gaps()
        self.assertTrue(any(g["reason"] == "user_pause" and g["ts_end"] is None
                            for g in gaps), gaps)
        mon.resume()
        time.sleep(0.15)
        mon.stop()
        gaps = self.ps.list_gaps()
        open_pauses = [g for g in gaps
                       if g["reason"] == "user_pause" and g["ts_end"] is None]
        self.assertEqual(open_pauses, [])

    def test_process_down_gap_on_restart(self) -> None:
        from app.perception.schemas import MetaEvent
        from app.perception.l0_meta import L0Monitor
        old = int(time.time() * 1000) - 10 * 60_000
        self.ps.insert_meta_batch([
            MetaEvent(session_id="OLD", seq=1, ts_utc=old)])
        mon = L0Monitor(store=self.ps, provider=lambda: {},
                        idle_age_fn=lambda: 0.0, use_input_hooks=False,
                        poll_s=0.05, heartbeat_s=60, audit_every_s=99999)
        mon.start()
        time.sleep(0.1)
        mon.stop()
        gaps = self.ps.list_gaps()
        self.assertTrue(any(g["reason"] == "process_down" for g in gaps), gaps)


# ---------------------------------------------------------------------------
# Criterion 5 — pre-pixel privacy gate
# ---------------------------------------------------------------------------
class PrePixelPrivacyTests(PerceptionStoreMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        from app.perception.privacy_gate import PrivacyGate
        self.gate = PrivacyGate(blocklist_path=self.tmp / "blocklist.json")

    def test_password_manager_blocked_before_pixels(self) -> None:
        rule = self.gate.check("1Password — All Items")
        self.assertEqual(rule, "builtin:sensitive_title")
        cap_id = self.gate.record_exclusion(rule, window_id="42",
                                            ts_ms=1_700_000_000_000)
        self.assertIsNotNone(cap_id)
        caps = self.ps.recent_captures(0)
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0]["kind"], "excluded")
        self.assertEqual(caps[0]["exclusion_rule"], rule)
        self.assertIsNone(caps[0]["frame_sha256"])
        self.assertIsNone(caps[0]["thumb_sha256"])

    def test_desktop_capture_never_writes_frame_on_block(self) -> None:
        """Prove the gate runs BEFORE jpeg encode/save in the real loop path."""
        import numpy as np
        from app.services.desktop_capture import DesktopCapturePipeline

        pipe = DesktopCapturePipeline()
        pipe._screen_vlm_broken = True
        frames_dir = self.tmp / "frames"
        frames_dir.mkdir()
        ocr_calls: list = []

        with patch("app.services.desktop_capture._foreground_window",
                   return_value={"window": "Bitwarden", "hwnd": 1,
                                 "app": "Bitwarden.exe"}), \
             patch("app.perception.privacy_gate.gate", self.gate), \
             patch("app.services.desktop_capture._rgb_to_jpeg",
                   side_effect=AssertionError("pixels left RAM")), \
             patch("app.services.desktop_capture._downscale",
                   side_effect=AssertionError("pixels left RAM")), \
             patch("app.services.vlm.vlm") as vlm_mock:
            vlm_mock.describe = lambda *a, **k: ocr_calls.append(1) or {}
            rgb = np.zeros((40, 40, 3), dtype=np.uint8)
            pipe._analyze_screen(rgb, motion=1.0, ts=time.time(),
                                 fq={"capture_quality": 0.9})

        self.assertEqual(list(frames_dir.iterdir()), [],
                         "no frame bytes may land on disk for a blocked surface")
        self.assertEqual(ocr_calls, [], "no VLM/OCR call on blocked surface")
        caps = self.ps.recent_captures(0)
        self.assertTrue(any(c["kind"] == "excluded" for c in caps), caps)

    def test_user_blocklist_editable(self) -> None:
        self.gate.add_user_rule("titles", "MyBank Portal")
        self.assertIsNotNone(self.gate.check("Acme — MyBank Portal — Inbox"))
        self.gate.remove_user_rule("titles", "MyBank Portal")
        self.assertIsNone(self.gate.check("Acme — MyBank Portal — Inbox"))


# ---------------------------------------------------------------------------
# Criterion 6 — redaction pre-log / pre-egress
# ---------------------------------------------------------------------------
class RedactionRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="perc_redact_"))
        from app.services.escalate_log import escalate_log
        self._orig = (escalate_log._path, escalate_log._counts,
                      escalate_log._total)
        escalate_log._path = self.tmp / "escalate_distill.jsonl"
        from collections import Counter
        escalate_log._counts = Counter()
        escalate_log._total = 0

    def tearDown(self) -> None:
        from app.services.escalate_log import escalate_log
        escalate_log._path, escalate_log._counts, escalate_log._total = self._orig

    def test_planted_anthropic_key_never_in_log(self) -> None:
        from app.services.escalate_log import escalate_log
        local = {"description": f"key visible: {_ANT_KEY}",
                 "ocr_text": f"export KEY={_ANT_KEY}\ncontact {_EMAIL}",
                 "content_type": "document", "confidence": 0.4}
        parent = {"description": f"saw {_ANT_KEY} and {_PHONE}",
                  "ocr_text": _ANT_KEY, "content_type": "document",
                  "confidence": 0.9}
        escalate_log.record(
            task="vision.describe", reason="hard_type",
            local=local, parent=parent,
            local_model="minicpm-v", parent_model="claude-opus",
            frame_path="data/frames/x.jpg", source="desktop.screen",
            modality="vision")
        raw = (self.tmp / "escalate_distill.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(_ANT_KEY, raw)
        self.assertNotIn(_EMAIL, raw)
        self.assertNotIn("555) 123", raw)
        self.assertNotIn("555-123-4567", raw)
        row = json.loads(raw.strip())
        blob = json.dumps(row)
        self.assertIn("[REDACTED:", blob)

    def test_perception_redactor_tiers(self) -> None:
        from app.perception.redaction import (TIER_EGRESS, TIER_LOG,
                                              TIER_SECRETS, redact_text)
        text = f"key={_ANT_KEY} email={_EMAIL} phone={_PHONE}"
        secrets_only, hits_s = redact_text(text, TIER_SECRETS)
        self.assertNotIn(_ANT_KEY, secrets_only)
        self.assertIn(_EMAIL, secrets_only)          # PII kept at secrets tier
        self.assertTrue(hits_s)

        log_out, hits_l = redact_text(text, TIER_LOG)
        self.assertNotIn(_ANT_KEY, log_out)
        self.assertNotIn(_EMAIL, log_out)
        self.assertIn("email", hits_l)
        egress_out, _ = redact_text(text, TIER_EGRESS)
        self.assertNotIn(_ANT_KEY, egress_out)
        self.assertNotIn(_EMAIL, egress_out)

    def test_secret_kinds_gate_skips_model(self) -> None:
        from app.perception.redaction import secret_kinds
        self.assertIn("anthropic_key", secret_kinds(f"token {_ANT_KEY}"))
        self.assertEqual(secret_kinds("just an email alice@x.com"), [])


# ---------------------------------------------------------------------------
# Criterion 7 — spend cap
# ---------------------------------------------------------------------------
class SpendCapTests(PerceptionStoreMixin, unittest.TestCase):
    def test_exhausted_budget_denies_ambient_cloud(self) -> None:
        from app.perception.spend_cap import BudgetExhausted, spend_cap
        with patch.dict(os.environ, {"QUILL_CLOUD_BUDGET_USD_DAY": "0.01"}):
            self.ps.add_spend(0.01, "vision")
            self.assertFalse(spend_cap.allow("vision"))
            with self.assertRaises(BudgetExhausted):
                spend_cap.check("extract")
            # User-initiated chat is NOT drawn from the ambient budget.
            self.assertTrue(spend_cap.allow("chat"))

    def test_vlm_keeps_local_when_budget_exhausted(self) -> None:
        from app.services.vlm import VLMRouter

        class _Fake:
            model = "fake"
            def describe(self, _b):
                return {"description": "local", "ocr_text": "hi",
                        "content_type": "document", "confidence": 0.3,
                        "objects": [], "people_count": 0, "scene_type": "desk",
                        "title": "", "items": [], "item_confidences": []}

        with patch.dict(os.environ, {"QUILL_CLOUD_BUDGET_USD_DAY": "0.01"}):
            self.ps.add_spend(0.05, "vision")
            r = VLMRouter()
            r.local = _Fake()
            r.parent = _Fake()
            r._local_ok = True
            parent_calls = []
            r.parent.describe = lambda b: parent_calls.append(1) or {
                "description": "cloud", "ocr_text": "", "content_type": "document",
                "confidence": 0.9, "objects": [], "people_count": 0,
                "scene_type": "desk", "title": "", "items": [],
                "item_confidences": []}
            out = r.describe(b"jpeg-bytes", escalate=True,
                             capture_quality=0.9)
            self.assertEqual(parent_calls, [])
            route = out.get("_route") or {}
            self.assertIn("budget", json.dumps(route).lower()
                          + json.dumps(out).lower())

    def test_uncapped_escape_hatch(self) -> None:
        from app.perception.spend_cap import spend_cap
        with patch.dict(os.environ, {"QUILL_CLOUD_BUDGET_USD_DAY": "0"}):
            self.ps.add_spend(999.0, "vision")
            self.assertTrue(spend_cap.allow("vision"))


# ---------------------------------------------------------------------------
# Criterion 11 — erasure completeness
# ---------------------------------------------------------------------------
class ErasureCompletenessTests(PerceptionStoreMixin, unittest.TestCase):
    def test_erase_removes_sqlite_frames_distill_parquet(self) -> None:
        from app.perception.erasure import erase_window
        from app.perception.schemas import Capture, MetaEvent
        from app.services.escalate_log import escalate_log

        t0_ms = 1_700_000_000_000
        t1_ms = t0_ms + 60_000
        t0, t1 = t0_ms / 1000.0, t1_ms / 1000.0

        self.ps.insert_meta_batch([
            MetaEvent(session_id="S", seq=1, ts_utc=t0_ms + 1000)])
        self.ps.insert_capture(Capture(
            ts_utc=t0_ms + 2000, kind="excluded",
            exclusion_rule="builtin:sensitive_title", window_id="1"))

        frames = self.tmp / "desktop_frames"
        frames.mkdir()
        frame = frames / f"screen_{t0 + 1:.3f}.jpg"
        frame.write_bytes(b"FAKEJPEG")

        distill = self.tmp / "escalate_distill.jsonl"
        distill.write_text(json.dumps({
            "time": t0 + 1, "source": "desktop.screen",
            "frame_path": str(frame), "local": {"ocr_text": "secret"},
        }) + "\n", encoding="utf-8")
        _orig_path = escalate_log._path
        escalate_log._path = distill

        export = self.tmp / "export" / "meta_events" / "date=2023-11-14"
        export.mkdir(parents=True)
        (export / "part.parquet").write_bytes(b"PARQUET")

        class _FakeSettings:
            class desktop_capture:
                frame_dir = str(frames)
            class storage:
                data_dir = str(self.tmp)
            class escalate_log:
                path = str(distill)

        # Point Parquet root via data_dir.parent/export — erasure looks at
        # data_dir.parent / "export" OR cwd "export". Use a cwd-relative
        # symlink-free approach: patch the helper's roots via settings + a
        # local export/ under tmp by temporarily chdir... Simpler: put
        # export under tmp and patch _erase_parquet_partitions via the
        # settings.storage.data_dir.parent trick — if data_dir is tmp,
        # parent/export is sibling. So write there too.
        sibling = self.tmp.parent / "export" / "meta_events" / "date=2023-11-14"
        # Avoid polluting the real parent; instead patch the function.
        with patch("app.config.settings") as settings_mock, \
             patch("app.perception.erasure._erase_parquet_partitions",
                   return_value=1) as pq, \
             patch("app.perception.erasure.get_pstore", return_value=self.ps), \
             patch("app.storage.get_store") as gs, \
             patch("app.vectorstore.get_vectorstore") as gv:
            settings_mock.desktop_capture.frame_dir = str(frames)
            settings_mock.storage.data_dir = str(self.tmp)
            settings_mock.escalate_log.path = str(distill)

            class _Store:
                def erase_events_window(self, a, b, **kw):
                    return {"event_ids": [99], "fact_ids": [7],
                            "frame_paths": [str(frame)],
                            "events": 1, "facts": 1, "relations": 0}
            gs.return_value = _Store()

            class _VS:
                def delete_ids(self, ids):
                    self.ids = list(ids)
                    return len(ids)
            vs = _VS()
            gv.return_value = vs

            try:
                manifest = erase_window(t0_ms, t1_ms)
            finally:
                escalate_log._path = _orig_path

        self.assertEqual(self.ps.counts()["meta_events"], 0)
        self.assertEqual(self.ps.counts()["captures"], 0)
        self.assertFalse(frame.exists(), "frame file must be gone")
        remaining = distill.read_text(encoding="utf-8").strip()
        self.assertEqual(remaining, "")
        self.assertEqual(manifest["distill_rows"], 1)
        self.assertEqual(manifest["frames"], 1)
        self.assertTrue(vs.ids)
        pq.assert_called_once()
        # Honest gap spanning the erasure.
        gaps = self.ps.list_gaps()
        self.assertTrue(any(g["reason"] == "privacy_excluded"
                            and g["ts_start"] == t0_ms
                            and g["ts_end"] == t1_ms for g in gaps), gaps)


# ---------------------------------------------------------------------------
# Criterion 4 — crash safety
# ---------------------------------------------------------------------------
class CrashSafetyTests(PerceptionStoreMixin, unittest.TestCase):
    def test_wal_survives_reopen_and_dangling_gap_closes(self) -> None:
        from app.perception.schemas import MetaEvent
        from app.perception.store import PerceptionStore
        self.ps.insert_meta_batch([
            MetaEvent(session_id="S", seq=1, ts_utc=1_700_000_000_000)])
        open_id = self.ps.add_gap(1_700_000_010_000, None, "user_pause")
        db_path = self.ps.db_path
        self.ps.close()
        # Simulate process death: reopen fresh connection (like a restart).
        import app.perception.store as store_mod
        store_mod._pstore = None
        ps2 = PerceptionStore(db_path)
        store_mod._pstore = ps2
        self.assertEqual(ps2.user_version(), 3)
        self.assertEqual(ps2.counts()["meta_events"], 1)
        closed = ps2.close_dangling_gaps(1_700_000_020_000)
        self.assertEqual(closed, 1)
        gaps = ps2.list_gaps()
        row = next(g for g in gaps if g["id"] == open_id)
        self.assertEqual(row["ts_end"], 1_700_000_020_000)

    def test_sleep_gap_from_wall_clock_jump(self) -> None:
        from app.perception.l0_meta import L0Monitor
        clock = {"t": 1000.0}

        def now():
            return clock["t"]

        mon = L0Monitor(store=self.ps,
                        provider=lambda: {"app_name": "x", "window_id": "1",
                                          "window_title": "hi",
                                          "display_hash": "abc"},
                        idle_age_fn=lambda: 0.0, use_input_hooks=False,
                        poll_s=0.01, debounce_ms=0, heartbeat_s=60,
                        batch_commit_s=0.01, gap_threshold_s=5.0,
                        audit_every_s=99999)
        mon._now = now
        mon._tick(now=clock["t"])
        clock["t"] += 30.0          # > gap_threshold_s → sleep
        mon._tick(now=clock["t"])
        mon.flush(force=True, now=clock["t"])
        gaps = self.ps.list_gaps()
        self.assertTrue(any(g["reason"] == "sleep" for g in gaps), gaps)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
class PerceptionEndpointTests(PerceptionStoreMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self) -> None:
        super().setUp()
        from app.perception.privacy_gate import PrivacyGate
        self.gate = PrivacyGate(blocklist_path=self.tmp / "blocklist.json")
        self._gate_patch = patch("app.perception.privacy_gate.gate", self.gate)
        self._gate_patch.start()
        self.addCleanup(self._gate_patch.stop)

    def test_status_recent_blocklist_erase(self) -> None:
        from app.perception.schemas import MetaEvent
        now = int(time.time() * 1000)
        self.ps.insert_meta_batch([
            MetaEvent(session_id="S", seq=1, ts_utc=now - 1000)])

        r = self.client.get("/perception/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("capturing", body)
        self.assertIn("l0", body)
        self.assertIn("spend", body)

        r = self.client.get("/perception/recent", params={"minutes": 5})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["meta_events"])

        r = self.client.get("/perception/blocklist")
        self.assertEqual(r.status_code, 200)
        self.assertIn("builtin", r.json())

        r = self.client.post("/perception/blocklist",
                             json={"kind": "titles", "value": "SecretApp"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("secretapp", r.json()["user"]["titles"])

        r = self.client.delete("/perception/blocklist",
                               params={"kind": "titles", "value": "SecretApp"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("secretapp", r.json()["user"]["titles"])

        with patch("app.perception.erasure.erase_window",
                   return_value={"ok": True}) as ew:
            r = self.client.post("/perception/erase",
                                 json={"ts_start_ms": now - 10_000,
                                       "ts_end_ms": now})
            self.assertEqual(r.status_code, 200)
            ew.assert_called_once()

        r = self.client.post("/perception/erase",
                             json={"ts_start_ms": now, "ts_end_ms": now})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
