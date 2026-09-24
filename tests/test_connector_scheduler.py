"""Connector capture & task fulfillment spec — Feature 1: connector ambient
capture.

A stub connector's items land uniformly (source=<id>.<kind>, observed tier,
never_authorizes, connector_id / external_id / thread_id, privacy_class);
a re-sync never re-lands an item (items_landed stays flat — the soak
criterion); consent gates background sync; the per-chat toggle reads source
prefixes off the registry; the salience gate fires against an open slot
and an open commitment, is rate-limited and deduped, and defers during
meeting mode.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))


class _OverlapEnv(unittest.TestCase):
    """Deterministic similarity (no embedder load); restored after each test
    so a full-suite run never inherits it."""

    def setUp(self):
        self._prev_env = {k: os.environ.get(k) for k in (
            "QUILL_SLOT_SIM", "QUILL_TASK_COMPLETION_SYNC")}
        os.environ["QUILL_SLOT_SIM"] = "overlap"

    def tearDown(self):
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

from app.services import salience  # noqa: E402
from app.services import slots  # noqa: E402
from app.services import task_completion as tc  # noqa: E402
from app.services.connectors import scheduler  # noqa: E402

NOW = 1_726_600_000.0


def _mk(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db")


class StubMail:
    id = "stubmail"
    label = "Stub Mail"
    tool_names = ("Stub Mail",)
    availability = "ready"
    kind = "directory"
    category = "calendar"
    source_prefixes = ("stubmail.mail", "stubmail.calendar")
    sync_interval_s = 60

    def __init__(self, items=None):
        self.items = list(items or [])
        self.calls = 0
        self._connected = True

    def configured(self): return True
    def connected(self): return self._connected
    def status(self): return {"id": self.id, "label": self.label,
                              "availability": self.availability,
                              "configured": True, "connected": self._connected}
    def begin_connect(self, **kw): return {"ok": True}
    def complete_connect(self, code, state, *, redirect_uri): return {"ok": True}
    def sync(self): return {"ok": True}
    def disconnect(self): self._connected = False; return {"ok": True}

    def fetch_items(self, *, cursor=None, now=None):
        self.calls += 1
        return list(self.items), {**(cursor or {}), "since": now}


def _mail(i: int, subject: str, frm: str = "Dana Whitfield <dana@acme.com>",
          thread: str | None = None, body: str | None = None) -> dict:
    return {"kind": "mail", "external_id": f"<m{i}@acme>", "thread_id": thread or f"T{i}",
            "ts": NOW + i, "title": subject,
            "text": f"From: {frm}\nSubject: {subject}", "people": ["Dana Whitfield"],
            "from": frm, "body": body}


class LandingTests(_OverlapEnv):
    def test_items_land_uniformly_and_idempotently(self):
        stub = StubMail([_mail(1, "Boston deal quote attached"),
                         _mail(2, "Lunch?"),
                         {"kind": "calendar", "external_id": "c1", "ts": NOW + 5,
                          "title": "Call with Marc", "start": NOW + 5, "end": NOW + 1500,
                          "attendees": [{"name": "Marc"}], "people": ["Marc"]}])
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                res = scheduler.sync_connector(stub, store=store, now=NOW + 10)
                self.assertEqual((res["fetched"], res["landed"]), (3, 3))
                st = store.connector_sync_get("stubmail")
                self.assertEqual(st["items_landed"], 3)
                self.assertEqual(st["cursor"]["since"], NOW + 10)
                self.assertEqual(st["next_sync"], NOW + 10 + 60)
                rows = store.recent_events(source_substr="stubmail", limit=10)
                self.assertEqual({r["source"] for r in rows},
                                 {"stubmail.mail", "stubmail.calendar"})
                mail = next(r for r in rows if r["raw"].startswith("From: Dana"))
                meta = mail["meta"] if isinstance(mail["meta"], dict) else json.loads(mail["meta"])
                self.assertEqual(meta["epistemic"], "observed")
                self.assertTrue(meta["never_authorizes"])
                self.assertEqual(meta["connector_id"], "stubmail")
                self.assertTrue(meta["external_id"].startswith("<m"))
                self.assertIn(meta["privacy_class"], ("public", "internal", "personal",
                                                      "sensitive", "never-send"))
                self.assertEqual(mail["modality"], "system")   # metadata only
                # Not due yet → skipped; forced → re-fetched but nothing re-lands.
                self.assertEqual(scheduler.sync_connector(
                    stub, store=store, now=NOW + 20).get("skipped"), "not due")
                for tick in range(3):
                    res = scheduler.sync_connector(stub, store=store,
                                                   now=NOW + 100 + tick * 60, force=True)
                    self.assertEqual(res["landed"], 0)
                self.assertEqual(store.connector_sync_get("stubmail")["items_landed"], 3)
                self.assertEqual(len(store.recent_events(source_substr="stubmail",
                                                         limit=50)), 3)
                # An edited item (same external id, new content) lands once more.
                stub.items[1] = _mail(2, "Lunch? (moved to 1 pm)")
                res = scheduler.sync_connector(stub, store=store, now=NOW + 400, force=True)
                self.assertEqual(res["landed"], 1)
                # A body makes it a DOCUMENT.
                stub.items.append(_mail(9, "Quote PDF", body="Full quote text here."))
                scheduler.sync_connector(stub, store=store, now=NOW + 500, force=True)
                doc = store.recent_events(source_substr="stubmail.mail", limit=1)[0]
                self.assertEqual(doc["modality"], "document")
            finally:
                store.close()

    def test_fetch_error_is_recorded_not_raised(self):
        class Broken(StubMail):
            def fetch_items(self, *, cursor=None, now=None):
                raise RuntimeError("token expired")
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                res = scheduler.sync_connector(Broken(), store=store, now=NOW)
                self.assertFalse(res["ok"])
                st = scheduler.status("stubmail", store=store)
                self.assertIn("token expired", st["last_error"])
                self.assertEqual(st["items_landed"], 0)
                self.assertEqual(st["last_sync"], NOW)
            finally:
                store.close()

    def test_consent_gates_background_sync(self):
        stub = StubMail()
        with mock.patch("app.services.capture_consent.allows_connector",
                        return_value=False):
            self.assertFalse(scheduler.syncable(stub))
        with mock.patch("app.services.capture_consent.allows_connector",
                        return_value=True):
            self.assertTrue(scheduler.syncable(stub))
            stub.disconnect()
            self.assertFalse(scheduler.syncable(stub))
        sentence = scheduler.consent_sentence(StubMail())
        self.assertIn("every 1 minute", sentence)
        self.assertIn("read-only", sentence)

    def test_consent_record_round_trips(self):
        from app.services import capture_consent as cc
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(cc, "_path", return_value=Path(td) / "consent.json"):
            cc._cached = None
            try:
                self.assertFalse(cc.allows_connector("stubmail"))
                rec = cc.record_connector("stubmail", enabled=True,
                                          sentence="polls headers", interval_s=60)
                self.assertTrue(rec["enabled"])
                cc._cached = None
                self.assertTrue(cc.allows_connector("stubmail"))
                self.assertFalse(cc.load()["consented"])   # capture untouched
                cc.record_connector("stubmail", enabled=False)
                self.assertFalse(cc.allows_connector("stubmail"))
            finally:
                cc._cached = None

    def test_session_prefixes_come_from_the_registry(self):
        from app.services.connectors import session
        stub = StubMail()
        with mock.patch("app.services.connectors.registry.all",
                        return_value=[stub]):
            self.assertEqual(session.source_prefixes()["stubmail"],
                             ("stubmail.mail", "stubmail.calendar"))
            with mock.patch.object(session, "active_ids", return_value=set()):
                self.assertEqual(session.blocked_source_prefixes(),
                                 ("stubmail.mail", "stubmail.calendar"))
            with mock.patch.object(session, "active_ids", return_value={"stubmail"}):
                self.assertEqual(session.blocked_source_prefixes(), ())

    def test_source_policy_classifies_connector_sources(self):
        from app.services import source_policy as sp
        self.assertEqual(sp.classify_source(event_source="google.mail"), "connector_mail")
        self.assertEqual(sp.classify_source(event_source="google.calendar"),
                         "connector_calendar")
        self.assertEqual(sp.classify_source(event_source="slack.dm"), "connector_mail")
        self.assertEqual(sp.classify_source(event_source="agent.fetch"), "agent_fetch")
        pol = sp.policy_for("connector_mail")
        self.assertTrue(pol.create_commitments)
        self.assertTrue(pol.create_person_candidates)
        self.assertFalse(sp.policy_for("agent_fetch").create_commitments)


class SalienceTests(_OverlapEnv):
    def setUp(self):
        super().setUp()
        self._td = tempfile.TemporaryDirectory()
        self.store = _mk(self._td.name)
        salience.reset_for_tests()
        self.notices = []
        self._pe = mock.patch.object(salience, "_emit",
                                     side_effect=lambda n: self.notices.append(n))
        self._pe.start()
        self._pw = mock.patch.object(salience, "watch_phrases", return_value=[])
        self._pw.start()
        self._po = mock.patch.object(slots, "offer_fill", return_value=True)
        self._po.start()
        os.environ["QUILL_TASK_COMPLETION_SYNC"] = "1"
        tc.attach()

    def tearDown(self):
        tc.detach()
        self._po.stop(); self._pw.stop(); self._pe.stop()
        salience.reset_for_tests()
        self.store.close(); self._td.cleanup()
        super().tearDown()

    def test_mail_item_fires_against_an_open_slot_within_one_interval(self):
        sid = slots.create(self.store, "Boston deal quote",
                           requester={"kind": "user", "id": None}, now=NOW)
        stub = StubMail([_mail(1, "Boston deal quote attached (Q-1042)")])
        res = scheduler.sync_connector(stub, store=self.store, now=NOW + 30)
        self.assertEqual(res["landed"], 1)
        cands = self.store.list_slot_candidates(sid, verdict="offered")
        self.assertEqual(len(cands), 1)      # the watcher scored it above threshold
        # The salience notice for a fill IS the Deliver / Not it offer.
        eid = cands[0]["event_id"]
        ev = self.store.get_event(eid)
        salience.reset_for_tests()   # the insert hook already scored it once
        out = salience.evaluate(self.store, eid, ev, fills=[{"fact_id": sid,
                                                              "score": 1.0,
                                                              "need": "Boston deal quote"}],
                                now=NOW + 30)
        self.assertGreaterEqual(out["score"], salience.THRESHOLD)
        self.assertEqual(out["via"], "slot_offer")

    def test_reply_from_a_counterparty_you_wait_on_notifies_once(self):
        fid = tc.create_user_task(self.store, "Send Dana Whitfield the deck",
                                  counterparty="Dana Whitfield", kind="send", now=NOW)
        self.assertTrue(fid)
        stub = StubMail([_mail(1, "Re: the deck — thanks!", thread="T-A"),
                         _mail(2, "Re: the deck — one more thing", thread="T-A")])
        scheduler.sync_connector(stub, store=self.store, now=NOW + 30)
        # Same thread → deduped to one notice.
        self.assertEqual(len(self.notices), 1)
        n = self.notices[0]
        self.assertIn("Dana Whitfield", n["text"])
        self.assertEqual(n["stream"]["type"], "connector.salient_item")
        self.assertEqual(n["stream"]["connector_id"], "stubmail")
        self.assertEqual(n["stream"]["task_id"], fid)

    def test_below_threshold_is_a_silent_persist(self):
        stub = StubMail([_mail(1, "Newsletter: 10 productivity tips",
                               frm="news@example.com")])
        stub.items[0]["people"] = []
        scheduler.sync_connector(stub, store=self.store, now=NOW + 30)
        self.assertEqual(self.notices, [])
        self.assertEqual(len(self.store.recent_events(source_substr="stubmail",
                                                      limit=5)), 1)

    def test_rate_limit_and_meeting_mode_deferral(self):
        tc.create_user_task(self.store, "Send Dana Whitfield the deck",
                            counterparty="Dana Whitfield", kind="send", now=NOW)
        items = [_mail(i, f"Re: deck {i}", thread=f"T{i}") for i in range(8)]
        with mock.patch.object(salience, "NOTICES_PER_HOUR", 3):
            scheduler.sync_connector(StubMail(items), store=self.store, now=NOW + 30)
        self.assertEqual(len(self.notices), 3)
        salience.reset_for_tests()
        self.notices.clear()
        with mock.patch("app.services.meeting_mode.status",
                        return_value={"active": True}):
            scheduler.sync_connector(StubMail([_mail(20, "Re: deck 20", thread="T20")]),
                                     store=self.store, now=NOW + 60, force=True)
            self.assertEqual(self.notices, [])
            self.assertEqual(salience.deferred_count(), 1)
            self.assertEqual(salience.flush_deferred(), 0)
        with mock.patch("app.services.meeting_mode.status",
                        return_value={"active": False}):
            self.assertEqual(salience.flush_deferred(), 1)
        self.assertEqual(len(self.notices), 1)

    def test_watch_phrase_matches(self):
        self._pw.stop()
        try:
            with mock.patch.object(salience, "watch_phrases",
                                   return_value=["anything from Acme"]):
                stub = StubMail([_mail(1, "anything from Acme lands here",
                                       frm="x@y.z")])
                stub.items[0]["people"] = []
                scheduler.sync_connector(stub, store=self.store, now=NOW + 30)
            self.assertEqual(len(self.notices), 1)
            self.assertIn("watch phrase", self.notices[0]["text"])
        finally:
            self._pw.start()


class GoogleItemShapeTests(_OverlapEnv):
    def test_mail_and_calendar_items_are_metadata_only(self):
        from app.services.connectors.google import google
        m = google._mail_item({"id": "<x@y>", "thread_id": "th1", "ts": NOW,
                               "headers": {"from": "Dana Whitfield <dana@acme.com>",
                                           "to": "me@me.com", "subject": "Boston quote"}})
        self.assertEqual((m["kind"], m["external_id"], m["thread_id"], m["title"]),
                         ("mail", "<x@y>", "th1", "Boston quote"))
        self.assertEqual(m["people"][0], "Dana Whitfield")
        self.assertIsNone(m.get("body"))
        c = google._calendar_item({"id": "c1", "title": "Call with Marc",
                                   "start": NOW, "end": NOW + 1500,
                                   "attendees": [{"email": "m@x", "name": "Marc"}],
                                   "organizer": None})
        self.assertEqual(c["kind"], "calendar")
        self.assertEqual(c["people"], ["Marc"])
        self.assertEqual(c["end"], NOW + 1500)
        self.assertEqual(google.source_prefixes[-1], "google.calendar")

    def test_fetch_items_uses_window_and_subject(self):
        from app.services.connectors.google import google
        with mock.patch("app.services.exhaust_ingest.fetch_gmail_headers",
                        return_value=[]) as fg, \
                mock.patch("app.services.exhaust_ingest.fetch_calendar_events",
                           return_value=[]):
            items, cur = google.fetch_items(cursor={"since": NOW - 3600}, now=NOW)
        self.assertEqual(items, [])
        self.assertEqual(cur["since"], NOW)
        self.assertTrue(fg.call_args.kwargs["include_subject"])
        self.assertLess(fg.call_args.kwargs["days"], 0.2)   # one hour + overlap


class GmailMetadataScopeTests(unittest.TestCase):
    """gmail.metadata refuses ``q`` (HTTP 403 "Metadata scope does not
    support 'q' parameter", live 2026-09-23): the window is a newest-first
    walk that stops at the cutoff, never a search."""

    def test_list_has_no_q_and_stops_at_cutoff(self):
        from app.services import exhaust_ingest as ex
        urls = []
        now = NOW
        msgs = {"m1": now - 3600, "m2": now - 2 * 86400, "m3": now - 40 * 86400}

        def fake_get(url):
            urls.append(url)
            if "/messages?" in url and "/messages/" not in url:
                if "pageToken" in url:
                    return {"messages": [{"id": "m3"}]}
                return {"messages": [{"id": "m1"}, {"id": "m2"}],
                        "nextPageToken": "p2"}
            mid = url.split("/messages/")[1].split("?")[0]
            return {"id": mid, "threadId": "t-" + mid,
                    "internalDate": str(int(msgs[mid] * 1000)),
                    "payload": {"headers": [{"name": "From", "value": "a@x"},
                                            {"name": "Message-ID", "value": f"<{mid}>"}]}}

        with mock.patch.object(ex, "_google_get", fake_get):
            rows = ex.fetch_gmail_headers(days=7, now=now)
        self.assertEqual([r["id"] for r in rows], ["<m1>", "<m2>"])
        for u in urls:
            self.assertNotIn("q=", u)
        # m3 is older than the window: the walk fetched it, then stopped.
        self.assertFalse(any("pageToken=p3" in u for u in urls))

    def test_capture_consent_withdrawal_keeps_connector_consent(self):
        import tempfile
        from pathlib import Path
        from app.services import capture_consent as cc
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cc, "_path", lambda: Path(td) / "c.json"), \
                 mock.patch.object(cc, "_apply_save_audio", lambda *_: None), \
                 mock.patch.object(cc, "_apply_capability_flags", lambda *_: None):
                cc._cached = None
                cc.record_connector("google", enabled=True, sentence="s", interval_s=300)
                self.assertTrue(cc.allows_connector("google"))
                cc.save({"mic": True})
                self.assertTrue(cc.allows_connector("google"))
                cc.save(consented=False)
                self.assertTrue(cc.allows_connector("google"))
                self.assertFalse(cc.allows("mic"))
            cc._cached = None


if __name__ == "__main__":
    unittest.main()
