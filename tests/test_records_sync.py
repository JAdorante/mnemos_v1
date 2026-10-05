"""Write-back sync against real Postgres + faked external systems.

Phase 2 acceptance covered here:
  * preview shown at approval equals the change written, byte for byte
    (the hash covers the preview)
  * a manually edited external field is flagged as drift and never
    overwritten
  * verified writes, ~24h of backoff then failed + admin alert, conflicts on
    a moved field, SKIP LOCKED workers never share a job, tenancy on every
    new endpoint

Skips without QUILL_ORG_TEST_DATABASE_URL (tests/records_pg.py).
"""
from __future__ import annotations

import base64
import os
import threading
import time

from app.services.records.canonical import canonical_hash
from org_coordinator.connectors import http
from org_coordinator.connectors.gdrive import _reset_tokens
from org_coordinator.records import sync
from tests.connector_fakes import FakeWorld
from tests.records_pg import PgTestCase, make_app
from tests.test_records_org_service import _org, payload

DAY = 86400.0


class SyncTests(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from fastapi.testclient import TestClient
        cls._sk = os.environ.get("QUILL_ORG_SECRETS_KEY")
        os.environ["QUILL_ORG_SECRETS_KEY"] = base64.b64encode(b"s" * 32).decode()
        cls.client = TestClient(make_app(cls.db))
        cls.a = _org(cls, "gamma")
        cls.b = _org(cls, "delta")
        admin = cls.a["admin"]
        # the deal scope maps to a HubSpot deal; a log doc on the team
        admin.patch(f"/orgs/{cls.a['org_id']}/scopes/{cls.a['deal']}",
                    {"external_ref": "hubspot:deal/101"})
        hs = admin.post(f"/orgs/{cls.a['org_id']}/connectors",
                        {"kind": "hubspot", "name": "HubSpot",
                         "secret": {"token": "hs-token"}})
        assert hs.status_code == 200, hs.text
        cls.hs = hs.json()["id"]
        r = admin.post(f"/connectors/{cls.hs}/mappings",
                       {"kind": "status", "predicate": "deal.stage",
                        "op": "set_property", "field": "dealstage",
                        "value_path": "value.state"})
        assert r.status_code == 200, r.text

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._sk is None:
            os.environ.pop("QUILL_ORG_SECRETS_KEY", None)
        else:
            os.environ["QUILL_ORG_SECRETS_KEY"] = cls._sk
        super().tearDownClass()

    def setUp(self) -> None:
        self.world = FakeWorld()
        self.world.hubspot.add("deals", "101", dealstage="appointmentscheduled")
        http.set_transport(self.world.transport)
        _reset_tokens()
        # drain anything an earlier test left due, against this test's world
        sync.drain(self.db, now=time.time() + 400 * DAY)

    def tearDown(self) -> None:
        http.set_transport(None)

    # -- helpers --
    def stage_payload(self, state="closedwon", **kw):
        p = payload(self.a, scope=self.a["deal"], subject="entity:acme",
                    predicate="deal.stage", value={"state": state}, **kw)
        p["preview"] = self.a["rep"].post("/packets/preview",
                                          {"payload": p}).json()["preview"]
        return p

    def submit(self, p, seat="lead"):
        return self.a[seat].post("/packets", {
            "packet_id": p["packet_id"], "payload": p,
            "payload_hash": canonical_hash(p), "approved_via": "button"})

    def jobs(self):
        return self.a["admin"].get("/sync_jobs").json()["jobs"]

    def job_for(self, version_id):
        return [j for j in self.jobs() if j["record_version_id"] == version_id]

    # -- preview --
    def test_preview_names_the_exact_external_change(self):
        p = self.stage_payload()
        self.assertEqual(p["preview"], [{
            "connector_id": self.hs, "kind": "hubspot",
            "target": "hubspot:deals/101", "op": "set_property",
            "fields": {"dealstage": "closedwon"}, "text": None, "marker": None,
            "diff": [{"field": "dealstage", "before": "appointmentscheduled",
                      "after": "closedwon"}]}])

    def test_unmapped_record_has_an_empty_preview(self):
        p = payload(self.a, scope=self.a["deal"], predicate="deal.owner")
        out = self.a["rep"].post("/packets/preview", {"payload": p}).json()
        self.assertEqual(out["preview"], [])

    def test_missing_or_altered_preview_is_stale(self):
        p = self.stage_payload()
        bare = dict(p, preview=None)
        r = self.submit(bare)
        self.assertEqual(r.json()["detail"]["code"], "preview_stale")
        forged = dict(p, preview=[dict(p["preview"][0],
                                       fields={"dealstage": "closedlost"})])
        self.assertEqual(self.submit(forged).json()["detail"]["code"],
                         "preview_stale")
        self.assertEqual(self.world.hubspot.patches, [])

    # -- write path --
    def test_approved_preview_is_written_byte_for_byte_and_verified(self):
        p = self.stage_payload()
        out = self.submit(p).json()
        [job] = self.job_for(out["record_version_id"])
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["idempotency_key"],
                         f"{canonical_hash(p)}:{out['version']}")
        counts = sync.drain(self.db)
        self.assertEqual(counts, {"verified": 1})
        [patch] = self.world.hubspot.patches
        signed = p["preview"][0]
        self.assertEqual(patch["properties"], signed["fields"])
        self.assertEqual(
            canonical_hash(patch["properties"]),
            canonical_hash({d["field"]: d["after"] for d in signed["diff"]}))
        [job] = self.job_for(out["record_version_id"])
        self.assertEqual(job["state"], "verified")
        self.assertEqual(job["written_json"], {"dealstage": "closedwon"})

    def test_external_change_after_preview_is_a_conflict(self):
        p = self.stage_payload()
        out = self.submit(p).json()
        self.world.hubspot.edit("deals", "101", dealstage="contractsent")
        self.assertEqual(sync.drain(self.db), {"conflict": 1})
        self.assertEqual(self.world.hubspot.patches, [])
        self.assertEqual(self.world.hubspot.objects[("deals", "101")]
                         ["properties"]["dealstage"], "contractsent")
        [job] = self.job_for(out["record_version_id"])
        self.assertEqual(job["state"], "conflict")
        hb = self.a["lead"].get("/nodes/heartbeat").json()
        states = {s["record_version_id"]: s["state"] for s in hb["sync"]}
        self.assertEqual(states[out["record_version_id"]], "conflict")

    def test_transient_failures_back_off_then_fail_with_an_alert(self):
        p = self.stage_payload()
        out = self.submit(p).json()
        t = time.time()
        seen = []
        for i in range(sync.MAX_ATTEMPTS):
            self.world.hubspot.fail = [503] * 3
            res = sync.run_one(self.db, now=t)
            seen.append(res["state"])
            if i < sync.MAX_ATTEMPTS - 1:
                self.assertIsNone(sync.run_one(self.db, now=t))   # not due yet
                t += sync.BACKOFF_S[i] + 1
        self.assertEqual(seen, ["pending"] * 5 + ["failed"])
        self.assertLess(sum(sync.BACKOFF_S[:5]) / 3600, 24)
        self.assertGreater(sum(sync.BACKOFF_S) / 3600, 20)
        [job] = self.job_for(out["record_version_id"])
        self.assertEqual((job["state"], job["attempts"]), ("failed", 6))
        alerts = self.a["admin"].get("/alerts").json()["alerts"]
        self.assertIn(job["id"], [x["object_ref"] for x in alerts])
        aid = [x for x in alerts if x["object_ref"] == job["id"]][0]["id"]
        self.a["admin"].post(f"/alerts/{aid}/ack")

    def test_permanent_failure_fails_at_once(self):
        p = self.stage_payload()
        out = self.submit(p).json()
        del self.world.hubspot.objects[("deals", "101")]
        self.assertEqual(sync.drain(self.db), {"failed": 1})
        [job] = self.job_for(out["record_version_id"])
        self.assertEqual(job["attempts"], 1)

    def test_skip_locked_workers_never_share_a_job(self):
        a = self.a
        for i in range(12):
            self.world.hubspot.add("deals", str(500 + i), dealstage="x")
            p = payload(a, scope=a["deal"], subject=f"entity:co{i}",
                        predicate="deal.stage", value={"state": f"s{i}"},
                        valid_from=time.time() - 60)
            p["target_ref"] = f"hubspot:deal/{500 + i}"
            p["preview"] = a["rep"].post("/packets/preview",
                                         {"payload": p}).json()["preview"]
            self.assertEqual(self.submit(p).status_code, 200)
        results: list[dict] = []
        lock = threading.Lock()

        def worker():
            while True:
                r = sync.run_one(self.db)
                if r is None:
                    return
                with lock:
                    results.append(r)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = [r["job_id"] for r in results]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 12)
        self.assertEqual(len(self.world.hubspot.patches), 12)

    # -- drift --
    def test_drift_is_flagged_never_overwritten_and_acknowledged(self):
        a = self.a
        p = self.stage_payload()
        out = self.submit(p).json()
        sync.drain(self.db)
        self.assertEqual(sync.drift_sweep(self.db, a["org_id"])["drift"], 0)
        self.world.hubspot.edit("deals", "101", dealstage="closedlost")
        res = sync.drift_sweep(self.db, a["org_id"])
        self.assertEqual(res["drift"], 1)
        self.assertEqual(self.world.hubspot.objects[("deals", "101")]
                         ["properties"]["dealstage"], "closedlost")
        self.assertEqual(len(self.world.hubspot.patches), 1)   # only ours
        # the approver sees it; a reader does not
        drift = a["lead"].get("/nodes/heartbeat").json()["drift"]
        mine = [d for d in drift if d["record_id"] == out["record_id"]]
        self.assertEqual(mine[0]["external"], "closedlost")
        self.assertEqual(mine[0]["written"], "closedwon")
        self.assertEqual(a["reader"].get("/nodes/heartbeat").json()["drift"], [])
        vers = a["lead"].get(f"/records/{out['record_id']}/versions").json()
        self.assertEqual(vers["record"]["drift_status"], "externally_modified")
        # sweeping again does not open a second notice
        self.assertEqual(sync.drift_sweep(self.db, a["org_id"])["drift"], 0)
        # an approved acknowledgement resolves it
        ack = payload(a, scope=a["deal"], subject="entity:acme",
                      predicate="external_change",
                      value={"state": "externally_modified"})
        ack["resolves_drift"] = [mine[0]["id"]]
        ack["preview"] = a["rep"].post("/packets/preview",
                                       {"payload": ack}).json()["preview"]
        self.assertEqual(self.submit(ack).status_code, 200)
        left = [d for d in a["lead"].get("/nodes/heartbeat").json()["drift"]
                if d["record_id"] == out["record_id"]]
        self.assertEqual(left, [])
        vers = a["lead"].get(f"/records/{out['record_id']}/versions").json()
        self.assertIsNone(vers["record"]["drift_status"])

    def test_a_newer_sparrow_write_is_not_drift(self):
        p1 = self.stage_payload("negotiation")
        self.submit(p1)
        sync.drain(self.db)
        p2 = self.stage_payload("closedwon")
        self.submit(p2)
        sync.drain(self.db)
        self.assertEqual(sync.drift_sweep(self.db, self.a["org_id"])["drift"], 0)

    # -- connectors admin + secrets --
    def test_connector_admin_only_and_secret_never_returned(self):
        from sqlalchemy import text
        rows = self.a["admin"].get(
            f"/orgs/{self.a['org_id']}/connectors").json()["connectors"]
        self.assertNotIn("secret_enc", rows[0])
        self.assertTrue(rows[0]["has_secret"])
        for seat in ("lead", "rep", "reader"):
            r = self.a[seat].get(f"/orgs/{self.a['org_id']}/connectors")
            self.assertEqual(r.json()["detail"]["code"], "lacks_admin")
        with self.db.engine.begin() as c:
            raw = c.execute(text("SELECT secret_enc FROM connectors WHERE id = :i"),
                            {"i": self.hs}).scalar()
        self.assertNotIn("hs-token", raw)

    def test_bad_mapping_and_connector_config_refused(self):
        a = self.a
        r = a["admin"].post(f"/connectors/{self.hs}/mappings",
                            {"kind": "status", "predicate": "x", "op": "post"})
        self.assertEqual(r.json()["detail"]["code"], "bad_mapping")
        r = a["admin"].post(f"/connectors/{self.hs}/mappings",
                            {"kind": "status", "predicate": "x",
                             "op": "set_property", "field": "f",
                             "transform": "eval"})
        self.assertEqual(r.json()["detail"]["code"], "bad_mapping")
        r = a["admin"].post(f"/orgs/{a['org_id']}/connectors",
                            {"kind": "webhook", "name": "w",
                             "config": {"url": "http://insecure"},
                             "secret": {"signing_secret": "s"}})
        self.assertEqual(r.json()["detail"]["code"], "bad_connector_config")

    def test_cross_org_on_every_new_endpoint(self):
        a, b = self.a, self.b
        intruder = b["admin"]
        probes = {
            "connectors": intruder.get(f"/orgs/{a['org_id']}/connectors"),
            "connect": intruder.post(f"/orgs/{a['org_id']}/connectors",
                                     {"kind": "webhook", "name": "x"}),
            "mappings": intruder.get(f"/connectors/{self.hs}/mappings"),
            "add_mapping": intruder.post(f"/connectors/{self.hs}/mappings",
                                         {"kind": "status", "predicate": "x",
                                          "op": "set_property", "field": "f"}),
            "disable": intruder.patch(f"/connectors/{self.hs}",
                                      {"status": "disabled"}),
            "preview": intruder.post("/packets/preview",
                                     {"payload": payload(a, scope=a["deal"])}),
        }
        for name, r in probes.items():
            self.assertIn(r.status_code, (403, 404), f"{name}: {r.text}")
        self.assertEqual(intruder.get("/sync_jobs").json()["jobs"], [])
        self.assertEqual(intruder.get("/alerts").json()["alerts"], [])
        self.assertEqual(intruder.get("/nodes/heartbeat").json()["drift"], [])
        self.assertEqual(
            a["admin"].get(f"/connectors/{self.hs}/mappings").status_code, 200)

    # -- roster + peer-answer rule --
    def test_roster_and_answerable_respect_both_readers(self):
        a = self.a
        a["rep"].get("/nodes/heartbeat", params={"peer_url": "https://rep.test"})
        roster = a["lead"].get("/nodes/heartbeat").json()["roster"]
        sales = [t for t in roster if t["scope_id"] == a["team"]][0]
        urls = {m["member_id"]: m["peer_url"] for m in sales["members"]}
        self.assertEqual(urls[a["rep"].member_id], "https://rep.test")
        p = payload(a, scope=a["deal"], subject="entity:initrode",
                    predicate="deal.stage", value={"state": "negotiation"})
        p["target_ref"] = None
        self.world.hubspot.add("deals", "101", dealstage="x")
        p["preview"] = a["rep"].post("/packets/preview",
                                     {"payload": p}).json()["preview"]
        self.submit(p)
        hits = a["lead"].post("/records/answerable", {
            "asker_member_id": a["rep"].member_id,
            "question": "what stage is the Initrode deal in?"}).json()["records"]
        self.assertEqual(hits[0]["value"], {"state": "negotiation"})
        # an asker from another org reads nothing here
        none = a["lead"].post("/records/answerable", {
            "asker_member_id": self.b["rep"].member_id,
            "question": "what stage is the Initrode deal in?"}).json()["records"]
        self.assertEqual(none, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
