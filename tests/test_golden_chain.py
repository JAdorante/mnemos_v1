"""The end-to-end golden test: one moment, every subsystem, still tied
together.

"I still owe Dana the pricing follow-up — Marc said forty-nine a month."

1. The utterance is captured.
2. Extraction produces a commitment claim with its verbatim quote.
3. The user approves it: a decision and a learning pair.
4. The claim is promoted to an org record (real Org Record Service).
5. An email is drafted and approved: a decision and a learning pair.
6. The commitment closes on verified evidence.

Run this after every consolidation refactor. It needs the records Postgres
(QUILL_ORG_TEST_DATABASE_URL, see tests/records_pg.py) and skips without it.
What it stubs: the extraction model (its JSON is fixed below) and the
draft's wording (the packet's fields are given, not generated). Everything
between them runs for real.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import decisions, learning_store
from app.services.consolidation import Turn
from app.services.records import claim_builder, node_store, org_client, promotion
from tests.records_pg import PgTestCase, make_app
from tests.test_records_e2e import Node

UTTERANCE = ("I still owe Dana the pricing follow-up. "
             "Marc said forty-nine a month.")
SPAN = "I still owe Dana the pricing follow-up"
FACTS = {
    "tasks": [], "claims": [], "entities": [], "relations": [],
    "commitments": [{
        "text": "send Dana the pricing follow-up",
        "from_person": "me", "to_person": "Dana", "due": "",
        "confidence": 0.9, "source_span": SPAN,
        "assertion": "stated_by_user",
    }],
}

_ENV = {"QUILL_LEARNING": "1", "QUILL_EXEMPLARS": "0",
        "QUILL_FLEET_EXEMPLARS_OFFER": "0", "QUILL_CLAIM_PROPOSE_MIN_CONF": "2",
        "QUILL_CAPTURE_TTL_DAYS": "30", "QUILL_EGRESS_RUNG": "connected",
        "QUILL_COMMIT_RESOLVE": "1"}


class GoldenChain(PgTestCase):
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
        boot = service.bootstrap_org(cls.db, name="Golden",
                                     admin_email="me@golden.test")
        cls.org_id, cls.root = boot["org_id"], boot["root_scope_id"]
        cls.invite = boot["invite_code"]

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in _ENV}
        os.environ.update(_ENV)
        org_client.set_transport(self.transport)
        from app.services import team_layer
        from app.services.model_log import model_log
        self.tmp = tempfile.mkdtemp(prefix="golden-")
        self._patches = [
            patch.object(team_layer, "_teams_path",
                         lambda: Path(self.tmp) / "peer_teams.json"),
            patch.object(model_log, "_path",
                         Path(self.tmp) / "model_calls.jsonl"),
        ]
        for p in self._patches:
            p.start()
        self.node = Node("golden")

    def tearDown(self) -> None:
        org_client.set_transport(None)
        for p in reversed(self._patches):
            p.stop()
        self.node.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # -- steps ---------------------------------------------------------------
    def capture(self, store) -> int:
        return store.insert(Event(time=time.time(), modality=Modality.AUDIO,
                                  raw=UTTERANCE, source="audio.mic",
                                  meta={"meeting_session_id": 1}))

    def extract(self, store, eid: int) -> int:
        from app.services.extractor import Extractor
        from app.services.people_pipeline import ResolveResult
        ex = Extractor(store=store)
        turn = Turn(start=time.time() - 1, end=time.time(), speaker="me",
                    text=UTTERANCE, event_ids=[eid], n_utterances=1)
        with patch.object(ex, "_resolve_person_ref",
                          return_value=ResolveResult(None, "leave_open")), \
                patch.object(ex, "_persist_entities", return_value=(0, 0)), \
                patch.object(ex, "_record_faithfulness", return_value=None), \
                patch("app.services.extractor._index_fact",
                      lambda *a, **k: None), \
                patch("app.services.task_offer.offer_task",
                      lambda *a, **k: None), \
                patch("app.services.fact_gate._similar_active",
                      return_value=[]):
            ex._persist(turn, FACTS, time.time())
        with store._lock:
            row = store._conn.execute(
                "SELECT id FROM facts WHERE kind='commitment' "
                "AND source_event_id=?", (eid,)).fetchone()
        self.assertIsNotNone(row, "extraction wrote no commitment")
        return int(row["id"])

    def join_org(self) -> str:
        org_client.join("http://org.test", self.invite, "node-golden")
        from org_coordinator.records import service
        p = service.authenticate(self.db, org_client._access_token())
        team = service.create_scope(self.db, p, self.org_id, kind="team",
                                    name="Sales", parent_id=self.root)["id"]
        org_client.heartbeat()
        return team

    # -- the chain -----------------------------------------------------------
    def test_one_moment_end_to_end(self):
        store = self.node.store
        with self.node.active():
            team = self.join_org()

            # 1. captured
            eid = self.capture(store)

            # 2. extracted: a commitment fact, and a claim with its quote
            fid = self.extract(store, eid)
            fact = store.get_fact(fid)
            self.assertEqual(fact["source_span"], SPAN)
            claim_builder.run_once(store)
            [claim] = node_store.list_claims(store, status=None)
            self.assertEqual(claim["kind"], "commitment")
            self.assertEqual(claim["evidence"][0]["span"], SPAN)
            self.assertEqual(claim["evidence"][0]["event_id"], eid)

            # 3. approved: one decision, one pair derived from it
            store.review_fact(fid, "approved")
            pid = learning_store.record_fact_verdict(fact, "accepted",
                                                     store=store)
            self.assertTrue(pid, "fact approval produced no learning pair")
            [d_fact] = decisions.recent(subject=f"fact:{fid}", store=store)
            self.assertEqual(d_fact["verdict"], "approved")
            self.assertEqual(d_fact["learning_pair_id"], pid)
            pair = store.get_learning_pair(pid)
            self.assertEqual(pair["source_refs"]["fact_id"], fid)
            self.assertEqual(pair["source_refs"]["decision_id"], d_fact["id"])

            # 4. promoted to an org record by one human approval
            minted = promotion.propose(store, claim["id"], scope_id=team)
            out = promotion.decide(store, minted["packet_id"], "approve",
                                   minted["payload_hash"],
                                   source=promotion.LIVE_SESSION,
                                   approved_via="button")
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["claim_status"], "recorded")
            prov = org_client._call(
                "GET", f"/records/{out['record_id']}/provenance")
            self.assertEqual(prov["evidence"][0]["event_ref"], eid)
            [d_rec] = [d for d in decisions.recent(surface="records.review",
                                                   store=store)
                       if d["verdict"] == "approved"]
            self.assertEqual(d_rec["shown_hash"], minted["payload_hash"])
            self.assertTrue(d_rec["learning_pair_id"])

            # 5. drafted and approved, citing the commitment
            from app.services.agent_log import Recorder
            rec = Recorder(store=store)
            rec.start_run("email Dana the pricing follow-up",
                          source_fact_ids=[fid])
            pkt = rec.record_packet(
                summary="Send email to Dana",
                goal="email Dana the pricing follow-up",
                fields={"to": "dana@example.com",
                        "subject": "Pricing follow-up",
                        "body": "Hi Dana, as promised: it is $49 a month."},
                source_fact_ids=[fid])
            rec.record_decision(pkt, "approve", approved_via="button")
            [d_pkt] = decisions.recent(subject=f"packet:{pkt}", store=store)
            self.assertEqual((d_pkt["surface"], d_pkt["verdict"]),
                             ("agent.packet", "approved"))
            draft_pair = store.get_learning_pair(d_pkt["learning_pair_id"])
            self.assertEqual(draft_pair["task_type"], "agent.draft")

            # 6. closed on verified evidence, not on the agent's say-so
            from app.services import commitment_complete, commitment_state
            from app.services.outcome_verify import verify_email_sent
            ev = verify_email_sent(page_text="Message sent. "
                                             "Moved to Sent Items.")
            self.assertEqual(ev.status, "verified")
            sent = store.insert(Event(
                time=time.time(), modality=Modality.SYSTEM,
                raw="Message sent to Dana: Pricing follow-up",
                source="agent.verify", meta={"packet_id": pkt}))
            closed = commitment_complete.complete_from_verified_send(
                [fid], packet_id=pkt, evidence_event_id=sent, store=store)
            self.assertEqual(len(closed), 1)
            last = store.last_transition(fid)
            self.assertEqual(last["to_state"], "completed")
            self.assertTrue(commitment_state.done_is_attributable(last))

        # Across the chain: every human verdict is in one trail and fed the
        # learning loop; the record that left the node is in the egress view.
        s = decisions.summary(store=store)["surfaces"]
        for surface in ("facts.review", "records.review", "agent.packet"):
            self.assertGreaterEqual(s[surface]["learned"], 1, surface)
        trail = [json.loads(ln) for ln in
                 (Path(self.tmp) / "model_calls.jsonl").read_text()
                 .splitlines()]
        self.assertIn("egress.records", {r["task"] for r in trail})
