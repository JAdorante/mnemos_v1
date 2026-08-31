"""Erasure — the "leave nothing behind" path.

The claims under test are the ones the pilot agreement puts in writing: the
confirmation cannot be tripped by reflex, every capture directory is emptied
and not just ``data/``, shipped config is kept unless asked for, capture stops
before the delete, and the receipt survives the directory it describes.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import wipe


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="quill_wipe_"))
        self.data = self.root / "data"
        self.sessions = self.root / "sessions"
        self.desk = self.root / "desktop_agent" / "sessions"
        for d in (self.data, self.sessions, self.desk):
            d.mkdir(parents=True)
        (self.data / "quill.db").write_bytes(b"x" * 4096)
        (self.data / "source_policies.json").write_text("{}")
        (self.data / "audio").mkdir()
        (self.data / "audio" / "clip.wav").write_bytes(b"y" * 2048)
        (self.sessions / "abc").mkdir()
        (self.sessions / "abc" / "shot.png").write_bytes(b"z" * 512)
        (self.desk / "desktop_audit.jsonl").write_text("{}\n")
        (self.root / ".credentials.env").write_text("ANTHROPIC_API_KEY=sk-x\n")

        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": str(self.data)},
                              clear=False)
        self.env.start()
        # install_root() is the real repo checkout and the session dirs default
        # to CWD-relative paths inside it; a test that forgets these patches
        # would delete the developer's own sessions/.
        self.root_patch = patch.object(wipe, "install_root",
                                       lambda: self.root)
        self.root_patch.start()
        self.browser_patch = patch.object(wipe, "_browser_sessions_dir",
                                          lambda: self.sessions)
        self.browser_patch.start()
        self.desk_patch = patch.object(wipe, "_desktop_sessions_dir",
                                       lambda: self.desk)
        self.desk_patch.start()
        # Capture control and DB handles are exercised in their own tests.
        self.stop_patch = patch.object(
            wipe, "stop_capture", lambda: {"ok": True, "revoked": True,
                                           "stopped": True, "errors": []})
        self.stop_patch.start()
        self.close_patch = patch.object(wipe, "_close_stores", lambda: [])
        self.close_patch.start()

    def tearDown(self) -> None:
        for p in (self.close_patch, self.stop_patch, self.desk_patch,
                  self.browser_patch, self.root_patch, self.env):
            p.stop()
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _receipt(self, res: dict) -> dict:
        path = Path(res["receipt_path"])
        return json.loads(path.read_text())


class TestConfirmation(_Base):
    def test_wrong_phrase_deletes_nothing(self) -> None:
        for bad in ("", "yes", "delete", "DELETE MY MEMORIES"):
            with self.assertRaises(wipe.WipeRefused):
                wipe.wipe(bad)
        self.assertTrue((self.data / "quill.db").is_file())
        self.assertTrue((self.sessions / "abc" / "shot.png").is_file())

    def test_phrase_is_case_and_space_forgiving(self) -> None:
        res = wipe.wipe("  delete my memory  ")
        self.assertTrue(res["ok"])
        self.assertFalse((self.data / "quill.db").exists())

    def test_guard_refuses_unsafe_roots(self) -> None:
        with self.assertRaises(wipe.WipeRefused):
            wipe._guard(Path(Path.cwd().anchor))
        with self.assertRaises(wipe.WipeRefused):
            wipe._guard(Path.home())


class TestPreview(_Base):
    def test_counts_every_capture_directory(self) -> None:
        p = wipe.preview()
        keys = {r["key"] for r in p["targets"]}
        self.assertEqual(keys, {"data", "browser_sessions", "desktop_sessions"})
        by = {r["key"]: r for r in p["targets"]}
        # quill.db + clip.wav, but not the kept source_policies.json.
        self.assertEqual(by["data"]["files"], 2)
        self.assertEqual(by["data"]["bytes"], 4096 + 2048)
        self.assertEqual(by["browser_sessions"]["files"], 1)
        self.assertEqual(by["desktop_sessions"]["files"], 1)
        self.assertEqual(p["total_files"], 4)
        self.assertEqual(p["confirm_phrase"], wipe.CONFIRM_PHRASE)

    def test_reports_credentials_present(self) -> None:
        self.assertEqual(
            wipe.preview()["credentials_present"],
            [str(self.root / ".credentials.env")])


class TestWipe(_Base):
    def test_empties_all_three_targets(self) -> None:
        res = wipe.wipe(wipe.CONFIRM_PHRASE)
        self.assertTrue(res["ok"])
        self.assertTrue(res["complete"])
        self.assertFalse((self.data / "quill.db").exists())
        self.assertFalse((self.data / "audio").exists())
        self.assertFalse((self.sessions / "abc").exists())
        self.assertFalse((self.desk / "desktop_audit.jsonl").exists())
        # The directories themselves stay: a running server keeps its paths.
        for d in (self.data, self.sessions, self.desk):
            self.assertTrue(d.is_dir())

    def test_keeps_shipped_config_by_default(self) -> None:
        wipe.wipe(wipe.CONFIRM_PHRASE)
        self.assertTrue((self.data / "source_policies.json").is_file())

    def test_full_removes_shipped_config(self) -> None:
        res = wipe.wipe(wipe.CONFIRM_PHRASE, full=True)
        self.assertFalse((self.data / "source_policies.json").exists())
        self.assertEqual(res["kept"], [])

    def test_credentials_only_on_request(self) -> None:
        creds = self.root / ".credentials.env"
        wipe.wipe(wipe.CONFIRM_PHRASE)
        self.assertTrue(creds.is_file())
        res = wipe.wipe(wipe.CONFIRM_PHRASE, credentials=True)
        self.assertFalse(creds.exists())
        self.assertEqual(res["credentials_removed"], [".credentials.env"])

    def test_stops_capture_before_deleting(self) -> None:
        order: list[str] = []
        self.stop_patch.stop()

        def _stop() -> dict:
            # The database must still be here when capture is told to stop —
            # stopping afterwards races the delete and recreates the store.
            order.append("stop")
            order.append("db" if (self.data / "quill.db").is_file() else "gone")
            return {"ok": True, "revoked": True, "stopped": True, "errors": []}

        with patch.object(wipe, "stop_capture", _stop):
            wipe.wipe(wipe.CONFIRM_PHRASE)
        self.assertEqual(order, ["stop", "db"])
        self.stop_patch.start()


class TestReceipt(_Base):
    def test_written_outside_the_wiped_directory(self) -> None:
        res = wipe.wipe(wipe.CONFIRM_PHRASE)
        path = Path(res["receipt_path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.root)
        self.assertNotIn(str(self.data), str(path))

    def test_records_what_was_there_and_what_survived(self) -> None:
        res = wipe.wipe(wipe.CONFIRM_PHRASE)
        rec = self._receipt(res)
        self.assertEqual(rec["kind"], "mnemos.deletion_receipt/1")
        self.assertEqual(rec["files_before"], 4)
        self.assertEqual(rec["bytes_before"], 4096 + 2048 + 512 + 3)
        self.assertEqual(rec["kept"], list(wipe.KEEP_NAMES))
        self.assertEqual(rec["failures"], [])
        self.assertTrue(rec["complete"])
        self.assertIn("no server", rec["statement"])
        self.assertEqual({t["key"] for t in rec["targets"]},
                         {"data", "browser_sessions", "desktop_sessions"})

    def test_carries_no_personal_content(self) -> None:
        (self.data / "quill.db").write_bytes(b"secret meeting transcript")
        res = wipe.wipe(wipe.CONFIRM_PHRASE)
        blob = Path(res["receipt_path"]).read_text()
        self.assertNotIn("secret meeting transcript", blob)
        self.assertNotIn("sk-x", blob)

    def test_partial_failure_is_reported_not_hidden(self) -> None:
        real = wipe.shutil.rmtree

        def boom(path, *a, **kw):
            if Path(path).name == "audio":
                raise PermissionError("file in use")
            return real(path, *a, **kw)

        with patch.object(wipe.shutil, "rmtree", boom):
            res = wipe.wipe(wipe.CONFIRM_PHRASE)
        self.assertFalse(res["ok"])
        self.assertFalse(res["complete"])
        self.assertTrue(any("file in use" in f for f in res["failures"]))
        self.assertIn("could not be removed", res["statement"])
        # Everything else still went.
        self.assertFalse((self.data / "quill.db").exists())


class TestStopCapture(unittest.TestCase):
    def test_revokes_consent_and_stops_pipelines(self) -> None:
        calls: list[str] = []

        class _Consent:
            SOURCES = ("mic",)

            @staticmethod
            def save(sources=None, *, consented=None):
                calls.append(f"save:{consented}")
                return {}

            @staticmethod
            def status():
                return {"consented": False}

        import sys
        import types
        routes = types.ModuleType("app.api.routes")
        routes.stop_all = lambda: calls.append("stop_all")
        with patch.dict(sys.modules, {"app.api.routes": routes}), \
                patch("app.services.capture_consent.save", _Consent.save), \
                patch("app.services.capture_consent.status", _Consent.status):
            out = wipe.stop_capture()
        self.assertTrue(out["ok"])
        self.assertEqual(calls, ["save:False", "stop_all"])
        self.assertTrue(out["revoked"])
        self.assertTrue(out["stopped"])

    def test_survives_a_broken_half(self) -> None:
        import sys
        import types
        routes = types.ModuleType("app.api.routes")

        def _boom() -> None:
            raise RuntimeError("no pipelines here")

        routes.stop_all = _boom
        with patch.dict(sys.modules, {"app.api.routes": routes}), \
                patch("app.services.capture_consent.save",
                      lambda *a, **k: {}):
            out = wipe.stop_capture()
        self.assertFalse(out["ok"])
        self.assertTrue(out["revoked"])
        self.assertFalse(out["stopped"])
        self.assertTrue(any("no pipelines" in e for e in out["errors"]))


class TestCloseStores(unittest.TestCase):
    def test_closes_and_drops_the_store_singleton(self) -> None:
        import app.storage as storage

        class _Fake:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        fake = _Fake()
        original = storage._store
        storage._store = fake
        try:
            closed = wipe._close_stores()
        finally:
            storage._store = original
        self.assertTrue(fake.closed)
        self.assertIn("store", closed)


class TestTargetsFollowTheAgents(unittest.TestCase):
    """Both session roots are env-relocatable and CWD-relative by default.

    Hard-coding ``<install>/sessions`` here would delete the wrong directory
    on a relocated install and, worse, leave the real captures in place while
    reporting a clean wipe.
    """

    def test_browser_root_comes_from_the_agent_config(self) -> None:
        import browser_agent.config as bc
        with patch.object(bc, "SESSIONS_ROOT", Path("/tmp/quill-browser-x")):
            self.assertEqual(wipe._browser_sessions_dir(),
                             Path("/tmp/quill-browser-x").resolve())

    def test_desktop_root_comes_from_the_agent_config(self) -> None:
        import desktop_agent.config as dc
        with patch.object(dc, "SESSIONS_ROOT", Path("/tmp/quill-desk-x")):
            self.assertEqual(wipe._desktop_sessions_dir(),
                             Path("/tmp/quill-desk-x").resolve())

    def test_falls_back_when_an_agent_cannot_be_imported(self) -> None:
        import sys
        with patch.dict(sys.modules, {"browser_agent.config": None}):
            self.assertEqual(wipe._browser_sessions_dir(),
                             (wipe.install_root() / "sessions").resolve())


class TestEgressRoute(unittest.TestCase):
    """/privacy/egress is assembled from four other modules' payloads.

    The failure mode is silent: read a key that was renamed and the tester is
    shown "never" for something that did happen. So the contract is asserted
    against the real producers rather than against a mock of them.
    """

    def test_reads_keys_that_actually_exist(self) -> None:
        from app.services import update_check, usage_ledger
        ping = usage_ledger.ping_status()
        for key in ("consented", "url_configured", "will_ping",
                    "last_ping_at", "last_error"):
            self.assertIn(key, ping)
        upd = update_check.status()
        for key in ("enabled", "url_configured", "checked_at", "state"):
            self.assertIn(key, upd)
        from app.perception.spend_cap import spend_cap
        for key in ("budget_usd_day", "spent_usd", "denied_today", "uncapped"):
            self.assertIn(key, spend_cap.status())

    def test_assembles_all_four_sections(self) -> None:
        from app.api import adoption
        out = adoption.privacy_egress()
        self.assertTrue(out["ok"])
        for section in ("spend", "cloud", "usage_ping", "update_check"):
            self.assertIn(section, out)

    def test_a_broken_source_does_not_take_the_page_down(self) -> None:
        from app.api import adoption
        from app.services.model_log import model_log

        def boom(**_kw):
            raise RuntimeError("trail unreadable")

        with patch.object(model_log, "egress_inventory", boom):
            out = adoption.privacy_egress()
        self.assertTrue(out["ok"])
        self.assertFalse(out["cloud"]["ok"])
        self.assertIn("trail unreadable", out["cloud"]["error"])
        self.assertIn("spend", out)


class TestWipeRoutes(unittest.TestCase):
    def test_bad_confirmation_is_a_400_not_a_500(self) -> None:
        from fastapi import HTTPException

        from app.api import adoption
        with self.assertRaises(HTTPException) as ctx:
            adoption.privacy_wipe(adoption.WipeIn(confirm="yes"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn(wipe.CONFIRM_PHRASE, str(ctx.exception.detail))

    def test_stop_route_delegates_to_the_service(self) -> None:
        from app.api import adoption
        sentinel = {"ok": True, "revoked": True, "stopped": True, "errors": []}
        with patch.object(wipe, "stop_capture", lambda: sentinel):
            self.assertIs(adoption.privacy_stop(), sentinel)

    def test_preview_route_never_deletes(self) -> None:
        from app.api import adoption
        with patch.object(wipe, "wipe") as never:
            out = adoption.privacy_wipe_preview()
        never.assert_not_called()
        self.assertEqual(out["confirm_phrase"], wipe.CONFIRM_PHRASE)


if __name__ == "__main__":
    unittest.main()
