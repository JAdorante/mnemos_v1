"""Unit tests for the team layer on top of the peer channel.

Covers policy packs, named teams, group ask parse/fan-out, offline mailbox,
TLS URL guard, shared loop IDs, and meeting pairing offers. Hermetic — no
network; peer HTTP is mocked.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import peer_channel as pch  # noqa: E402
from app.services import team_layer as tl  # noqa: E402


class TeamLayerBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="team_")
        os.environ["QUILL_PEER_REGISTRY"] = str(Path(self._tmp) / "peers.json")
        os.environ["QUILL_PEER_ASKS"] = str(Path(self._tmp) / "asks.json")
        os.environ["QUILL_PEER_SENT"] = str(Path(self._tmp) / "sent.json")
        os.environ["QUILL_PEER_MAILBOX"] = str(Path(self._tmp) / "mailbox.json")
        os.environ["QUILL_PEER_TEAMS"] = str(Path(self._tmp) / "teams.json")
        os.environ["QUILL_PEER_LOOPS"] = str(Path(self._tmp) / "loops.json")
        os.environ["QUILL_PEER_INGEST"] = "0"
        pch._pairing = None

    def tearDown(self) -> None:
        for key in ("QUILL_PEER_REGISTRY", "QUILL_PEER_ASKS", "QUILL_PEER_SENT",
                    "QUILL_PEER_MAILBOX", "QUILL_PEER_TEAMS", "QUILL_PEER_LOOPS",
                    "QUILL_PEER_INGEST"):
            os.environ.pop(key, None)
        pch._pairing = None

    def _pair(self, name: str = "Sarah Chen") -> str:
        start = pch.start_pairing()
        pch.claim_pairing(start["code"], name, "http://127.0.0.1:9", "t" * 20)
        return next(iter(json.loads(
            Path(os.environ["QUILL_PEER_REGISTRY"]).read_text(encoding="utf-8"))))


class TlsGuardTests(unittest.TestCase):
    def test_localhost_http_ok(self) -> None:
        t = tl.url_transport("http://127.0.0.1:8000")
        self.assertTrue(t["ok"])
        self.assertTrue(t["local"])
        self.assertIsNone(t["warning"])

    def test_https_ok(self) -> None:
        t = tl.url_transport("https://mnemos.example:8443")
        self.assertTrue(t["ok"])
        self.assertTrue(t["tls"])

    def test_lan_http_warns(self) -> None:
        t = tl.url_transport("http://192.0.2.9:8000")
        self.assertTrue(t["ok"])
        self.assertIsNotNone(t["warning"])

    def test_require_tls_blocks_lan_http(self) -> None:
        fake = SimpleNamespace(require_tls=True)
        with mock.patch.object(tl, "_peer_cfg", return_value=fake):
            t = tl.url_transport("http://192.0.2.9:8000")
        self.assertFalse(t["ok"])
        self.assertIn("TLS", t["error"])


class PackTests(TeamLayerBase):
    def test_manager_pack_autos_work_not_personal(self) -> None:
        pid = self._pair()
        res = tl.apply_pack(pid, "manager")
        self.assertTrue(res["ok"], res)
        pol = pch.get_policy(pid)
        self.assertEqual(pol["work"], "auto")
        self.assertEqual(pol["availability"], "auto")
        self.assertEqual(pol["personal"], "offer")
        rec = pch.peers()[0]
        self.assertEqual(rec["policy_pack"], "manager")

    def test_unknown_pack_refused(self) -> None:
        pid = self._pair()
        self.assertFalse(tl.apply_pack(pid, "bossware")["ok"])

    def test_vendor_denies_everything(self) -> None:
        pid = self._pair()
        tl.apply_pack(pid, "vendor")
        self.assertTrue(all(v == "deny" for v in pch.get_policy(pid).values()))


class TeamGroupTests(TeamLayerBase):
    def test_upsert_and_parse_hash(self) -> None:
        pid = self._pair()
        res = tl.upsert_team("Platform", peer_ids=[pid])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["team"]["slug"], "platform")
        got = tl.parse_group_ask("ask #platform: what's blocking Nexus?")
        self.assertIsNotNone(got)
        self.assertEqual(got["peer_ids"], [pid])
        self.assertEqual(got["kind"], "question")
        self.assertFalse(got["unknown"])

    def test_parse_the_team_handoff(self) -> None:
        pid = self._pair()
        tl.upsert_team("Platform", peer_ids=[pid])
        got = tl.parse_group_ask("ask the platform team to review the beta slides")
        self.assertEqual(got["kind"], "handoff")
        self.assertEqual(got["question"], "review the beta slides")

    def test_unknown_team_does_not_guess(self) -> None:
        got = tl.parse_group_ask("ask #design: who owns the kit?")
        self.assertTrue(got["unknown"])
        self.assertEqual(got["peer_ids"], [])

    def test_non_group_is_none(self) -> None:
        self.assertIsNone(tl.parse_group_ask("ask sarah: are the slides done?"))
        self.assertIsNone(tl.parse_group_ask("ask the professor about homework"))

    def test_peer_channel_parse_delegates_group(self) -> None:
        pid = self._pair()
        tl.upsert_team("Platform", peer_ids=[pid])
        got = pch.parse_team_ask("ask #platform: status?")
        self.assertTrue(got["fanout"])
        self.assertEqual(got["peer_ids"], [pid])

    def test_fanout_asks_each_member(self) -> None:
        pid = self._pair()
        tl.upsert_team("Platform", peer_ids=[pid])
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "pending"}):
            res = tl.fanout_ask("platform", "what's blocking Nexus?")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["asked"], 1)
        self.assertEqual(res["results"][0]["status"], "pending")
        row = pch.answers()[0]
        self.assertEqual(row["team_slug"], "platform")
        self.assertEqual(row["team_ask_id"], res["team_ask_id"])

    def test_rollup_when_all_terminal(self) -> None:
        pid = self._pair()
        peer = pch.peers()[0]
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "pending"}):
            a = pch.ask(pid, "q1", team_slug="platform", team_ask_id="t1")
            b = pch.ask(pid, "q2", team_slug="platform", team_ask_id="t1")
        # Only the first ask is outstanding in handle_answer's matching
        # (same peer) — use two answers by recording directly.
        self.assertIsNone(tl.maybe_rollup("t1"))
        rec = {"peer_id": pid, "name": peer["name"]}
        pch.handle_answer(rec, {"ask_id": a["ask_id"], "answer": "legal"})
        self.assertIsNone(tl.maybe_rollup("t1"))
        pch.handle_answer(rec, {"ask_id": b["ask_id"], "answer": "infra"})
        text = tl.maybe_rollup("t1")
        self.assertIsNotNone(text)
        self.assertIn("2 of 2", text)
        self.assertIn("legal", text)


class MailboxTests(TeamLayerBase):
    def test_unreachable_then_flush(self) -> None:
        pid = self._pair()
        with mock.patch.object(pch, "_post_json",
                               side_effect=OSError("refused")):
            res = pch.ask(pid, "when?")
        self.assertEqual(res["status"], "queued")
        self.assertEqual(len(tl.mailbox_list(pid)), 1)
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "pending"}):
            flushed = tl.flush_mailbox(pid)
        self.assertEqual(flushed["flushed"], 1)
        self.assertEqual(tl.mailbox_list(pid), [])
        self.assertEqual(pch.answers(res["ask_id"])[0]["status"], "pending")


class LoopTests(TeamLayerBase):
    def test_handoff_mints_loop_on_both_sides(self) -> None:
        pid = self._pair()
        start = pch.start_pairing()
        # Re-use the already-paired peer; grab inbound token from registry? We
        # don't have it. Pair a second time? Use the claimed token from a fresh
        # pair and run handle_ask as the answerer.
        pch._pairing = None
        # The existing peer is the claim-side (we are the desktop). Outbound
        # ask mints a loop; inbound handle_ask stores the same id.
        with mock.patch.object(pch, "_post_json",
                               return_value={"ok": True, "status": "pending"}):
            res = pch.ask(pid, "review the slides", kind="handoff")
        loop_id = pch.answers(res["ask_id"])[0]["loop_id"]
        self.assertTrue(loop_id)
        loops = tl.loops()
        self.assertEqual(loops[0]["status"], "offered")
        self.assertEqual(loops[0]["loop_id"], loop_id)

        claim = pch.start_pairing()
        claimed = pch.claim_pairing(claim["code"], "Marc", "http://127.0.0.1:8",
                                    "u" * 20)
        peer = pch.authenticate(f"Bearer {claimed['token']}")
        pch.handle_ask(peer, {"ask_id": "h9", "kind": "handoff",
                              "question": "review the slides",
                              "loop_id": loop_id})
        local_id = pch.pending_asks()[0]["id"]
        with mock.patch.object(pch, "_deliver", return_value=True):
            pch.decide_ask(local_id, approve=True)
        found = [r for r in tl.loops() if r["loop_id"] == loop_id
                 and r.get("side") == "receiver"]
        self.assertEqual(found[0]["status"], "open")


class PingTests(TeamLayerBase):
    def test_handle_ping_online(self) -> None:
        start = pch.start_pairing()
        claimed = pch.claim_pairing(start["code"], "Sarah",
                                    "http://127.0.0.1:9", "t" * 20)
        peer = pch.authenticate(f"Bearer {claimed['token']}")
        self.assertEqual(pch.peers()[0]["presence"], "unknown")
        res = tl.handle_ping(peer)
        self.assertTrue(res["ok"])
        self.assertEqual(pch.peers()[0]["presence"], "online")


class OfferTests(TeamLayerBase):
    def test_skips_self_and_paired(self) -> None:
        self._pair("Sarah Chen")
        attendees = [
            {"name": "Sarah Chen", "email": "sarah@x.test"},
            {"name": "You Person", "email": "me@x.test"},
            {"name": "Alex Kim", "email": "alex@x.test"},
        ]
        with mock.patch.object(tl, "_collect_attendees", return_value=attendees), \
             mock.patch.object(tl, "_self_keys",
                               return_value={"you person", "you", "me@x.test"}):
            offers = tl.pairing_offers()
        names = {o["name"] for o in offers}
        self.assertIn("Alex Kim", names)
        self.assertNotIn("Sarah Chen", names)
        self.assertNotIn("You Person", names)


class PresenceTests(unittest.TestCase):
    def test_buckets(self) -> None:
        self.assertEqual(tl.presence_of(None), "unknown")
        self.assertEqual(tl.presence_of(time.time()), "online")
        self.assertEqual(tl.presence_of(time.time() - 10_000), "offline")


if __name__ == "__main__":
    unittest.main()
