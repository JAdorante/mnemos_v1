"""Records layer end to end: real nodes (temp SQLite stores) talking to the
real Org Record Service (Postgres) in-process through org_client's transport.

  capture event -> accepted fact_candidate -> claim -> packet -> human approve
  -> record version in Postgres -> provenance back to the node's evidence
  pointer -> capture expires on the node -> org evidence flips to expired

Skips without QUILL_ORG_TEST_DATABASE_URL (tests/records_pg.py).
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import app.config
from app.events import Event, Modality
from app.services.records import (claim_builder, node_store, org_client,
                                  promotion, retention)
from app.storage import Store
from tests.records_pg import PgTestCase, make_app

DAY = 86400.0


class Node:
    def __init__(self, name: str) -> None:
        self.name = name
        self.dir = tempfile.mkdtemp(prefix=f"node-{name}-")
        self.store = Store(db_path=Path(self.dir) / "quill.db",
                           audio_dir=Path(self.dir) / "audio")
        real = app.config.settings
        self.settings = dataclasses.replace(
            real, storage=dataclasses.replace(real.storage, data_dir=self.dir))

    @contextlib.contextmanager
    def active(self):
        """org_client keeps one membership file per data dir and one cached
        access token per process; switch both when switching nodes."""
        with patch.object(app.config, "settings", self.settings):
            org_client._access.clear()
            try:
                yield self
            finally:
                org_client._access.clear()

    def close(self) -> None:
        try:
            self.store._conn.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


class RecordsEndToEnd(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from fastapi.testclient import TestClient

        from org_coordinator.records import service
        cls.client = TestClient(make_app(cls.db))

        def transport(method, path, body, headers):
            r = cls.client.request(method, path, json=body, headers=headers)
            return r.status_code, r.json()

        cls.transport = staticmethod(transport)
        boot = service.bootstrap_org(cls.db, name="E2E",
                                     admin_email="lead@e2e.test")
        cls.org_id, cls.root = boot["org_id"], boot["root_scope_id"]
        cls.lead_invite = boot["invite_code"]

    def setUp(self) -> None:
        self._env = {k: os.environ.get(k) for k in
                     ("QUILL_LEARNING", "QUILL_CLAIM_PROPOSE_MIN_CONF",
                      "QUILL_CAPTURE_TTL_DAYS")}
        os.environ["QUILL_LEARNING"] = "0"
        os.environ["QUILL_CLAIM_PROPOSE_MIN_CONF"] = "2"
        os.environ["QUILL_CAPTURE_TTL_DAYS"] = "30"
        org_client.set_transport(self.transport)
        self.nodes: list[Node] = []

    def tearDown(self) -> None:
        org_client.set_transport(None)
        for n in self.nodes:
            n.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def node(self, name: str) -> Node:
        n = Node(name)
        self.nodes.append(n)
        return n

    def _lead_node(self) -> Node:
        """The admin's node. Bootstrap mints one invite, so the lead joins
        once per class and the membership file is copied to later tests."""
        lead = self.node("lead")
        cls = type(self)
        with lead.active():
            if not getattr(cls, "_lead_membership", None):
                org_client.join("http://org.test", self.lead_invite, "node-lead")
                cls._lead_membership = org_client.membership()
                # a team scope under the root, external_ref for target hints
                from org_coordinator.records import service
                p = service.authenticate(self.db, org_client._access_token())
                cls.team = service.create_scope(
                    self.db, p, self.org_id, kind="team", name="Sales",
                    parent_id=self.root, external_ref="hubspot:team/9")["id"]
            else:
                org_client._save(dict(cls._lead_membership))
            org_client.heartbeat()
        return lead

    def _capture_promise(self, node: Node, text="Send Acme the order form",
                         span="I'll send Acme the order form today",
                         t: float | None = None) -> int:
        eid = node.store.insert(Event(time=t or time.time(),
                                      modality=Modality.AUDIO, raw=span,
                                      source="audio.mic",
                                      meta={"meeting_session_id": 3}))
        cid = node.store.add_fact_candidate(
            turn_hash=f"t{time.time_ns()}", kind="commitment",
            payload={"form": "promise", "text": text, "topic": "",
                     "from_person": "me", "to_person": "Acme",
                     "due": "", "confidence": 0.92, "source_span": span,
                     "assertion": "asserted"},
            source_span=span, speaker="me", confidence=0.92,
            prompt_version="extract-v3", schema_version="facts-schema-v3",
            source_event_id=eid)
        node.store.set_fact_candidate_status(cid, "accepted")
        return eid

    def test_capture_to_record_with_one_human_approval(self):
        lead = self._lead_node()
        with lead.active():
            eid = self._capture_promise(lead)
            claim_builder.run_once(lead.store)
            [claim] = node_store.list_claims(lead.store, status=None)
            self.assertEqual(claim["proposed_scope"], self.team)
            minted = promotion.propose(lead.store, claim["id"],
                                       scope_id=self.team)
            out = promotion.decide(lead.store, minted["packet_id"], "approve",
                                   minted["payload_hash"],
                                   source=promotion.LIVE_SESSION,
                                   approved_via="button")
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["claim_status"], "recorded")
            member = org_client.member_id()
            rec = org_client.current_record(self.team, f"member:{member}",
                                            "owes")
            self.assertEqual(rec["value_json"]["text"],
                             "Send Acme the order form")
            prov = org_client._call(
                "GET", f"/records/{out['record_id']}/provenance")
            self.assertEqual(prov["packet"]["payload_hash"],
                             minted["payload_hash"])
            self.assertEqual(prov["evidence"][0]["event_ref"], eid)
            self.assertEqual(prov["evidence"][0]["node_id"], "node-lead")
            self.assertIsNone(prov["evidence"][0]["quote"])
            self.assertEqual(prov["claim"]["id"], claim["id"])

            # same packet again -> same version, nothing new written
            packet = node_store.get_packet(lead.store, minted["packet_id"])
            again = org_client.submit_packet(promotion.submission_body(packet))
            self.assertTrue(again["idempotent"])
            self.assertEqual(again["record_version_id"], out["record_version_id"])

            # the capture expires on the node; the org record survives with
            # its evidence marked expired
            retention.sweep(lead.store, now=time.time() + 31 * DAY,
                            run_mode="enforce")
            self.assertIsNotNone(retention.tombstone(lead.store, eid))
            counts = org_client.drain_outbox(lead.store,
                                             now=time.time() + 31 * DAY)
            self.assertEqual(counts["delivered"], 1)
            prov = org_client._call(
                "GET", f"/records/{out['record_id']}/provenance")
            self.assertEqual(prov["evidence"][0]["evidence_status"], "expired")
            self.assertEqual(prov["version"]["value"]["text"],
                             "Send Acme the order form")

    def test_tampered_packet_is_refused_by_the_service(self):
        lead = self._lead_node()
        with lead.active():
            self._capture_promise(lead, text="Ship the pilot kit",
                                  span="I'll ship the pilot kit")
            claim_builder.run_once(lead.store)
            [claim] = node_store.list_claims(lead.store, status=None)
            minted = promotion.propose(lead.store, claim["id"],
                                       scope_id=self.team)
            body = {"packet_id": minted["packet_id"],
                    "payload": dict(minted["payload"],
                                    value={"text": "Ship two kits",
                                           "owner": "me"}),
                    "payload_hash": minted["payload_hash"],
                    "approved_via": "button"}
            with self.assertRaises(org_client.OrgRefused) as cm:
                org_client.submit_packet(body)
            self.assertEqual(cm.exception.code, "payload_hash_mismatch")

    def test_propose_only_member_forwards_and_an_approver_records(self):
        lead = self._lead_node()
        rep = self.node("rep")
        with lead.active():
            inv = org_client._call("POST", f"/orgs/{self.org_id}/invites",
                                   {"email": f"rep{time.time_ns()}@e2e.test"})
            rep_member = inv["member_id"]
            org_client._call("POST", f"/scopes/{self.team}/grants",
                             {"member_id": rep_member,
                              "permission": "propose"})
        with rep.active():
            org_client.join("http://org.test", inv["invite_code"], "node-rep")
            self._capture_promise(rep, text="Send the SOW",
                                  span="I'll send the SOW")
            claim_builder.run_once(rep.store)
            [claim] = node_store.list_claims(rep.store, status=None)
            minted = promotion.propose(rep.store, claim["id"],
                                       scope_id=self.team)
            with self.assertRaises(promotion.PromotionError) as cm:
                promotion.decide(rep.store, minted["packet_id"], "approve",
                                 minted["payload_hash"],
                                 source=promotion.LIVE_SESSION,
                                 approved_via="button")
            self.assertEqual(cm.exception.code, "approver_lacks_grant")
            promotion.forward(rep.store, minted["packet_id"],
                              source=promotion.LIVE_SESSION)
        with lead.active():
            queue = org_client.forwarded_packets()
            [pkt] = [q for q in queue if q["packet_id"] == minted["packet_id"]]
            self.assertNotIn("span", str(pkt["payload"]["evidence"]))
            out = promotion.approve_forwarded(
                pkt, source=promotion.LIVE_SESSION, approved_via="button",
                payload_hash=pkt["payload_hash"])
            self.assertFalse(out["idempotent"])
            prov = org_client._call(
                "GET", f"/records/{out['record_id']}/provenance")
            self.assertEqual(prov["packet"]["approved_by"],
                             org_client.member_id())
            self.assertEqual(prov["packet"]["proposed_by"], rep_member)
