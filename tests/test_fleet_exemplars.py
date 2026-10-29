"""Exemplar sharing over the relay (distillation over the network, step 2).

One person's accepted or corrected answer becomes an `exemplar` offer, a
human approves it, the relay carries it, and a teammate's Sparrow files it as
a `peer_shared` exemplar its local model can retrieve.

Covered: the built-in kind (and that the relay knows it), every offer gate
(flag, verdict, confirmation, type, length, privacy floor, route rule,
share-downgraded-to-offer, one offer per pair), the learning_store hook, the
intake gates (flag, provenance, receiver-side privacy), retrieval ranking and
the `_peer` gate, purge, the owner routes, and a two-node round trip through
the real inbound handler with separate origin ledgers.
"""
from __future__ import annotations

import os
import time
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from app.events import Event, Modality
from app.services import exemplar_store as xs
from app.services.fleet import envelope as env
from app.services.fleet import exemplar_intake, exemplar_share, inbound
from app.services.fleet import relay_client, router, state
from tests.fleet_support import FleetTestCase, fleet_app
from tests.test_exemplar_store import fake_encode_many

NODE_TOKEN = "node-a-token-0123456789abcdef"
INBOUND = "fi_inbound-token-0123456789abcdef0123"

EXTRA_ENV = ("QUILL_FLEET_EXEMPLARS_OFFER", "QUILL_FLEET_EXEMPLARS_ACCEPT",
             "QUILL_EXEMPLARS", "QUILL_EXEMPLAR_GATES_PATH",
             "QUILL_EXEMPLAR_USES_PATH", "QUILL_LEARNING", "QUILL_RELAY_KINDS")


def pair(**over) -> dict:
    row = {"id": uuid.uuid4().hex, "task_type": "extraction.task",
           "input_text": "Sam said he would send the vendor deck by Friday.",
           "final_target": "Send the vendor deck (Sam, due Friday)",
           "verdict": "edited", "human_confirmed": True,
           "privacy_class": "internal", "verdict_source": "facts.review"}
    row.update(over)
    return row


class Base(FleetTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved_extra = {k: os.environ.get(k) for k in EXTRA_ENV}
        os.environ.update({
            "QUILL_FLEET_EXEMPLARS_OFFER": "1",
            "QUILL_FLEET_EXEMPLARS_ACCEPT": "1",
            "QUILL_EXEMPLARS": "1",
            "QUILL_LEARNING": "1",
            "QUILL_EXEMPLAR_GATES_PATH": str(self.tmp / "gates.json"),
            "QUILL_EXEMPLAR_USES_PATH": str(self.tmp / "uses.jsonl"),
        })
        state.set_relay(url="http://relay.test", node_id="node-a",
                        token=NODE_TOKEN,
                        inbound_token_sha256=env.link_key(INBOUND))
        self.sent = []

        def fake_post(url, body, token=""):
            self.sent.append({"url": url, "body": body, "token": token})
            return 200, {"ok": True, "seq": len(self.sent)}

        self.xstore = xs.ExemplarStore(path=str(self.tmp / "lance"))
        for p in (mock.patch.object(relay_client, "_post",
                                    side_effect=fake_post),
                  mock.patch.object(xs, "_embed", fake_encode_many),
                  mock.patch.object(xs, "exemplar_store", self.xstore)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self) -> None:
        for k, v in self._saved_extra.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def allow(self, action="offer", topic="learn.*") -> None:
        router.save_rules([{"topic": topic, "action": action}])

    def peer_event(self, signal: dict, provenance="peer") -> Event:
        return Event(time=time.time(), modality=Modality.SYSTEM, raw="x",
                     source="peer.signal" if provenance == "peer"
                     else "fleet.signal",
                     meta={"signal": signal, "provenance": provenance,
                           "fleet_seq": 1})


class KindTests(Base):
    def test_exemplar_is_built_in_everywhere(self) -> None:
        self.assertIn("exemplar", env.load_kinds(None))
        os.environ["QUILL_RELAY_KINDS"] = str(self.tmp / "missing.json")
        from org_coordinator import relay
        self.assertIn("exemplar", relay.load_kinds())

    def test_kind_types_are_retrievable_extraction_types_only(self) -> None:
        routable = {t for ts in xs.ROUTER_TASK_TYPES.values() for t in ts}
        self.assertEqual(set(exemplar_share.SHAREABLE_TYPES),
                         routable - {"escalation.text"})

    def test_parent_answers_cannot_ride(self) -> None:
        """escalation.text targets are often a parent model's answer; held
        back pending a provider-terms review, at every hop."""
        self.allow()
        res = exemplar_share.offer_pair(pair(task_type="escalation.text"))
        self.assertEqual(res["status"], "skipped")
        sig = exemplar_share.build_signal(pair())
        bad = dict(sig, body=dict(sig["body"], task_type="escalation.text"))
        with self.assertRaises(env.SignalError):
            env.validate(bad, kinds=env.load_kinds(None))

    def test_a_registry_cannot_redefine_it(self) -> None:
        with self.assertRaises(ValueError):
            env.load_kinds({"kinds": {"exemplar": {"fields": {}}}})

    def test_lineage_fields_and_subjects_cannot_ride(self) -> None:
        sig = exemplar_share.build_signal(pair())
        bad = dict(sig, body=dict(sig["body"], learning_pair_id="abc"))
        with self.assertRaises(env.SignalError) as cm:
            env.validate(bad, kinds=env.load_kinds(None))
        self.assertEqual(cm.exception.code, "forbidden_field")
        with self.assertRaises(env.SignalError):
            env.validate(dict(sig, subject="Sam"),
                         kinds=env.load_kinds(None))

    def test_shadow_tiers_are_not_a_valid_quality_tier(self) -> None:
        sig = exemplar_share.build_signal(pair())
        bad = dict(sig, body=dict(sig["body"], quality_tier="shadow_autotrust"))
        with self.assertRaises(env.SignalError):
            env.validate(bad, kinds=env.load_kinds(None))


class OfferTests(Base):
    def test_off_by_default(self) -> None:
        os.environ.pop("QUILL_FLEET_EXEMPLARS_OFFER")
        self.allow()
        self.assertEqual(exemplar_share.offer_pair(pair())["status"], "skipped")
        self.assertEqual(router.list_offers(), [])

    def test_needs_the_fleet_on_too(self) -> None:
        os.environ["QUILL_FLEET"] = "0"
        self.allow()
        self.assertEqual(exemplar_share.offer_pair(pair())["status"], "skipped")

    def test_no_route_rule_keeps_it_home(self) -> None:
        self.assertEqual(exemplar_share.offer_pair(pair())["status"], "local")
        self.assertEqual(router.list_offers(), [])

    def test_a_share_rule_still_only_offers(self) -> None:
        self.allow("share")
        res = exemplar_share.offer_pair(pair())
        self.assertEqual(res["status"], "offered")
        self.assertEqual(self.sent, [])
        [offer] = router.list_offers("pending")
        self.assertEqual(offer["signal"]["kind"], "exemplar")
        self.assertEqual(offer["signal"]["topic"], "learn.extraction.task")
        self.assertEqual(offer["signal"]["body"]["quality_tier"],
                         "human_edited")

    def test_offer_carries_no_local_lineage(self) -> None:
        self.allow()
        p = pair()
        exemplar_share.offer_pair(p)
        sig = router.list_offers("pending")[0]["signal"]
        self.assertNotIn(p["id"], str(sig["body"]))
        self.assertNotIn("facts.review", str(sig))

    def test_one_offer_per_pair(self) -> None:
        self.allow()
        p = pair()
        self.assertEqual(exemplar_share.offer_pair(p)["status"], "offered")
        self.assertEqual(exemplar_share.offer_pair(p)["status"], "duplicate")
        self.assertEqual(len(router.list_offers()), 1)

    def test_only_confirmed_positive_shareable_pairs(self) -> None:
        self.allow()
        for p in (pair(verdict="rejected"), pair(verdict="shadow_disagree"),
                  pair(human_confirmed=False),
                  pair(task_type="audit.forget"), pair(task_type="agent.act"),
                  pair(final_target="")):
            self.assertEqual(exemplar_share.offer_pair(p)["status"],
                             "skipped", p)
        self.assertEqual(router.list_offers(), [])

    def test_too_long_is_skipped_not_truncated(self) -> None:
        self.allow()
        res = exemplar_share.offer_pair(pair(final_target="x" * 5001))
        self.assertEqual(res["status"], "skipped")

    def test_stamped_privacy_class_is_the_floor(self) -> None:
        """Redaction can make a stored row look harmless; the class stamped
        on the raw text still keeps it home."""
        self.allow()
        res = exemplar_share.offer_pair(pair(privacy_class="personal"))
        self.assertEqual(res["status"], "refused")
        self.assertIn("privacy_class", res["reason"])
        self.assertEqual(router.list_offers(), [])

    def test_never_share_topic_and_blocked_list_still_apply(self) -> None:
        router.save_rules([{"topic": "learn.*", "action": "offer"}],
                          {"topics": [], "kinds": ["exemplar"]})
        self.assertEqual(exemplar_share.offer_pair(pair())["status"], "offered")
        os.remove(self.tmp / "fleet_blocked.json")
        self.assertEqual(exemplar_share.offer_pair(pair())["status"], "refused")

    def test_approval_sends_it_once(self) -> None:
        self.allow()
        exemplar_share.offer_pair(pair())
        offer = router.list_offers("pending")[0]
        row = router.decide_offer(offer["offer_id"], True,
                                  sha256=offer["sha256"])
        self.assertEqual(row["status"], "sent")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["body"]["signal"]["kind"], "exemplar")


class LearningHookTests(Base):
    def setUp(self) -> None:
        super().setUp()
        from app.storage import Store
        self.store = Store(db_path=self.tmp / "t.db",
                           audio_dir=self.tmp / "audio")
        self.allow()

    def test_a_confirmed_edit_becomes_an_offer(self) -> None:
        from app.services import learning_store as ls
        pid = ls.record(task_type="extraction.task",
                        input_text="Priya will book the offsite venue.",
                        final_target="Book the offsite venue (Priya)",
                        verdict="edited", verdict_source="facts.review",
                        store=self.store)
        self.assertTrue(pid)
        [offer] = router.list_offers("pending")
        self.assertEqual(offer["signal"]["signal_id"], f"lp-{pid}")
        # confirm() re-notifies; it must not double-offer.
        from app.services import learning_store as ls2
        ls2._offer_to_fleet(self.store.get_learning_pair(pid))
        self.assertEqual(len(router.list_offers()), 1)

    def test_an_unconfirmed_shadow_pair_is_not_offered(self) -> None:
        from app.services import learning_store as ls
        ls.record(task_type="extraction.task",
                  input_text="Priya will book the offsite venue.",
                  final_target="Book the offsite venue (Priya)",
                  verdict="accepted", verdict_source="shadow.grader",
                  human_confirmed=False, store=self.store)
        self.assertEqual(router.list_offers(), [])


class IntakeTests(Base):
    def wire(self, **body_over) -> dict:
        sig = exemplar_share.build_signal(pair())
        sig["body"].update(body_over)
        return sig

    def test_peer_exemplar_is_filed_as_peer_shared(self) -> None:
        exemplar_intake.on_event(self.peer_event(self.wire()), sync=True)
        [row] = self.xstore.list_rows()
        self.assertEqual(row["quality_tier"], "peer_shared")
        self.assertTrue(row["learning_pair_id"].startswith(xs.PEER_PREFIX))

    def test_redelivery_replaces_rather_than_duplicates(self) -> None:
        sig = self.wire()
        exemplar_intake.ingest(sig)
        exemplar_intake.ingest(sig)
        self.assertEqual(len(self.xstore.list_rows()), 1)

    def test_off_by_default(self) -> None:
        os.environ.pop("QUILL_FLEET_EXEMPLARS_ACCEPT")
        exemplar_intake.on_event(self.peer_event(self.wire()), sync=True)
        self.assertEqual(self.xstore.list_rows(), [])

    def test_only_peer_provenance(self) -> None:
        exemplar_intake.on_event(self.peer_event(self.wire(), "local"),
                                 sync=True)
        self.assertEqual(self.xstore.list_rows(), [])

    def test_receiver_reruns_the_privacy_rule(self) -> None:
        with mock.patch("app.services.privacy_class.classify_text",
                        return_value="personal"):
            exemplar_intake.on_event(self.peer_event(self.wire()), sync=True)
        self.assertEqual(self.xstore.list_rows(), [])

    def test_unroutable_type_is_dropped(self) -> None:
        self.assertIsNone(exemplar_intake.ingest(
            self.wire(task_type="audit.forget")))

    def test_local_exemplars_outrank_a_peers_at_equal_similarity(self) -> None:
        text = "Sam said he would send the vendor deck by Friday."
        exemplar_intake.ingest(self.wire())
        self.xstore.ingest_pair(pair(input_text=text, verdict="accepted",
                                     final_target="Vendor deck to Sam"))
        got = self.xstore.examples(("extraction.task",), text, k=2,
                                   token_budget=10_000)
        self.assertEqual([g["tier"] for g in got],
                         ["human_accepted", "peer_shared"])

    def test_peer_gate_and_purge(self) -> None:
        text = "Sam said he would send the vendor deck by Friday."
        exemplar_intake.ingest(self.wire())
        self.xstore.set_gate(xs.PEER_GATE, True, reason="test")
        self.assertEqual(self.xstore.examples(("extraction.task",), text), [])
        self.xstore.set_gate(xs.PEER_GATE, False)
        self.assertEqual(len(self.xstore.examples(("extraction.task",), text)),
                         1)
        self.assertEqual(self.xstore.purge_peer(), 1)
        self.assertEqual(self.xstore.list_rows(), [])

    def test_owner_routes(self) -> None:
        client = TestClient(fleet_app())
        exemplar_intake.ingest(self.wire())
        r = client.get("/fleet/exemplars").json()
        self.assertEqual((r["offer"], r["accept"], r["peer_exemplars"]),
                         (True, True, 1))
        self.assertEqual(client.post("/fleet/exemplars/purge").json()["removed"],
                         1)


class RoundTripTests(Base):
    def test_a_correction_on_a_reaches_bs_exemplar_store(self) -> None:
        """Node A offers, its owner approves, the relay re-signs for B's link,
        and B's real inbound handler plus intake file it. A and B keep
        separate origin ledgers, as two processes would."""
        self.allow()
        exemplar_share.offer_pair(pair())
        offer = router.list_offers("pending")[0]
        router.decide_offer(offer["offer_id"], True, sha256=offer["sha256"])
        wire = self.sent[0]["body"]["signal"]

        os.environ["QUILL_FLEET_ORIGINS"] = str(self.tmp / "b_origins.json")
        self.bus.subscribe(lambda e: exemplar_intake.on_event(e, sync=True))
        relayed = env.sign(dict(wire), env.link_key(INBOUND))
        status, res = inbound.handle(f"Bearer {INBOUND}",
                                     {"signal": relayed,
                                      "sender_node": "node-a"})
        self.assertEqual((status, res["status"]), (200, "accepted"), res)
        [row] = self.xstore.list_rows()
        self.assertEqual(row["quality_tier"], "peer_shared")
        self.assertEqual(row["target_text"],
                         "Send the vendor deck (Sam, due Friday)")

    def test_a_does_not_learn_from_its_own_exemplar_coming_back(self) -> None:
        self.allow()
        exemplar_share.offer_pair(pair())
        offer = router.list_offers("pending")[0]
        router.decide_offer(offer["offer_id"], True, sha256=offer["sha256"])
        relayed = env.sign(dict(self.sent[0]["body"]["signal"]),
                           env.link_key(INBOUND))
        self.bus.subscribe(lambda e: exemplar_intake.on_event(e, sync=True))
        status, res = inbound.handle(f"Bearer {INBOUND}", {"signal": relayed})
        self.assertEqual(res["status"], "dropped")
        self.assertEqual(self.xstore.list_rows(), [])
