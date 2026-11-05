"""A note saved from an assistant reaches a teammate, against the real Org
Record Service (Postgres):

  save -> review -> accept and share -> project record -> teammate searches and
  reads it, cited -> a later note replaces it -> history kept -> removing the
  note takes it off the project.

Skips without QUILL_ORG_TEST_DATABASE_URL (tests/records_pg.py).
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import app.storage
from app.services import assistant_inbox, projects, remote_mcp
from app.services.memory import memory
from app.services.records import org_client, promotion
from app.services.worker import worker
from tests.records_pg import PgTestCase, make_app
from tests.test_records_e2e import Node

GRANT = {"id": "g1", "assistant": "claude", "label": "Claude"}
ENV = {"QUILL_LEARNING": "0", "QUILL_CLAIM_PROPOSE_MIN_CONF": "2",
       "QUILL_CAPTURE_TTL_DAYS": "30", "QUILL_EGRESS_RUNG": "connected"}


class SharedProjects(PgTestCase):
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
        boot = service.bootstrap_org(cls.db, name="Ravenry",
                                     admin_email="lead@e2e.test")
        cls.org_id, cls.root = boot["org_id"], boot["root_scope_id"]
        cls.lead_invite = boot["invite_code"]
        cls.team = None
        cls.lead_membership = None

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in ENV}
        os.environ.update(ENV)
        org_client.set_transport(self.transport)
        from app.services import team_layer
        self.tmp = tempfile.mkdtemp(prefix="project-e2e-")
        self._patches = [
            patch.object(team_layer, "_teams_path",
                         lambda: Path(self.tmp) / "peer_teams.json"),
            patch.object(worker, "enqueue"),
            patch("app.services.attachments._index_event"),
        ]
        for p in self._patches:
            p.start()
        self.nodes = []

    def tearDown(self) -> None:
        org_client.set_transport(None)
        for p in reversed(self._patches):
            p.stop()
        for n in self.nodes:
            n.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def seat(self, name: str):
        n = Node(name)
        self.nodes.append(n)
        return n

    def on(self, node: Node):
        """Run as this seat: its membership, inbox and store."""
        import contextlib

        @contextlib.contextmanager
        def ctx():
            with node.active(), \
                    patch.dict(os.environ, {"QUILL_DATA_DIR": node.dir}), \
                    patch.object(app.storage, "get_store", lambda: node.store), \
                    patch.object(memory, "_ensure_store", lambda: node.store):
                yield node
        return ctx()

    def _p(self):
        return self.service.authenticate(self.db, org_client._access_token())

    def invite(self, email: str, role: str = "member") -> str:
        out = self.service.create_invite(
            self.db, self._p(), self.org_id,
            email=f"{uuid.uuid4().hex[:8]}-{email}", role=role)
        return out["invite_code"]

    def join_team(self):
        """Lead (admin, with a team) and a member who can only propose. The
        bootstrap invite works once, so later tests copy the lead's file."""
        cls = type(self)
        lead, member = self.seat("lead"), self.seat("member")
        with self.on(lead):
            if cls.lead_membership is None:
                org_client.join("http://org.test", self.lead_invite, "node-lead")
                cls.lead_membership = org_client.membership()
                cls.team = self.service.create_scope(
                    self.db, self._p(), self.org_id, kind="team", name="Team",
                    parent_id=self.root)["id"]
            else:
                org_client._save(dict(cls.lead_membership))
            org_client.heartbeat()
            code = self.invite("member@e2e.test")
        with self.on(member):
            org_client.join("http://org.test", code, "node-member")
            mid = org_client.member_id()
        with self.on(lead):
            self.service.set_grant(self.db, self._p(), cls.team, member_id=mid,
                                   permission="propose")
        with self.on(member):
            org_client.heartbeat()
        return lead, member, cls.team

    def save_and_accept(self, node, text, project="Atlas", **kw):
        accept = {k: kw.pop(k) for k in ("replaces", "decision_status",
                                         "share") if k in kw}
        accept.setdefault("share", True)
        out = assistant_inbox.propose(GRANT, {"text": text, "project": project,
                                              **kw})
        res = assistant_inbox.accept(out["inbox_id"],
                                     source=promotion.LIVE_SESSION, **accept)
        self.assertTrue(res["ok"], res)
        return out["inbox_id"], res

    # -----------------------------------------------------------------------
    def test_a_saved_decision_reaches_a_teammate_and_can_be_replaced(self):
        lead, member, team = self.join_team()
        with self.on(lead):
            inbox_id, res = self.save_and_accept(
                lead, "We will use Postgres for the Atlas ledger.",
                kind="decision", decided_by="Lead", title="Ledger database",
                rationale="We need row level security.")
            self.assertEqual(res["project"], "Atlas")
            shared = res["shared"]
            self.assertTrue(shared["ok"], shared)
            self.assertEqual(shared["state"], "recorded")
            record_id = shared["record_id"]
            atlas = next(s for s in org_client.cached_scopes()
                         if s["name"] == "Atlas")
            self.assertEqual((atlas["kind"], atlas["parent_id"]), ("project", team))

        with self.on(member):
            org_client.heartbeat()
            # On the teammate's own list, without having saved anything.
            row = next(p for p in projects.registry() if p["name"] == "Atlas")
            self.assertTrue(row["shared"])
            # Reads the project.
            ctx = projects.context("atlas")
            self.assertEqual(ctx["project"], "Atlas")
            self.assertEqual(ctx["team"], "included")
            rec = ctx["records"][0]
            self.assertEqual(rec["record_id"], record_id)
            self.assertEqual(rec["kind"], "decision")
            self.assertEqual(rec["status"], "decided")
            self.assertEqual(rec["decided_by"], "Lead")
            self.assertEqual(rec["drafted_by"], "Claude")
            self.assertEqual(rec["rationale"], "We need row level security.")
            self.assertTrue(rec["approved_by"])
            # Searches with a question, and chat grounding cites it.
            hits = projects.team_records(None, query="what database for the ledger?")
            self.assertEqual([h["record_id"] for h in hits], [record_id])
            lines = projects.grounding_lines("which database does the ledger use?")
            self.assertIn(record_id, lines[1])
            self.assertEqual(projects.team_records(None, query="kubernetes"), [])
            # Their cloud assistant reads it only with the team permission.
            team_grant = {**GRANT, "scopes": {"team"}}
            out = remote_mcp.project_context(team_grant, {"project": "Atlas"})
            self.assertEqual(out["records"][0]["record_id"], record_id)
            read_only = {**GRANT, "scopes": {"read"}}
            self.assertEqual(remote_mcp.project_context(
                read_only, {"project": "Atlas"})["records"], [])

        with self.on(lead):
            # The reviewer sees what it may replace, and replaces it.
            pending = assistant_inbox.propose(GRANT, {
                "text": "We will use SQLite for the Atlas ledger after all.",
                "project": "Atlas", "kind": "decision", "decided_by": "Lead",
                "title": "Ledger database"})
            similar = assistant_inbox.similar_records(pending["inbox_id"])
            self.assertEqual(similar["records"][0]["record_id"], record_id)
            res = assistant_inbox.accept(
                pending["inbox_id"], share=True, replaces=record_id,
                source=promotion.LIVE_SESSION)
            self.assertEqual(res["shared"]["record_id"], record_id)

        with self.on(member):
            recs = projects.team_records("Atlas")
            self.assertEqual(len(recs), 1, "the old decision must not stay current")
            self.assertIn("SQLite", recs[0]["text"])
            self.assertEqual(recs[0]["version"], 2)
            self.assertEqual(projects.team_records(None, query="postgres"), [])
            versions = org_client.record_versions(record_id)["versions"]
            self.assertIn("Postgres", " ".join(
                str(v["value_json"]) for v in versions))

    def test_a_member_who_can_only_propose_publishes_to_a_project(self):
        lead, member, _ = self.join_team()
        with self.on(member):
            _, res = self.save_and_accept(member, "Beta launch needs SOC 2.",
                                          project="Beta launch",
                                          kind="requirement")
            self.assertEqual(res["shared"]["state"], "recorded", res)
            perms = org_client.cached_permissions(next(
                s["id"] for s in org_client.cached_scopes()
                if s["name"] == "Beta launch"))
            self.assertIn("approve", perms)
        with self.on(lead):
            org_client.heartbeat()
            self.assertEqual(len(projects.team_records("Beta launch")), 1)

    def test_two_teammates_naming_one_project_share_one_scope(self):
        lead, member, _ = self.join_team()
        with self.on(lead):
            self.save_and_accept(lead, "Gamma uses Rust.", project="Gamma")
        with self.on(member):
            self.save_and_accept(member, "Gamma ships in May.", project="GAMMA")
            names = [s["name"] for s in org_client.cached_scopes()
                     if s["kind"] == "project"]
            self.assertEqual(names.count("Gamma"), 1)
            self.assertNotIn("GAMMA", names)
            self.assertEqual(len(projects.team_records("Gamma")), 2)

    def test_not_shared_stays_on_my_seat(self):
        lead, member, _ = self.join_team()
        with self.on(lead):
            self.save_and_accept(lead, "Private Delta thought.", project="Delta",
                                 share=False)
            self.assertEqual(len(projects.filed_notes("Delta")), 1)
            self.assertFalse([s for s in org_client.cached_scopes()
                              if s["name"] == "Delta"])
        with self.on(member):
            org_client.heartbeat()
            self.assertEqual(projects.team_records(None, query="private delta"), [])
            self.assertNotIn("Delta", [p["name"] for p in projects.registry()])

    def test_sharing_needs_a_live_session(self):
        lead, _, _ = self.join_team()
        with self.on(lead):
            out = assistant_inbox.propose(GRANT, {"text": "Epsilon note.",
                                                  "project": "Epsilon"})
            res = assistant_inbox.accept(out["inbox_id"], share=True,
                                         source="http.unverified")
            self.assertTrue(res["ok"], "the note is still accepted")
            self.assertFalse(res["shared"]["ok"])
            self.assertEqual(res["shared"]["code"], "not_live_session")
            self.assertEqual(projects.team_records("Epsilon"), [])
            # A retry from a live session works.
            again = assistant_inbox.share_note(out["inbox_id"],
                                               source=promotion.LIVE_SESSION)
            self.assertEqual(again["state"], "recorded", again)

    def test_removing_a_shared_note_takes_it_off_the_project(self):
        lead, member, _ = self.join_team()
        with self.on(lead):
            inbox_id, res = self.save_and_accept(lead, "Zeta budget is 40k.",
                                                 project="Zeta")
            record_id = res["shared"]["record_id"]
            out = assistant_inbox.remove(inbox_id)
            self.assertTrue(out["ok"], out)
            self.assertNotIn("warning", out)
        with self.on(member):
            org_client.heartbeat()
            self.assertEqual(projects.team_records("Zeta"), [])
            self.assertEqual(projects.team_records(None, query="budget"), [])
            # History stays for audit.
            self.assertTrue(org_client.record_versions(record_id)["versions"])

    def test_a_reader_cannot_retract(self):
        lead, member, _ = self.join_team()
        with self.on(lead):
            _, res = self.save_and_accept(lead, "Eta note.", project="Eta")
            rid = res["shared"]["record_id"]
            code = self.invite("reader@e2e.test", role="viewer")
        reader = self.seat("reader")
        with self.on(reader):
            org_client.join("http://org.test", code, "node-reader")
            with self.assertRaises(org_client.OrgRefused):
                org_client.retract_record(rid)

    def test_a_viewer_cannot_create_a_project(self):
        lead, _, team = self.join_team()
        with self.on(lead):
            code = self.invite("viewer@e2e.test", role="viewer")
        viewer = self.seat("viewer")
        with self.on(viewer):
            org_client.join("http://org.test", code, "node-viewer")
            with self.assertRaises(org_client.OrgRefused):
                org_client.ensure_project("Theta", team)

    def test_audit_chain_still_verifies(self):
        from org_coordinator.records import audit
        lead, _, _ = self.join_team()
        with self.on(lead):
            self.save_and_accept(lead, "Iota note.", project="Iota")
        with self.db.tenant(self.org_id) as repo:
            rows = list(repo.iter_audit())
        self.assertTrue(audit.verify(rows)["ok"])
