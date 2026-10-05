"""Write-back connector contract suites (records layer, Phase 2).

Every connector must: map deterministically; preview the exact change; write
it; prove it by read-back; survive a retry without writing twice; refuse to
overwrite a field that moved since the preview; and classify failures as
transient (retry) or permanent. The same contract runs against in-process
fakes always, and against a real HubSpot sandbox when
QUILL_HUBSPOT_SANDBOX_TOKEN and QUILL_HUBSPOT_SANDBOX_DEAL are set.

No Postgres needed.
"""
from __future__ import annotations

import base64
import json
import os
import unittest

from org_coordinator.connectors import http, secrets
from org_coordinator.connectors.base import (ConflictError, PermanentError,
                                             TransientError, plan_core)
from org_coordinator.connectors.gdrive import GoogleDriveConnector, _reset_tokens
from org_coordinator.connectors.hubspot import HubSpotConnector
from org_coordinator.connectors.mapping import (MappingError, apply_transform,
                                                render_text, validate_row)
from org_coordinator.connectors.webhook import WebhookConnector
from tests.connector_fakes import FakeWorld

STATUS_VERSION = {"kind": "status", "predicate": "deal.stage",
                  "value": {"state": "closedwon", "text": "signed Friday"},
                  "subject_ref": "entity:acme", "subject_label": "Acme",
                  "valid_from": 1_790_000_000.0, "scope_id": "scp_deal",
                  "packet_id": "01J9PKT0000000000000000000"}
COMMIT_VERSION = {"kind": "commitment", "predicate": "owes",
                  "value": {"text": "Send the order form", "owner": "Dana",
                            "counterparty": "Acme", "due": "2026-10-09"},
                  "subject_ref": "member:mem_1", "subject_label": "Dana",
                  "valid_from": 1_790_000_000.0, "scope_id": "scp_deal",
                  "packet_id": "01J9PKT1111111111111111111"}
STAGE_MAP = {"op": "set_property", "field": "dealstage",
             "value_path": "value.state", "transform": "identity"}


class FakeBacked(unittest.TestCase):
    def setUp(self) -> None:
        self.world = FakeWorld()
        http.set_transport(self.world.transport)
        _reset_tokens()

    def tearDown(self) -> None:
        http.set_transport(None)


class ConnectorContract:
    """Mixin: subclasses provide make(), version, mapping, and external hooks."""

    version: dict = STATUS_VERSION
    mapping: dict = STAGE_MAP
    target_hint: str | None = None
    can_conflict = False
    # HubSpot/Drive detect a repeat themselves; a webhook leaves dedupe to the
    # receiver's Idempotency-Key, so only the external count is checked.
    self_dedupes = True

    def make(self):  # pragma: no cover - abstract
        raise NotImplementedError

    def plan(self):
        c = self.make()
        target = c.resolve_target({"external_ref": self.scope_ref},
                                  self.version["subject_ref"], self.target_hint)
        self.assertTrue(target)
        return c, c.map_record(self.version, target, self.mapping)

    def test_map_record_is_deterministic(self):
        c, p1 = self.plan()
        _c, p2 = self.plan()
        self.assertEqual(json.dumps(p1, sort_keys=True),
                         json.dumps(p2, sort_keys=True))
        self.assertEqual(plan_core(p1), plan_core(p2))

    def test_preview_then_write_then_read_back(self):
        c, plan = self.plan()
        diff = c.preview(plan)
        self.assertTrue(diff)
        expected = {d["field"]: d["before"] for d in diff}
        out = c.write(plan, "key-1", expected=expected)
        self.assertEqual(out["skipped"], [])
        self.assertTrue(c.verified(plan, c.read_back(plan)))
        self.assertEqual(self.written_after(), self.preview_after(diff))

    def test_retry_never_writes_twice(self):
        c, plan = self.plan()
        diff = c.preview(plan)
        expected = {d["field"]: d["before"] for d in diff}
        c.write(plan, "key-1", expected=expected)
        before = self.write_count()
        again = c.write(plan, "key-1", expected=expected)
        self.assertEqual(self.write_count(), before)
        if self.self_dedupes:
            self.assertTrue(again["skipped"])

    def test_moved_field_is_a_conflict_not_an_overwrite(self):
        if not self.can_conflict:
            self.skipTest("append/post connectors have no prior value")
        c, plan = self.plan()
        diff = c.preview(plan)
        expected = {d["field"]: d["before"] for d in diff}
        self.external_edit()
        with self.assertRaises(ConflictError):
            c.write(plan, "key-1", expected=expected)
        self.assertNotEqual(self.written_after(), self.preview_after(diff))


class HubSpotContract(ConnectorContract, FakeBacked):
    scope_ref = "hubspot:deal/101"
    can_conflict = True

    def setUp(self) -> None:
        super().setUp()
        self.world.hubspot.add("deals", "101", dealstage="appointmentscheduled",
                               amount="5000")

    def make(self):
        return HubSpotConnector("con_hs", {}, {"token": "hs-token"})

    def written_after(self):
        return self.world.hubspot.objects[("deals", "101")]["properties"]["dealstage"]

    def preview_after(self, diff):
        return diff[0]["after"]

    def write_count(self):
        return len(self.world.hubspot.patches)

    def external_edit(self):
        self.world.hubspot.edit("deals", "101", dealstage="contractsent")

    def test_preview_shows_before_and_after(self):
        c, plan = self.plan()
        self.assertEqual(c.preview(plan), [{"field": "dealstage",
                                            "before": "appointmentscheduled",
                                            "after": "closedwon"}])

    def test_transient_and_permanent_errors(self):
        c, plan = self.plan()
        self.world.hubspot.fail = [503]
        with self.assertRaises(TransientError):
            c.preview(plan)
        self.world.hubspot.fail = [429]
        with self.assertRaises(TransientError):
            c.preview(plan)
        bad = c.map_record(self.version, "hubspot:deals/999", self.mapping)
        with self.assertRaises(PermanentError):
            c.preview(bad)

    def test_team_ref_is_not_a_write_target(self):
        c = self.make()
        self.assertIsNone(c.resolve_target({"external_ref": "hubspot:team/1"},
                                           "entity:x", None))

    def test_note_is_created_once_and_associated(self):
        c = self.make()
        plan = c.map_record(COMMIT_VERSION, "hubspot:deals/101",
                            {"op": "create_note"})
        c.write(plan, "k")
        c.write(plan, "k")
        notes = self.world.hubspot.created["notes"]
        self.assertEqual(len(notes), 1)
        note = next(iter(notes.values()))
        self.assertIn("Dana owes: Send the order form → Acme (due 2026-10-09)",
                      note["properties"]["hs_note_body"])
        self.assertEqual(note["associations"][0]["types"][0]["associationTypeId"], 214)
        self.assertTrue(c.verified(plan, c.read_back(plan)))

    def test_task_due_date_is_the_commitment_due_date(self):
        c = self.make()
        plan = c.map_record(COMMIT_VERSION, "hubspot:deals/101",
                            {"op": "create_task"})
        c.write(plan, "k")
        task = next(iter(self.world.hubspot.created["tasks"].values()))
        self.assertEqual(task["properties"]["hs_timestamp"], "1791504000000")


class GoogleDriveContract(ConnectorContract, FakeBacked):
    scope_ref = "gdrive:doc/DOC1234567890"
    mapping = {"op": "append_entry"}
    version = COMMIT_VERSION

    def setUp(self) -> None:
        super().setUp()
        self.world.docs.add("DOC1234567890")

    def make(self):
        return GoogleDriveConnector("con_gd", {"client_id": "cid"},
                                    {"client_secret": "cs",
                                     "refresh_token": "rt-1"})

    def written_after(self):
        return self.world.docs.text("DOC1234567890").splitlines()[-1]

    def preview_after(self, diff):
        return diff[0]["after"]

    def write_count(self):
        return len(self.world.docs.docs["DOC1234567890"])

    def test_entry_is_dated_and_marked(self):
        _c, plan = self.plan()
        self.assertTrue(plan["text"].startswith("2026-09-21 — Dana owes:"))
        self.assertIn("[sparrow-01J9PKT1111111111111111111]", plan["text"])

    def test_access_token_is_refreshed_once_and_cached(self):
        c, plan = self.plan()
        c.preview(plan)
        c.write(plan, "k")
        c.read_back(plan)
        self.assertEqual(self.world.docs.refreshes, 1)


class WebhookContract(ConnectorContract, FakeBacked):
    scope_ref = None
    mapping = {"op": "post"}
    self_dedupes = False

    def make(self):
        return WebhookConnector("con_wh", {"url": "https://hooks.example.test/in"},
                                {"signing_secret": "whsec"})

    def written_after(self):
        return next(iter(self.world.webhook.received.values()))["value"]

    def preview_after(self, diff):
        return diff[0]["after"]["value"]

    def write_count(self):
        return len(self.world.webhook.received)

    def test_signature_and_idempotency_key(self):
        c, plan = self.plan()
        c.write(plan, "abc:1")
        self.assertIn("abc:1", self.world.webhook.received)
        bad = WebhookConnector("con_wh", {"url": "https://hooks.example.test/in"},
                               {"signing_secret": "wrong"})
        with self.assertRaises(PermanentError):
            bad.write(plan, "abc:2")

    def test_plain_http_refused(self):
        with self.assertRaises(PermanentError):
            WebhookConnector("c", {"url": "http://x"}, {"signing_secret": "s"})


@unittest.skipUnless(os.environ.get("QUILL_HUBSPOT_SANDBOX_TOKEN")
                     and os.environ.get("QUILL_HUBSPOT_SANDBOX_DEAL"),
                     "set QUILL_HUBSPOT_SANDBOX_TOKEN + _DEAL for the live suite")
class HubSpotSandboxContract(ConnectorContract, unittest.TestCase):
    """The same contract against a real HubSpot sandbox account. Writes the
    `description` property of the named sandbox deal."""
    can_conflict = False        # can't edit the sandbox from outside here
    mapping = {"op": "set_property", "field": "description",
               "value_path": "value.text", "transform": "identity"}

    @property
    def scope_ref(self):
        return f"hubspot:deal/{os.environ['QUILL_HUBSPOT_SANDBOX_DEAL']}"

    @property
    def version(self):
        import time
        return dict(STATUS_VERSION, value={"state": "x",
                                           "text": f"sparrow test {time.time()}"})

    def setUp(self) -> None:
        self._v = self.version

    def plan(self):
        c = self.make()
        return c, c.map_record(self._v, self.scope_ref, self.mapping)

    def make(self):
        return HubSpotConnector("con_live", {}, {
            "token": os.environ["QUILL_HUBSPOT_SANDBOX_TOKEN"]})

    def written_after(self):
        c, plan = self.plan()
        return c.read_back(plan)["description"]

    def preview_after(self, diff):
        return diff[0]["after"]

    def write_count(self):
        return 0


class MappingTests(unittest.TestCase):
    def test_transforms(self):
        self.assertEqual(apply_transform("date_ms", "2026-10-09"), "1791504000000")
        self.assertEqual(apply_transform("number", 5000.0), "5000")
        self.assertEqual(apply_transform("number", "12.5"), "12.5")
        self.assertEqual(apply_transform("lower", "ClosedWon"), "closedwon")
        self.assertEqual(apply_transform('enum:{"won": "closedwon"}', "won"),
                         "closedwon")
        with self.assertRaises(MappingError):
            apply_transform('enum:{"won": "closedwon"}', "lost")
        with self.assertRaises(MappingError):
            apply_transform("date_ms", "next friday")

    def test_rows_are_validated(self):
        validate_row({"transform": "identity", "value_path": "value.state"})
        for bad in ({"transform": "eval"}, {"transform": "enum:[1]"},
                    {"transform": "identity", "value_path": "payload.x"}):
            with self.assertRaises(MappingError):
                validate_row(bad)

    def test_field_update_names_its_own_field(self):
        c = HubSpotConnector("c", {}, {"token": "t"})
        v = dict(STATUS_VERSION, kind="field_update", predicate="deal.amount",
                 value={"field": "amount", "value": 12000.0})
        plan = c.map_record(v, "hubspot:deals/1",
                            {"op": "set_property", "field": "$field"})
        self.assertEqual(plan["fields"], {"amount": "12000"})

    def test_render_text_is_fixed(self):
        self.assertEqual(render_text(STATUS_VERSION),
                         "Acme deal.stage: closedwon — signed Friday")


class SecretsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("QUILL_ORG_SECRETS_KEY")
        os.environ["QUILL_ORG_SECRETS_KEY"] = base64.b64encode(b"k" * 32).decode()

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("QUILL_ORG_SECRETS_KEY", None)
        else:
            os.environ["QUILL_ORG_SECRETS_KEY"] = self._prev

    def test_round_trip_and_no_plaintext(self):
        sealed = secrets.seal({"token": "pat-na1-SECRET"}, org_id="o",
                              connector_id="c")
        self.assertNotIn("SECRET", sealed)
        self.assertEqual(secrets.open_(sealed, org_id="o", connector_id="c"),
                         {"token": "pat-na1-SECRET"})

    def test_bound_to_its_connector_and_org(self):
        sealed = secrets.seal({"token": "t"}, org_id="o", connector_id="c")
        for org, con in (("o", "other"), ("other", "c")):
            with self.assertRaises(secrets.SecretsError):
                secrets.open_(sealed, org_id=org, connector_id=con)

    def test_missing_key_refuses(self):
        os.environ.pop("QUILL_ORG_SECRETS_KEY")
        with self.assertRaises(secrets.SecretsError):
            secrets.seal({}, org_id="o", connector_id="c")


if __name__ == "__main__":
    unittest.main()
