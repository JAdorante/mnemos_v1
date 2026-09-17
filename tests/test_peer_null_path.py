"""Connector capture & task fulfillment spec — Feature 4: peer asks with a
real null path.

null_result reasons are distinct and serialize round-trip; a miss and a
denial no longer look the same to the asker; the receiving user gets the
four options (deferred in meeting mode); hop is capped; an inbound
peer.update or slot_resolved must match an ask WE sent; a slot_request is
auto-accepted only under team policy.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import peer_channel as pch  # noqa: E402
from app.services import slots  # noqa: E402
from app.services import team_layer as tl  # noqa: E402
from tests.test_peer_channel import PeerChannelBase  # noqa: E402


class NullResultShapeTests(unittest.TestCase):
    def test_reasons_are_distinct_and_round_trip(self):
        seen = set()
        for reason in pch.NULL_REASONS:
            nr = pch.null_result(reason, slot_offered=(reason == "no_memory"))
            wire = json.loads(json.dumps(nr))
            self.assertEqual(pch._sanitize_null(wire), nr)
            seen.add(wire["reason"])
        self.assertEqual(seen, {"no_memory", "policy_denied", "offline"})
        with self.assertRaises(ValueError):
            pch.null_result("nope")
        # Junk from an older / hostile peer degrades to a plain miss.
        self.assertEqual(pch._sanitize_null({"reason": "weird"})["reason"], "no_memory")
        self.assertEqual(pch._sanitize_null(None)["reason"], "no_memory")

    def test_chat_wording_distinguishes_miss_denial_offline(self):
        miss = pch._format_null_chat("Sarah", pch.null_result("no_memory", slot_offered=True))
        deny = pch._format_null_chat("Sarah", pch.null_result("policy_denied"))
        off = pch._format_null_chat("Sarah", pch.null_result("offline"))
        self.assertIn("nothing in memory", miss)
        self.assertIn("slot", miss)
        self.assertIn("disclosure policy", deny)
        self.assertIn("queued", off)
        self.assertEqual(len({miss, deny, off}), 3)


class _StoreMixin:
    """A temp process-wide store for the test, with every singleton that
    caches a store pinned and restored — a temp store must never outlive the
    test inside `memory` or the job worker."""

    def _store_up(self):
        from app import storage
        from app.storage import Store
        from app.services.memory import memory
        from app.services.worker import worker as job_worker
        self._sdir = tempfile.mkdtemp(prefix="nullstore_")
        self.store = Store(Path(self._sdir) / "t.db")
        self._prev_store = storage._store
        self._prev_singletons = (memory._store, job_worker._store)
        self._prev_sim = os.environ.get("QUILL_SLOT_SIM")
        os.environ["QUILL_SLOT_SIM"] = "overlap"
        storage._store = self.store

    def _store_down(self):
        from app import storage
        from app.services.memory import memory
        from app.services.worker import worker as job_worker
        storage._store = self._prev_store
        memory._store, job_worker._store = self._prev_singletons
        if self._prev_sim is None:
            os.environ.pop("QUILL_SLOT_SIM", None)
        else:
            os.environ["QUILL_SLOT_SIM"] = self._prev_sim
        self.store.close()


class ReceiverNullPathTests(PeerChannelBase, _StoreMixin):
    def setUp(self):
        super().setUp()
        self._store_up()
        self.offers = []
        self._pw = mock.patch("app.services.agent_bridge.worker")
        self.worker = self._pw.start()
        self.worker.propose_peer_null_options.side_effect = \
            lambda cand: self.offers.append(cand) or True
        self.worker.propose_slot_create.side_effect = \
            lambda cand: self.offers.append(cand) or True
        self._pc = mock.patch.object(pch, "classify_question", return_value="work")
        self._pc.start()
        self._pcomp = mock.patch.object(
            pch, "compose_answer",
            return_value={"text": "I don't have anything in my memory on that.",
                          "claims": [], "as_of": None, "near_miss": False,
                          "redacted": []})
        self._pcomp.start()

    def tearDown(self):
        self._pcomp.stop(); self._pc.stop(); self._pw.stop()
        self._store_down()
        super().tearDown()

    def _auto_peer(self) -> tuple[str, dict]:
        self._claimed_peer("User 1")
        reg = self._registry()
        pid = next(iter(reg))
        pch.set_policy(pid, {"availability": "auto", "work": "auto",
                             "contact": "offer", "personal": "offer",
                             "other": "offer"})
        reg = self._registry()
        return pid, {"peer_id": pid, **reg[pid]}

    def test_auto_miss_returns_typed_null_and_offers_four_options(self):
        pid, peer = self._auto_peer()
        res = pch.handle_ask(peer, {"ask_id": "a1", "hop": 0, "origin_id": "a1",
                                    "question": "What's the status of the Boston quote?"})
        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["response_kind"], "null_result")
        self.assertEqual(res["null_result"], {"reason": "no_memory",
                                              "slot_offered": True})
        self.assertEqual(res["claims"], [])
        self.assertEqual(len(self.offers), 1)
        cand = self.offers[0]
        self.assertEqual(cand["options"], ["search", "source", "team", "slot"])
        self.assertEqual(cand["need"], "Boston quote")
        self.assertEqual(cand["peer_id"], pid)

    def test_forwarded_ask_cannot_fan_out_again_and_hop_is_capped(self):
        pid, peer = self._auto_peer()
        res = pch.handle_ask(peer, {"ask_id": "a2", "hop": 1, "origin_id": "a0",
                                    "question": "Boston quote?"})
        self.assertEqual(res["response_kind"], "null_result")
        self.assertNotIn("team", self.offers[-1]["options"])
        res = pch.handle_ask(peer, {"ask_id": "a3", "hop": 2, "origin_id": "a0",
                                    "question": "Boston quote?"})
        self.assertFalse(res["ok"])
        self.assertIn("hop", res["error"])

    def test_policy_deny_is_typed_policy_denied(self):
        pid, peer = self._auto_peer()
        pch.set_policy(pid, {"availability": "offer", "work": "deny",
                             "contact": "offer", "personal": "offer",
                             "other": "offer"})
        reg = self._registry()
        res = pch.handle_ask({"peer_id": pid, **reg[pid]},
                             {"ask_id": "a4", "question": "Boston quote?"})
        self.assertEqual(res["status"], "declined")
        self.assertEqual(res["null_result"]["reason"], "policy_denied")
        self.assertEqual(self.offers, [])   # a denial never offers options

    def test_meeting_mode_defers_the_offer_until_it_ends(self):
        pid, peer = self._auto_peer()
        with mock.patch("app.services.meeting_mode.status",
                        return_value={"active": True}):
            res = pch.handle_ask(peer, {"ask_id": "a5",
                                        "question": "Boston quote?"})
        self.assertEqual(res["null_result"]["slot_offered"], True)
        self.assertEqual(self.offers, [])
        self.assertEqual(len(pch.deferred_null_offers()), 1)
        with mock.patch("app.services.meeting_mode.status",
                        return_value={"active": True}):
            self.assertEqual(pch.flush_deferred_null_offers(), 0)
        with mock.patch("app.services.meeting_mode.status",
                        return_value={"active": False}):
            self.assertEqual(pch.flush_deferred_null_offers(), 1)
        self.assertEqual(len(self.offers), 1)
        self.assertEqual(pch.deferred_null_offers(), [])

    def test_slot_option_creates_slot_for_the_peer(self):
        pid, peer = self._auto_peer()
        pch.handle_ask(peer, {"ask_id": "a6", "question": "Boston quote?"})
        cand = self.offers[0]
        out = pch.resolve_null_option(cand, "slot")
        self.assertTrue(out["ok"])
        row = self.store.get_task(out["slot_id"])
        self.assertEqual(row["status"], "awaiting_data")
        self.assertEqual(row["slot"]["requester"], {"kind": "peer", "id": pid,
                                                    "name": "User 1",
                                                    "ask_id": "a6"})
        self.assertEqual(row["slot"]["origin_id"], "a6")
        # A "none" tap leaves nothing watching.
        self.assertTrue(pch.resolve_null_option(cand, None)["ok"])
        self.assertEqual(len(slots.open_slots(self.store)), 1)

    def test_search_option_reoffers_team_and_slot_on_miss(self):
        pid, peer = self._auto_peer()
        pch.handle_ask(peer, {"ask_id": "a7", "question": "Boston quote?"})
        cand = self.offers[0]
        with mock.patch("app.services.memory.memory.search", return_value=[]):
            out = pch.resolve_null_option(cand, "search")
        self.assertEqual(out["hits"], 0)
        self.assertEqual(out["reoffered"], ["team", "slot"])
        self.assertEqual(self.offers[-1]["options"], ["team", "slot"])

    def test_slot_request_offers_unless_team_policy_auto_accepts(self):
        pid, peer = self._auto_peer()
        res = pch.handle_ask(peer, {"ask_id": "s1", "kind": "slot_request",
                                    "origin_id": "o1",
                                    "question": "Boston deal quote"})
        self.assertEqual(res["status"], "pending")
        self.assertEqual(res["response_kind"], "slot_offered")
        self.assertEqual(self.offers[-1]["need"], "Boston deal quote")
        self.assertEqual(slots.open_slots(self.store), [])
        tl.upsert_team("Sales", [pid])
        tl.set_team_policy("sales", {"auto_accept_slots": True})
        res = pch.handle_ask(peer, {"ask_id": "s2", "kind": "slot_request",
                                    "origin_id": "o2",
                                    "question": "Boston deal quote"})
        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["response_kind"], "slot_offered")
        row = self.store.get_task(res["slot_id"])
        self.assertEqual(row["slot"]["origin_id"], "o2")

    def test_slot_resolved_closes_only_that_peers_slots(self):
        pid, peer = self._auto_peer()
        mine = slots.create(self.store, "Boston deal quote",
                            requester={"kind": "user", "id": None})
        theirs = slots.create(self.store, "Boston deal quote",
                              requester={"kind": "peer", "id": pid, "name": "User 1",
                                         "ask_id": "a9"}, origin_id="o9")
        out = pch.handle_slot_resolved(peer, {"origin_id": "o9", "reason": "filled by User 3"})
        self.assertEqual(out["closed"], [theirs])
        self.assertEqual(self.store.get_task(theirs)["status"], "cancelled")
        self.assertEqual(self.store.get_task(mine)["status"], "awaiting_data")


class AskerNullPathTests(PeerChannelBase, _StoreMixin):
    def setUp(self):
        super().setUp()
        self._store_up()
        self.results = []
        self._pe = mock.patch.object(pch, "_emit_peer_result",
                                     side_effect=lambda t: self.results.append(t))
        self._pe.start()
        self._pen = mock.patch.object(pch, "enrich_peer_question",
                                      side_effect=lambda q: q)
        self._pen.start()

    def tearDown(self):
        self._pen.stop(); self._pe.stop()
        self._store_down()
        super().tearDown()

    def _peer_id(self) -> str:
        self._claimed_peer("User 2")
        return next(iter(self._registry()))

    def test_sync_null_is_recorded_with_reason_and_board_waits(self):
        pid = self._peer_id()
        with mock.patch.object(pch, "_post_json", return_value={
                "ok": True, "status": "answered", "answer": "nothing",
                "claims": [], "response_kind": "null_result",
                "null_result": {"reason": "no_memory", "slot_offered": True}}):
            res = pch.ask(pid, "What's the status of the Boston quote?")
        self.assertEqual(res["status"], "null")
        self.assertEqual(res["null_result"]["reason"], "no_memory")
        row = pch.answers(res["ask_id"])[0]
        self.assertEqual((row["status"], row["null_reason"], row["slot_offered"],
                          row["waiting_on"]), ("null", "no_memory", True, "User 2"))
        self.assertIn("nothing in memory", self.results[-1])
        waiting = [t for t in self.store.list_tasks(("open",))
                   if t.get("task_kind") == "peer_ask"]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["counterparty_name"], "User 2")
        self.assertEqual(waiting[0]["commitment_state"], "waiting")
        self.assertEqual(waiting[0]["slot"]["peer_ask_id"], res["ask_id"])

    def test_declined_and_offline_are_typed_too(self):
        pid = self._peer_id()
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "declined"}):
            res = pch.ask(pid, "Boston quote?")
        self.assertEqual(res["null_result"]["reason"], "policy_denied")
        self.assertEqual(pch.answers(res["ask_id"])[0]["null_reason"], "policy_denied")
        self.assertIn("disclosure policy", self.results[-1])
        with mock.patch.object(pch, "_post_json", side_effect=OSError("down")):
            res = pch.ask(pid, "Boston quote?")
        self.assertEqual(res["status"], "queued")
        self.assertEqual(pch.answers(res["ask_id"])[0]["null_reason"], "offline")

    def test_update_must_match_an_ask_we_sent_and_settles_the_board(self):
        pid = self._peer_id()
        peer = {"peer_id": pid, "name": "User 2"}
        self.assertFalse(pch.handle_update(peer, {"ask_id": "never",
                                                  "answer": "x"})["ok"])
        with mock.patch.object(pch, "_post_json", return_value={
                "ok": True, "status": "answered", "claims": [],
                "response_kind": "null_result",
                "null_result": {"reason": "no_memory", "slot_offered": True}}):
            res = pch.ask(pid, "Boston quote?")
        out = pch.handle_update(peer, {"ask_id": res["ask_id"], "need": "Boston quote",
                                       "answer": "Quote Q-1042: $42,000."})
        self.assertEqual(out["response_kind"], "fill")
        self.assertTrue(out["event_id"])
        row = pch.answers(res["ask_id"])[0]
        self.assertEqual((row["status"], row["response_kind"]), ("answered", "fill"))
        ev = self.store.get_event(out["event_id"])
        self.assertEqual(ev["source"], "peer.update")
        self.assertIn("User 2's Sparrow found", self.results[-1])
        done = [t for t in self.store.list_tasks(("done",))
                if t.get("task_kind") == "peer_ask"]
        self.assertEqual(len(done), 1)
        self.assertEqual(self.store.last_transition(done[0]["fact_id"])["evidence_id"],
                         out["event_id"])
        # Second fill within the minute: logged, not delivered again.
        again = pch.handle_update(peer, {"ask_id": res["ask_id"],
                                         "answer": "Quote Q-1042: $42,000."})
        self.assertEqual(again["status"], "already_resolved")
        # A typed policy_denied update lands as a denial, not silence.
        with mock.patch.object(pch, "_post_json", return_value={
                "ok": True, "status": "answered", "claims": [],
                "response_kind": "null_result",
                "null_result": {"reason": "no_memory", "slot_offered": True}}):
            res2 = pch.ask(pid, "Boston quote v2?")
        out2 = pch.handle_update(peer, {"ask_id": res2["ask_id"],
                                        "response_kind": "null_result",
                                        "null_result": {"reason": "policy_denied"}})
        self.assertEqual(out2["response_kind"], "null_result")
        self.assertEqual(pch.answers(res2["ask_id"])[0]["status"], "declined")


class FanoutMergeTests(PeerChannelBase, _StoreMixin):
    def setUp(self):
        super().setUp()
        self._store_up()
        self._prev_env = {k: os.environ.get(k) for k in (
            "QUILL_TEAM_FANOUT_DEADLINE_S", "QUILL_TEAM_FANOUT_SEQUENTIAL")}
        os.environ["QUILL_TEAM_FANOUT_DEADLINE_S"] = "0.2"
        os.environ["QUILL_TEAM_FANOUT_SEQUENTIAL"] = "1"
        self._pen = mock.patch.object(pch, "enrich_peer_question",
                                      side_effect=lambda q: q)
        self._pen.start()

    def tearDown(self):
        self._pen.stop()
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._store_down()
        super().tearDown()

    def test_bare_hashtag_form_parses(self):
        self._claimed_peer("User 2")
        pid = next(iter(self._registry()))
        tl.upsert_team("Team", [pid])
        g = tl.parse_group_ask("#Team what is the status of the Boston deal?")
        self.assertTrue(g and g["fanout"] and not g["unknown"])
        self.assertEqual(g["question"], "what is the status of the Boston deal?")
        self.assertEqual(g["peer_ids"], [pid])

    def test_merge_lists_nulls_by_name_with_attribution(self):
        start = pch.start_pairing()
        pch.claim_pairing(start["code"], "User 2", "http://u2.test", "t" * 20)
        start = pch.start_pairing()
        pch.claim_pairing(start["code"], "User 3", "http://u3.test", "u" * 20)
        reg = self._registry()
        ids = list(reg)
        tl.upsert_team("Team", ids)
        by_url = {"http://u2.test": {"ok": True, "status": "answered", "claims": [],
                                     "answer": "nothing", "response_kind": "null_result",
                                     "null_result": {"reason": "no_memory",
                                                     "slot_offered": True}},
                  "http://u3.test": {"ok": True, "status": "answered",
                                     "answer": "Quote Q-1042 is $42k, sent Tuesday.",
                                     "claims": [{"text": "Quote Q-1042 is $42k",
                                                 "fact_id": 1, "event_id": 1,
                                                 "ts": 1.0}]}}
        with mock.patch.object(pch, "_post_json",
                               side_effect=lambda url, payload, token=None, timeout=None:
                               by_url[url.rsplit("/peer/", 1)[0]]):
            res = tl.fanout_ask("team", "what is the status of the Boston deal?")
            merged = tl.merge_fanout(res["team_ask_id"])
        self.assertEqual(res["asked"], 2)
        self.assertEqual(merged["nulls"], ["User 2"])
        self.assertEqual(merged["answered"], ["User 3"])
        self.assertFalse(merged["all_null"])
        self.assertIn("- User 2: nothing", merged["text"])
        self.assertIn("- User 3: Quote Q-1042", merged["text"])
        # Every fan-out ask carried the loop-protection fields.
        rows = [r for r in pch.answers() if r["team_ask_id"] == res["team_ask_id"]]
        self.assertEqual({r["hop"] for r in rows}, {0})
        self.assertEqual({r["origin_id"] for r in rows}, {res["origin_id"]})


if __name__ == "__main__":
    unittest.main()
