"""Records layer — node side: claims, promotion packets, live-session approval,
retention expiry. No Postgres; the org service is a fake transport here (the
real service is exercised in test_records_org_service / test_records_e2e).
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app.config
from app.events import Event, Modality
from app.services.records import (claim_builder, node_store, org_client,
                                  promotion, retention)
from app.services.records.canonical import canonical_hash
from app.storage import Store

DAY = 86400.0
_ENV_KEYS = ("QUILL_RECORDS", "QUILL_CAPTURE_EXPIRY", "QUILL_CAPTURE_TTL_DAYS",
             "QUILL_CLAIM_PROPOSE_MIN_CONF", "QUILL_LEARNING")


class FakeOrg:
    """Stands in for the Org Record Service over org_client's transport seam."""

    def __init__(self) -> None:
        self.mode = "ok"            # ok | down | refuse
        self.refuse_code = "approver_lacks_grant"
        self.submitted: list[dict] = []
        self.versions: dict[str, str] = {}
        self.preview: list[dict] = []

    def __call__(self, method, path, body, headers):
        if self.mode == "down":
            raise org_client.OrgUnavailable("connection refused")
        if path == "/auth/token":
            return 200, {"access_token": "tok", "expires_at": time.time() + 900}
        if path == "/packets" and method == "POST":
            if self.mode == "refuse":
                return 403, {"detail": {"code": self.refuse_code,
                                        "message": "no"}}
            self.submitted.append(body)
            h = body["payload_hash"]
            if h in self.versions:
                return 200, {"record_id": "rec_1",
                             "record_version_id": self.versions[h],
                             "version": 1, "idempotent": True}
            self.versions[h] = f"rv_{len(self.versions) + 1}"
            return 200, {"record_id": "rec_1",
                         "record_version_id": self.versions[h],
                         "version": 1, "idempotent": False}
        if path == "/packets/preview":
            return 200, {"preview": list(self.preview)}
        if path.startswith("/records"):
            return 200, {"records": []}
        if path == "/evidence/expired":
            return 200, {"ok": True, "marked": 0}
        return 404, {"detail": {"code": "not_found"}}


class RecordsNodeBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "quill.db",
                           audio_dir=Path(self.tmp) / "audio")
        self.store.audio_dir.mkdir(parents=True, exist_ok=True)
        real = app.config.settings
        patched = dataclasses.replace(
            real, storage=dataclasses.replace(real.storage, data_dir=self.tmp))
        self._settings = patch.object(app.config, "settings", patched)
        self._settings.start()
        self._env = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["QUILL_LEARNING"] = "0"
        os.environ["QUILL_CLAIM_PROPOSE_MIN_CONF"] = "2"   # no auto-propose
        self.org = FakeOrg()
        org_client.set_transport(self.org)
        # team_layer reads its own module settings: pin its registry here so
        # an org team sync can never touch the real data/peer_teams.json.
        from app.services import team_layer
        self._teams = patch.object(team_layer, "_teams_path",
                                   lambda: Path(self.tmp) / "peer_teams.json")
        self._teams.start()

    def tearDown(self) -> None:
        org_client.set_transport(None)
        self._teams.stop()
        self._settings.stop()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            self.store._conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers --
    def event(self, raw: str, *, source: str = "audio.mic", t: float | None = None,
              meta: dict | None = None, modality=Modality.AUDIO) -> int:
        meta = dict(meta if meta is not None else {"meeting_session_id": 7})
        return self.store.insert(Event(time=t or time.time(), modality=modality,
                                       raw=raw, source=source, meta=meta))

    def candidate(self, kind: str, payload: dict, *, event_id: int,
                  span: str | None = None, speaker: str = "me",
                  conf: float = 0.9, status: str = "accepted") -> int:
        cid = self.store.add_fact_candidate(
            turn_hash=f"t{time.time_ns()}", kind=kind, payload=payload,
            source_span=span if span is not None else payload.get("source_span", ""),
            speaker=speaker, confidence=conf, prompt_version="extract-v3",
            schema_version="facts-schema-v3", source_event_id=event_id)
        self.store.set_fact_candidate_status(cid, status)
        return cid

    def promise(self, text="Send Sam the deck", owner="me", to="Sam",
                span="I'll send Sam the deck tomorrow", **kw) -> int:
        eid = kw.pop("event_id", None) or self.event(span)
        return self.candidate("commitment", {
            "form": "promise", "text": text, "topic": "", "from_person": owner,
            "to_person": to, "due": "2026-10-09", "confidence": 0.9,
            "source_span": span, "assertion": "asserted"}, event_id=eid,
            span=span, **kw)

    def price(self, value="$49", subject="Pro plan", span=None, **kw) -> int:
        span = span or f"the Pro plan is {value} a month"
        eid = kw.pop("event_id", None) or self.event(span)
        return self.candidate("claim", {
            "text": f"Pro plan costs {value}", "confidence": 0.9,
            "source_span": span, "assertion": "asserted", "subject": subject,
            "predicate": "priced_at", "object": value}, event_id=eid,
            span=span, **kw)

    def join(self, perms=("read", "propose", "approve")) -> None:
        org_client._save({"service_url": "http://org.test", "org_id": "org_a",
                          "member_id": "mem_1", "node_id": "node-1",
                          "credential": "org_a.crd_1.secret",
                          "scopes": [{"id": "scp_team", "name": "Sales",
                                      "kind": "team", "parent_id": None,
                                      "external_ref": "hubspot:team/1",
                                      "permissions": list(perms)}],
                          "policy": {}})

    def build(self) -> dict:
        return claim_builder.run_once(self.store)

    def only_claim(self) -> dict:
        rows = node_store.list_claims(self.store, status=None)
        self.assertEqual(len(rows), 1, rows)
        return rows[0]


class MigrationTests(RecordsNodeBase):
    def test_events_get_policy_columns_and_expiry_stamp(self):
        os.environ["QUILL_CAPTURE_TTL_DAYS"] = "30"
        t = time.time()
        eid = self.event("hello", t=t)
        row = self.store._conn.execute(
            "SELECT privacy_class, expires_at, hold_ids, consent_mode "
            "FROM events WHERE id = ?", (eid,)).fetchone()
        self.assertAlmostEqual(row["expires_at"], t + 30 * DAY, places=3)
        self.assertTrue(row["privacy_class"])
        self.assertIsNone(row["hold_ids"])

    def test_ttl_is_clamped_to_the_allowed_range(self):
        os.environ["QUILL_CAPTURE_TTL_DAYS"] = "2"
        self.assertEqual(retention.policy()["capture_ttl_days"], 7.0)
        os.environ["QUILL_CAPTURE_TTL_DAYS"] = "9999"
        self.assertEqual(retention.policy()["capture_ttl_days"], 365.0)

    def test_migration_is_idempotent(self):
        self.store._migrate_records()
        self.store._migrate_records()
        cols = {r["name"] for r in self.store._conn.execute(
            "PRAGMA table_info(claims)").fetchall()}
        self.assertIn("capture_source", cols)
        kcols = {r["name"] for r in self.store._conn.execute(
            "PRAGMA table_info(kg_predicates)").fetchall()}
        self.assertTrue({"recorded_at", "superseded_at"} <= kcols)


class ClaimBuilderTests(RecordsNodeBase):
    def test_promise_becomes_commitment_claim_with_evidence(self):
        self.promise()
        out = self.build()
        self.assertEqual(out["outcomes"], {"created": 1})
        c = self.only_claim()
        self.assertEqual((c["kind"], c["predicate"], c["subject_ref"]),
                         ("commitment", "owes", "self"))
        self.assertEqual(c["value"]["counterparty"], "Sam")
        self.assertEqual(c["capture_source"], "meeting")
        self.assertEqual(len(c["evidence"]), 1)
        ev = c["evidence"][0]
        self.assertEqual(ev["span"], "I'll send Sam the deck tomorrow")
        self.assertEqual(len(ev["quote_hash"]), 64)
        self.assertEqual(c["status"], "draft")

    def test_rebuild_is_a_noop(self):
        self.promise()
        self.build()
        self.assertEqual(self.build()["scanned"], 0)

    def test_claim_without_evidence_is_rejected(self):
        eid = self.event("x")
        self.candidate("commitment", {"form": "promise", "text": "Do it",
                                      "from_person": "me", "to_person": ""},
                       event_id=eid, span="")
        self.assertEqual(self.build()["outcomes"], {"no_evidence": 1})
        self.assertEqual(node_store.list_claims(self.store, status=None), [])

    def test_unpromotable_kinds_are_skipped(self):
        eid = self.event("meet andy at 8:30")
        self.candidate("commitment", {"form": "meeting", "text": "Meet Andy",
                                      "from_person": "me", "to_person": "Andy"},
                       event_id=eid, span="meet andy at 8:30")
        self.candidate("claim", {"text": "it's nice out", "subject": "",
                                 "predicate": "", "object": ""},
                       event_id=eid, span="meet andy at 8:30")
        self.assertEqual(self.build()["outcomes"], {"not_promotable": 2})

    def test_only_accepted_candidates_are_read(self):
        self.promise(status="dropped")
        self.assertEqual(self.build()["scanned"], 0)

    def test_same_fact_twice_merges_evidence(self):
        self.price("$49")
        self.price("$49", span="yeah Pro is $49 per month")
        self.build()
        c = self.only_claim()
        self.assertEqual(len(c["evidence"]), 2)

    def test_conflicting_values_surface_together(self):
        self.price("$49")
        self.price("$59")
        self.build()
        rows = node_store.list_claims(self.store, status=None)
        self.assertEqual({r["status"] for r in rows}, {"conflicting"})
        a, b = rows
        self.assertEqual(a["conflict_with"], b["id"])
        self.assertEqual(b["conflict_with"], a["id"])

    def test_two_different_promises_are_two_claims_not_a_conflict(self):
        self.promise(text="Send Sam the deck")
        self.promise(text="Book the venue", span="I'll book the venue")
        self.build()
        rows = node_store.list_claims(self.store, status=None)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["status"] for r in rows}, {"draft"})

    def test_sensitive_stays_personal(self):
        eid = self.event("I'll send Sam the deck tomorrow",
                         meta={"meeting_session_id": 7,
                               "privacy_class": "sensitive"})
        self.promise(event_id=eid)
        self.build()
        c = self.only_claim()
        self.assertTrue(c["personal_only"])
        self.assertIn("sensitive", c["status_reason"])

    def test_ambient_claim_is_personal_unless_owner_spoke(self):
        e1 = self.event("I'll send Sam the deck tomorrow", meta={})
        self.promise(event_id=e1, speaker="me")
        e2 = self.event("Dana will book the venue", meta={})
        self.promise(event_id=e2, owner="Dana", text="Book the venue",
                     span="Dana will book the venue", speaker="SPEAKER_2")
        self.build()
        by_owner = {r["value"]["owner"]: r for r in
                    node_store.list_claims(self.store, status=None)}
        self.assertFalse(by_owner["me"]["personal_only"])
        self.assertTrue(by_owner["Dana"]["personal_only"])
        self.assertEqual(by_owner["Dana"]["capture_source"], "ambient")

    def test_screen_evidence_only_proposes_status_or_field_update(self):
        eid = self.event("Pro plan $49", source="desktop.screen", meta={},
                         modality=Modality.VISION)
        self.price(event_id=eid, span="Pro plan $49")
        self.build()
        c = self.only_claim()
        self.assertEqual(c["capture_source"], "screen")
        self.assertTrue(c["personal_only"])

    def test_scope_suggestion_from_single_usable_scope(self):
        self.join()
        self.promise()
        self.build()
        c = self.only_claim()
        self.assertEqual(c["proposed_scope"], "scp_team")
        self.assertEqual(c["target_hint"], "hubspot:team/1")

    def test_auto_propose_above_threshold(self):
        self.join()
        os.environ["QUILL_CLAIM_PROPOSE_MIN_CONF"] = "0.5"
        self.promise()
        self.build()
        c = self.only_claim()
        self.assertEqual(c["status"], "proposed")
        self.assertIsNotNone(node_store.open_packet_for_claim(self.store, c["id"]))

    def test_unpromoted_claims_expire_at_claim_ttl(self):
        self.promise()
        self.build()
        c = self.only_claim()
        claim_builder.expire_stale(self.store, now=time.time() + 91 * DAY)
        self.assertEqual(node_store.get_claim(self.store, c["id"])["status"],
                         "expired")


class PromotionTests(RecordsNodeBase):
    def setUp(self) -> None:
        super().setUp()
        self.join()
        self.promise()
        self.build()
        self.claim = self.only_claim()

    def mint(self, **kw) -> dict:
        return promotion.propose(self.store, self.claim["id"],
                                 scope_id="scp_team", **kw)

    def approve(self, minted, **kw) -> dict:
        kw.setdefault("source", promotion.LIVE_SESSION)
        kw.setdefault("approved_via", "button")
        return promotion.decide(self.store, minted["packet_id"], "approve",
                                minted["payload_hash"], **kw)

    def test_packet_payload_is_the_exact_record_body(self):
        m = self.mint()
        self.assertEqual(m["payload_hash"], canonical_hash(m["payload"]))
        p = m["payload"]
        self.assertEqual(p["subject_ref"], "member:mem_1")
        self.assertEqual(p["scope_id"], "scp_team")
        self.assertEqual(p["org_id"], "org_a")
        self.assertNotIn("quote", p["evidence"][0])
        self.assertAlmostEqual(p["expires_at"] - p["minted_at"], 7 * DAY)
        self.assertEqual(p["record_key"], self.claim["canonical_hash"])
        stored = node_store.get_packet(self.store, m["packet_id"])
        self.assertEqual(stored["payload_json"],
                         json.dumps(m["payload"], sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=False))

    def test_approve_records_and_is_idempotent(self):
        out = self.approve(self.mint())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["claim_status"], "recorded")
        c = node_store.get_claim(self.store, self.claim["id"])
        self.assertEqual((c["status"], c["record_ref"]), ("recorded", "rv_1"))
        # Resubmitting the same packet gets the same version back.
        packet = node_store.get_packet(self.store, self.org.submitted[0]["packet_id"])
        again = org_client.submit_packet(promotion.submission_body(packet))
        self.assertEqual(again["record_version_id"], "rv_1")
        self.assertTrue(again["idempotent"])

    def test_no_non_human_source_can_approve(self):
        """Phase 1 acceptance: no approval path accepts input from memory,
        peer answers or agent steps (trust.source_can_authorize)."""
        m = self.mint()
        for source in ("memory", "peer.answer", "agent.step", "omi:meeting",
                       "external:web", "exhaust.gmail", "org.digest",
                       "http.unverified", "claim_builder", ""):
            for decision in ("approve", "reject", "edit"):
                with self.assertRaises(promotion.PromotionError) as cm:
                    promotion.decide(self.store, m["packet_id"], decision,
                                     m["payload_hash"], source=source,
                                     approved_via="button")
                self.assertEqual(cm.exception.code, "not_live_session")
        self.assertEqual(self.org.submitted, [])
        self.assertEqual(node_store.get_packet(self.store, m["packet_id"])["state"],
                         "open")

    def test_approved_via_must_be_button_or_typed(self):
        m = self.mint()
        with self.assertRaises(promotion.PromotionError) as cm:
            self.approve(m, approved_via="auto")
        self.assertEqual(cm.exception.code, "bad_approved_via")

    def test_stale_hash_refused(self):
        m = self.mint()
        with self.assertRaises(promotion.PromotionError) as cm:
            promotion.decide(self.store, m["packet_id"], "approve", "0" * 64,
                             source=promotion.LIVE_SESSION, approved_via="button")
        self.assertEqual(cm.exception.code, "payload_hash_mismatch")

    def test_tampered_stored_payload_refused(self):
        m = self.mint()
        p = dict(m["payload"], scope_id="scp_other")
        self.store._conn.execute(
            "UPDATE promotion_packets SET payload_json = ? WHERE id = ?",
            (json.dumps(p), m["packet_id"]))
        self.store._conn.commit()
        with self.assertRaises(promotion.PromotionError) as cm:
            self.approve(m)
        self.assertEqual(cm.exception.code, "payload_hash_mismatch")

    def test_expired_packet_refused(self):
        m = self.mint()
        with self.assertRaises(promotion.PromotionError) as cm:
            self.approve(m, now=time.time() + 8 * DAY)
        self.assertEqual(cm.exception.code, "packet_expired")

    def test_edit_mints_a_new_packet_and_closes_the_old(self):
        m = self.mint()
        new_value = dict(self.claim["value"], due="2026-10-10")
        out = promotion.decide(self.store, m["packet_id"], "edit",
                               m["payload_hash"], source=promotion.LIVE_SESSION,
                               approved_via="button", value=new_value)
        self.assertNotEqual(out["payload_hash"], m["payload_hash"])
        self.assertEqual(node_store.get_packet(self.store, m["packet_id"])["state"],
                         "edited")
        self.assertEqual(node_store.get_claim(self.store, self.claim["id"])["status"],
                         "edited")
        # The old hash can no longer approve anything.
        with self.assertRaises(promotion.PromotionError):
            self.approve(m)
        done = self.approve(out)
        self.assertEqual(done["claim_status"], "recorded")
        self.assertEqual(self.org.submitted[-1]["payload"]["value"]["due"],
                         "2026-10-10")

    def test_edit_validates_against_the_kind_schema(self):
        m = self.mint()
        with self.assertRaises(promotion.PromotionError) as cm:
            promotion.decide(self.store, m["packet_id"], "edit",
                             m["payload_hash"], source=promotion.LIVE_SESSION,
                             approved_via="button", value={"text": ""})
        self.assertEqual(cm.exception.code, "invalid_value")

    def test_reject_feeds_the_learning_loop(self):
        m = self.mint()
        with patch("app.services.learning_store.record") as rec:
            promotion.decide(self.store, m["packet_id"], "reject",
                             m["payload_hash"], source=promotion.LIVE_SESSION,
                             approved_via="button", reason="never said it")
        kw = rec.call_args.kwargs
        self.assertEqual(kw["verdict"], "rejected")
        self.assertEqual(kw["task_type"], "records.claim.commitment")
        self.assertEqual(kw["source_refs"]["reason"], "never said it")
        self.assertEqual(promotion.precision(self.store)["commitment"]["rejected"], 1)

    def test_quote_travels_only_when_allowed(self):
        with self.assertRaises(promotion.PromotionError) as cm:
            self.mint(include_quote=True)
        self.assertEqual(cm.exception.code, "quote_not_allowed")
        org_client._save({**org_client.membership()})
        retention.save_org_policy({"retention": {"quote_in_records": True}})
        m = self.mint(include_quote=True)
        self.assertEqual(m["payload"]["evidence"][0]["quote"],
                         "I'll send Sam the deck tomorrow")

    def test_service_down_queues_then_drains(self):
        self.org.mode = "down"
        out = self.approve(self.mint())
        self.assertTrue(out["queued"])
        self.assertEqual(node_store.get_claim(self.store, self.claim["id"])["status"],
                         "approved")
        self.org.mode = "ok"
        counts = org_client.drain_outbox(self.store, now=time.time() + 3600)
        self.assertEqual(counts["delivered"], 1)
        self.assertEqual(node_store.get_claim(self.store, self.claim["id"])["status"],
                         "recorded")

    def test_queued_packet_past_ttl_fails_instead_of_sending(self):
        self.org.mode = "down"
        self.approve(self.mint())
        self.org.mode = "ok"
        org_client.drain_outbox(self.store, now=time.time() + 8 * DAY)
        self.assertEqual(self.org.submitted, [])
        c = node_store.get_claim(self.store, self.claim["id"])
        self.assertEqual(c["status"], "failed")

    def test_service_refusal_fails_the_claim_with_its_code(self):
        self.org.mode = "refuse"
        out = self.approve(self.mint())
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "approver_lacks_grant")
        c = node_store.get_claim(self.store, self.claim["id"])
        self.assertEqual(c["status"], "failed")

    def test_propose_only_member_cannot_approve_locally(self):
        self.join(perms=("read", "propose"))
        with self.assertRaises(promotion.PromotionError) as cm:
            self.approve(self.mint())
        self.assertEqual(cm.exception.code, "approver_lacks_grant")

    def test_personal_claim_cannot_be_proposed(self):
        node_store.update_claim(self.store, self.claim["id"], personal_only=True)
        with self.assertRaises(promotion.PromotionError) as cm:
            self.mint()
        self.assertEqual(cm.exception.code, "personal_only")

    def test_picking_one_side_of_a_conflict_rejects_the_other(self):
        self.price("$49")
        self.price("$59")
        self.build()
        a, b = node_store.list_claims(self.store, status="conflicting")
        promotion.propose(self.store, a["id"], scope_id="scp_team")
        self.assertEqual(node_store.get_claim(self.store, b["id"])["status"],
                         "rejected")

    def test_node_audit_chain_records_every_step_and_verifies(self):
        self.approve(self.mint())
        actions = [e["action"] for e in node_store.audit_entries(self.store)]
        for a in ("packet.mint", "packet.approve", "packet.recorded"):
            self.assertIn(a, actions)
        self.assertTrue(node_store.verify_audit_chain(self.store)["ok"])
        self.store._conn.execute(
            "UPDATE node_audit_log SET object_ref = 'x' WHERE seq = 1")
        self.store._conn.commit()
        bad = node_store.verify_audit_chain(self.store)
        self.assertEqual((bad["ok"], bad["bad_seq"]), (False, 1))


class LiveSessionRouteTests(RecordsNodeBase):
    """The HTTP layer decides LIVE_SESSION from the request, never the body."""

    def setUp(self) -> None:
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api import records_routes
        self.join()
        self.promise()
        self.build()
        self.claim = self.only_claim()
        self.minted = promotion.propose(self.store, self.claim["id"],
                                        scope_id="scp_team")
        app_ = FastAPI()
        app_.include_router(records_routes.router)
        self._store_patch = patch.object(records_routes, "_store",
                                         lambda: self.store)
        self._store_patch.start()
        self.client = TestClient(app_)

    def tearDown(self) -> None:
        self._store_patch.stop()
        super().tearDown()

    def post_decide(self, *, csrf=True, bearer=False, site=None, **body):
        body = {"decision": "approve",
                "payload_hash": self.minted["payload_hash"],
                "approved_via": "button", **body}
        headers = {}
        cookies = {}
        if csrf:
            cookies["quill_csrf"] = "tok123"
            headers["X-CSRF-Token"] = "tok123"
        if bearer:
            headers["Authorization"] = "Bearer agent-token"
        if site:
            headers["Sec-Fetch-Site"] = site
        self.client.cookies.clear()
        for k, v in cookies.items():
            self.client.cookies.set(k, v)
        return self.client.post(f"/packets/{self.minted['packet_id']}/decide",
                                json=body, headers=headers)

    def test_bearer_caller_cannot_approve(self):
        r = self.post_decide(bearer=True)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "not_live_session")

    def test_missing_csrf_header_cannot_approve(self):
        r = self.post_decide(csrf=False)
        self.assertEqual(r.json()["code"], "not_live_session")

    def test_cross_site_fetch_cannot_approve(self):
        r = self.post_decide(site="cross-site")
        self.assertEqual(r.json()["code"], "not_live_session")

    def test_account_seat_needs_a_signed_in_session(self):
        with patch("app.services.account.exists", return_value=True), \
             patch("app.services.account.session_valid", return_value=False):
            r = self.post_decide()
        self.assertEqual(r.json()["code"], "not_live_session")

    def test_typed_confirmation_must_match_the_hash(self):
        r = self.post_decide(approved_via="typed", confirm="nope")
        self.assertEqual(r.json()["code"], "typed_confirmation_mismatch")

    def test_live_page_request_approves(self):
        with patch("app.services.account.exists", return_value=False):
            r = self.post_decide(approved_via="typed",
                                 confirm=self.minted["payload_hash"][:8])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["claim_status"], "recorded")

    def test_queue_endpoint_groups_by_scope_and_subject(self):
        d = self.client.get("/claims").json()
        self.assertIn("scp_team", d["groups"])
        self.assertEqual(d["claims"][0]["open_packet"]["payload_hash"],
                         self.minted["payload_hash"])


class RetentionTests(RecordsNodeBase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["QUILL_CAPTURE_TTL_DAYS"] = "30"
        self.now = time.time()
        self.old = self.event("old capture", t=self.now - 40 * DAY)
        self.fresh = self.event("fresh capture", t=self.now - 1 * DAY)

    def ids(self) -> set[int]:
        return {r["id"] for r in self.store._conn.execute(
            "SELECT id FROM events").fetchall()}

    def test_dry_run_deletes_nothing_and_receipts(self):
        out = retention.sweep(self.store, now=self.now, run_mode="dry_run")
        self.assertEqual(out["would_expire"]["n_events"], 1)
        self.assertEqual(self.ids(), {self.old, self.fresh})
        rec = node_store.audit_entries(self.store, action="expiry.dry_run")
        self.assertEqual(rec[0]["payload"]["n_events"], 1)

    def test_default_mode_is_dry_run(self):
        os.environ.pop("QUILL_CAPTURE_EXPIRY", None)
        self.assertEqual(retention.mode(), "dry_run")

    def test_enforce_tombstones_and_cascades(self):
        wav = self.store.audio_dir / "old.wav"
        wav.write_bytes(b"RIFF")
        self.store._conn.execute("UPDATE events SET audio_path = ? WHERE id = ?",
                                 (str(wav), self.old))
        self.store._conn.execute(
            "INSERT INTO turns (start, end, speaker, text, event_ids) "
            "VALUES (?,?,?,?,?)", (0, 1, "me", "old capture",
                                   json.dumps([self.old])))
        self.store._conn.commit()
        pid = self.store.upsert_kg_predicate(subj_type="entity", subj_id=1,
                                             predicate="works_at",
                                             obj_type="entity", obj_id=2)
        self.store.add_kg_evidence(pid, event_id=self.old, quote="old capture")
        # a claim citing the old event keeps its pointer, marked expired
        self.promise(event_id=self.old)
        self.build()

        class Vec:
            deleted: list = []

            def delete_ids(self, ids):
                Vec.deleted = list(ids)
                return len(ids)

        out = retention.sweep(self.store, now=self.now, run_mode="enforce",
                              vectors=Vec())
        exp = out["expired"]
        self.assertEqual(exp["n_events"], 1)
        self.assertEqual(self.ids(), {self.fresh})
        self.assertFalse(wav.exists())
        self.assertEqual(Vec.deleted, [self.old])
        self.assertEqual(exp["turns"], 1)
        self.assertEqual(exp["kg_evidence"], 1)
        self.assertEqual(exp["unsupported_predicates"], 1)
        status = self.store._conn.execute(
            "SELECT status FROM kg_predicates WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(status["status"], "unsupported")
        tomb = retention.tombstone(self.store, self.old)
        self.assertEqual(tomb["modality"], "audio")
        self.assertEqual(len(tomb["content_hash"]), 64)
        claim = self.only_claim()
        self.assertEqual(claim["evidence"][0]["status"], "expired")
        self.assertEqual(claim["status"], "draft")         # Tier 2 survives
        receipt = node_store.audit_entries(self.store, action="expiry.receipt")
        self.assertEqual(receipt[0]["payload"]["n_events"], 1)
        self.assertNotIn("old capture", json.dumps(receipt[0]["payload"]))
        outbox = node_store.due_outbox(self.store, now=self.now + 1)
        self.assertEqual(outbox[0]["body"]["event_refs"], [self.old])

    def test_held_rows_survive_expiry(self):
        self.store._conn.execute("UPDATE events SET hold_ids = ? WHERE id = ?",
                                 (json.dumps(["hold_1"]), self.old))
        self.store._conn.commit()
        out = retention.sweep(self.store, now=self.now, run_mode="enforce")
        self.assertEqual(out["expired"]["n_events"], 0)
        self.assertIn(self.old, self.ids())

    def test_pre_migration_rows_never_expire(self):
        self.store._conn.execute(
            "UPDATE events SET expires_at = NULL WHERE id = ?", (self.old,))
        self.store._conn.commit()
        retention.sweep(self.store, now=self.now + 400 * DAY, run_mode="enforce")
        self.assertIn(self.old, self.ids())

    def test_audio_ttl_strips_audio_but_keeps_the_transcript(self):
        wav = self.store.audio_dir / "fresh.wav"
        wav.write_bytes(b"RIFF")
        self.store._conn.execute(
            "UPDATE events SET audio_path = ?, time = ? WHERE id = ?",
            (str(wav), self.now - 8 * DAY, self.fresh))
        self.store._conn.commit()
        out = retention.sweep(self.store, now=self.now, run_mode="enforce")
        self.assertEqual(out["expired"]["audio_stripped_events"], 1)
        self.assertFalse(wav.exists())
        self.assertIn(self.fresh, self.ids())

    def test_files_outside_the_data_dir_are_never_unlinked(self):
        outside = Path(tempfile.mkdtemp()) / "keep.wav"
        outside.write_bytes(b"RIFF")
        try:
            self.store._conn.execute(
                "UPDATE events SET audio_path = ? WHERE id = ?",
                (str(outside), self.old))
            self.store._conn.commit()
            retention.sweep(self.store, now=self.now, run_mode="enforce")
            self.assertTrue(outside.exists())
        finally:
            shutil.rmtree(outside.parent, ignore_errors=True)

    def test_status_reports_upcoming_by_source(self):
        st = retention.status(self.store, now=self.now)
        self.assertEqual(st["mode"], retention.mode())
        self.assertEqual(st["upcoming_by_source"]["meeting"]["24h"], 1)


if __name__ == "__main__":
    unittest.main()
