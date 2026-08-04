"""Plan 5.1 — evidence-anchored verification registry."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class OutcomeVerifyUnitTests(unittest.TestCase):
    def test_llm_only_never_verified(self):
        from app.services import outcome_verify as ov

        ev = ov.llm_only_evidence(True, "looks sent")
        self.assertEqual(ev.status, ov.OUTCOME_UNCERTAIN)
        self.assertEqual(ov.step_record_status(evidence=ev), ov.OUTCOME_UNCERTAIN)
        self.assertEqual(
            ov.status_from_evidence(None, llm_satisfied=True),
            ov.OUTCOME_UNCERTAIN)

    def test_sent_folder_toast_verifies(self):
        from app.services import outcome_verify as ov

        ev = ov.verify_email_sent(
            page_text="Message sent. Moved to Sent Items.",
            url="https://mail.example/inbox",
            drafted=["Following up on pricing"],
        )
        self.assertTrue(ev.ok)
        self.assertEqual(ev.status, ov.VERIFIED)
        self.assertEqual(ev.source, ov.SRC_SENT)

    def test_composer_clear_alone_not_verified(self):
        from app.services import outcome_verify as ov

        # No toast, not in Sent folder — DOM opinion only.
        ev = ov.verify_email_sent(
            page_text="Thanks for the update",
            url="https://mail.example/u/0/#inbox",
            drafted=["Hello Marc"],
        )
        self.assertFalse(ev.ok)
        self.assertEqual(ev.status, ov.OUTCOME_UNCERTAIN)

    def test_mail_query_verifies(self):
        from app.services import outcome_verify as ov

        ev = ov.verify_email_sent(
            page_text="",
            url="https://mail.example/inbox",
            drafted=["Hello"],
            mail_query=lambda q: {"id": "msg-1", "folder": "Sent"},
            query={"to": "marc@x.com"},
        )
        self.assertTrue(ev.ok)
        self.assertEqual(ev.source, ov.SRC_MAIL)
        self.assertEqual(ev.status, ov.VERIFIED)

    def test_file_stat_readback(self):
        from app.services import outcome_verify as ov

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "note.txt"
            body = "hello evidence"
            t0 = time.time()
            p.write_text(body, encoding="utf-8")
            ev = ov.verify_file(p, expect_bytes=len(body.encode("utf-8")),
                                min_mtime=t0 - 1)
            self.assertTrue(ev.ok)
            self.assertEqual(ev.status, ov.VERIFIED)
            miss = ov.verify_file(Path(td) / "missing.txt")
            self.assertFalse(miss.ok)
            self.assertEqual(miss.status, ov.FAILED)

    def test_calendar_get_readback(self):
        from app.services import outcome_verify as ov

        def fake_get(href, calendar="Home"):
            return {"ok": True, "uid": "abc", "href": href, "status": 200}

        ev = ov.verify_calendar_event("cal/abc.ics", getter=fake_get)
        self.assertTrue(ev.ok)
        self.assertEqual(ev.source, ov.SRC_CAL)
        self.assertEqual(ev.status, ov.VERIFIED)

        def miss(href, calendar="Home"):
            return {"ok": False, "status": 404, "error": "gone"}

        bad = ov.verify_calendar_event("cal/nope.ics", getter=miss)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.status, ov.FAILED)


class WriteFileStatTests(unittest.TestCase):
    def test_write_file_returns_verified_stat(self):
        from desktop_agent.driver import DesktopDriver

        with tempfile.TemporaryDirectory() as td:
            d = DesktopDriver(
                jail_root=Path(td),
                on_approve=lambda *a, **k: True,
                on_log=lambda _s: None,
            )
            res = d.write_file("hello.txt", "abc")
            self.assertTrue(res.get("ok"))
            self.assertEqual(res.get("step_status"), "verified")
            self.assertTrue((res.get("verify") or {}).get("ok"))
            self.assertEqual((res.get("verify") or {}).get("source"), "os.stat")


class AgentStepsStatusTests(unittest.TestCase):
    def test_record_defaults_empty_to_uncertain(self):
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                rid = store.start_agent_run("goal", surface="test")
                store.record_agent_steps(rid, [
                    {"step_index": 0, "action_type": "click",
                     "input": {}, "output": "ok", "verification": "llm",
                     "status": None},
                    {"step_index": 1, "action_type": "click",
                     "input": {}, "output": "ok",
                     "status": "outcome_uncertain"},
                    {"step_index": 2, "action_type": "click",
                     "input": {}, "output": "ok", "status": "verified"},
                ])
                rows = (store.agent_run(rid) or {}).get("step_log") or []
                statuses = [r["status"] for r in rows]
                self.assertEqual(statuses[0], "outcome_uncertain")
                self.assertEqual(statuses[1], "outcome_uncertain")
                self.assertEqual(statuses[2], "verified")
            finally:
                store.close()

    def test_create_event_attaches_verify(self):
        from app.services import icloud_calendar as cal

        put_resp = mock.Mock(status_code=201)
        get_resp = mock.Mock(status_code=200,
                             text="BEGIN:VCALENDAR\nUID:x\nEND:VCALENDAR")
        fake_icloud = mock.Mock(sync_enabled=True)

        with mock.patch.object(cal, "settings",
                               mock.Mock(icloud=fake_icloud)), \
             mock.patch.object(cal.icloud_account, "_read_saved",
                               return_value=("u", "p")), \
             mock.patch.object(cal, "discover",
                               return_value=("https://cal/",
                                             [{"href": "c/", "name": "Home"}])), \
             mock.patch.object(cal.requests, "put", return_value=put_resp), \
             mock.patch.object(cal.requests, "get", return_value=get_resp):
            out = cal.create_event("Standup", "2030-01-01T10:00:00")
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("step_status"), "verified")
        self.assertTrue((out.get("verify") or {}).get("ok"))


if __name__ == "__main__":
    unittest.main()
