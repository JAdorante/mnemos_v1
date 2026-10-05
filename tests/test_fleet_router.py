"""Outbound router: Sparrow, not the agent, decides what leaves.

Unmatched means local, offer creates a packet, never_share topics and kinds
always need a human, and blocked subjects are refused before anything
reaches the network.
"""
from __future__ import annotations

import json
import time
from unittest import mock

from app.services.fleet import envelope as env
from app.services.fleet import relay_client, router, state
from tests.fleet_support import FleetTestCase, full_signal

NODE_TOKEN = "node-token-0123456789abcdef0123"


class RouterBase(FleetTestCase):
    def setUp(self) -> None:
        super().setUp()
        state.set_relay(url="http://relay.test", node_id="node-a",
                        token=NODE_TOKEN,
                        inbound_token_sha256=env.link_key("inbound-x" * 4))
        self.sent = []

        def fake_post(url, body, token=""):
            self.sent.append({"url": url, "body": body, "token": token})
            return 200, {"ok": True, "seq": len(self.sent)}

        p = mock.patch.object(relay_client, "_post", side_effect=fake_post)
        p.start()
        self.addCleanup(p.stop)

    def rules(self, *rules) -> None:
        router.save_rules(list(rules))

    def signal(self, **over) -> dict:
        now = time.time()
        return full_signal(ts=now, expires_at=now + 600, **over)


class DecisionTests(RouterBase):
    def test_unmatched_signal_stays_local(self) -> None:
        d = router.apply(self.signal())
        self.assertEqual(d.action, "local")
        self.assertEqual(d.reason, "no matching rule")
        self.assertEqual(self.sent, [])
        self.assertEqual(router.list_offers(), [])

    def test_missing_or_malformed_routes_file_means_local(self) -> None:
        (self.tmp / "fleet_routes.json").write_text("{not json",
                                                    encoding="utf-8")
        self.assertEqual(router.route(self.signal()).action, "local")
        (self.tmp / "fleet_routes.json").write_text('{"rules": "share"}',
                                                    encoding="utf-8")
        self.assertEqual(router.route(self.signal()).action, "local")

    def test_a_rule_without_an_action_defaults_to_offer(self) -> None:
        self.rules({"topic": "eng.status"})
        self.assertEqual(router.route(self.signal()).action, "offer")

    def test_share_signs_increments_hops_and_sends_unchanged_bytes(self) -> None:
        self.rules({"topic": "eng.status", "action": "share"})
        sig = self.signal()
        d = router.apply(sig)
        self.assertEqual(d.action, "share")
        self.assertEqual(len(self.sent), 1)
        wire = self.sent[0]["body"]["signal"]
        self.assertEqual(self.sent[0]["url"], "http://relay.test/relay/publish")
        self.assertEqual(wire["hops"], 1)
        self.assertTrue(env.verify(wire, env.link_key(NODE_TOKEN)))
        # Everything but hops and sig is byte-for-byte what the agent sent.
        strip = lambda s: {k: v for k, v in s.items() if k not in ("hops", "sig")}
        self.assertEqual(strip(wire), strip(sig))

    def test_share_never_touches_the_peer_llm_egress_path(self) -> None:
        self.rules({"topic": "eng.status", "action": "share"})
        from app.services import peer_channel
        with mock.patch.object(peer_channel, "compose_peer_claims",
                               side_effect=AssertionError("LLM egress used")):
            self.assertEqual(router.apply(self.signal()).action, "share")

    def test_local_rule(self) -> None:
        self.rules({"topic": "eng.*", "action": "local"})
        self.assertEqual(router.apply(self.signal()).action, "local")

    def test_most_specific_rule_wins_then_most_restrictive(self) -> None:
        self.rules({"topic": "eng.*", "action": "share"},
                   {"topic": "eng.status", "producer": "agent:pm",
                    "action": "local"},
                   {"topic": "eng.status", "action": "share"})
        self.assertEqual(router.route(self.signal()).action, "local")
        other = self.signal(producer="agent:fx")
        self.assertEqual(router.route(other).action, "share")
        self.rules({"topic": "eng.status", "action": "share"},
                   {"topic": "eng.status", "action": "offer"})
        self.assertEqual(router.route(self.signal()).action, "offer")

    def test_no_relay_means_nothing_leaves(self) -> None:
        state.clear_relay()
        self.rules({"topic": "eng.status", "action": "share"})
        d = router.apply(self.signal())
        self.assertEqual(d.action, "local")
        self.assertEqual(self.sent, [])


class BlockedSubjectTests(RouterBase):
    def test_blocked_subject_is_refused_before_the_network(self) -> None:
        self.rules({"topic": "eng.status", "action": "share"})
        d = router.apply(self.signal(subject="project  FALCON"))
        self.assertEqual(d.action, "refused")
        self.assertEqual(d.reason, "blocked_subject")
        self.assertEqual(self.sent, [])

    def test_blocked_offer_is_refused_too(self) -> None:
        self.rules({"topic": "eng.status", "action": "offer"})
        self.assertEqual(router.apply(self.signal(subject="Project Falcon")).action,
                         "refused")
        self.assertEqual(router.list_offers(), [])

    def test_missing_blocked_list_fails_closed(self) -> None:
        (self.tmp / "fleet_blocked.json").unlink()
        self.rules({"topic": "eng.status", "action": "share"})
        d = router.apply(self.signal())
        self.assertEqual(d.action, "refused")
        self.assertIn("blocked_list_unavailable", d.reason)
        self.assertEqual(self.sent, [])

    def test_unshareable_licence_is_refused(self) -> None:
        self.rules({"topic": "eng.status", "action": "share"})
        d = router.apply(self.signal(sources=[{"name": "vendor",
                                              "license": "vendor_only"}]))
        self.assertEqual((d.action, d.reason),
                         ("refused", "license_not_shareable"))

    def test_signal_already_at_the_hop_cap_cannot_leave(self) -> None:
        self.rules({"topic": "eng.status", "action": "share"})
        d = router.apply(self.signal(hops=2))
        self.assertEqual((d.action, d.reason), ("refused", "too_many_hops"))


class NeverShareTests(RouterBase):
    NEVER = {"topics": ["hr.*", "*.positions"], "kinds": ["finding"]}

    def test_never_share_topic_can_never_be_share_at_write(self) -> None:
        for topic in ("hr.reviews", "hr.*", "desk.positions", "*"):
            with self.subTest(topic=topic):
                with self.assertRaises(router.RouteError):
                    router.save_rules([{"topic": topic, "action": "share"}],
                                      self.NEVER)
        router.save_rules([{"topic": "hr.reviews", "action": "offer"}],
                          self.NEVER)

    def test_saving_rules_keeps_the_current_never_share_list(self) -> None:
        router.save_rules([], self.NEVER)
        router.save_rules([{"topic": "eng.status", "action": "share"}])
        self.assertEqual(router.load_policy()[1],
                         {"topics": sorted(self.NEVER["topics"]),
                          "kinds": self.NEVER["kinds"]})
        with self.assertRaises(router.RouteError):
            router.save_rules([{"topic": "hr.pay", "action": "share"}])

    def test_hand_edited_share_on_a_never_share_topic_is_downgraded(self) -> None:
        (self.tmp / "fleet_routes.json").write_text(json.dumps({
            "rules": [{"topic": "hr.reviews", "action": "share"}],
            "never_share": self.NEVER}), encoding="utf-8")
        self.assertEqual(router.load_rules()[0]["action"], "offer")
        d = router.route(self.signal(topic="hr.reviews"))
        self.assertEqual(d.action, "offer")

    def test_enforcement_downgrades_even_if_load_is_bypassed(self) -> None:
        router.save_rules([], self.NEVER)
        rule = {"topic": "desk.positions", "producer": "*", "action": "share"}
        with mock.patch.object(router, "match", return_value=rule):
            self.assertEqual(router.route(
                self.signal(topic="desk.positions")).action, "offer")

    def test_never_share_kind_needs_a_human_on_any_topic(self) -> None:
        router.save_rules([{"topic": "eng.*", "action": "share"}], self.NEVER)
        finding = self.signal(kind="finding", subject=None,
                              body={"severity": "high"})
        self.assertEqual(router.route(finding).action, "offer")
        self.assertEqual(router.route(self.signal()).action, "share")

    def test_unreadable_never_share_turns_every_share_into_offer(self) -> None:
        (self.tmp / "fleet_routes.json").write_text(json.dumps({
            "rules": [{"topic": "eng.status", "action": "share"}],
            "never_share": "everything"}), encoding="utf-8")
        self.assertEqual(router.route(self.signal()).action, "offer")

    def test_unknown_kind_cannot_leave(self) -> None:
        router.save_rules([{"topic": "eng.status", "action": "share"}])
        (self.tmp / "fleet_kinds.json").unlink()
        d = router.route(self.signal())
        self.assertEqual((d.action, d.reason), ("refused", "unknown_kind"))

    def test_the_peer_channel_has_no_domain_specific_class(self) -> None:
        from app.services import peer_channel as pch
        self.assertEqual(pch.CLASSES, ("availability", "work", "contact",
                                       "personal", "other"))

    def test_a_paired_peer_cannot_send_or_receive_signals(self) -> None:
        from app.services import peer_channel as pch
        res = pch.handle_ask({"peer_id": "p", "name": "Pat"},
                             {"ask_id": "a1", "question": "x",
                              "kind": "signal"})
        self.assertFalse(res["ok"])
        self.assertIn("relay", res["error"])
        res = pch.ask("p", "x", kind="signal")
        self.assertFalse(res["ok"])


class OfferTests(RouterBase):
    def setUp(self) -> None:
        super().setUp()
        self.rules({"topic": "eng.status", "action": "offer"})
        self.assertEqual(router.apply(self.signal()).action, "offer")
        self.offer = router.list_offers("pending")[0]

    def test_offer_creates_a_packet_bound_to_the_canonical_bytes(self) -> None:
        self.assertEqual(self.offer["sha256"], env.digest(self.offer["signal"]))
        self.assertEqual(self.sent, [])

    def test_approval_must_quote_the_shown_hash(self) -> None:
        with self.assertRaises(router.RouteError):
            router.decide_offer(self.offer["offer_id"], True, sha256="nope")
        row = router.decide_offer(self.offer["offer_id"], True,
                                  sha256=self.offer["sha256"])
        self.assertEqual(row["status"], "sent")
        self.assertEqual(len(self.sent), 1)

    def test_an_edited_signal_needs_a_fresh_approval(self) -> None:
        old = self.offer["sha256"]
        edited = router.edit_offer(self.offer["offer_id"],
                                   {"summary": "Revised: auction was fine."})
        self.assertNotEqual(edited["sha256"], old)
        with self.assertRaises(router.RouteError):
            router.decide_offer(self.offer["offer_id"], True, sha256=old)
        router.decide_offer(self.offer["offer_id"], True,
                            sha256=edited["sha256"])
        self.assertEqual(self.sent[0]["body"]["signal"]["summary"],
                         "Revised: auction was fine.")

    def test_edits_are_limited_to_the_view(self) -> None:
        for field in ("subject", "kind", "topic", "producer", "origin_id"):
            with self.assertRaises(router.RouteError):
                router.edit_offer(self.offer["offer_id"], {field: "X"})

    def test_decline_sends_nothing(self) -> None:
        row = router.decide_offer(self.offer["offer_id"], False)
        self.assertEqual(row["status"], "declined")
        self.assertEqual(self.sent, [])

    def test_gates_rerun_at_approval_time(self) -> None:
        (self.tmp / "fleet_blocked.json").write_text(
            '{"subjects": ["Atlas migration"]}', encoding="utf-8")
        row = router.decide_offer(self.offer["offer_id"], True,
                                  sha256=self.offer["sha256"])
        self.assertEqual(row["status"], "refused")
        self.assertEqual(self.sent, [])


class OutboxTests(RouterBase):
    def test_unreachable_relay_queues_and_drains(self) -> None:
        self.rules({"topic": "eng.status", "action": "share"})
        relay_client._post.side_effect = lambda *a, **k: (0, {"error": "down"})
        router.apply(self.signal())
        self.assertEqual(len(relay_client.outbox()), 1)
        relay_client._post.side_effect = lambda url, body, token="": (
            self.sent.append(body) or (200, {"ok": True}))
        out = relay_client.drain_outbox()
        self.assertEqual(out, {"sent": 1, "dropped": 0, "pending": 0})
        self.assertEqual(relay_client.outbox(), [])

    def test_relay_refusal_is_not_retried(self) -> None:
        self.rules({"topic": "eng.status", "action": "share"})
        relay_client._post.side_effect = lambda *a, **k: (403, {"error": "barrier"})
        res = relay_client.send(self.signal())
        self.assertFalse(res["ok"])
        self.assertEqual(relay_client.outbox(), [])

    def test_expired_signals_are_dropped_from_the_outbox(self) -> None:
        now = time.time()
        relay_client._enqueue(full_signal(ts=now - 20, expires_at=now - 1), "x")
        self.assertEqual(relay_client.drain_outbox()["dropped"], 1)
