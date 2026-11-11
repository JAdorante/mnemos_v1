"""Team search against the real Org Record Service (Postgres).

  * the table itself refuses a title or snippet outside a shared project;
  * search tiers each pointer before ranking, by the reader's AND the
    owner's live permissions;
  * pointers come out when a grant is revoked, a member departs or leaves,
    a member stops sharing a project, or the org leaves Connected;
  * one org never sees another's pointers;
  * two seats end to end: publish, search, chat grounding, a connected
    assistant, and a delete that reaches the teammate's next search.

Skips without QUILL_ORG_TEST_DATABASE_URL (tests/records_pg.py).
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from unittest import mock

from tests.records_pg import PgTestCase, make_app

NOW = time.time()


def mail(ref, *, scope=None, tags=(), people=(), title=None, snippet=None,
         at=None):
    return {"item_ref": ref, "kind": "email", "scope_id": scope,
            "tags": list(tags), "people": list(people), "at": at or NOW - 60,
            "source": "google.mail", "title": title, "snippet": snippet}


class OrgFixture(PgTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from org_coordinator.records import pointer_index, service
        cls.svc, cls.pi = service, pointer_index

    @classmethod
    def new_org(cls, name: str) -> dict:
        svc, db = cls.svc, cls.db
        boot = svc.bootstrap_org(db, name=name, admin_email=f"lead@{name}.t")
        j = svc.join(db, invite_code=boot["invite_code"], node_id=f"n-{name}")
        admin = cls.auth(j["credential"])
        team = svc.create_scope(db, admin, boot["org_id"], kind="team",
                                name="Sales", parent_id=boot["root_scope_id"])
        ops = svc.create_scope(db, admin, boot["org_id"], kind="team",
                               name="Ops", parent_id=boot["root_scope_id"])
        bay = svc.ensure_project(db, admin, boot["org_id"], name="Bayview",
                                 parent_id=team["id"])
        orb = svc.ensure_project(db, admin, boot["org_id"], name="Orbital",
                                 parent_id=ops["id"])
        return {"id": boot["org_id"], "root": boot["root_scope_id"],
                "admin": admin, "sales": team["id"], "ops": ops["id"],
                "bayview": bay["id"], "orbital": orb["id"]}

    @classmethod
    def auth(cls, credential: str):
        tok = cls.svc.issue_token(cls.db, credential)["access_token"]
        return cls.svc.authenticate(cls.db, tok)

    def member(self, name: str, *, team: str | None = None, org=None):
        org = org or self.org
        inv = self.svc.create_invite(self.db, org["admin"], org["id"],
                                     email=f"{name}-{uuid.uuid4().hex[:6]}@t",
                                     role="member")
        j = self.svc.join(self.db, invite_code=inv["invite_code"],
                          node_id=f"n-{name}-{uuid.uuid4().hex[:4]}")
        p = self.auth(j["credential"])
        if team:
            self.svc.set_grant(self.db, org["admin"], org[team],
                               member_id=p.member_id, permission="propose")
        return p

    def count(self, p) -> int:
        with self.db.tenant(p.org_id) as repo:
            return repo.count_pointers(p.member_id)


class IndexRules(OrgFixture):
    def setUp(self):
        self.org = self.new_org(f"Idx{uuid.uuid4().hex[:6]}")

    def test_the_table_refuses_text_outside_a_shared_project(self):
        from sqlalchemy.exc import IntegrityError
        sales = self.member("dana", team="sales")
        with self.assertRaises(IntegrityError):
            with self.db.tenant(self.org["id"]) as repo:
                repo.upsert_pointers([{
                    "owner_id": sales.member_id, "item_ref": "ev:raw",
                    "node_id": "n", "kind": "email", "scope_id": None,
                    "tags": [], "people": [], "terms": [], "text_terms": [],
                    "item_at": NOW, "source": "google.mail",
                    "title": "Pricing - approved", "snippet": None,
                    "refs": 0, "published_at": NOW}])

    def test_a_scope_the_owner_cannot_read_loses_its_text(self):
        sam = self.member("sam", team="ops")
        out = self.pi.publish(self.db, sam, [mail(
            "ev:1", scope=self.org["bayview"], tags=["Bayview"],
            title="Pricing - approved", snippet="Core plus intake")])
        self.assertEqual((out["published"], out["downgraded"]), (1, 1))
        with self.db.tenant(self.org["id"]) as repo:
            row = next(r for r in repo.pointer_candidates(
                ["bayview"], open_scopes=[], exclude_owner="nobody")
                if r["owner_id"] == sam.member_id)
        self.assertEqual((row["scope_id"], row["title"], row["snippet"],
                          row["text_terms"]), (None, None, None, []))

    def test_tiers_follow_both_readers_and_owners(self):
        dana = self.member("dana", team="sales")
        cleo = self.member("cleo", team="ops")
        self.pi.publish(self.db, dana, [mail(
            "ev:10", scope=self.org["bayview"], tags=["Bayview"],
            people=["Rachel Osei"], title="Pricing - approved",
            snippet="Core plus the intake add-on")])
        self.pi.publish(self.db, cleo, [mail(
            "ev:20", scope=self.org["orbital"], tags=["Orbital"],
            people=["Counsel"], title="Breach notice",
            snippet="Orbital considers the POC a breach")])
        bob = self.member("bob", team="sales")
        # Bob reads Bayview (via Sales), not Orbital (Ops).
        out = self.pi.search(self.db, bob, query="bayview pricing")
        self.assertEqual([p["title"] for p in out["pointers"]],
                         ["Pricing - approved"])
        self.assertEqual(out["pointers"][0]["project"], "Bayview")
        out = self.pi.search(self.db, bob, query="orbital")
        self.assertEqual(out["pointers"], [])
        self.assertEqual([(a["owner"], a["matched"]) for a in out["ask"]],
                         [(cleo.member_id, ["orbital"])])
        self.assertNotIn("Breach", str(out))
        self.assertEqual(self.pi.search(self.db, bob, query="breach notice"),
                         {"index": "open", "pointers": [], "ask": []})
        # The org admin reads every project and opens it.
        out = self.pi.search(self.db, self.org["admin"], query="breach")
        self.assertEqual([p["title"] for p in out["pointers"]],
                         ["Breach notice"])

    def test_losing_read_takes_that_projects_pointers_out(self):
        eve = self.member("eve")
        self.svc.set_grant(self.db, self.org["admin"], self.org["orbital"],
                           member_id=eve.member_id, permission="read")
        self.pi.publish(self.db, eve, [
            mail("ev:30", scope=self.org["orbital"], tags=["Orbital"],
                 title="Ops review"),
            mail("ev:31", tags=["Orbital"], people=["Ann"])])
        self.assertEqual(self.count(eve), 2)
        self.svc.set_grant(self.db, self.org["admin"], self.org["orbital"],
                           member_id=eve.member_id, permission="read",
                           revoke=True)
        with self.db.tenant(self.org["id"]) as repo:
            self.assertEqual(repo.pointer_refs(eve.member_id), ["ev:31"])

    def test_departure_and_leaving_purge_everything(self):
        fay = self.member("fay", team="sales")
        gus = self.member("gus", team="sales")
        for who in (fay, gus):
            self.pi.publish(self.db, who, [mail("ev:40", tags=["Bayview"],
                                                people=["Rachel Osei"])])
        out = self.svc.depart_member(self.db, self.org["admin"],
                                     self.org["id"], fay.member_id)
        self.assertEqual(out["pointers_withdrawn"], 1)
        self.assertEqual(self.count(fay), 0)
        self.assertEqual(self.svc.leave(self.db, gus)["pointers_withdrawn"], 1)
        bob = self.member("bob2", team="sales")
        owners = {a["owner"] for a in self.pi.search(
            self.db, bob, query="bayview")["ask"]}
        self.assertFalse({fay.member_id, gus.member_id} & owners)
        with self.assertRaises(self.svc.ServiceError):
            self.pi.publish(self.db, gus, [mail("ev:41", tags=["Bayview"])])

    def test_unshare_and_reconcile(self):
        hal = self.member("hal", team="sales")
        self.pi.publish(self.db, hal, [
            mail("ev:50", scope=self.org["bayview"], tags=["Bayview"],
                 title="Kickoff"),
            mail("ev:51", tags=["Bayview"]), mail("ev:52", tags=["Bayview"])])
        self.assertEqual(self.pi.unshare(self.db, hal,
                                         self.org["bayview"])["withdrawn"], 1)
        out = self.pi.reconcile(self.db, hal, ["ev:51"])
        self.assertEqual((out["withdrawn"], out["kept"]), (1, 1))
        self.assertEqual(self.pi.reconcile(self.db, hal, [])["withdrawn"], 1)
        self.assertEqual(self.count(hal), 0)

    def test_common_scopes_are_projects_both_read(self):
        ivy = self.member("ivy", team="sales")
        jon = self.member("jon", team="ops")
        kim = self.member("kim", team="sales")
        self.assertEqual(self.pi.common_scopes(
            self.db, ivy, member_id=jon.member_id)["scopes"], [])
        got = self.pi.common_scopes(self.db, ivy, member_id=kim.member_id)
        self.assertEqual([s["name"] for s in got["scopes"]], ["Bayview"])


class RungRules(OrgFixture):
    """Each test gets its own org: closing the index empties it."""

    def setUp(self):
        self.org = self.new_org(f"Rung{uuid.uuid4().hex[:6]}")

    def test_leaving_connected_empties_and_closes_the_index(self):
        dana = self.member("dana", team="sales")
        self.pi.publish(self.db, dana, [mail("ev:1", tags=["Bayview"])])
        self.svc.put_policy(self.db, self.org["admin"], self.org["id"],
                            {"rung": "sealed"})
        self.assertEqual(self.count(dana), 0)
        with self.assertRaises(self.svc.ServiceError) as cm:
            self.pi.publish(self.db, dana, [mail("ev:2", tags=["Bayview"])])
        self.assertEqual(cm.exception.code, "index_closed")
        self.assertEqual(self.pi.search(self.db, dana, query="bayview")["index"],
                         "closed:rung_sealed")
        hb = self.svc.heartbeat(self.db, dana)
        self.assertEqual(hb["team_index"], "closed:rung_sealed")
        # Taking pointers back never depends on the index being open.
        self.assertTrue(self.pi.withdraw(self.db, dana, ["ev:1"])["ok"])

    def test_admin_can_turn_publishing_off(self):
        dana = self.member("dana", team="sales")
        self.pi.publish(self.db, dana, [mail("ev:1", tags=["Bayview"])])
        self.svc.put_policy(self.db, self.org["admin"], self.org["id"],
                            {"policy": {"team_search": {"publish": False}}})
        self.assertEqual(self.count(dana), 0)
        self.assertEqual(self.svc.heartbeat(self.db, dana)["team_index"],
                         "closed:disabled")
        with self.assertRaises(self.svc.ServiceError):
            self.svc.put_policy(self.db, self.org["admin"], self.org["id"],
                                {"policy": {"team_search": {"publish": "no"}}})

    def test_one_org_never_sees_anothers_pointers(self):
        other = self.new_org(f"Other{uuid.uuid4().hex[:6]}")
        dana = self.member("dana", team="sales")
        self.pi.publish(self.db, dana, [mail("ev:1", tags=["Bayview"],
                                             people=["Rachel Osei"])])
        out = self.pi.search(self.db, other["admin"], query="bayview rachel")
        self.assertEqual((out["pointers"], out["ask"]), ([], []))
        with self.db.tenant(other["id"]) as repo:
            self.assertEqual(repo.count_pointers(), 0)


class TwoSeats(PgTestCase):
    """Lead and member seats through the real HTTP API and org_client."""

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
        cls.service = service
        boot = service.bootstrap_org(cls.db, name="Seats",
                                     admin_email="lead@seats.test")
        cls.org_id, cls.root = boot["org_id"], boot["root_scope_id"]
        cls.lead_invite = boot["invite_code"]

    def setUp(self) -> None:
        import app.storage
        from app.services import egress, team_index, team_layer
        from app.services.memory import memory
        from app.services.model_log import model_log
        from app.services.records import org_client
        from app.services.worker import worker
        from tests.test_records_e2e import Node
        self.Node, self.org_client, self.team_index = Node, org_client, team_index
        self.app_storage, self.memory = app.storage, memory
        self.tmp = tempfile.mkdtemp(prefix="team-seats-")
        self._trail = model_log._path
        model_log._path = Path(self.tmp) / "model_calls.jsonl"
        self._patches = [
            mock.patch.dict(os.environ, {
                "QUILL_TEAM_SEARCH": "on", "QUILL_EGRESS_RUNG": "",
                "QUILL_LEARNING": "0"}),
            mock.patch.object(egress, "_org_rung", (0.0, "")),
            mock.patch.object(team_layer, "_teams_path",
                              lambda: Path(self.tmp) / "peer_teams.json"),
            mock.patch.object(worker, "enqueue"),
        ]
        for p in self._patches:
            p.start()
        org_client.set_transport(self.transport)
        self.nodes = []

    def tearDown(self) -> None:
        from app.services.model_log import model_log
        from app.storage import remove_delete_hook
        remove_delete_hook(self.team_index._on_store_change)
        self.team_index._attached_store = None
        self.org_client.set_transport(None)
        for p in reversed(self._patches):
            p.stop()
        model_log._path = self._trail
        for n in self.nodes:
            n.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def on(self, node):
        import contextlib

        @contextlib.contextmanager
        def ctx():
            with node.active(), \
                    mock.patch.dict(os.environ, {"QUILL_DATA_DIR": node.dir}), \
                    mock.patch.object(self.app_storage, "get_store",
                                      lambda: node.store), \
                    mock.patch.object(self.memory, "_ensure_store",
                                      lambda: node.store):
                self.team_index._attached_store = node.store
                yield node
        return ctx()

    def _p(self):
        return self.service.authenticate(self.db,
                                         self.org_client._access_token())

    def test_publish_search_ground_and_delete(self):
        from app.events import Event, Modality
        from app.services import remote_mcp, team_search
        from app.storage import add_delete_hook
        oc = self.org_client
        lead, member = self.Node("lead"), self.Node("member")
        self.nodes += [lead, member]
        add_delete_hook(self.team_index._on_store_change)
        with self.on(lead):
            oc.join("http://org.test", self.lead_invite, "node-lead")
            team = self.service.create_scope(
                self.db, self._p(), self.org_id, kind="team", name="Team",
                parent_id=self.root)["id"]
            self.service.ensure_project(self.db, self._p(), self.org_id,
                                        name="Bayview", parent_id=team)
            code = self.service.create_invite(
                self.db, self._p(), self.org_id,
                email="member@seats.test")["invite_code"]
            oc.heartbeat()
        with self.on(member):
            oc.join("http://org.test", code, "node-member")
            mid = oc.member_id()
        with self.on(lead):
            self.service.set_grant(self.db, self._p(), team, member_id=mid,
                                   permission="propose")
            for name in ("Bayview", "Orbital"):
                lead.store.resolve_entity(name, kind="project", ts=NOW)
            ids = []
            for subj, body, ent in (
                    ("Pricing - approved", "We agreed Core plus intake.",
                     "Bayview"),
                    ("Breach notice", "Orbital considers it a breach.",
                     "Orbital")):
                ids.append(int(lead.store.insert(Event(
                    time=NOW - 3600, modality=Modality.DOCUMENT, raw=body,
                    summary=f"[google.mail] {subj}", source="google.mail",
                    people=["Rachel Osei"], entities=[ent],
                    meta={"origin": "connector", "title": subj,
                          "from": "Rachel Osei <r@bay.test>"}))))
            out = self.team_index.sync(lead.store)
            self.assertEqual((out["mode"], out["published"]), ("central", 2))

        with self.on(member):
            oc.heartbeat()
            res = team_search.search("bayview pricing")
            self.assertEqual([p["title"] for p in res["pointers"]],
                             ["Pricing - approved"])
            self.assertEqual(res["pointers"][0]["location"]["ref"],
                             f"ev:{ids[0]}")
            # Orbital is the lead's own project, never shared: route only.
            res = team_search.search("orbital")
            self.assertEqual(res["pointers"], [])
            self.assertEqual(res["ask"][0]["matched"], ["orbital"])
            self.assertNotIn("Breach", str(res))
            lines = team_search.grounding_lines("what did we agree on bayview?")
            self.assertIn("Pricing - approved", "\n".join(lines))
            grant = {"id": "g", "assistant": "claude", "label": "Claude",
                     "scopes": {"team"}}
            out = remote_mcp.call_tool(grant, "team_search",
                                       {"query": "bayview pricing"})
            self.assertEqual(out["pointers"][0]["title"], "Pricing - approved")

        with self.on(lead):
            lead.store.erase_event(ids[0])
            import threading
            for t in threading.enumerate():
                if t.name == "team-index-withdraw":
                    t.join(timeout=10)
        with self.on(member):
            self.assertEqual(team_search.search("bayview pricing")["pointers"],
                             [])
