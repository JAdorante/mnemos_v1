"""A crash must not take a node out of its org, and a node it did take out can
be restored. The service half needs Postgres (tests/records_pg.py)."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.config
from app.services.records import org_client, retention
from tests.records_pg import PgTestCase, make_app
from tests.test_records_e2e import Node


class DurableWrites(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="membership-")
        real = app.config.settings
        settings = dataclasses.replace(
            real, storage=dataclasses.replace(real.storage, data_dir=self.dir))
        self._p = patch.object(app.config, "settings", settings)
        self._p.start()
        org_client._warned_unreadable = False

    def tearDown(self):
        self._p.stop()
        org_client._warned_unreadable = False
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_membership_reaches_disk_before_the_replace(self):
        order = []
        real_replace = os.replace
        with patch("os.fsync", side_effect=lambda fd: order.append("fsync")), \
                patch("os.replace", side_effect=lambda a, b: (
                    order.append("replace"), real_replace(a, b))):
            org_client._save({"member_id": "m", "credential": "c"})
        self.assertEqual(order, ["fsync", "replace"])
        self.assertTrue(org_client.joined())

    def test_membership_stays_private(self):
        org_client._save({"member_id": "m", "credential": "c"})
        mode = stat.S_IMODE(os.stat(org_client._path()).st_mode)
        self.assertEqual(mode, 0o600)

    def test_org_policy_reaches_disk_before_the_replace(self):
        with patch("os.fsync") as sync:
            retention.save_org_policy({"capture_ttl_days": 30})
        sync.assert_called_once()
        self.assertEqual(retention.org_policy(), {"capture_ttl_days": 30})

    def test_zeroed_file_reads_as_not_joined_and_says_so_once(self):
        org_client._path().write_bytes(b"\0" * 2400)
        with patch("builtins.print") as out:
            self.assertFalse(org_client.joined())
            self.assertFalse(org_client.joined())
        self.assertEqual(out.call_count, 1)
        self.assertIn("unreadable", out.call_args[0][0])

    def test_a_missing_file_is_just_not_joined(self):
        with patch("builtins.print") as out:
            self.assertFalse(org_client.joined())
        out.assert_not_called()


class Reissue(PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from fastapi.testclient import TestClient

        from org_coordinator.records import service
        cls.service = service
        cls.client = TestClient(make_app(cls.db))

        def transport(method, path, body, headers):
            r = cls.client.request(method, path, json=body, headers=headers)
            return r.status_code, r.json()

        cls.transport = staticmethod(transport)
        boot = service.bootstrap_org(cls.db, name="Crash",
                                     admin_email="admin@crash.test")
        cls.org_id, cls.root = boot["org_id"], boot["root_scope_id"]
        cls.invite = boot["invite_code"]

    def setUp(self):
        org_client.set_transport(self.transport)
        from app.services import team_layer
        self.tmp = tempfile.mkdtemp(prefix="reissue-")
        self._teams = patch.object(team_layer, "_teams_path",
                                   lambda: Path(self.tmp) / "peer_teams.json")
        self._teams.start()
        self.node = Node("admin")

    def tearDown(self):
        org_client.set_transport(None)
        self._teams.stop()
        self.node.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_admin_locked_out_by_a_crash_comes_back_as_the_same_member(self):
        svc = self.service
        with self.node.active():
            org_client.join("http://org.test", self.invite, "node-admin")
            before = org_client.membership()
            old_credential = before["credential"]
            p = svc.authenticate(self.db, org_client._access_token())
            scope = svc.create_scope(self.db, p, self.org_id, kind="team",
                                     name="Team", parent_id=self.root)["id"]
            org_client.heartbeat()

            org_client._path().write_bytes(b"\0" * 2400)
            org_client._access.clear()
            self.assertFalse(org_client.joined())

            out = svc.reissue_credential(self.db, self.org_id,
                                         member_id=before["member_id"])
            self.assertEqual(out["node_id"], "node-admin")
            self.assertEqual(out["revoked_credentials"], 1)
            org_client.join("http://org.test", out["invite_code"], out["node_id"])

            after = org_client.membership()
            self.assertEqual(after["member_id"], before["member_id"])
            self.assertEqual(after["node_id"], "node-admin")
            self.assertNotEqual(after["credential"], old_credential)
            self.assertIn("admin", org_client.cached_permissions(scope))

            # The old credential is dead, and the invite works only once.
            with self.assertRaises(svc.ServiceError):
                svc.issue_token(self.db, old_credential)
            with self.assertRaises(org_client.OrgRefused):
                org_client._call("POST", "/join", {
                    "invite_code": out["invite_code"], "node_id": "x",
                    "label": ""}, auth=False)

        with self.db.tenant(self.org_id) as repo:
            rows = list(repo.iter_audit())
        self.assertIn("member.reissue", [r["action"] for r in rows])
        from org_coordinator.records import audit
        self.assertTrue(audit.verify(rows)["ok"])

    def test_only_an_active_member_can_be_reissued(self):
        with self.assertRaises(self.service.ServiceError) as ctx:
            self.service.reissue_credential(self.db, self.org_id,
                                            member_id="mem_nope")
        self.assertEqual(ctx.exception.code, "member_not_found")

    def test_members_view_holds_no_secrets(self):
        rows = self.service.members_view(self.db, self.org_id)
        self.assertTrue(rows)
        blob = json.dumps(rows).lower()
        self.assertNotIn("secret", blob)
        self.assertNotIn("credential", blob)
