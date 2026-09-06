"""Unit tests for app.services.peer_channel — the Sparrow <-> Sparrow team
channel (mutual pairing, per-peer tokens, disclosure-gated ask/answer).

Pin the trust model: codes are single-use, expiring, brute-force-limited;
pairing is MUTUAL (one round trip leaves both sides authenticated); what we
accept is stored hash-only; the default posture queues every inbound ask for
the human ("offer"); auto mode composes through the grounded answer path with
the redact egress gate; an inbound answer must match an ask WE sent to THAT
peer. Peer traffic lands as observed-tier context events, never commands.

Run with either:
    python -m unittest discover -s tests        # zero dependencies
    pytest tests/                               # if pytest is installed
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.events import Modality, bus  # noqa: E402
from app.services import peer_channel as pch  # noqa: E402


class PeerChannelBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="peer_")
        os.environ["QUILL_PEER_REGISTRY"] = str(Path(self._tmp) / "peers.json")
        os.environ["QUILL_PEER_ASKS"] = str(Path(self._tmp) / "asks.json")
        os.environ["QUILL_PEER_SENT"] = str(Path(self._tmp) / "sent.json")
        os.environ["QUILL_PEER_MAILBOX"] = str(Path(self._tmp) / "mailbox.json")
        os.environ["QUILL_PEER_TEAMS"] = str(Path(self._tmp) / "teams.json")
        os.environ["QUILL_PEER_LOOPS"] = str(Path(self._tmp) / "loops.json")
        # Keep answers as bus context events in these tests — the ingest path
        # (Phase 3) writes to the real store and is tested with mocks below.
        os.environ["QUILL_PEER_INGEST"] = "0"
        pch._pairing = None
        self.events: list = []
        bus.subscribe(self._collect)

    def tearDown(self) -> None:
        for key in ("QUILL_PEER_REGISTRY", "QUILL_PEER_ASKS",
                    "QUILL_PEER_SENT", "QUILL_PEER_INGEST",
                    "QUILL_PEER_MAILBOX", "QUILL_PEER_TEAMS",
                    "QUILL_PEER_LOOPS", "QUILL_PEER_REQUIRE_TLS"):
            os.environ.pop(key, None)
        bus._subscribers.remove(self._collect)
        pch._pairing = None

    def _collect(self, ev) -> None:
        self.events.append(ev)

    def _claimed_peer(self, name: str = "Sarah") -> dict:
        """Pair as the DESKTOP side (a remote claimed our code)."""
        start = pch.start_pairing()
        self.assertTrue(start["ok"], start)
        claim = pch.claim_pairing(start["code"], name,
                                  "http://198.51.100.7:8000",
                                  "remote-minted-token-for-us-0123456789")
        self.assertTrue(claim["ok"], claim)
        return claim

    def _registry(self) -> dict:
        return json.loads(
            Path(os.environ["QUILL_PEER_REGISTRY"]).read_text(encoding="utf-8"))


class PairingTests(PeerChannelBase):
    def test_claim_is_mutual_and_hash_only(self) -> None:
        claim = self._claimed_peer()
        reg = self._registry()
        rec = reg[next(iter(reg))]
        # What THEY present to us: hash only, never the minted token.
        self.assertNotIn(claim["token"], json.dumps(reg))
        self.assertEqual(rec["token_sha256"], pch._hash(claim["token"]))
        # What WE present to them: the token they minted, kept verbatim.
        self.assertEqual(rec["outbound_token"],
                         "remote-minted-token-for-us-0123456789")
        self.assertEqual(rec["base_url"], "http://198.51.100.7:8000")

    def test_code_single_use_and_lockout(self) -> None:
        start = pch.start_pairing()
        self._claim_ok = pch.claim_pairing(start["code"], "x",
                                           "http://h:1", "t" * 20)
        again = pch.claim_pairing(start["code"], "y", "http://h:1", "t" * 20)
        self.assertFalse(again["ok"])
        pch.start_pairing()
        errors: list[str] = []
        for _ in range(10):
            res = pch.claim_pairing("000000x", "z", "http://h:1", "t" * 20)
            self.assertFalse(res["ok"])
            errors.append(res["error"])
        # Brute force hits the attempt cap, cancels the offer, and stays dead.
        self.assertTrue(any("cancelled" in e for e in errors))
        self.assertIn("no active pairing", errors[-1])

    def test_claim_validates_callback_fields(self) -> None:
        start = pch.start_pairing()
        bad_url = pch.claim_pairing(start["code"], "x", "not-a-url", "t" * 20)
        self.assertFalse(bad_url["ok"])
        short = pch.claim_pairing(start["code"], "x", "http://h:1", "short")
        self.assertFalse(short["ok"])
        # Neither malformed claim consumed the code.
        good = pch.claim_pairing(start["code"], "x", "http://h:1", "t" * 20)
        self.assertTrue(good["ok"], good)

    def test_join_stores_both_directions(self) -> None:
        sent_payloads: list = []

        def fake_post(url, payload, token=None):
            sent_payloads.append((url, payload))
            return {"ok": True, "peer_id": "abc", "name": "Justin",
                    "token": "their-token-for-us"}

        with mock.patch.object(pch, "_post_json", side_effect=fake_post):
            res = pch.join("http://192.0.2.9:8000/", "123456")
        self.assertTrue(res["ok"], res)
        url, payload = sent_payloads[0]
        self.assertEqual(url, "http://192.0.2.9:8000/peer/pair/claim")
        rec = self._registry()[res["peer_id"]]
        # We present what they returned; we accept (hash of) what we minted.
        self.assertEqual(rec["outbound_token"], "their-token-for-us")
        self.assertEqual(rec["token_sha256"],
                         pch._hash(payload["token_for_caller"]))
        self.assertEqual(rec["name"], "Justin")

    def test_authenticate_and_revoke(self) -> None:
        claim = self._claimed_peer()
        peer = pch.authenticate(f"Bearer {claim['token']}")
        self.assertIsNotNone(peer)
        self.assertEqual(peer["name"], "Sarah")
        self.assertIsNone(pch.authenticate("Bearer wrong"))
        self.assertIsNone(pch.authenticate(None))
        self.assertTrue(pch.revoke(peer["peer_id"]))
        self.assertIsNone(pch.authenticate(f"Bearer {claim['token']}"))


class InboundAskTests(PeerChannelBase):
    def _peer(self) -> dict:
        claim = self._claimed_peer()
        return pch.authenticate(f"Bearer {claim['token']}")

    def test_default_posture_queues_for_human(self) -> None:
        peer = self._peer()
        res = pch.handle_ask(peer, {"ask_id": "a1", "question": "deadline?"})
        self.assertEqual(res["status"], "pending")
        pend = pch.pending_asks()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["question"], "deadline?")
        # The ask landed as observed context, not a command.
        ask_events = [e for e in self.events if e.source == "peer.ask"]
        self.assertEqual(len(ask_events), 1)
        self.assertEqual(ask_events[0].modality, Modality.SYSTEM)

    def test_ask_requires_fields(self) -> None:
        peer = self._peer()
        self.assertFalse(pch.handle_ask(peer, {"question": "q"})["ok"])
        self.assertFalse(pch.handle_ask(peer, {"ask_id": "a"})["ok"])
        self.assertFalse(pch.handle_ask(peer, "nope")["ok"])

    def test_auto_mode_composes_and_redacts(self) -> None:
        peer = self._peer()
        auto = SimpleNamespace(peer=SimpleNamespace(
            enabled=True, auto_answer=True, max_text_chars=4000,
            max_pending_asks=50, history=200,
            peers_path=os.environ["QUILL_PEER_REGISTRY"],
            asks_path=os.environ["QUILL_PEER_ASKS"],
            sent_path=os.environ["QUILL_PEER_SENT"]))
        with mock.patch.object(pch, "settings", auto), \
             mock.patch.object(pch, "compose_peer_answer",
                               return_value={"text": "Friday Aug 7.",
                                             "redacted": []}):
            res = pch.handle_ask(peer, {"ask_id": "a2", "question": "when?"})
        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["answer"], "Friday Aug 7.")
        self.assertEqual(res["redacted"], [])

    def test_approve_composes_delivers_and_finishes(self) -> None:
        peer = self._peer()
        pch.handle_ask(peer, {"ask_id": "a3", "question": "when?"})
        local_id = pch.pending_asks()[0]["id"]
        delivered: list = []
        with mock.patch.object(pch, "compose_peer_answer",
                               return_value={"text": "Friday Aug 7.",
                                             "redacted": []}), \
             mock.patch.object(pch, "_deliver",
                               side_effect=lambda rec, p: delivered.append(p) or True):
            res = pch.decide_ask(local_id, approve=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(delivered[0], {"ask_id": "a3", "answer": "Friday Aug 7."})
        self.assertEqual(pch.pending_asks(), [])

    def test_deny_notifies_peer(self) -> None:
        peer = self._peer()
        pch.handle_ask(peer, {"ask_id": "a4", "question": "salary?"})
        local_id = pch.pending_asks()[0]["id"]
        delivered: list = []
        with mock.patch.object(pch, "_deliver",
                               side_effect=lambda rec, p: delivered.append(p) or True):
            res = pch.decide_ask(local_id, approve=False)
        self.assertEqual(res["status"], "denied")
        self.assertEqual(delivered[0], {"ask_id": "a4", "declined": True})
        self.assertEqual(pch.pending_asks(), [])


class DisclosurePolicyTests(PeerChannelBase):
    def _peer(self) -> dict:
        claim = self._claimed_peer()
        return pch.authenticate(f"Bearer {claim['token']}")

    def test_policy_validation(self) -> None:
        peer = self._peer()
        pid = peer["peer_id"]
        self.assertFalse(pch.set_policy(pid, {"wizardry": "auto"})["ok"])
        self.assertFalse(pch.set_policy(pid, {"work": "maybe"})["ok"])
        self.assertFalse(pch.set_policy("nobody", {"work": "auto"})["ok"])
        # The hard floor: personal can never be granted auto.
        res = pch.set_policy(pid, {"personal": "auto"})
        self.assertFalse(res["ok"])
        self.assertIn("personal", res["error"])
        # A partial policy fills the rest with "offer".
        res = pch.set_policy(pid, {"availability": "auto"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["policy"]["availability"], "auto")
        self.assertEqual(res["policy"]["work"], "offer")

    def test_default_policy_skips_classifier_and_offers(self) -> None:
        peer = self._peer()
        with mock.patch.object(pch, "classify_question") as classify:
            res = pch.handle_ask(peer, {"ask_id": "p1", "question": "when?"})
        self.assertEqual(res["status"], "pending")
        classify.assert_not_called()

    def test_allowed_class_auto_answers(self) -> None:
        peer = self._peer()
        pch.set_policy(peer["peer_id"], {"availability": "auto"})
        with mock.patch.object(pch, "classify_question",
                               return_value="availability"), \
             mock.patch.object(pch, "compose_peer_answer",
                               return_value={"text": "Friday Aug 7.",
                                             "redacted": []}):
            res = pch.handle_ask(peer, {"ask_id": "p2",
                                        "question": "free thursday?"})
        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["topic"], "availability")

    def test_other_classes_still_queue(self) -> None:
        peer = self._peer()
        pch.set_policy(peer["peer_id"], {"availability": "auto"})
        with mock.patch.object(pch, "classify_question", return_value="work"):
            res = pch.handle_ask(peer, {"ask_id": "p3",
                                        "question": "project status?"})
        self.assertEqual(res["status"], "pending")

    def test_deny_class_declines_without_queueing(self) -> None:
        peer = self._peer()
        pch.set_policy(peer["peer_id"], {"contact": "deny"})
        with mock.patch.object(pch, "classify_question", return_value="contact"):
            res = pch.handle_ask(peer, {"ask_id": "p4",
                                        "question": "his number?"})
        self.assertEqual(res["status"], "declined")
        self.assertEqual(pch.pending_asks(), [])

    def test_classifier_failure_falls_back_to_offer(self) -> None:
        peer = self._peer()
        pch.set_policy(peer["peer_id"], {"availability": "auto"})
        with mock.patch.object(pch, "classify_question", return_value=None):
            res = pch.handle_ask(peer, {"ask_id": "p5", "question": "???"})
        self.assertEqual(res["status"], "pending")

    def test_personal_never_autos_even_if_stored(self) -> None:
        # Belt + braces: a policy file edited by hand to personal=auto still
        # cannot leak — enforcement re-floors it to offer.
        peer = self._peer()
        import json as _json
        from pathlib import Path as _P
        reg_path = _P(os.environ["QUILL_PEER_REGISTRY"])
        reg = _json.loads(reg_path.read_text(encoding="utf-8"))
        reg[peer["peer_id"]]["policy"] = {c: "offer" for c in pch.CLASSES}
        reg[peer["peer_id"]]["policy"]["personal"] = "auto"
        reg_path.write_text(_json.dumps(reg), encoding="utf-8")
        with mock.patch.object(pch, "classify_question", return_value="personal"):
            res = pch.handle_ask(peer, {"ask_id": "p6", "question": "salary?"})
        self.assertEqual(res["status"], "pending")

    def test_synchronous_decline_recorded_on_asker(self) -> None:
        self._claimed_peer()
        pid = next(iter(self._registry()))
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "declined"}):
            res = pch.ask(pid, "what is his salary?")
        self.assertEqual(res["status"], "declined")
        self.assertEqual(pch.answers(res["ask_id"])[0]["status"], "declined")


class OutboundAskTests(PeerChannelBase):
    def _peer_id(self) -> str:
        self._claimed_peer()
        return next(iter(self._registry()))

    def test_synchronous_answer_recorded_as_context(self) -> None:
        pid = self._peer_id()
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "answered",
                                             "answer": "Friday Aug 7."}):
            res = pch.ask(pid, "when is the deadline?")
        self.assertEqual(res["status"], "answered")
        rows = pch.answers()
        self.assertEqual(rows[0]["status"], "answered")
        self.assertEqual(rows[0]["answer"], "Friday Aug 7.")
        ans_events = [e for e in self.events if e.source == "peer.answer"]
        self.assertEqual(len(ans_events), 1)

    def test_pending_then_delivery(self) -> None:
        pid = self._peer_id()
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "pending"}):
            res = pch.ask(pid, "when?")
        self.assertEqual(res["status"], "pending")
        peer = {"peer_id": pid, "name": "Sarah"}
        got = pch.handle_answer(peer, {"ask_id": res["ask_id"],
                                       "answer": "Friday Aug 7."})
        self.assertEqual(got["status"], "recorded")
        self.assertEqual(pch.answers(res["ask_id"])[0]["answer"], "Friday Aug 7.")

    def test_unsolicited_answers_refused(self) -> None:
        pid = self._peer_id()
        peer = {"peer_id": pid, "name": "Sarah"}
        self.assertFalse(pch.handle_answer(peer, {"ask_id": "never-sent",
                                                  "answer": "gotcha"})["ok"])
        # Right ask_id, WRONG peer: also refused.
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "pending"}):
            res = pch.ask(pid, "when?")
        other = {"peer_id": "someone-else", "name": "Mallory"}
        self.assertFalse(pch.handle_answer(other, {"ask_id": res["ask_id"],
                                                   "answer": "gotcha"})["ok"])

    def test_unreachable_peer_is_queued(self) -> None:
        pid = self._peer_id()
        with mock.patch.object(pch, "_post_json",
                               side_effect=OSError("connection refused")):
            res = pch.ask(pid, "when?")
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "queued")
        self.assertEqual(pch.answers(res["ask_id"])[0]["status"], "queued")


class ChatIntentTests(PeerChannelBase):
    def setUp(self) -> None:
        super().setUp()
        self._claimed_peer("Sarah Chen")
        self.pid = next(iter(self._registry()))

    def test_parse_forms(self) -> None:
        for text in ("ask Sarah: are the slides done?",
                     "Ask sarah, are the slides done",
                     "ask Sarah Chen: slides done?",
                     "ask Sarah's Sparrow: are the slides done?",
                     "ask sarah whether the slides are done"):
            got = pch.parse_team_ask(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got["peer_id"], self.pid, text)
            self.assertIn("slides", got["question"], text)

    def test_parse_user_n_keeps_digit_out_of_question(self) -> None:
        """'ask User 2 about X' must not leave '2' in the question body."""
        self._claimed_peer("User 2")
        uid = next(pid for pid, rec in self._registry().items()
                   if rec.get("name") == "User 2")
        for text, needle in (
            ("ask User 2 about Venture Pulse", "Venture Pulse"),
            ("ask User 2 what he knows about Venture Pulse", "Venture Pulse"),
            ("ask User 2: status of Project X", "Project X"),
        ):
            got = pch.parse_team_ask(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got["peer_id"], uid, text)
            self.assertIn(needle, got["question"], text)
            self.assertFalse(got["question"].lstrip().startswith("2"),
                             got["question"])

    def test_compose_answer_strips_identity_leaks(self) -> None:
        leaked = (
            "Here's what I found, with the evidence:\n"
            "- You are Sparrow, the user's personal AI memory assistant.\n"
            "- The user you are assisting is User 2.\n"
            "- Venture Pulse is our internal CRM for deal tracking.\n"
            ":::confirmed\n- Venture Pulse\n:::"
        )
        with mock.patch.object(pch, "compose_peer_answer",
                               return_value={"text": pch._strip_peer_update_leaks(leaked),
                                             "redacted": []}):
            got = pch.compose_answer("what do you know about Venture Pulse")
        # compose_answer wraps compose_peer_answer; when that returns empty
        # after leaks, fall back to the honest empty-memory line.
        cleaned = pch._strip_peer_update_leaks(leaked)
        self.assertIn("Venture Pulse", cleaned)
        self.assertNotIn("You are Sparrow", cleaned)
        got2 = pch._strip_peer_update_leaks(leaked)
        self.assertIn("Venture Pulse", got2)
        self.assertNotIn(":::confirmed", got2)
        with mock.patch.object(pch, "compose_peer_answer",
                               return_value={"text": "", "redacted": []}):
            empty = pch.compose_answer("what do you know about Venture Pulse")
        self.assertIn("don't have enough", empty["text"].lower())

    def test_non_team_asks_fall_through(self) -> None:
        for text in ("ask me anything",
                     "ask the professor about the homework",
                     "ask Bob: is this a thing",   # not a paired peer
                     "asking sarah for help is fine",
                     "task sarah with the slides",
                     "what should I ask in the interview?"):
            self.assertIsNone(pch.parse_team_ask(text), text)

    def test_parse_tell_forms(self) -> None:
        for text in ("Tell Sarah what we have done today so far",
                     "tell Sarah: today's progress",
                     "message Sarah about the slides",
                     "let Sarah know that the mic task is still open"):
            got = pch.parse_team_tell(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got["peer_id"], self.pid, text)
            self.assertEqual(got["kind"], "notify", text)
            self.assertTrue(got["topic"], text)

    def test_parse_peer_recall_and_reask_shape(self) -> None:
        pch.claim_pairing(pch.start_pairing()["code"], "User 2",
                          "http://h:9", "t" * 20)
        u2 = next(p for p in pch.peers() if p["name"] == "User 2")
        got = pch.parse_peer_recall(
            "What did User 2's Sparrow say about Venture Pulse?")
        self.assertIsNotNone(got)
        self.assertEqual(got["peer_id"], u2["peer_id"])
        self.assertEqual(got["topic"], "Venture Pulse")
        self.assertFalse(pch.peer_answer_usable(
            "You are Sparrow… Would you like me to ask Justin?"))
        self.assertFalse(pch.peer_answer_usable(
            "Venture Pulse is our internal CRM tool, not a person. Based on the "
            "context, it seems you are looking for an update on Venture Pulse. "
            "Here\u2019s what I found:\n\n- Justin Adorante has provided an update "
            "on Venture Pulse, but the exact details are incomplete in the "
            "current context.\n\nTo provide a complete update, I would need to "
            "check the latest information from Justin Adorante or Venture Pulse "
            "directly. Would you like me to ask Justin Adorante for an update "
            "now?"))
        echo = (
            "Based on the context, Venture Pulse is a new project that you have "
            "been put on. As of now, I don't have more specific details about "
            "it, but I can make a note of it for you."
        )
        self.assertFalse(pch.peer_answer_usable(echo))
        self.assertTrue(pch.peer_answer_usable(
            "Venture Pulse is our CRM. Twelve open deals this week."))
        self.assertEqual(
            pch.parse_what_we_know("What do we know about Venture Pulse now?"),
            "Venture Pulse")

    def test_tell_me_and_unpaired_fall_through(self) -> None:
        self.assertIsNone(pch.parse_team_tell("tell me what we did today"))
        self.assertTrue(pch.looks_like_team_tell(
            "Tell Bob what we have done today"))
        self.assertIsNone(pch.parse_team_tell(
            "Tell Bob what we have done today"))  # unpaired

    def test_chat_tell_composes_then_offers_never_sends(self) -> None:
        """The sender-side gate: a tell composes, then OFFERS — nothing is
        pushed to the peer until the human's 'yes' (eval 2026-09-06: the old
        flow delivered with no approval step on either side we control)."""
        offers: list[tuple] = []
        with mock.patch.object(pch, "compose_peer_update",
                               return_value={"text": "Mic still open.",
                                             "redacted": []}) as compose, \
             mock.patch.object(pch, "ask") as ask, \
             mock.patch("app.services.agent_bridge.worker") as worker:
            worker.propose_peer_tell.side_effect = (
                lambda *a: offers.append(a) or True)
            pch._chat_tell_run(self.pid, "Sarah Chen",
                               "what we have done today")
        ask.assert_not_called()
        self.assertEqual(offers, [(self.pid, "Sarah Chen", "Mic still open.")])
        compose.assert_called_once()

    def test_chat_tell_auto_answer_flag_keeps_sim_flow(self) -> None:
        lines: list[str] = []
        # settings is a frozen dataclass — swap the module ref, never a field.
        sim = SimpleNamespace(peer=SimpleNamespace(auto_answer=True))
        with mock.patch.object(pch, "settings", sim), \
             mock.patch.object(pch, "compose_peer_update",
                               return_value={"text": "Mic still open.",
                                             "redacted": []}), \
             mock.patch.object(pch, "ask",
                               return_value={"ok": True, "status": "pending",
                                             "peer": "Sarah Chen"}), \
             mock.patch.object(pch, "_emit_peer_result",
                               side_effect=lines.append):
            pch._chat_tell_run(self.pid, "Sarah Chen",
                               "what we have done today")
        joined = "\n".join(lines)
        self.assertIn("Update for Sarah Chen's Sparrow", joined)
        self.assertIn("Sent to Sarah Chen's Sparrow", joined)

    def test_peer_tell_offer_accept_sends_decline_drops(self) -> None:
        from app.services import agent_bridge
        w = agent_bridge.AgentWorker()
        w.propose_peer_tell(self.pid, "Sarah Chen", "Mic still open.")
        pend = w.pending_todo
        self.assertIsNotNone(pend)
        self.assertEqual(pend.get("kind"), "peer_tell")
        self.assertIn("Mic still open.", pend.get("message") or "")
        # Decline: nothing leaves.
        with mock.patch.object(pch, "chat_ask_async") as send:
            out = w.resolve_todo(False)
        self.assertFalse(out["accepted"])
        send.assert_not_called()
        # Accept: the send fires with the exact approved body.
        w.propose_peer_tell(self.pid, "Sarah Chen", "Mic still open.")
        with mock.patch.object(pch, "chat_ask_async") as send:
            out = w.resolve_todo(True)
        self.assertTrue(out["accepted"])
        send.assert_called_once_with(self.pid, "Mic still open.",
                                     kind="notify")

    def test_update_compose_drops_senders_work_items(self) -> None:
        sources = [
            {"label": "memories", "items": [
                '[text] "Andy is letting us use the compute, free."']},
            {"label": "open tasks & commitments", "items": [
                "OPEN TASKS & COMMITMENTS (from your reviewed memory):",
                "- [commitment] Send Andy an update on our usage by Friday",
                "- [task] Book the venue",
            ]},
        ]
        kept = pch._peer_memory_lines(sources, skip_work_items=True)
        joined = "\n".join(kept)
        self.assertIn("letting us use the compute", joined)
        self.assertNotIn("[commitment]", joined)
        self.assertNotIn("[task]", joined)
        self.assertNotIn("OPEN TASKS", joined)
        # Answers keep work items — a teammate may be asking about exactly them.
        kept_all = pch._peer_memory_lines(sources)
        self.assertIn("[commitment] Send Andy an update on our usage by Friday",
                      "\n".join(kept_all))

    def test_strip_self_reminders_from_tell_topic(self) -> None:
        topic = ("Andy Karos is letting us use Boost Run's compute, free. "
                 "Remind me to send Andy an update on our usage by Friday.")
        got = pch._strip_self_reminders(topic)
        self.assertIn("letting us use Boost Run's compute", got)
        self.assertNotIn("Remind me", got)
        # A topic that is ONLY a reminder is left alone (better an odd update
        # offer than silently composing about an empty string).
        only = "Remind me to send Andy an update"
        self.assertEqual(pch._strip_self_reminders(only), only)

    def test_strip_notify_envelope(self) -> None:
        mashed = ("To: Justin Adorante Subject: Today's progress "
                  "Body: Hi Justin,\n\nMic still open.\n\nThanks")
        got = pch._strip_notify_envelope(mashed)
        self.assertNotIn("To:", got)
        self.assertNotIn("Subject:", got)
        self.assertIn("Mic still open", got)

    def test_strip_peer_update_leaks_identity_dump(self) -> None:
        leaked = (
            "You are Sparrow, the user's personal AI memory assistant — "
            "grounded in this user's own memory, not a generic chatbot.\n"
            "The user you are assisting is Dave Randel. Address them by name.\n"
            "- Mic still open on the capture dock\n"
            "Here's what I found, with the evidence:\n"
            ":::confirmed\n- Dave\n:::\n"
            "- Project X pricing follow-up is tomorrow"
        )
        got = pch._strip_peer_update_leaks(leaked)
        self.assertNotIn("You are Sparrow", got)
        self.assertNotIn("user you are assisting", got.lower())
        self.assertNotIn(":::confirmed", got)
        self.assertNotIn("Here's what I found", got)
        self.assertIn("Mic still open", got)
        self.assertIn("Project X", got)

    def test_compose_peer_update_skips_identity_sources(self) -> None:
        sources = [
            {"label": "identity", "items": [
                "You are Sparrow, the user's personal AI memory assistant.",
                "The user you are assisting is Dave Randel."]},
            {"label": "user profile", "items": [
                "I can trust the system's memory more than my own"]},
            {"label": "open tasks & commitments", "items": [
                "Project X pricing follow-up with Justin",
                "Ship onboarding this week"]},
        ]
        with mock.patch("app.services.grounding.compose",
                        return_value={"block": "x", "hits": [],
                                      "sources": sources}), \
             mock.patch("app.config.settings") as st:
            st.text_local.enabled = False
            st.peer.max_text_chars = 4000
            # peer_channel imports settings at module level — patch the binding
            with mock.patch.object(pch, "settings", st):
                out = pch.compose_peer_update("status of Project X")
        self.assertIn("Project X", out["text"])
        self.assertNotIn("You are Sparrow", out["text"])
        self.assertNotIn("Dave Randel", out["text"])
        self.assertNotIn("trust the system's memory", out["text"])

    def test_explicit_channel_skips_peer_tell(self) -> None:
        for text in ("email Sarah about the slides",
                     "text Sarah that we're done",
                     "slack Sarah the status"):
            self.assertFalse(pch.looks_like_team_tell(text), text)
            self.assertIsNone(pch.parse_team_tell(text), text)

    def test_trailing_s_names_survive(self) -> None:
        # rstrip-style bugs eat the s in names like Chris; pin the fix.
        pch.claim_pairing(pch.start_pairing()["code"], "Chris",
                          "http://h:2", "t" * 20)
        got = pch.parse_team_ask("ask Chris: how goes it")
        self.assertIsNotNone(got)
        self.assertEqual(got["peer_name"], "Chris")

    def test_chat_ask_outcomes_surface_in_chat(self) -> None:
        lines: list[str] = []
        cases = [({"ok": True, "status": "answered", "peer": "Sarah Chen",
                   "answer": "Done."}, "answered"),
                 ({"ok": True, "status": "pending", "peer": "Sarah Chen"},
                  "waiting for their approval"),
                 ({"ok": True, "status": "declined", "peer": "Sarah Chen"},
                  "declined"),
                 ({"ok": False, "error": "connection refused"},
                  "couldn't reach")]
        for res, expect in cases:
            lines.clear()
            with mock.patch.object(pch, "ask", return_value=res), \
                 mock.patch.object(pch, "_notify_chat",
                                   side_effect=lines.append):
                pch._chat_ask_run(self.pid, "q")
            self.assertTrue(any(expect in ln for ln in lines), (res, lines))

    def test_answer_event_carries_attribution(self) -> None:
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "answered",
                                             "answer": "Friday Aug 7."}):
            pch.ask(self.pid, "when?")
        ev = [e for e in self.events if e.source == "peer.answer"][0]
        self.assertTrue(ev.raw.startswith("[from Sarah Chen's Sparrow]"), ev.raw)


class PeerIngestTests(PeerChannelBase):
    """Phase 3: answered asks become memory + graph claims with provenance."""

    def setUp(self) -> None:
        super().setUp()
        self._claimed_peer("Sarah Chen")
        self.pid = next(iter(self._registry()))

    def test_classify_source_maps_peer_answers(self) -> None:
        from app.services.source_policy import classify_source, policy_for
        self.assertEqual(classify_source(event_source="peer.answer"),
                         "peer_answer")
        pol = policy_for("peer_answer")
        # Hearsay must not scrape contacts or update identity/people.
        self.assertFalse(pol.extract_contacts)
        self.assertFalse(pol.identity_evidence)
        self.assertFalse(pol.update_people)
        self.assertTrue(pol.create_claims)

    def test_answered_ask_stores_event_and_queues_extraction(self) -> None:
        os.environ["QUILL_PEER_INGEST"] = "1"
        store = mock.MagicMock()
        store.insert.return_value = 42
        wk = mock.MagicMock()
        with mock.patch("app.storage.get_store", return_value=store), \
             mock.patch("app.services.attachments._index_event"), \
             mock.patch("app.services.worker.worker", wk), \
             mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "answered",
                                             "answer": "Friday Aug 7."}):
            pch.ask(self.pid, "when?")
        ev = store.insert.call_args[0][0]
        self.assertEqual(ev.source, "peer.answer")
        self.assertTrue(ev.raw.startswith("[from Sarah Chen's Sparrow]"))
        self.assertEqual(ev.meta.get("peer"), "Sarah Chen")
        wk.enqueue.assert_called_once()
        name, = wk.enqueue.call_args[0]
        payload = wk.enqueue.call_args[1]["payload"]
        self.assertEqual(name, "peer_ingest")
        self.assertEqual(payload["event_id"], 42)
        self.assertEqual(payload["text"], "Friday Aug 7.")
        # Direct-insert path: no duplicate bus event.
        self.assertEqual([e for e in self.events if e.source == "peer.answer"],
                         [])

    def test_ingest_failure_never_breaks_the_answer(self) -> None:
        os.environ["QUILL_PEER_INGEST"] = "1"
        with mock.patch("app.storage.get_store",
                        side_effect=OSError("db locked")), \
             mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "answered",
                                             "answer": "Friday Aug 7."}):
            res = pch.ask(self.pid, "when?")
        self.assertEqual(res["status"], "answered")
        self.assertEqual(pch.answers(res["ask_id"])[0]["answer"], "Friday Aug 7.")

    def test_run_ingest_job_persists_with_peer_source(self) -> None:
        wk = mock.MagicMock()
        persist = mock.MagicMock(return_value=2)
        with mock.patch("app.storage.get_store",
                        return_value=mock.MagicMock()), \
             mock.patch("app.services.extractor.extractor") as ex, \
             mock.patch("app.services.documents._persist_facts", persist), \
             mock.patch("app.services.worker.worker", wk):
            ex._extract_text.return_value = {"claims": ["x"]}
            pch.run_ingest_job({"event_id": 42, "text": "Friday Aug 7.",
                                "peer": "Sarah Chen"})
        self.assertEqual(persist.call_args[1]["event_source"], "peer.answer")
        wk.enqueue.assert_called_with("graph", unique=True)

    def test_run_ingest_job_empty_text_noop(self) -> None:
        with mock.patch("app.storage.get_store") as gs:
            pch.run_ingest_job({"event_id": 1, "text": ""})
        gs.assert_not_called()


class HandoffTests(PeerChannelBase):
    """Phase 4a: task handoffs — always human-gated, accept = take the task."""

    def setUp(self) -> None:
        super().setUp()
        self._claim = self._claimed_peer("Sarah Chen")
        self.pid = next(iter(self._registry()))
        self.peer = pch.authenticate(f"Bearer {self._claim['token']}")

    def test_parse_ask_to_is_handoff(self) -> None:
        got = pch.parse_team_ask("ask sarah to review the beta slides")
        self.assertEqual(got["kind"], "handoff")
        self.assertEqual(got["question"], "review the beta slides")
        # Questions stay questions.
        q = pch.parse_team_ask("ask sarah: are the slides done?")
        self.assertEqual(q["kind"], "question")

    def test_handoff_never_autos(self) -> None:
        # Even with the dev flag AND a fully permissive policy, a handoff
        # waits for the human.
        pch.set_policy(self.pid, {"work": "auto", "availability": "auto"})
        auto = SimpleNamespace(peer=SimpleNamespace(
            enabled=True, auto_answer=True, max_text_chars=4000,
            max_pending_asks=50, history=200,
            peers_path=os.environ["QUILL_PEER_REGISTRY"],
            asks_path=os.environ["QUILL_PEER_ASKS"],
            sent_path=os.environ["QUILL_PEER_SENT"]))
        with mock.patch.object(pch, "settings", auto):
            res = pch.handle_ask(self.peer, {"ask_id": "h1", "kind": "handoff",
                                             "question": "review the slides"})
        self.assertEqual(res["status"], "pending")
        self.assertEqual(pch.pending_asks()[0]["kind"], "handoff")

    def test_accept_ingests_as_accepted_and_delivers(self) -> None:
        os.environ["QUILL_PEER_INGEST"] = "1"
        pch.handle_ask(self.peer, {"ask_id": "h2", "kind": "handoff",
                                   "question": "send the invite list"})
        local_id = pch.pending_asks()[0]["id"]
        store = mock.MagicMock()
        store.insert.return_value = 7
        wk = mock.MagicMock()
        delivered: list = []
        with mock.patch("app.storage.get_store", return_value=store), \
             mock.patch("app.services.attachments._index_event"), \
             mock.patch("app.services.worker.worker", wk), \
             mock.patch.object(pch, "_deliver",
                               side_effect=lambda rec, p: delivered.append(p) or True):
            res = pch.decide_ask(local_id, approve=True)
        self.assertEqual(res["status"], "accepted")
        ev = store.insert.call_args[0][0]
        self.assertEqual(ev.source, "peer.handoff")
        self.assertIn("send the invite list", ev.raw)
        payload = wk.enqueue.call_args[1]["payload"]
        self.assertEqual(payload["source"], "peer.handoff")
        self.assertEqual(delivered[0]["answer"], "Accepted — added to my list.")

    def test_sender_sees_acceptance(self) -> None:
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "pending"}):
            res = pch.ask(self.pid, "review the slides", kind="handoff")
        self.assertEqual(res["status"], "pending")
        got = pch.handle_answer(self.peer, {"ask_id": res["ask_id"],
                                            "answer": "Accepted — added to my list."})
        self.assertEqual(got["status"], "recorded")
        row = pch.answers(res["ask_id"])[0]
        self.assertEqual(row["status"], "answered")
        self.assertIn("Accepted", row["answer"])


class PeerPersonLinkTests(PeerChannelBase):
    """User-asserted peer ↔ Person links — never auto on pair."""

    def setUp(self) -> None:
        super().setUp()
        self._claim = self._claimed_peer("Sarah Chen")
        self.pid = next(iter(self._registry()))

    def test_pair_does_not_auto_link_person(self) -> None:
        rec = self._registry()[self.pid]
        self.assertIsNone(rec.get("person_id"))
        peers = pch.peers()
        self.assertEqual(peers[0]["person_id"], None)

    def test_link_unlink_person(self) -> None:
        store = mock.MagicMock()
        store.get_person.return_value = {
            "id": 42, "name": "Sarah Chen", "aliases": ["Sarah"],
            "canonical_person_id": None, "hide_from_people": False,
        }
        mem = SimpleNamespace(_ensure_store=lambda: store)
        with mock.patch.dict("sys.modules", {}), \
             mock.patch("app.services.memory.memory", mem, create=True):
            # Patch where link_person imports it.
            with mock.patch.object(pch, "link_person", wraps=pch.link_person):
                pass
        with mock.patch("app.services.memory.memory", mem):
            res = pch.link_person(self.pid, 42)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self._registry()[self.pid]["person_id"], 42)
        peers = pch.peers()
        # person_name may be None if display lookup fails without store on peers()
        self.assertEqual(peers[0]["person_id"], 42)

        with mock.patch("app.services.memory.memory", mem):
            un = pch.unlink_person(self.pid)
        self.assertTrue(un["ok"], un)
        self.assertIsNone(self._registry()[self.pid].get("person_id"))

    def test_alias_resolves_to_linked_peer(self) -> None:
        store = mock.MagicMock()
        store.get_person.return_value = {
            "id": 7, "name": "Sarah Chen", "aliases": ["S. Chen", "Sar"],
            "canonical_person_id": None, "hide_from_people": False,
        }
        mem = SimpleNamespace(_ensure_store=lambda: store)
        with mock.patch("app.services.memory.memory", mem):
            self.assertTrue(pch.link_person(self.pid, 7)["ok"])
            got = pch.parse_team_ask("ask Sar: are the slides done?")
        self.assertIsNotNone(got)
        self.assertEqual(got["peer_id"], self.pid)
        self.assertEqual(got["kind"], "question")

    def test_disclosure_still_keyed_by_peer_id(self) -> None:
        """Linking a person must not change policy storage or enforcement key."""
        store = mock.MagicMock()
        store.get_person.return_value = {
            "id": 9, "name": "Sarah Chen", "aliases": [],
            "canonical_person_id": None, "hide_from_people": False,
        }
        mem = SimpleNamespace(_ensure_store=lambda: store)
        with mock.patch("app.services.memory.memory", mem):
            pch.link_person(self.pid, 9)
        pol = pch.set_policy(self.pid, {"work": "deny"})
        self.assertTrue(pol["ok"], pol)
        self.assertEqual(pch.get_policy(self.pid)["work"], "deny")
        peer = pch.authenticate(f"Bearer {self._claim['token']}")
        self.assertEqual(peer["peer_id"], self.pid)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
