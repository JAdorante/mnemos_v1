"""Consent latency (Phase 3) — what "pending" means once nobody is offline.

Hosted put both tenants on one box, so an ask is no longer a message waiting
in someone's inbox: the asker is sitting in real time while a human is not
looking at their screen. Three consequences, pinned here:

  * a pack can be chosen AT PAIRING, the only moment either person is thinking
    about this relationship (the settings table is not a place people visit
    during a two-week trial);
  * the asker is told when their question is stuck on a human, with the wait
    in it, instead of silence;
  * a burst of asks is one prompt, not one interruption each.

`personal` is never auto, in any pack, at any setting — asserted again here
because Phase 3 is the phase that widens defaults.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import peer_channel as pch  # noqa: E402
from app.services import team_layer  # noqa: E402
from tests.test_peer_channel import PeerChannelBase  # noqa: E402


class PilotPackTests(unittest.TestCase):
    def test_pilot_grants_match_manager_but_the_label_does_not(self) -> None:
        """Same grants, different name: calling a colleague your manager in
        the UI misdescribes what people are consenting to."""
        self.assertEqual(team_layer.POLICY_PACKS["pilot"],
                         team_layer.POLICY_PACKS["manager"])
        self.assertNotEqual(team_layer.PACK_SHORT["pilot"],
                            team_layer.PACK_SHORT["manager"])

    def test_pilot_auto_answers_work_and_availability_only(self) -> None:
        pack = team_layer.POLICY_PACKS["pilot"]
        self.assertEqual(pack["availability"], "auto")
        self.assertEqual(pack["work"], "auto")
        self.assertEqual(pack["contact"], "offer")
        self.assertEqual(pack["other"], "offer")

    def test_no_pack_may_auto_answer_personal(self) -> None:
        for name, pack in team_layer.POLICY_PACKS.items():
            self.assertNotEqual(pack["personal"], "auto", name)

    def test_every_pack_has_a_short_label_and_a_blurb(self) -> None:
        for name in team_layer.POLICY_PACKS:
            self.assertIn(name, team_layer.PACK_SHORT, name)
            self.assertIn(name, team_layer.PACK_BLURB, name)
            # Three words, because this is read mid-pairing or not at all.
            self.assertLessEqual(len(team_layer.PACK_SHORT[name].split()), 3,
                                 name)

    def test_list_packs_carries_the_short_label(self) -> None:
        rows = {p["id"]: p for p in team_layer.list_packs()}
        self.assertIn("pilot", rows)
        self.assertTrue(rows["pilot"]["short"])
        self.assertTrue(rows["pilot"]["blurb"])


class PackAtPairingTests(PeerChannelBase):
    def _claim_with(self, pack: str | None) -> dict:
        start = pch.start_pairing()
        return pch.claim_pairing(start["code"], "Sarah",
                                 "http://198.51.100.7:8000",
                                 "remote-minted-token-0123456789", pack=pack)

    def _policy(self, peer_id: str) -> dict:
        return self._registry()[peer_id].get("policy") or {}

    def test_a_pack_chosen_at_pairing_is_applied(self) -> None:
        res = self._claim_with("pilot")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["pack"], "pilot")
        self.assertEqual(self._policy(next(iter(self._registry())))["work"],
                         "auto")

    def test_no_choice_and_no_default_stays_all_offer(self) -> None:
        """The safe posture remains the default for normal deployments."""
        res = self._claim_with(None)
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["pack"])
        policy = self._policy(next(iter(self._registry())))
        self.assertTrue(all(a == "offer" for a in policy.values()) or not policy)

    def test_deployment_default_applies_when_the_human_picks_nothing(self) -> None:
        os.environ["QUILL_PEER_DEFAULT_PACK"] = "pilot"
        try:
            res = self._claim_with(None)
            self.assertEqual(res["pack"], "pilot")
        finally:
            os.environ.pop("QUILL_PEER_DEFAULT_PACK", None)

    def test_an_explicit_choice_beats_the_deployment_default(self) -> None:
        os.environ["QUILL_PEER_DEFAULT_PACK"] = "pilot"
        try:
            res = self._claim_with("vendor")
            self.assertEqual(res["pack"], "vendor")
            self.assertEqual(
                self._policy(next(iter(self._registry())))["work"], "deny")
        finally:
            os.environ.pop("QUILL_PEER_DEFAULT_PACK", None)

    def test_an_unknown_pack_pairs_anyway_at_the_safe_posture(self) -> None:
        """A bad pack name must never cost the pairing itself."""
        res = self._claim_with("not-a-pack")
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["pack"])

    def test_join_side_also_chooses_its_own_pack(self) -> None:
        """Each side decides what IT will disclose; the choice is not shared."""
        sent: list = []

        def fake_post(url, payload, token=None):
            sent.append(payload)
            return {"ok": True, "peer_id": "abc", "name": "Justin",
                    "token": "their-token-for-us"}

        with mock.patch.object(pch, "_post_json", side_effect=fake_post):
            res = pch.join("http://192.0.2.9:8000/", "123456", pack="pilot")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["pack"], "pilot")
        self.assertNotIn("pack", sent[0])


class PairingPageTests(unittest.TestCase):
    """3.2's real risk is not the backend — it is shipping a pack the pairing
    form never offers, leaving every trial pair on the default forever."""

    def setUp(self) -> None:
        from app.api.peer_page import PEER_PAGE
        self.page = PEER_PAGE
        self.flat = PEER_PAGE.replace(" ", "").replace("\n", "")

    def test_the_choice_is_offered_where_people_pair(self) -> None:
        self.assertIn('id="joinPack"', self.page)
        self.assertIn("When they ask me something", self.page)

    def test_both_join_paths_send_the_choice(self) -> None:
        """Invite paste and address+code are two buttons; missing either one
        means half of users silently get the default."""
        self.assertEqual(self.flat.count("pack:$('joinPack').value"), 2)

    def test_the_form_preselects_the_deployment_default(self) -> None:
        self.assertIn("DEFAULT_PACK=s.default_pack", self.flat)

    def test_the_selected_pack_explains_itself(self) -> None:
        self.assertIn("joinPackWhy", self.page)

    def test_status_exposes_the_default_pack(self) -> None:
        bits = team_layer.status_bits()
        self.assertIn("default_pack", bits)
        self.assertTrue(bits["default_pack"])


class PendingNudgeTests(PeerChannelBase):
    def _sent_row(self, *, age_s: float, status: str = "pending") -> None:
        path = Path(os.environ["QUILL_PEER_SENT"])
        path.write_text(json.dumps([{
            "ask_id": "a1", "peer_id": "p1", "peer_name": "Sarah",
            "question": "what's the compute situation?", "kind": "question",
            "created_at": time.time() - age_s, "status": status,
            "answer": None, "answered_at": None,
        }]), encoding="utf-8")

    def test_a_long_wait_tells_the_asker_a_human_is_the_holdup(self) -> None:
        self._sent_row(age_s=300)
        os.environ["QUILL_PEER_PENDING_NUDGE_S"] = "120"
        try:
            with mock.patch.object(pch, "_emit_peer_result") as emit:
                nudged = pch.nudge_stale_pending()
        finally:
            os.environ.pop("QUILL_PEER_PENDING_NUDGE_S", None)
        self.assertEqual(len(nudged), 1)
        msg = emit.call_args.args[0]
        self.assertIn("hasn't responded", msg)
        self.assertIn("Sarah", msg)
        self.assertIn("waiting", msg)

    def test_a_fresh_ask_is_left_alone(self) -> None:
        self._sent_row(age_s=5)
        os.environ["QUILL_PEER_PENDING_NUDGE_S"] = "120"
        try:
            with mock.patch.object(pch, "_emit_peer_result") as emit:
                self.assertEqual(pch.nudge_stale_pending(), [])
        finally:
            os.environ.pop("QUILL_PEER_PENDING_NUDGE_S", None)
        emit.assert_not_called()

    def test_the_asker_is_told_once_not_every_tick(self) -> None:
        self._sent_row(age_s=300)
        os.environ["QUILL_PEER_PENDING_NUDGE_S"] = "120"
        try:
            with mock.patch.object(pch, "_emit_peer_result"):
                self.assertEqual(len(pch.nudge_stale_pending()), 1)
                self.assertEqual(pch.nudge_stale_pending(), [])
                self.assertEqual(pch.nudge_stale_pending(), [])
        finally:
            os.environ.pop("QUILL_PEER_PENDING_NUDGE_S", None)

    def test_answered_asks_are_never_nudged(self) -> None:
        self._sent_row(age_s=300, status="answered")
        os.environ["QUILL_PEER_PENDING_NUDGE_S"] = "120"
        try:
            self.assertEqual(pch.nudge_stale_pending(), [])
        finally:
            os.environ.pop("QUILL_PEER_PENDING_NUDGE_S", None)

    def test_zero_disables_it(self) -> None:
        self._sent_row(age_s=9999)
        os.environ["QUILL_PEER_PENDING_NUDGE_S"] = "0"
        try:
            self.assertEqual(pch.nudge_stale_pending(), [])
        finally:
            os.environ.pop("QUILL_PEER_PENDING_NUDGE_S", None)


class BatchedDisclosureTests(PeerChannelBase):
    def _peer(self) -> dict:
        claim = self._claimed_peer()
        return pch.authenticate(f"Bearer {claim['token']}")

    def test_one_ask_reads_as_one_ask(self) -> None:
        peer = self._peer()
        with mock.patch.object(pch, "_notify_chat") as notify:
            pch.handle_ask(peer, {"ask_id": "a1", "question": "deadline?"})
        msg = notify.call_args.args[0]
        self.assertIn("deadline?", msg)
        self.assertNotIn("things from", msg)

    def test_a_burst_becomes_one_prompt_listing_everything(self) -> None:
        """One interruption per ask is how a disclosure queue trains someone
        to ignore it."""
        peer = self._peer()
        with mock.patch.object(pch, "_notify_chat") as notify:
            pch.handle_ask(peer, {"ask_id": "a1", "question": "deadline?"})
            pch.handle_ask(peer, {"ask_id": "a2", "question": "budget?"})
            pch.handle_ask(peer, {"ask_id": "a3", "question": "who owns it?"})
        last = notify.call_args.args[0]
        self.assertIn("3 things from Sarah", last)
        for q in ("deadline?", "budget?", "who owns it?"):
            self.assertIn(q, last)

    def test_the_latest_prompt_is_a_complete_picture(self) -> None:
        """Each announcement re-states the whole queue, so the human acts once
        instead of reassembling it from scrollback."""
        peer = self._peer()
        with mock.patch.object(pch, "_notify_chat") as notify:
            pch.handle_ask(peer, {"ask_id": "a1", "question": "deadline?"})
            pch.handle_ask(peer, {"ask_id": "a2", "question": "budget?"})
        self.assertIn("deadline?", notify.call_args.args[0])

    def test_a_decided_ask_drops_out_of_the_batch(self) -> None:
        peer = self._peer()
        pch.handle_ask(peer, {"ask_id": "a1", "question": "deadline?"})
        local_id = pch.pending_asks()[0]["id"]
        with mock.patch.object(pch, "_deliver", return_value=True), \
             mock.patch.object(pch, "compose_answer",
                               return_value={"text": "Friday.", "claims": [],
                                             "as_of": None, "near_miss": False,
                                             "redacted": []}):
            pch.decide_ask(local_id, True)
        with mock.patch.object(pch, "_notify_chat") as notify:
            pch.handle_ask(peer, {"ask_id": "a2", "question": "budget?"})
        msg = notify.call_args.args[0]
        self.assertIn("budget?", msg)
        self.assertNotIn("deadline?", msg)

    def test_handoffs_keep_their_own_wording_inside_a_batch(self) -> None:
        peer = self._peer()
        with mock.patch.object(pch, "_notify_chat") as notify:
            pch.handle_ask(peer, {"ask_id": "a1", "question": "deadline?"})
            pch.handle_ask(peer, {"ask_id": "a2", "kind": "handoff",
                                  "question": "write the release notes"})
        msg = notify.call_args.args[0]
        self.assertIn("hand you a task", msg)
        self.assertIn("asks", msg)


if __name__ == "__main__":
    unittest.main()
