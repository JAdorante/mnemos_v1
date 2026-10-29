"""Meeting debrief — thank-you recaps to the people on a call.

Covers the send-only grant (scopes, state purpose, callback dispatch, the read
token left alone), the provider send calls, the roster and relationship, the
drafts (private jots and egress-refused items kept out, template fallback),
and the send gate: live human only, the exact draft shown, once per person.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services import mail_send, meeting_debrief
from app.services import exhaust_ingest as ex
from app.services.connectors import outlook as ol

NOW = 1_761_700_000.0

ME = {"name": "Justin Ado", "first": "Justin", "emails": {"justin@ravenry.us"},
      "person_id": None, "domains": {"ravenry.us"}}


def _store(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db", audio_dir=Path(td) / "audio")


class _Env(unittest.TestCase):
    """Set env flags in setUp and RESTORE them (never pop blindly)."""
    ENV: dict[str, str] = {}

    def setUp(self) -> None:
        self._prev = {k: os.environ.get(k) for k in self.ENV}
        os.environ.update(self.ENV)

    def tearDown(self) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- the send-only grant ---------------------------------------------------------
class SendScopeTests(unittest.TestCase):
    def test_google_send_grant_is_exactly_gmail_send(self) -> None:
        mail_send.assert_send_scopes("google", mail_send.GMAIL_SEND_SCOPE)
        with self.assertRaises(PermissionError):
            mail_send.assert_send_scopes(
                "google", mail_send.GMAIL_SEND_SCOPE + " " + ex.SCOPES[0])
        with self.assertRaises(PermissionError):
            mail_send.assert_send_scopes("google", ex.SCOPES[0])

    def test_outlook_tolerates_already_consented_read_scopes_only(self) -> None:
        mail_send.assert_send_scopes(
            "outlook", "https://graph.microsoft.com/Mail.Send Mail.ReadBasic "
                       "Calendars.Read offline_access openid User.Read")
        with self.assertRaises(PermissionError):
            mail_send.assert_send_scopes("outlook", "Mail.Send Mail.ReadWrite")
        with self.assertRaises(PermissionError):
            mail_send.assert_send_scopes("outlook", "Mail.ReadBasic offline_access")


class OutlookReadGuardTests(_Env):
    ENV = {"QUILL_MAIL_SEND": "0"}

    def test_read_grant_tolerates_mail_send_only_where_sending_is_on(self) -> None:
        os.environ["QUILL_MAIL_SEND"] = "0"
        with self.assertRaises(PermissionError):
            ol.assert_metadata_scopes("Mail.ReadBasic Calendars.Read Mail.Send")
        os.environ["QUILL_MAIL_SEND"] = "1"
        ol.assert_metadata_scopes("Mail.ReadBasic Calendars.Read Mail.Send")
        with self.assertRaises(PermissionError):
            ol.assert_metadata_scopes("Mail.ReadBasic Calendars.Read Mail.ReadWrite")


class _OAuthSandbox(_Env):
    ENV = {"QUILL_MAIL_SEND": "1"}

    def setUp(self) -> None:
        super().setUp()
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data = Path(self._td.name)
        from app import config as _cfg
        cfg = _cfg.OutlookConfig.__new__(_cfg.OutlookConfig)
        object.__setattr__(cfg, "client_id", "11111111-2222-3333-4444-555555555555")
        object.__setattr__(cfg, "client_secret", "")
        object.__setattr__(cfg, "tenant", "common")
        for target, val in (
                ("_cfg", lambda: cfg),
                ("_token_path", lambda: self.data / "connectors/outlook/token.json"),
                ("_oauth_state_path",
                 lambda: self.data / "connectors/outlook/oauth_state.json")):
            p = patch.object(ol, target, val)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(ex, "_oauth_state_path",
                         lambda: self.data / "connectors/google/oauth_state.json")
        p.start()
        self.addCleanup(p.stop)


class GrantFlowTests(_OAuthSandbox):
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.adoption import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_off_by_default(self) -> None:
        os.environ["QUILL_MAIL_SEND"] = "0"
        r = mail_send.begin_grant("outlook", public_base="https://seat.example.com")
        self.assertFalse(r["ok"])
        self.assertIn("off", r["error"])

    def test_grant_asks_only_for_send_and_marks_the_state(self) -> None:
        r = mail_send.begin_grant("outlook", public_base="https://seat.example.com",
                                  return_path="/meeting/note/4")
        self.assertTrue(r["ok"])
        self.assertIn("Mail.Send", r["auth_url"])
        self.assertNotIn("Mail.ReadBasic", r["auth_url"])
        self.assertTrue(mail_send.is_send_state("outlook", r["state"]))
        # The relay reads the connector's own state file.
        self.assertTrue(ol.has_oauth_state(r["state"]))

    def test_callback_stores_a_send_token_and_leaves_the_read_token(self) -> None:
        read = self.data / "connectors/outlook/token.json"
        read.parent.mkdir(parents=True, exist_ok=True)
        read.write_text(json.dumps({"access_token": "READ"}))
        r = mail_send.begin_grant("outlook", public_base="https://seat.example.com",
                                  return_path="/meeting/note/4")
        with patch.object(mail_send, "_token_request", lambda p, f: {
                "access_token": "SEND", "refresh_token": "r",
                "scope": "Mail.Send offline_access User.Read"}):
            resp = self._client().get(
                f"/oauth/outlook/callback?code=abc&state={r['state']}",
                follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"],
                         "https://seat.example.com/meeting/note/4?send_granted=outlook")
        self.assertEqual(json.loads(read.read_text())["access_token"], "READ")
        self.assertEqual(mail_send.load_tokens("outlook")["access_token"], "SEND")
        self.assertEqual(mail_send.active_provider(), "outlook")
        mail_send.revoke("outlook")
        self.assertFalse(mail_send.granted("outlook"))
        self.assertTrue(read.is_file())

    def test_a_wider_grant_is_refused_and_not_stored(self) -> None:
        r = mail_send.begin_grant("outlook", public_base="https://seat.example.com")
        with patch.object(mail_send, "_token_request", lambda p, f: {
                "access_token": "X", "scope": "Mail.Send Mail.ReadWrite"}):
            out = mail_send.complete_grant("outlook", "abc", r["state"])
        self.assertFalse(out["ok"])
        self.assertFalse(mail_send.granted("outlook"))

    def test_read_connect_state_is_not_a_send_state(self) -> None:
        start = ol.start_oauth_redirect("https://seat.example.com")
        self.assertFalse(mail_send.is_send_state("outlook", start["state"]))


class ProviderSendTests(_OAuthSandbox):
    def test_gmail_posts_a_raw_rfc822_message(self) -> None:
        calls = []
        tok = self.data / "connectors/google/send_token.json"
        tok.parent.mkdir(parents=True, exist_ok=True)
        tok.write_text(json.dumps({"access_token": "t", "obtained_at": 9e12}))

        def fake_post(url, payload, token):
            calls.append((url, payload))
            return 200, {"id": "m1", "threadId": "th1"}
        with patch.object(mail_send, "_post_json", fake_post):
            out = mail_send.send("google", to="sarah@acme.com", subject="Thanks",
                                 body="Hi Sarah", to_name="Sarah Chen")
        self.assertTrue(out["ok"])
        self.assertEqual(out["message_id"], "m1")
        url, payload = calls[0]
        self.assertTrue(url.endswith("/users/me/messages/send"))
        import base64
        raw = base64.urlsafe_b64decode(payload["raw"]).decode()
        self.assertIn("To: Sarah Chen <sarah@acme.com>", raw)
        self.assertIn("Subject: Thanks", raw)

    def test_graph_posts_send_mail(self) -> None:
        calls = []
        tok = self.data / "connectors/outlook/send_token.json"
        tok.parent.mkdir(parents=True, exist_ok=True)
        tok.write_text(json.dumps({"access_token": "t", "obtained_at": 9e12}))
        with patch.object(mail_send, "_post_json",
                          lambda u, p, t: (calls.append((u, p)) or (202, {}))):
            out = mail_send.send("outlook", to="sarah@acme.com", subject="S", body="B")
        self.assertTrue(out["ok"])
        self.assertTrue(calls[0][0].endswith("/me/sendMail"))
        self.assertEqual(calls[0][1]["message"]["toRecipients"][0]["emailAddress"]
                         ["address"], "sarah@acme.com")

    def test_no_grant_no_send(self) -> None:
        out = mail_send.send("google", to="a@b.com", subject="s", body="b")
        self.assertFalse(out["ok"])


# --- the debrief ---------------------------------------------------------------------
class _Meeting(_Env):
    ENV = {"QUILL_MEETING_DEBRIEF": "1", "QUILL_MAIL_SEND": "1"}

    def setUp(self) -> None:
        super().setUp()
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.store = _store(self._td.name)
        p = patch.object(meeting_debrief, "_user", lambda store: dict(ME))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(meeting_debrief, "_graph_org", lambda store, name: "")
        p.start()
        self.addCleanup(p.stop)
        self.rid = self._seed()

    def _seed(self) -> int:
        from app.events import Event, Modality
        eid = self.store.insert(Event(
            time=NOW - 500, modality=Modality.AUDIO,
            raw="Sarah will send the security questionnaire Friday",
            summary="turn", source="audio.whisper"))
        fid = self.store.add_commitment(
            "Sarah sends the security questionnaire by Friday",
            source_event_id=eid, source_span="send the security questionnaire",
            confidence=0.9, extracted_at=NOW - 470)
        ms = self.store.insert_meeting_session(
            title="Acme onboarding", source="manual", consent="transcript_only",
            status="ended", attendees=[
                {"name": "Sarah Chen", "email": "sarah@acme.com"},
                {"name": "Dave Park", "email": "dave@ravenry.us"},
                {"name": "Hugh Salva"},
                {"name": "Justin Ado", "email": "justin@ravenry.us"},
            ], t_start=NOW - 600, t_end=NOW - 400)
        self.msid = int(ms["id"]) if isinstance(ms, dict) else int(ms)
        rid = self.store.add_reflection(
            scope="meeting", subject_type="meeting_session", subject_id=self.msid,
            period_start=NOW - 600, period_end=NOW - 400,
            summary="Acme onboarding\n\nKicked off the Acme rollout.",
            model="test", confidence=0.8, created_at=NOW - 390)
        self.store.add_reflection_item(
            rid, kind="commitment", text="Sarah sends the security questionnaire by Friday",
            subject="Sarah Chen", confidence=0.9, source_fact_ids=[fid],
            created_at=NOW - 389)
        self.store.add_reflection_item(
            rid, kind="decision", text="Pilot starts with the support team",
            confidence=0.8, source_fact_ids=[], created_at=NOW - 388)
        self.store.add_reflection_item(
            rid, kind="note", text="PRIVATE: Sarah seemed annoyed about pricing",
            detail="live notepad jot", confidence=1.0, source_fact_ids=[],
            created_at=NOW - 387)
        return int(rid)

    def _draft(self, reply=None, **kw):
        reply = reply if reply is not None else (lambda *a, **k: {
            "subject": "Thanks — Acme onboarding", "body": "Hi there,\n\nThanks!\n\nJustin"})
        with patch("app.services.model_router.router.complete_json",
                   side_effect=reply) as m:
            out = meeting_debrief.draft(self.store, self.rid, **kw)
        return out, m


class RosterTests(_Meeting):
    def test_everyone_but_the_user_with_relationships(self) -> None:
        st = meeting_debrief.state(self.store, self.rid)
        by = {a["name"]: a for a in st["attendees"]}
        self.assertEqual(set(by), {"Sarah Chen", "Dave Park", "Hugh Salva"})
        self.assertEqual(by["Dave Park"]["relationship"], "colleague")
        self.assertEqual(by["Dave Park"]["relationship_source"], "suggested")
        self.assertEqual(by["Sarah Chen"]["relationship"], "external")
        self.assertEqual(by["Hugh Salva"]["email"], "")

    def test_a_relationship_the_user_sets_wins_and_sticks(self) -> None:
        out = meeting_debrief.set_relationship(
            self.store, name="Sarah Chen", email="sarah@acme.com",
            relationship="client")
        self.assertTrue(out["ok"])
        sarah = next(a for a in meeting_debrief.state(self.store, self.rid)["attendees"]
                     if a["name"] == "Sarah Chen")
        self.assertEqual((sarah["relationship"], sarah["relationship_source"]),
                         ("client", "you"))
        self.assertFalse(meeting_debrief.set_relationship(
            self.store, name="Sarah Chen", email="", relationship="nemesis")["ok"])

    def test_an_email_from_people_fills_a_roster_gap_without_rekeying(self) -> None:
        key_before = next(a["key"] for a in
                          meeting_debrief.state(self.store, self.rid)["attendees"]
                          if a["name"] == "Hugh Salva")
        pid = self.store.resolve_person("Hugh Salva", ts=NOW)
        self.store.set_person_attr(pid, "email", "hugh@salva.co", None, NOW)
        hugh = next(a for a in meeting_debrief.state(self.store, self.rid)["attendees"]
                    if a["name"] == "Hugh Salva")
        self.assertEqual(hugh["email"], "hugh@salva.co")
        self.assertEqual(hugh["key"], key_before)


class DraftTests(_Meeting):
    def test_one_draft_per_person_and_private_jots_never_reach_the_model(self) -> None:
        out, m = self._draft()
        self.assertEqual(out["drafted"], 3)
        packets = [c.kwargs["messages"][0]["content"] for c in m.call_args_list]
        self.assertTrue(all("PRIVATE" not in p for p in packets))
        sarah = next(p for p in packets if "TO: Sarah Chen" in p)
        self.assertIn("THEIRS (commitment)", sarah)
        self.assertIn("RELATIONSHIP: external", sarah)
        self.assertTrue(all(c.args[0] == "debrief" for c in m.call_args_list))

    def test_redraft_keeps_edits_unless_asked(self) -> None:
        self._draft()
        _, m = self._draft()
        self.assertEqual(m.call_count, 0)
        _, m = self._draft(regenerate=True, keys=["sarah@acme.com"])
        self.assertEqual(m.call_count, 1)

    def test_no_model_still_gives_a_reviewable_draft(self) -> None:
        def boom(*a, **k):
            raise RuntimeError("no key")
        out, _ = self._draft(reply=boom)
        sarah = next(a for a in out["attendees"] if a["name"] == "Sarah Chen")
        self.assertEqual(sarah["model"], "template")
        self.assertIn("Hi Sarah", sarah["body"])
        self.assertIn("security questionnaire", sarah["body"])

    def test_items_the_egress_rule_keeps_home_are_withheld(self) -> None:
        from app.services import privacy_class

        def refuse(text, **kw):
            return "privacy_class=sensitive" if "support team" in text else None
        with patch.object(privacy_class, "egress_refusal", side_effect=refuse):
            out, m = self._draft()
        self.assertEqual(out["withheld"], 1)
        self.assertTrue(all("support team" not in c.kwargs["messages"][0]["content"]
                            for c in m.call_args_list))


class SendGateTests(_Meeting):
    def setUp(self) -> None:
        super().setUp()
        self._draft()
        p = patch.object(mail_send, "active_provider", lambda: "google")
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        p = patch.object(mail_send, "send", lambda prov, **kw: (
            self.sent.append(kw) or {"ok": True, "message_id": f"m{len(self.sent)}"}))
        p.start()
        self.addCleanup(p.stop)

    def _sel(self, key="sarah@acme.com"):
        row = meeting_debrief.rows(self.store, self.rid)[key]
        return [{"key": key, "body_hash": row["body_hash"]}]

    def test_only_a_live_human_sends(self) -> None:
        out = meeting_debrief.send_selected(self.store, self.rid, self._sel(),
                                            live_human=False)
        self.assertFalse(out["ok"])
        self.assertEqual(self.sent, [])

    def test_sends_the_exact_draft_once(self) -> None:
        sel = self._sel()
        out = meeting_debrief.send_selected(self.store, self.rid, sel, live_human=True)
        self.assertEqual(out["sent"], 1)
        self.assertEqual(self.sent[0]["to"], "sarah@acme.com")
        again = meeting_debrief.send_selected(self.store, self.rid, sel, live_human=True)
        self.assertEqual(again["sent"], 0)
        self.assertEqual(again["results"][0]["error"], "already sent")
        self.assertEqual(len(self.sent), 1)
        # A sent row survives a redraft untouched.
        self._draft(regenerate=True)
        self.assertEqual(meeting_debrief.rows(self.store, self.rid)
                         ["sarah@acme.com"]["status"], "sent")

    def test_an_edit_after_review_refuses_the_stale_hash(self) -> None:
        sel = self._sel()
        meeting_debrief.edit(self.store, self.rid, "sarah@acme.com",
                             subject="Thanks", body="changed elsewhere")
        out = meeting_debrief.send_selected(self.store, self.rid, sel, live_human=True)
        self.assertEqual(out["sent"], 0)
        self.assertIn("changed", out["results"][0]["error"])
        self.assertEqual(self.sent, [])

    def test_no_address_no_send_and_a_failure_is_retryable(self) -> None:
        out = meeting_debrief.send_selected(
            self.store, self.rid, self._sel("name:hugh salva"), live_human=True)
        self.assertEqual(out["results"][0]["error"], "no email address")
        with patch.object(mail_send, "send", lambda prov, **kw: {
                "ok": False, "error": "403"}):
            out = meeting_debrief.send_selected(self.store, self.rid, self._sel(),
                                                live_human=True)
        self.assertEqual(meeting_debrief.rows(self.store, self.rid)
                         ["sarah@acme.com"]["status"], "failed")
        out = meeting_debrief.send_selected(self.store, self.rid, self._sel(),
                                            live_human=True)
        self.assertEqual(out["sent"], 1)


class RouteTests(_Meeting):
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import routes
        app = FastAPI()
        app.include_router(routes.router)
        return TestClient(app)

    def test_send_route_refuses_a_bearer_or_cross_site_caller(self) -> None:
        self._draft()
        row = meeting_debrief.rows(self.store, self.rid)["sarah@acme.com"]
        sent = []
        with patch("app.api.routes.memory._ensure_store", lambda: self.store), \
                patch.object(mail_send, "send", lambda *a, **k: sent.append(1)):
            c = self._client()
            body = {"items": [{"key": "sarah@acme.com", "body_hash": row["body_hash"]}]}
            r = c.post(f"/meeting/note/{self.rid}/debrief/send", json=body,
                       headers={"Authorization": "Bearer x"})
            self.assertEqual(r.status_code, 403)
            r = c.post(f"/meeting/note/{self.rid}/debrief/send", json=body)
            self.assertEqual(r.status_code, 403)
            st = c.get(f"/meeting/note/{self.rid}/debrief").json()
        self.assertEqual(sent, [])
        self.assertEqual(len(st["attendees"]), 3)


class OfferTests(_Meeting):
    def test_offer_names_the_people_and_yes_drafts_without_sending(self) -> None:
        fake = MagicMock()
        with patch("app.services.agent_bridge.worker", fake):
            self.assertTrue(meeting_debrief.offer(self.store, self.rid))
        offer = fake._add_offer.call_args.args[0]
        self.assertEqual(offer["kind"], "meeting_debrief")
        self.assertEqual(offer["reflection_id"], self.rid)
        self.assertIn("Sarah Chen", offer["message"])
        self.assertNotIn("Justin Ado", offer["items"])

        from app.services.agent_bridge import AgentWorker
        me = MagicMock()
        with patch.object(meeting_debrief, "draft",
                          return_value={"attendees": [{"status": "draft"}]}) as d, \
                patch.object(mail_send, "send") as s:
            out = AgentWorker._resolve_meeting_debrief(me, offer, True)
            import time
            for _ in range(50):
                if d.called and any(c.args[0] == "result"
                                    for c in me._emit.call_args_list):
                    break
                time.sleep(0.02)
        self.assertTrue(out["accepted"])
        self.assertTrue(d.called)
        s.assert_not_called()
        self.assertTrue(any("#debrief" in str(c) for c in me._emit.call_args_list))

    def test_no_roster_no_offer(self) -> None:
        self.store._conn.execute(
            "UPDATE meeting_sessions SET attendees_json='[]' WHERE id=?", (self.msid,))
        self.store._conn.commit()
        fake = MagicMock()
        with patch("app.services.agent_bridge.worker", fake):
            self.assertFalse(meeting_debrief.offer(self.store, self.rid))
        fake._add_offer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
