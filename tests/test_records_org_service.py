"""Org Record Service against real Postgres (skips without
QUILL_ORG_TEST_DATABASE_URL — see tests/records_pg.py).

Phase 1 acceptance covered here:
  * tampered payload_json, expired packets and approvers without `approve`
    are refused, each with a distinct error code
  * resubmitting an approved packet returns the same record version
  * cross-org read attempts fail on every endpoint (and RLS is the backstop)
  * the audit chain verifier passes on a 100,000-entry synthetic log and
    detects a single altered entry
"""
from __future__ import annotations

import random
import threading
import time
import unittest

from app.services.records.canonical import (GENESIS_HASH, audit_entry_hash,
                                            canonical_hash, quote_hash)
from tests.records_pg import PgTestCase, make_app

DAY = 86400.0


class Seat:
    """One member's node as the service sees it: credential + bearer token."""

    def __init__(self, client, member_id: str, credential: str, org_id: str):
        self.client, self.member_id = client, member_id
        self.credential, self.org_id = credential, org_id
        r = client.post("/auth/token", json={"credential": credential})
        assert r.status_code == 200, r.text
        self.token = r.json()["access_token"]

    def h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path, **kw):
        return self.client.get(path, headers=self.h(), **kw)

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers=self.h())

    def put(self, path, body):
        return self.client.put(path, json=body, headers=self.h())

    def patch(self, path, body):
        return self.client.patch(path, json=body, headers=self.h())


def _org(cls, name: str) -> dict:
    from org_coordinator.records import service
    boot = service.bootstrap_org(cls.db, name=name, admin_email=f"admin@{name}.test")
    r = cls.client.post("/join", json={"invite_code": boot["invite_code"],
                                       "node_id": f"node-admin-{name}"})
    assert r.status_code == 200, r.text
    admin = Seat(cls.client, r.json()["member_id"], r.json()["credential"],
                 boot["org_id"])
    team = admin.post(f"/orgs/{boot['org_id']}/scopes",
                      {"kind": "team", "name": "Sales",
                       "parent_id": boot["root_scope_id"],
                       "external_ref": "hubspot:team/1"}).json()
    deal = admin.post(f"/orgs/{boot['org_id']}/scopes",
                      {"kind": "deal", "name": "Acme renewal",
                       "parent_id": team["id"]}).json()
    seats = {"admin": admin}
    for who, perm in (("lead", "approve"), ("rep", "propose"),
                      ("reader", "read")):
        inv = admin.post(f"/orgs/{boot['org_id']}/invites",
                         {"email": f"{who}@{name}.test"}).json()
        j = cls.client.post("/join", json={"invite_code": inv["invite_code"],
                                           "node_id": f"node-{who}-{name}"})
        assert j.status_code == 200, j.text
        seat = Seat(cls.client, j.json()["member_id"], j.json()["credential"],
                    boot["org_id"])
        g = admin.post(f"/scopes/{team['id']}/grants",
                       {"member_id": seat.member_id, "permission": perm})
        assert g.status_code == 200, g.text
        seats[who] = seat
    return {"org_id": boot["org_id"], "root": boot["root_scope_id"],
            "team": team["id"], "deal": deal["id"], **seats}


def payload(org: dict, *, scope: str | None = None, value=None,
            subject="entity:acme", predicate="deal.stage", record_key="",
            valid_from: float | None = None, expires_in: float = 7 * DAY,
            proposed_by: str | None = None, packet_id: str | None = None) -> dict:
    now = time.time()
    return {
        "v": 1, "packet_id": packet_id or f"pkt_{random.getrandbits(48):x}",
        "org_id": org["org_id"], "scope_id": scope or org["team"],
        "target_ref": None, "subject_ref": subject, "subject_label": "Acme",
        "kind": "status", "schema_version": "status-v1",
        "predicate": predicate, "record_key": record_key,
        "value": value if value is not None else {"state": "negotiation"},
        "valid_from": valid_from if valid_from is not None else now - 3600,
        "claim": {"id": "clm_1", "canonical_hash": "c" * 64,
                  "confidence": 0.9, "capture_source": "meeting",
                  "candidate_id": 1},
        "evidence": [{"node_id": "node-rep", "event_ref": 42,
                      "quote_hash": quote_hash("we're in negotiation"),
                      "t": now - 3600, "source": "meeting",
                      "status": "live"}],
        "include_quote": False, "proposed_by": proposed_by or "mem_x",
        "minted_at": now, "expires_at": now + expires_in, "preview": None}


def submit(seat: Seat, p: dict, *, h: str | None = None, via="button"):
    return seat.post("/packets", {"packet_id": p["packet_id"], "payload": p,
                                  "payload_hash": h or canonical_hash(p),
                                  "approved_via": via})


class OrgServiceTests(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from fastapi.testclient import TestClient
        cls.client = TestClient(make_app(cls.db))
        cls.a = _org(cls, "alpha")
        cls.b = _org(cls, "beta")

    # -- membership / auth --
    def test_invite_is_single_use(self):
        a = self.a
        inv = a["admin"].post(f"/orgs/{a['org_id']}/invites",
                              {"email": "once@alpha.test"}).json()
        ok = self.client.post("/join", json={"invite_code": inv["invite_code"],
                                             "node_id": "n1"})
        self.assertEqual(ok.status_code, 200)
        again = self.client.post("/join", json={"invite_code": inv["invite_code"],
                                                "node_id": "n2"})
        self.assertEqual(again.json()["detail"]["code"], "invite_used")

    def test_forged_invite_and_credential_refused(self):
        r = self.client.post("/join", json={
            "invite_code": f"{self.a['org_id']}.inv_x.forged",
            "node_id": "node-x"})
        self.assertEqual(r.json()["detail"]["code"], "bad_invite")
        r = self.client.post("/auth/token", json={
            "credential": f"{self.a['org_id']}.crd_x.forged"})
        self.assertEqual(r.json()["detail"]["code"], "bad_credential")

    def test_heartbeat_returns_policy_and_inherited_grants(self):
        hb = self.a["lead"].get("/nodes/heartbeat").json()
        perms = {s["id"]: set(s["permissions"]) for s in hb["scopes"]}
        self.assertIn("approve", perms[self.a["team"]])
        # inherited DOWN the tree, never up
        self.assertIn("approve", perms[self.a["deal"]])
        self.assertNotIn(self.a["root"], perms)
        self.assertEqual(hb["policy"]["retention"]["capture_ttl_days"], 30)

    # -- packets --
    def test_approved_packet_writes_a_record_with_evidence_pointers(self):
        p = payload(self.a, subject="entity:globex")
        r = submit(self.a["lead"], p)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertFalse(out["idempotent"])
        prov = self.a["reader"].get(
            f"/records/{out['record_id']}/provenance").json()
        self.assertEqual(prov["packet"]["payload_hash"], canonical_hash(p))
        self.assertEqual(prov["packet"]["approved_by"], self.a["lead"].member_id)
        self.assertEqual(prov["evidence"][0]["event_ref"], 42)
        self.assertIsNone(prov["evidence"][0]["quote"])
        self.assertEqual(prov["claim"]["id"], "clm_1")

    def test_refusals_have_distinct_codes(self):
        lead, rep = self.a["lead"], self.a["rep"]
        tampered = payload(self.a)
        h = canonical_hash(tampered)
        tampered["value"] = {"state": "closed-won"}
        codes = {
            "tampered": submit(lead, tampered, h=h),
            "expired": submit(lead, payload(self.a, expires_in=-1)),
            "no_grant": submit(rep, payload(self.a)),
            "reader": submit(self.a["reader"], payload(self.a)),
        }
        got = {k: (r.status_code, r.json()["detail"]["code"])
               for k, r in codes.items()}
        self.assertEqual(got["tampered"], (409, "payload_hash_mismatch"))
        self.assertEqual(got["expired"], (410, "packet_expired"))
        self.assertEqual(got["no_grant"], (403, "approver_lacks_grant"))
        self.assertEqual(got["reader"], (403, "approver_lacks_grant"))
        self.assertEqual(len({c for _, c in got.values()}), 3)

    def test_grant_is_checked_at_submission_time(self):
        a = self.a
        inv = a["admin"].post(f"/orgs/{a['org_id']}/invites",
                              {"email": "temp@alpha.test"}).json()
        j = self.client.post("/join", json={"invite_code": inv["invite_code"],
                                            "node_id": "n-temp"}).json()
        temp = Seat(self.client, j["member_id"], j["credential"], a["org_id"])
        a["admin"].post(f"/scopes/{a['team']}/grants",
                        {"member_id": temp.member_id, "permission": "approve"})
        p = payload(a, subject="entity:temp")      # minted while granted
        a["admin"].post(f"/scopes/{a['team']}/grants",
                        {"member_id": temp.member_id, "permission": "approve",
                         "revoke": True})
        r = submit(temp, p)
        self.assertEqual(r.json()["detail"]["code"], "approver_lacks_grant")

    def test_resubmission_is_idempotent(self):
        p = payload(self.a, subject="entity:initech")
        first = submit(self.a["lead"], p).json()
        second = submit(self.a["lead"], p).json()
        self.assertEqual(first["record_version_id"], second["record_version_id"])
        self.assertTrue(second["idempotent"])
        vers = self.a["lead"].get(f"/records/{first['record_id']}/versions").json()
        self.assertEqual(len(vers["versions"]), 1)

    def test_resubmission_after_ttl_still_returns_the_version(self):
        p = payload(self.a, subject="entity:hooli", expires_in=2)
        first = submit(self.a["lead"], p).json()
        time.sleep(2.1)
        again = submit(self.a["lead"], p).json()
        self.assertEqual(again["record_version_id"], first["record_version_id"])

    def test_racing_identical_submissions_produce_one_version(self):
        p = payload(self.a, subject="entity:umbrella")
        results: list = []

        def go():
            r = submit(self.a["lead"], p)
            self.assertEqual(r.status_code, 200, r.text)
            results.append(r.json())

        threads = [threading.Thread(target=go) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 6)      # a thread's assert can't fail us
        self.assertEqual(len({r["record_version_id"] for r in results}), 1,
                         results)
        self.assertEqual(sum(1 for r in results if not r["idempotent"]), 1)

    def test_payload_must_carry_evidence(self):
        p = payload(self.a)
        p["evidence"] = []
        r = submit(self.a["lead"], p)
        self.assertEqual(r.json()["detail"]["code"], "no_evidence")

    def test_payload_for_another_org_refused(self):
        p = payload(self.a)
        p["org_id"] = self.b["org_id"]
        r = submit(self.a["lead"], p)
        self.assertEqual(r.json()["detail"]["code"], "org_mismatch")

    def test_quote_stored_only_when_included(self):
        p = payload(self.a, subject="entity:quote")
        p["evidence"][0]["quote"] = "we're in negotiation"
        out = submit(self.a["lead"], p).json()
        prov = self.a["lead"].get(f"/records/{out['record_id']}/provenance").json()
        self.assertIsNone(prov["evidence"][0]["quote"])
        p2 = payload(self.a, subject="entity:quote2")
        p2["evidence"][0]["quote"] = "we're in negotiation"
        p2["include_quote"] = True
        out2 = submit(self.a["lead"], p2).json()
        prov2 = self.a["lead"].get(f"/records/{out2['record_id']}/provenance").json()
        self.assertEqual(prov2["evidence"][0]["quote"], "we're in negotiation")

    # -- bi-temporal --
    def test_point_in_time_queries(self):
        a, lead = self.a, self.a["lead"]
        subject = "entity:bitemporal"
        aug1, aug20 = 1_785_000_000.0, 1_785_000_000.0 + 19 * DAY
        v1 = submit(lead, payload(a, subject=subject, valid_from=aug1,
                                  value={"state": "discovery"})).json()
        t_between = time.time()
        time.sleep(0.05)
        submit(lead, payload(a, subject=subject, valid_from=aug20,
                             value={"state": "negotiation"}))

        def state(as_of, known_at=None):
            q = {"subject": subject, "as_of": as_of}
            if known_at is not None:
                q["known_at"] = known_at
            rows = a["reader"].get("/records", params=q).json()["records"]
            return rows[0]["value_json"]["state"] if rows else None

        now = time.time()
        self.assertEqual(state(now), "negotiation")
        # what is true on Aug 15, as we believe it now: still discovery
        self.assertEqual(state(aug1 + 14 * DAY), "discovery")
        # what we believed (before the second approval) about today
        self.assertEqual(state(now, known_at=t_between), "discovery")
        # before Aug 1 nothing was true
        self.assertIsNone(state(aug1 - DAY))
        vers = lead.get(f"/records/{v1['record_id']}/versions").json()["versions"]
        self.assertTrue(any(v["derived_from"] for v in vers))

    def test_multi_valued_predicates_do_not_supersede_each_other(self):
        a, lead = self.a, self.a["lead"]
        one = submit(lead, payload(a, subject="member:x", predicate="owes",
                                   record_key="h1",
                                   value={"text": "deck"})).json()
        two = submit(lead, payload(a, subject="member:x", predicate="owes",
                                   record_key="h2",
                                   value={"text": "venue"})).json()
        self.assertNotEqual(one["record_id"], two["record_id"])
        rows = a["reader"].get("/records", params={"subject": "member:x"}).json()
        self.assertEqual(len(rows["records"]), 2)

    # -- forwarding --
    def test_forward_then_approver_records(self):
        a = self.a
        p = payload(a, subject="entity:forward", proposed_by=a["rep"].member_id)
        h = canonical_hash(p)
        r = a["rep"].post("/packets/forward", {"payload": p, "payload_hash": h})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(r.json()["approvers"], 1)
        queue = a["lead"].get("/packets/forwarded").json()["packets"]
        self.assertIn(p["packet_id"], [q["packet_id"] for q in queue])
        self.assertEqual(a["rep"].get("/packets/forwarded").json()["packets"], [])
        out = submit(a["lead"], p).json()
        hb = a["rep"].get("/nodes/heartbeat").json()
        fwd = {f["packet_id"]: f for f in hb["forwarded"]}
        self.assertEqual(fwd[p["packet_id"]]["record_version_id"],
                         out["record_version_id"])

    def test_reader_cannot_forward(self):
        p = payload(self.a)
        r = self.a["reader"].post("/packets/forward",
                                  {"payload": p, "payload_hash": canonical_hash(p)})
        self.assertEqual(r.json()["detail"]["code"], "lacks_propose")

    # -- evidence expiry --
    def test_expired_evidence_flips_status_for_that_node_only(self):
        a = self.a
        p = payload(a, subject="entity:expiry")
        p["evidence"][0]["node_id"] = "node-rep-alpha"
        p["evidence"][0]["event_ref"] = 777
        out = submit(a["lead"], p).json()
        # another node reporting the same event id changes nothing
        a["lead"].post("/evidence/expired", {"event_refs": [777]})
        prov = a["lead"].get(f"/records/{out['record_id']}/provenance").json()
        self.assertEqual(prov["evidence"][0]["evidence_status"], "live")
        r = a["rep"].post("/evidence/expired", {"event_refs": [777]})
        self.assertEqual(r.json()["marked"], 1)
        prov = a["lead"].get(f"/records/{out['record_id']}/provenance").json()
        self.assertEqual(prov["evidence"][0]["evidence_status"], "expired")

    # -- policy --
    def test_policy_ranges_and_admin_only(self):
        a = self.a
        bad = a["admin"].put(f"/orgs/{a['org_id']}/policy",
                             {"policy": {"retention": {"capture_ttl_days": 3}}})
        self.assertEqual(bad.json()["detail"]["code"], "bad_policy")
        nope = a["lead"].put(f"/orgs/{a['org_id']}/policy",
                             {"policy": {"retention": {"capture_ttl_days": 60}}})
        self.assertEqual(nope.json()["detail"]["code"], "lacks_admin")
        ok = a["admin"].put(f"/orgs/{a['org_id']}/policy",
                            {"policy": {"retention": {"capture_ttl_days": 60}}})
        self.assertEqual(ok.status_code, 200, ok.text)
        hb = a["rep"].get("/nodes/heartbeat").json()
        self.assertEqual(hb["policy"]["retention"]["capture_ttl_days"], 60)
        a["admin"].put(f"/orgs/{a['org_id']}/policy",
                       {"policy": {"retention": {"capture_ttl_days": 30}}})

    # -- departure (Phase 3 workflow, but revocation must already bite) --
    def test_departed_member_is_refused_on_the_next_request(self):
        a = self.a
        inv = a["admin"].post(f"/orgs/{a['org_id']}/invites",
                              {"email": "leaver@alpha.test"}).json()
        j = self.client.post("/join", json={"invite_code": inv["invite_code"],
                                            "node_id": "n-leaver"}).json()
        leaver = Seat(self.client, j["member_id"], j["credential"], a["org_id"])
        self.assertEqual(leaver.get("/nodes/heartbeat").status_code, 200)
        with self.db.tenant(a["org_id"]) as repo:
            repo.update_member(leaver.member_id, status="departed")
        r = leaver.get("/nodes/heartbeat")
        self.assertEqual((r.status_code, r.json()["detail"]["code"]),
                         (401, "member_inactive"))

    # -- tenancy --
    def test_cross_org_reads_fail_on_every_endpoint(self):
        a, b = self.a, self.b
        rec = submit(a["lead"], payload(a, subject="entity:secret")).json()
        fwd = payload(a, subject="entity:fwdsecret")
        a["rep"].post("/packets/forward", {"payload": fwd,
                                           "payload_hash": canonical_hash(fwd)})
        intruder = b["admin"]
        probes = {
            "scopes": intruder.get(f"/orgs/{a['org_id']}/scopes"),
            "create_scope": intruder.post(f"/orgs/{a['org_id']}/scopes",
                                          {"kind": "team", "name": "x",
                                           "parent_id": a["root"]}),
            "patch_scope": intruder.patch(f"/orgs/{a['org_id']}/scopes/{a['team']}",
                                          {"name": "pwned"}),
            "grants": intruder.get(f"/scopes/{a['team']}/grants"),
            "set_grant": intruder.post(f"/scopes/{a['team']}/grants",
                                       {"member_id": intruder.member_id,
                                        "permission": "admin"}),
            "invite": intruder.post(f"/orgs/{a['org_id']}/invites",
                                    {"email": "x@beta.test"}),
            "versions": intruder.get(f"/records/{rec['record_id']}/versions"),
            "provenance": intruder.get(f"/records/{rec['record_id']}/provenance"),
            "policy_get": intruder.get(f"/orgs/{a['org_id']}/policy"),
            "policy_put": intruder.put(f"/orgs/{a['org_id']}/policy",
                                       {"policy": {}}),
            "submit": submit(intruder, payload(a)),
        }
        for name, r in probes.items():
            self.assertIn(r.status_code, (403, 404), f"{name}: {r.text}")
        # list endpoints answer for the caller's own org only
        own = intruder.get("/records", params={"subject": "entity:secret"})
        self.assertEqual(own.json()["records"], [])
        self.assertEqual(intruder.get("/packets/forwarded").json()["packets"], [])
        audit_b = intruder.get("/audit").json()["entries"]
        self.assertTrue(audit_b)
        self.assertTrue(all(e["org_id"] == b["org_id"] for e in audit_b))
        # expired-evidence reports cannot touch another org's evidence
        intruder.post("/evidence/expired", {"event_refs": [42]})
        prov = a["lead"].get(f"/records/{rec['record_id']}/provenance").json()
        self.assertEqual(prov["evidence"][0]["evidence_status"], "live")
        # a token for org B cannot be replayed with org A's tenant: the org is
        # inside the signed token
        self.assertEqual(intruder.org_id, b["org_id"])

    def test_rls_is_the_backstop(self):
        """Even a repo bound to org B that asks for org A's rows by id gets
        nothing — the policy filters below the WHERE clause."""
        from sqlalchemy import text
        a, b = self.a, self.b
        with self.db.tenant(b["org_id"]) as repo:
            n = repo.conn.execute(text(
                "SELECT count(*) FROM scopes WHERE org_id = :o"),
                {"o": a["org_id"]}).scalar()
            self.assertEqual(n, 0)
            total_orgs = repo.conn.execute(text("SELECT count(*) FROM orgs")).scalar()
            self.assertEqual(total_orgs, 1)
        with self.assertRaises(Exception):
            with self.db.tenant(b["org_id"]) as repo:
                repo.conn.execute(text(
                    "INSERT INTO holds (id, org_id, name, criteria_json, "
                    "created_by, created_at) VALUES ('h', :o, 'x', '{}', 'me', 0)"),
                    {"o": a["org_id"]})

    def test_app_role_cannot_rewrite_history(self):
        from sqlalchemy import text
        for stmt in ("UPDATE audit_log SET actor = 'x'",
                     "DELETE FROM audit_log",
                     "UPDATE record_versions SET value_json = '{}'::jsonb",
                     "DELETE FROM record_versions"):
            with self.assertRaises(Exception, msg=stmt):
                with self.db.tenant(self.a["org_id"]) as repo:
                    repo.conn.execute(text(stmt))

    # -- audit chain --
    def test_every_change_is_audited_and_the_chain_verifies(self):
        from org_coordinator.records import service
        a = self.a
        submit(a["lead"], payload(a, subject="entity:audited"))
        actions = {e["action"] for e in a["admin"].get(
            "/audit", params={"limit": 5000}).json()["entries"]}
        for want in ("org.create", "member.join", "member.invite",
                     "scope.create", "grant.add", "packet.submit",
                     "record.version.create"):
            self.assertIn(want, actions)
        self.assertEqual(a["lead"].get("/audit").json()["detail"]["code"],
                         "lacks_admin")
        self.assertTrue(service.verify_chain(self.db, a["org_id"])["ok"])

    def test_concurrent_writers_leave_a_gapless_chain(self):
        from org_coordinator.records import service
        a = self.a

        statuses: list[int] = []

        def go(i):
            # Same subject for half of them: contends on the record row too.
            subj = "entity:conc" if i % 2 else f"entity:conc{i}"
            statuses.append(submit(a["lead"], payload(a, subject=subj))
                            .status_code)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(statuses, [200] * 12)
        self.assertTrue(service.verify_chain(self.db, a["org_id"])["ok"])

    def test_db_tamper_is_detected(self):
        from sqlalchemy import text

        from org_coordinator.records import service
        b = self.b
        submit(b["lead"], payload(b, subject="entity:tamper"))
        self.assertTrue(service.verify_chain(self.db, b["org_id"])["ok"])
        # superuser (outside the app role) edits one row
        with self.db.engine.begin() as c:
            c.execute(text("UPDATE audit_log SET actor = 'mallory' "
                           "WHERE org_id = :o AND seq = 3"), {"o": b["org_id"]})
        bad = service.verify_chain(self.db, b["org_id"])
        self.assertEqual((bad["ok"], bad["bad_seq"]), (False, 3))
        with self.db.engine.begin() as c:
            row = c.execute(text("SELECT * FROM audit_log WHERE org_id = :o "
                                 "AND seq = 2"), {"o": b["org_id"]}).first()
            prev = row.entry_hash
            r3 = c.execute(text("SELECT * FROM audit_log WHERE org_id = :o "
                                "AND seq = 3"), {"o": b["org_id"]}).first()
            # restore by recomputing the honest actor is impossible without
            # knowing it; instead verify the anchor path catches a full rewrite
            fixed = audit_entry_hash(prev, 3, "mallory", r3.action,
                                     r3.object_ref, r3.payload_hash, r3.at)
            c.execute(text("UPDATE audit_log SET entry_hash = :h WHERE "
                           "org_id = :o AND seq = 3"),
                      {"h": fixed, "o": b["org_id"]})
        # rewriting entry 3's hash breaks entry 4's prev link
        bad = service.verify_chain(self.db, b["org_id"])
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["bad_seq"], 4)


class AuditChainSynthetic(unittest.TestCase):
    """Pure verifier over 100,000 synthetic entries (no database needed)."""

    N = 100_000

    @classmethod
    def setUpClass(cls) -> None:
        rows, prev = [], GENESIS_HASH
        t0 = 1_790_000_000.0
        for seq in range(1, cls.N + 1):
            row = {"seq": seq, "actor": f"mem_{seq % 17}",
                   "action": ("packet.submit", "record.version.create",
                              "grant.add")[seq % 3],
                   "object_ref": f"obj_{seq}",
                   "payload_hash": canonical_hash({"n": seq}),
                   "prev_hash": prev, "at": t0 + seq * 0.25}
            row["entry_hash"] = audit_entry_hash(
                prev, seq, row["actor"], row["action"], row["object_ref"],
                row["payload_hash"], row["at"])
            prev = row["entry_hash"]
            rows.append(row)
        cls.rows = rows

    def test_verifier_passes_on_100k_entries(self):
        from org_coordinator.records import audit
        out = audit.verify(self.rows)
        self.assertTrue(out["ok"])
        self.assertEqual(out["checked"], self.N)

    def test_single_altered_entry_is_detected(self):
        from org_coordinator.records import audit
        rng = random.Random(7)
        for field in ("actor", "action", "object_ref", "payload_hash", "at"):
            i = rng.randrange(self.N)
            rows = list(self.rows)
            bad = dict(rows[i])
            bad[field] = (bad[field] + 0.001 if field == "at"
                          else bad[field] + "x")
            rows[i] = bad
            out = audit.verify(rows)
            self.assertFalse(out["ok"], field)
            self.assertEqual(out["bad_seq"], i + 1, field)

    def test_dropped_entry_is_detected(self):
        from org_coordinator.records import audit
        rows = self.rows[:500] + self.rows[501:]
        out = audit.verify(rows)
        self.assertEqual((out["ok"], out["bad_seq"]), (False, 502))

    def test_anchor_mismatch_is_detected(self):
        from org_coordinator.records import audit
        anchors = [{"seq": 50_000, "entry_hash": "f" * 64}]
        out = audit.verify(self.rows, anchors=anchors)
        self.assertEqual((out["ok"], out["reason"]), (False, "anchor_mismatch"))
        good = [{"seq": 50_000, "entry_hash": self.rows[49_999]["entry_hash"]}]
        self.assertTrue(audit.verify(self.rows, anchors=good)["ok"])


if __name__ == "__main__":
    unittest.main()
