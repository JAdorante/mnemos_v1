"""Records layer Phase 2, end to end: real nodes, real Org Record Service and
worker (Postgres), faked HubSpot.

  capture -> claim -> packet whose hash covers the HubSpot preview -> one
  human approval -> sync worker writes + reads back -> heartbeat tells the
  node "verified"; a field that moved before the write comes back to the
  node for re-approval with a fresh preview; a CRM edit after the write
  becomes a drift claim the approver acknowledges; a teammate's question is
  answered from the record, not from memory; the org team shows up as a
  read-only group.

The spec's Phase 2 gate names synthetic meeting AUDIO and a HubSpot SANDBOX;
this starts at the transcribed event and runs against the in-process fake.
"""
from __future__ import annotations

import base64
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services.records import (claim_builder, claim_schemas, node_store,
                                  org_client, promotion)
from app.services.records.canonical import claim_identity_hash, quote_hash
from org_coordinator.connectors import http
from org_coordinator.records import sync
from tests.connector_fakes import FakeWorld
from tests.records_pg import PgTestCase, make_app
from tests.test_records_e2e import Node


class WriteBackEndToEnd(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from fastapi.testclient import TestClient

        from org_coordinator.records import service
        cls._sk = os.environ.get("QUILL_ORG_SECRETS_KEY")
        os.environ["QUILL_ORG_SECRETS_KEY"] = base64.b64encode(b"e" * 32).decode()
        cls.client = TestClient(make_app(cls.db))

        def transport(method, path, body, headers):
            r = cls.client.request(method, path, json=body, headers=headers)
            return r.status_code, r.json()

        cls.transport = staticmethod(transport)
        boot = service.bootstrap_org(cls.db, name="WB", admin_email="lead@wb.test")
        cls.org_id, cls.root = boot["org_id"], boot["root_scope_id"]
        cls.invite = boot["invite_code"]

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._sk is None:
            os.environ.pop("QUILL_ORG_SECRETS_KEY", None)
        else:
            os.environ["QUILL_ORG_SECRETS_KEY"] = cls._sk
        super().tearDownClass()

    def setUp(self) -> None:
        self._env = {k: os.environ.get(k) for k in
                     ("QUILL_LEARNING", "QUILL_CLAIM_PROPOSE_MIN_CONF")}
        os.environ["QUILL_LEARNING"] = "0"
        os.environ["QUILL_CLAIM_PROPOSE_MIN_CONF"] = "2"
        org_client.set_transport(self.transport)
        self.world = FakeWorld()
        self.world.hubspot.add("deals", "77", dealstage="appointmentscheduled")
        http.set_transport(self.world.transport)
        from app.services import team_layer
        self._teams_dir = tempfile.mkdtemp(prefix="teams-")
        self._teams = patch.object(team_layer, "_teams_path",
                                   lambda: Path(self._teams_dir) / "teams.json")
        self._teams.start()
        self.nodes: list[Node] = []
        self.lead = self._lead()

    def tearDown(self) -> None:
        org_client.set_transport(None)
        http.set_transport(None)
        self._teams.stop()
        shutil.rmtree(self._teams_dir, ignore_errors=True)
        for n in self.nodes:
            n.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _lead(self) -> Node:
        lead = Node("lead")
        self.nodes.append(lead)
        cls = type(self)
        with lead.active():
            if not getattr(cls, "_membership", None):
                org_client.join("http://org.test", self.invite, "node-lead",
                                store=lead.store)
                cls._membership = org_client.membership()
                c = org_client._call
                cls.deal = c("POST", f"/orgs/{self.org_id}/scopes", {
                    "kind": "team", "name": "Acme deal team",
                    "parent_id": self.root,
                    "external_ref": "hubspot:deal/77"})["id"]
                hs = c("POST", f"/orgs/{self.org_id}/connectors", {
                    "kind": "hubspot", "name": "HubSpot",
                    "secret": {"token": "hs-token"}})["id"]
                c("POST", f"/connectors/{hs}/mappings", {
                    "kind": "commitment", "predicate": "owes",
                    "op": "create_note"})
                c("POST", f"/connectors/{hs}/mappings", {
                    "kind": "status", "predicate": "deal.stage",
                    "op": "set_property", "field": "dealstage",
                    "value_path": "value.state"})
            else:
                org_client._save(dict(cls._membership))
            org_client.heartbeat(lead.store)
        return lead

    def approve(self, store, minted):
        return promotion.decide(store, minted["packet_id"], "approve",
                                minted["payload_hash"],
                                source=promotion.LIVE_SESSION,
                                approved_via="button")

    def stage_claim(self, store, state="closedwon"):
        """A status claim as the builder will make once the extractor asks for
        status kinds (it does not yet) — inserted directly, same rules."""
        span = f"we just moved Acme to {state}"
        eid = store.insert(Event(time=time.time(), modality=Modality.AUDIO,
                                 raw=span, source="audio.mic",
                                 meta={"meeting_session_id": 1}))
        value = {"state": state}
        return node_store.insert_claim(store, {
            "kind": "status", "subject_ref": "entity:acme",
            "subject_label": "Acme", "predicate": "deal.stage",
            "value": value,
            "schema_version": claim_schemas.schema_version("status"),
            "canonical_hash": claim_identity_hash("status", "entity:acme",
                                                  "deal.stage", value),
            "confidence": 0.9,
            "evidence": [{"event_id": eid, "span": span,
                          "quote_hash": quote_hash(span), "speaker": "me",
                          "t": time.time(), "source": "meeting",
                          "status": "live"}],
            "privacy_class": "internal", "capture_source": "meeting",
            "proposed_scope": self.deal, "status": "draft"})

    # ------------------------------------------------------------------
    def test_meeting_promise_becomes_a_verified_hubspot_note(self):
        with self.lead.active():
            st = self.lead.store
            span = "I'll send Acme the revised order form by Friday"
            eid = st.insert(Event(time=time.time(), modality=Modality.AUDIO,
                                  raw=span, source="audio.mic",
                                  meta={"meeting_session_id": 4}))
            cid = st.add_fact_candidate(
                turn_hash="t-wb", kind="commitment",
                payload={"form": "promise", "text": "Send Acme the revised "
                         "order form", "topic": "", "from_person": "me",
                         "to_person": "Acme", "due": "2026-10-09",
                         "confidence": 0.9, "source_span": span,
                         "assertion": "asserted"},
                source_span=span, speaker="me", confidence=0.9,
                prompt_version="extract-v3", schema_version="facts-schema-v3",
                source_event_id=eid)
            st.set_fact_candidate_status(cid, "accepted")
            claim_builder.run_once(st)
            [claim] = node_store.list_claims(st, status=None)
            self.assertEqual(claim["proposed_scope"], self.deal)
            minted = promotion.propose(st, claim["id"], scope_id=self.deal)
            [pv] = minted["payload"]["preview"]
            self.assertEqual((pv["op"], pv["target"]),
                             ("create_note", "hubspot:deals/77"))
            out = self.approve(st, minted)            # the one human approval
            self.assertEqual(out["claim_status"], "recorded")
            self.assertEqual(self.world.hubspot.created["notes"], {})
            self.assertEqual(sync.drain(self.db), {"verified": 1})
            [note] = self.world.hubspot.created["notes"].values()
            self.assertEqual(note["properties"]["hs_note_body"], pv["text"])
            rec = org_client.heartbeat(st)["reconciled"]
            self.assertEqual(rec["verified"], 1)
            self.assertEqual(node_store.get_claim(st, claim["id"])["sync_state"],
                             "verified")

    def test_moved_field_comes_back_for_reapproval_with_a_fresh_preview(self):
        with self.lead.active():
            st = self.lead.store
            cid = self.stage_claim(st)
            first = promotion.propose(st, cid, scope_id=self.deal)
            self.assertEqual(first["payload"]["preview"][0]["diff"][0]["before"],
                             "appointmentscheduled")
            self.approve(st, first)
            self.world.hubspot.edit("deals", "77", dealstage="contractsent")
            self.assertEqual(sync.drain(self.db), {"conflict": 1})
            org_client.heartbeat(st)
            claim = node_store.get_claim(st, cid)
            self.assertEqual(claim["status"], "proposed")
            self.assertEqual(claim["sync_state"], "conflict")
            fresh = node_store.open_packet_for_claim(st, cid)
            self.assertNotEqual(fresh["payload_hash"], first["payload_hash"])
            self.assertEqual(fresh["payload"]["preview"][0]["diff"][0]["before"],
                             "contractsent")
            self.assertEqual(self.world.hubspot.patches, [])   # never overwrote
            out = self.approve(st, {"packet_id": fresh["id"],
                                    "payload_hash": fresh["payload_hash"]})
            self.assertEqual(out["claim_status"], "recorded")
            self.assertEqual(sync.drain(self.db), {"verified": 1})
            self.assertEqual(self.world.hubspot.objects[("deals", "77")]
                             ["properties"]["dealstage"], "closedwon")

    def test_preview_stale_at_submission_remints(self):
        with self.lead.active():
            st = self.lead.store
            cid = self.stage_claim(st, "negotiation")
            org_client.set_transport(None)                # service unreachable
            with patch.object(org_client, "_call",
                              side_effect=org_client.OrgUnavailable("down")):
                minted = promotion.propose(st, cid, scope_id=self.deal)
            org_client.set_transport(self.transport)
            self.assertIsNone(minted["payload"]["preview"])
            out = self.approve(st, minted)
            self.assertEqual(out["code"], "preview_stale")
            claim = node_store.get_claim(st, cid)
            self.assertEqual(claim["status"], "proposed")
            fresh = node_store.open_packet_for_claim(st, cid)
            self.assertTrue(fresh["payload"]["preview"])

    def test_crm_edit_becomes_a_drift_claim_the_approver_acknowledges(self):
        with self.lead.active():
            st = self.lead.store
            cid = self.stage_claim(st, "closedwon")
            self.approve(st, promotion.propose(st, cid, scope_id=self.deal))
            sync.drain(self.db)
            self.world.hubspot.edit("deals", "77", dealstage="closedlost")
            self.assertEqual(sync.drift_sweep(self.db, self.org_id)["drift"], 1)
            rec = org_client.heartbeat(st)["reconciled"]
            self.assertEqual(rec["drift_claims"], 1)
            [dc] = [c for c in node_store.list_claims(st, status=None)
                    if (c.get("origin_ref") or "").startswith("drift:")]
            self.assertEqual(dc["status"], "proposed")
            self.assertIn("'closedlost'", dc["value"]["text"])
            packet = node_store.open_packet_for_claim(st, dc["id"])
            self.assertEqual(packet["payload"]["resolves_drift"],
                             [dc["origin_ref"][6:]])
            self.approve(st, {"packet_id": packet["id"],
                              "payload_hash": packet["payload_hash"]})
            self.assertEqual(org_client.heartbeat(st)["drift"], [])
            # acknowledged, and still never overwritten
            self.assertEqual(self.world.hubspot.objects[("deals", "77")]
                             ["properties"]["dealstage"], "closedlost")
            # a second heartbeat does not make a second drift claim
            org_client.heartbeat(st)
            self.assertEqual(len([c for c in node_store.list_claims(
                st, status=None) if c.get("origin_ref")]), 1)

    def test_teammate_question_is_answered_from_the_record(self):
        from app.services import peer_channel, team_layer
        with self.lead.active():
            st = self.lead.store
            inv = org_client._call("POST", f"/orgs/{self.org_id}/invites",
                                   {"email": f"rep{time.time_ns()}@wb.test"})
            org_client._call("POST", f"/scopes/{self.deal}/grants",
                             {"member_id": inv["member_id"],
                              "permission": "propose"})
        rep = Node("rep")
        self.nodes.append(rep)
        with rep.active(), patch.object(peer_channel, "my_base_url",
                                        return_value="https://rep.example"):
            org_client.join("http://org.test", inv["invite_code"], "node-rep",
                            store=rep.store)
        with self.lead.active():
            st = self.lead.store
            cid = self.stage_claim(st, "negotiation")
            self.approve(st, promotion.propose(st, cid, scope_id=self.deal))
            with patch.object(peer_channel, "peers", return_value=[
                    {"peer_id": "peer-rep", "base_url": "https://rep.example/"}]):
                org_client.heartbeat(st)
            team = team_layer.get_team("org-acme-deal-team")
            self.assertEqual((team["managed_by"], team["peer_ids"]),
                             ("org", ["peer-rep"]))
            self.assertFalse(team_layer.set_team_members(team["slug"], [])["ok"])
            with patch.object(peer_channel, "compose_peer_claims",
                              side_effect=AssertionError("memory consulted")):
                ans = peer_channel.compose_answer(
                    "what stage is the Acme deal in?",
                    peer={"peer_id": "peer-rep",
                          "base_url": "https://rep.example"})
            self.assertEqual(ans["source"], "records")
            self.assertIn("negotiation", ans["text"])
            self.assertTrue(ans["claims"][0]["record_id"])
            # a stranger (no roster match) gets the memory path, not records
            with patch.object(peer_channel, "compose_peer_claims",
                              return_value={"claims": [], "prose": "",
                                            "as_of": None, "near_miss": False,
                                            "redacted": []}):
                other = peer_channel.compose_answer(
                    "what stage is the Acme deal in?",
                    peer={"peer_id": "x", "base_url": "https://stranger"})
            self.assertNotEqual(other.get("source"), "records")
