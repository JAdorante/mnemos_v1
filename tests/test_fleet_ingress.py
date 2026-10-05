"""Fleet ingress: per-agent tokens and POST /fleet/publish.

Bad tokens get 401, the rate limit gets 429, malformed signals get a 4xx with
a reason, and accepted signals publish with source="fleet.signal".
"""
from __future__ import annotations

import os

from fastapi.testclient import TestClient

from app.services.fleet import registry
from tests.fleet_support import FleetTestCase, agent_body, fleet_app


class IngressBase(FleetTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(fleet_app())
        self.events = []
        self.bus.subscribe(self.events.append)
        rec = registry.register_agent("rates", ["eng.status"], "both")
        self.token = rec["token"]
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def publish(self, body=None, headers=None):
        return self.client.post("/fleet/publish", json=body or agent_body(),
                                headers=self.auth if headers is None
                                else headers)


class RegistryTests(IngressBase):
    def test_token_is_returned_once_and_stored_hashed(self) -> None:
        raw = (self.tmp / "fleet_agents.json").read_text(encoding="utf-8")
        self.assertNotIn(self.token, raw)
        self.assertNotIn("token", registry.get_agent("rates") or {"token": 1})
        self.assertTrue(all("token_sha256" not in a
                            for a in registry.list_agents()))

    def test_reregistering_rotates_the_token(self) -> None:
        new = registry.register_agent("rates", ["eng.status"])["token"]
        self.assertIsNone(registry.authenticate(f"Bearer {self.token}"))
        self.assertIsNotNone(registry.authenticate(f"Bearer {new}"))

    def test_bad_names_roles_topics(self) -> None:
        for args in (("Bad Name", ["t"]), ("ok", []), ("ok", ["Bad Topic"])):
            with self.assertRaises(ValueError):
                registry.register_agent(*args)
        with self.assertRaises(ValueError):
            registry.register_agent("ok", ["t"], role="admin")


class AuthTests(IngressBase):
    def test_missing_and_bad_tokens_get_401(self) -> None:
        self.assertEqual(self.publish(headers={}).status_code, 401)
        self.assertEqual(self.publish(headers={
            "Authorization": "Bearer fa_not-a-real-token-at-all"}).status_code,
            401)
        self.assertEqual(self.publish(headers={
            "Authorization": "Basic abc"}).status_code, 401)

    def test_revoked_token_gets_401(self) -> None:
        registry.revoke_agent("rates")
        self.assertEqual(self.publish().status_code, 401)

    def test_fleet_off_is_404(self) -> None:
        os.environ["QUILL_FLEET"] = "0"
        self.assertEqual(self.publish().status_code, 404)

    def test_agent_routes_authenticate_themselves_past_the_lan_gate(self) -> None:
        from app.services import api_auth
        for path in ("/fleet/publish", "/fleet/stream", "/fleet/signals"):
            self.assertTrue(api_auth.path_is_exempt(path, "POST"), path)
            self.assertTrue(api_auth.csrf_path_exempt(path), path)
        # Owner routes stay behind the LAN gate.
        for path in ("/fleet/agents", "/fleet/routes", "/fleet/offers",
                     "/fleet/relay/register", "/fleet/status"):
            self.assertFalse(api_auth.path_is_exempt(path, "POST"), path)

    def test_an_agent_token_cannot_manage_the_fleet(self) -> None:
        for method, path, body in (
                ("put", "/fleet/routes", {"rules": []}),
                ("post", "/fleet/agents", {"name": "x", "topics": ["t"]}),
                ("get", "/fleet/offers", None),
                ("post", "/fleet/offers/of:x/decide", {"approve": True})):
            with self.subTest(path=path):
                r = getattr(self.client, method)(
                    path, headers=self.auth,
                    **({"json": body} if body is not None else {}))
                self.assertEqual(r.status_code, 403, r.text)

    def test_owner_auth_flag_requires_the_api_token(self) -> None:
        os.environ["QUILL_FLEET_OWNER_AUTH"] = "1"
        self.assertEqual(self.client.get("/fleet/status").status_code, 401)


class PublishTests(IngressBase):
    def test_accepted_signal_publishes_fleet_signal_with_provenance(self) -> None:
        r = self.publish()
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        sig = out["signal"]
        self.assertEqual(sig["producer"], "agent:rates")
        self.assertEqual(sig["hops"], 0)
        self.assertTrue(sig["origin_id"].startswith("s"))
        self.assertEqual(len(self.events), 1)
        ev = self.events[0]
        self.assertEqual(ev.source, "fleet.signal")
        self.assertEqual(ev.meta["signal"], sig)
        self.assertEqual(ev.meta["provenance"], "local")
        self.assertEqual(ev.meta["epistemic"], "inferred")
        self.assertTrue(ev.meta["never_authorizes"])
        self.assertIn("Atlas migration", ev.raw)
        self.assertIn("status_update", ev.raw)
        self.assertEqual(out["route"]["action"], "local")

    def test_timeline_summary_names_the_producer(self) -> None:
        self.publish()
        self.assertIn("agent:rates", self.events[0].summary)
        self.assertTrue(self.events[0].summary.startswith("[fleet signal]"))

    def test_malformed_signals_get_422_with_a_reason(self) -> None:
        mv = {"kind": "market_view", "subject": "TLT",
              "body": {"direction": "bearish", "horizon": "weeks"}}
        for body, code in ((agent_body(**mv, quantity=500), "forbidden_field"),
                           (agent_body(body={"status": "fine"}), "bad_value"),
                           (agent_body(kind="gossip"), "unknown_kind"),
                           (agent_body(producer="agent:evil"), "stamped_field"),
                           (agent_body(hops=0), "stamped_field"),
                           (agent_body(sources=[{"name": "x"}]),
                            "missing_field"),
                           (agent_body(surprise=1), "unknown_field")):
            with self.subTest(code=code):
                r = self.publish(body)
                self.assertEqual(r.status_code, 422, r.text)
                self.assertEqual(r.json()["error"], code)
                self.assertTrue(r.json()["reason"])
        self.assertEqual(self.events, [])

    def test_unregistered_topic_is_403(self) -> None:
        r = self.publish(agent_body(topic="sales.leads"))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "topic_not_allowed")

    def test_subscriber_role_cannot_publish(self) -> None:
        tok = registry.register_agent("reader", ["eng.status"],
                                      "subscriber")["token"]
        r = self.publish(headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r.status_code, 403)

    def test_rate_limit_gets_429(self) -> None:
        os.environ["QUILL_FLEET_RATE"] = "2"
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.publish().status_code, 200)
        r = self.publish()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["error"], "rate_limited")
        self.assertEqual(len(self.events), 2)

    def test_rate_limit_is_per_agent(self) -> None:
        os.environ["QUILL_FLEET_RATE"] = "1"
        other = registry.register_agent("other", ["eng.status"])["token"]
        self.assertEqual(self.publish().status_code, 200)
        self.assertEqual(self.publish(headers={
            "Authorization": f"Bearer {other}"}).status_code, 200)

    def test_retry_with_the_same_signal_id_is_idempotent(self) -> None:
        first = self.publish(agent_body(signal_id="run-42")).json()
        again = self.publish(agent_body(signal_id="run-42")).json()
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["signal"]["origin_id"],
                         again["signal"]["origin_id"])
        self.assertEqual(len(self.events), 1)

    def test_published_origin_is_recorded_as_our_own(self) -> None:
        from app.services.fleet import dedup
        sig = self.publish().json()["signal"]
        self.assertTrue(dedup.is_own(sig["origin_id"]))

    def test_default_ttl_is_applied(self) -> None:
        sig = self.publish().json()["signal"]
        self.assertAlmostEqual(sig["expires_at"] - sig["ts"], 3600, delta=1)


class KindsAndBlockedApiTests(IngressBase):
    def test_kind_defaults_to_note(self) -> None:
        r = self.publish({"topic": "eng.status", "summary": "Lunch moved."})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["signal"]["kind"], "note")
        self.assertEqual(r.json()["signal"]["body"], {})

    def test_owner_manages_kinds_and_bad_ones_are_refused(self) -> None:
        r = self.client.put("/fleet/kinds", json={"kinds": {
            "incident": {"subject": "optional",
                         "fields": {"sev": {"enum": [1, 2, 3]}},
                         "required": ["sev"], "forbidden": ["customer"]}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(set(r.json()["kinds"]), {"note", "incident"})
        self.assertEqual(self.client.put("/fleet/kinds", json={"kinds": {
            "x": {"fields": {"a": {"type": "object"}}}}}).status_code, 422)
        tok = registry.register_agent("ops", ["ops.incidents"])["token"]
        h = {"Authorization": f"Bearer {tok}"}
        ok = self.client.post("/fleet/publish", headers=h, json={
            "topic": "ops.incidents", "kind": "incident",
            "summary": "API latency spike", "body": {"sev": 2}})
        self.assertEqual(ok.status_code, 200, ok.text)
        bad = self.client.post("/fleet/publish", headers=h, json={
            "topic": "ops.incidents", "kind": "incident",
            "summary": "x", "body": {"sev": 2}, "customer": "ACME"})
        self.assertEqual(bad.json()["error"], "forbidden_field")

    def test_schema_publishes_every_kind(self) -> None:
        sch = self.client.get("/fleet/schema").json()
        self.assertIn("status_update", sch["x-kinds"])
        self.assertIn("note", sch["x-kinds"])

    def test_owner_manages_the_blocked_list(self) -> None:
        r = self.client.put("/fleet/blocked",
                            json={"subjects": ["Orion"], "patterns": ["acme*"]})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/fleet/blocked").json()
        self.assertEqual(got["subjects"], ["orion"])
        self.assertEqual(self.client.put("/fleet/kinds", headers=self.auth,
                                         json={"kinds": {}}).status_code, 403)


class SourcePolicyTests(FleetTestCase):
    def test_fleet_and_peer_signals_mint_claims_only(self) -> None:
        from app.services import source_policy as sp
        self.assertEqual(sp.classify_source(event_source="fleet.signal"),
                         "fleet_agent")
        self.assertEqual(sp.classify_source(event_source="peer.signal"),
                         "peer_signal")
        # A peer ANSWER keeps its own class.
        self.assertEqual(sp.classify_source(event_source="peer.answer"),
                         "peer_answer")
        for cls in ("fleet_agent", "peer_signal"):
            pol = sp.policy_for(cls)
            self.assertTrue(pol.create_claims, cls)
            self.assertFalse(pol.create_commitments, cls)
            self.assertFalse(pol.update_people, cls)
            self.assertFalse(pol.create_person_candidates, cls)

    def test_fleet_signals_never_authorize(self) -> None:
        from app.services.trust import source_can_authorize
        self.assertFalse(source_can_authorize("fleet.signal"))
        self.assertFalse(source_can_authorize("peer.signal",
                                              {"never_authorizes": True}))
