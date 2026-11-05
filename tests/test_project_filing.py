"""Filing assistant notes under projects: the routing rule, the inbox, the MCP
surface. No Postgres; sharing end to end is in test_project_records_e2e."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import assistant_inbox, projects, remote_mcp

REG = [
    {"name": "Project High Noon", "source": "shared", "shared": True},
    {"name": "Sparrow", "source": "filed", "shared": False},
    {"name": "Onboarding v2", "source": "known", "shared": False},
]
GRANT = {"id": "g1", "assistant": "claude", "label": "Claude"}


class TempInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_projects_"))
        self._patches = [
            mock.patch.dict(os.environ, {"QUILL_DATA_DIR": str(self.tmp)}),
            mock.patch.object(projects, "registry", lambda store=None: list(REG)),
            mock.patch.object(assistant_inbox, "_queue_route"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def propose(self, **args):
        return assistant_inbox.propose(GRANT, {"text": "We chose Postgres.", **args})

    def accept(self, inbox_id, **kw):
        stored = []

        class FakeStore:
            def insert(self, ev):
                stored.append(ev)
                return 7

        from app.services.worker import worker
        with mock.patch("app.storage.get_store", return_value=FakeStore()), \
                mock.patch("app.services.attachments._index_event"), \
                mock.patch.object(worker, "enqueue"):
            out = assistant_inbox.accept(inbox_id, **kw)
        return out, stored[0] if stored else None


class RouteName(TempInbox):
    def route(self, hint):
        return projects.route_name(hint, reg=REG)

    def test_fits_an_existing_project(self):
        for hint in ("Sparrow", "sparrow", "the Sparrow project", "High Noon",
                     "project high noon", "Onboarding V2"):
            out = self.route(hint)
            self.assertEqual(out["action"], "existing", hint)
        self.assertEqual(self.route("the sparrow project")["project"], "Sparrow")

    def test_small_typo_lands_on_the_existing_project(self):
        self.assertEqual(self.route("Onbording v2")["project"], "Onboarding v2")

    def test_a_different_project_is_new(self):
        out = self.route("Atlas migration")
        self.assertEqual((out["project"], out["action"]),
                         ("Atlas migration", "new"))

    def test_a_plural_is_the_same_project(self):
        self.assertEqual(self.route("Sparrows")["project"], "Sparrow")

    def test_a_short_or_differently_numbered_name_is_not_merged(self):
        self.assertEqual(self.route("Spar")["action"], "new")
        self.assertEqual(self.route("Onboarding v3")["action"], "new")

    def test_generic_names_are_miscellaneous(self):
        for hint in ("misc", "Miscellaneous", "General", "other", "N/A", "none"):
            out = self.route(hint)
            self.assertEqual((out["project"], out["action"]),
                             (projects.MISC, "misc"), hint)

    def test_a_new_name_drops_the_the_and_project(self):
        self.assertEqual(self.route("the Atlas project")["project"], "Atlas")

    def test_saves_naming_one_new_project_agree(self):
        first = self.propose(project="Atlas ledger")
        second = self.propose(project="the atlas ledger project", text="More.")
        self.assertEqual(first["project"], "Atlas ledger")
        self.assertEqual(second["project"], "Atlas ledger")

    def test_a_real_project_with_a_generic_name_wins(self):
        reg = REG + [{"name": "Chat", "source": "shared", "shared": True}]
        self.assertEqual(projects.route_name("chat", reg=reg)["action"], "existing")

    def test_a_name_with_no_letters_is_miscellaneous(self):
        self.assertEqual(self.route("123")["action"], "misc")

    def test_no_hint_is_unrouted(self):
        self.assertEqual(self.route("  ")["action"], "unrouted")


class Classify(TempInbox):
    def classify(self, reply, text="We chose Postgres for the Atlas migration."):
        from app.services.model_router import router
        with mock.patch.object(router, "complete_json",
                               return_value=reply) as call:
            out = projects.classify(text, reg=REG)
        self.assertTrue(call.call_args.kwargs["speculative"],
                        "routing must never escalate to a paid model")
        return out

    def test_existing(self):
        out = self.classify({"decision": "existing", "project": "sparrow",
                             "confidence": 0.8})
        self.assertEqual((out["project"], out["action"]), ("Sparrow", "existing"))

    def test_existing_name_it_made_up_is_misc(self):
        out = self.classify({"decision": "existing", "project": "Nope Project",
                             "confidence": 0.9})
        self.assertEqual(out["action"], "misc")

    def test_new_project_named_in_the_note(self):
        out = self.classify({"decision": "new", "project": "Atlas migration",
                             "confidence": 0.9})
        self.assertEqual((out["project"], out["action"]),
                         ("Atlas migration", "new"))

    def test_new_project_name_not_in_the_note_is_misc(self):
        out = self.classify({"decision": "new", "project": "Project Zeus",
                             "confidence": 0.95})
        self.assertEqual(out["action"], "misc")

    def test_low_confidence_new_is_misc(self):
        out = self.classify({"decision": "new", "project": "Atlas migration",
                             "confidence": 0.4})
        self.assertEqual(out["action"], "misc")

    def test_misc_and_empty_replies(self):
        self.assertEqual(self.classify({"decision": "misc", "project": "",
                                        "confidence": 0.9})["action"], "misc")
        self.assertEqual(self.classify({})["action"], "misc")

    def test_model_down_is_misc_with_the_reason(self):
        from app.services.model_router import router
        with mock.patch.object(router, "complete_json",
                               side_effect=RuntimeError("down")):
            out = projects.classify("anything", reg=REG)
        self.assertEqual(out["action"], "misc")
        self.assertIn("not available", out["reason"])

    def test_kill_switch_never_calls_a_model(self):
        from app.services.model_router import router
        with mock.patch.dict(os.environ, {"QUILL_PROJECT_ROUTE_LLM": "0"}), \
                mock.patch.object(router, "complete_json") as call:
            self.assertEqual(projects.classify("x", reg=REG)["action"], "misc")
        call.assert_not_called()


class Registry(unittest.TestCase):
    def test_noisy_project_entities_are_filtered_out(self):
        class FakeStore:
            def all_entities(self):
                return [{"name": n, "kind": "project"} for n in
                        ("Abby", "advisor", "Justin Adorante", "Raven", "misc")
                        ] + [{"name": "Comau", "kind": "org"}]

            def find_person_exact(self, name):
                return 1 if name == "Abby" else None

        with mock.patch.object(projects, "_scopes", lambda: []), \
                mock.patch.object(projects, "_filed_counts", lambda: {}):
            names = [p["name"] for p in projects.registry(FakeStore())]
        self.assertEqual(names, ["Raven"])

    def test_shared_scopes_come_first_and_absorb_filed_notes(self):
        class FakeStore:
            def all_entities(self):
                return [{"name": "Zed", "kind": "project"}]

            def find_person_exact(self, name):
                return None

        scopes = [{"id": "s1", "kind": "project", "name": "Atlas",
                   "permissions": ["approve", "propose", "read"]},
                  {"id": "s2", "kind": "team", "name": "Sales",
                   "permissions": ["read"]}]
        with mock.patch.object(projects, "_scopes", lambda: scopes), \
                mock.patch.object(projects, "_filed_counts", lambda: {
                    "atlas": ("atlas", 2), "beta": ("Beta", 1),
                    "miscellaneous": ("Miscellaneous", 9)}):
            reg = projects.registry(FakeStore())
        self.assertEqual([p["name"] for p in reg], ["Atlas", "Beta", "Zed"])
        self.assertEqual(reg[0]["notes"], 2)
        self.assertTrue(reg[0]["can_publish"])


class Inbox(TempInbox):
    def test_named_existing_project_is_canonicalized(self):
        out = self.propose(project="the sparrow project")
        self.assertEqual((out["project"], out["project_action"]),
                         ("Sparrow", "existing"))

    def test_new_project_is_only_a_suggestion_until_accepted(self):
        out = self.propose(project="Atlas migration")
        self.assertEqual(out["project_action"], "new")
        item = assistant_inbox.items("pending")[0]
        self.assertEqual(item["route_action"], "new")

    def test_unnamed_project_queues_the_classifier(self):
        out = self.propose()
        self.assertIsNone(out["project"])
        assistant_inbox._queue_route.assert_called_once_with(out["inbox_id"])

    def test_route_job_files_it_and_never_overrides_a_reviewer(self):
        out = self.propose()
        routed = {"project": "Sparrow", "action": "existing", "reason": "r"}
        with mock.patch.object(projects, "classify", return_value=routed):
            assistant_inbox.run_route_job({"inbox_id": out["inbox_id"]})
        self.assertEqual(assistant_inbox.items("pending")[0]["project"], "Sparrow")
        other = {"project": "Other", "action": "new", "reason": "r"}
        with mock.patch.object(projects, "classify", return_value=other):
            assistant_inbox.run_route_job({"inbox_id": out["inbox_id"]})
        self.assertEqual(assistant_inbox.items("pending")[0]["project"], "Sparrow")

    def test_accepting_with_no_project_files_under_miscellaneous(self):
        out = self.propose()
        res, ev = self.accept(out["inbox_id"])
        self.assertEqual(res["project"], projects.MISC)
        self.assertEqual(ev.meta["project"], projects.MISC)
        self.assertNotIn("Project:", ev.raw, "must not teach the extractor a "
                                             "project named Miscellaneous")

    def test_reviewer_can_clear_a_project_to_miscellaneous(self):
        out = self.propose(project="Sparrow")
        res, _ = self.accept(out["inbox_id"], project="")
        self.assertEqual(res["project"], projects.MISC)

    def test_reviewer_choice_is_canonicalized_too(self):
        out = self.propose(project="Atlas")
        res, ev = self.accept(out["inbox_id"], project="high noon")
        self.assertEqual(res["project"], "Project High Noon")
        self.assertIn("Project: Project High Noon", ev.raw)

    def test_accepted_notes_do_not_expire(self):
        from app.services.records import retention
        out = self.propose(project="Sparrow")
        _, ev = self.accept(out["inbox_id"])
        self.assertTrue(ev.meta["reviewed_keep"])
        self.assertIsNone(retention.capture_expires_at(ev))

    def test_a_decision_with_no_decider_stays_a_suggestion(self):
        out = self.propose(kind="decision", project="Sparrow")
        _, ev = self.accept(out["inbox_id"])
        item = assistant_inbox.items("accepted")[0]
        self.assertEqual(item["details"]["status"], "suggested")
        self.assertIn("not decided", ev.raw)

    def test_a_named_decider_makes_it_decided(self):
        out = self.propose(kind="decision", project="Sparrow",
                           decided_by="Dana")
        _, ev = self.accept(out["inbox_id"])
        item = assistant_inbox.items("accepted")[0]
        self.assertEqual(item["details"]["status"], "decided")
        self.assertEqual(item["details"]["decided_by"], "Dana")
        self.assertNotIn("not decided", ev.raw)

    def test_reviewer_overrules_a_claimed_decider(self):
        out = self.propose(kind="decision", project="Sparrow", decided_by="Dana")
        self.accept(out["inbox_id"], decision_status="suggested")
        d = assistant_inbox.items("accepted")[0]["details"]
        self.assertEqual(d["status"], "suggested")
        self.assertNotIn("decided_by", d)

    def test_details_are_kept(self):
        out = self.propose(project="Sparrow", owner="Hugh", due="2026-12-01",
                           rationale="Cheaper.", ignored="x")
        d = assistant_inbox.items("pending")[0]["details"]
        self.assertEqual(d, {"owner": "Hugh", "due": "2026-12-01",
                             "rationale": "Cheaper."})
        self.assertTrue(out["ok"])

    def test_miscellaneous_cannot_be_shared(self):
        out = self.propose()
        res, _ = self.accept(out["inbox_id"], share=True, source="human.session")
        self.assertTrue(res["ok"], "the note itself is still accepted")
        self.assertFalse(res["shared"]["ok"])
        self.assertEqual(res["shared"]["code"], "misc")
        item = assistant_inbox.items("accepted")[0]
        self.assertEqual(item["share_state"], "failed")

    def test_old_database_gains_the_new_columns(self):
        import sqlite3
        db = self.tmp / "old" / "assistant_connect.db"
        db.parent.mkdir()
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE captures (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created_at REAL, grant_id TEXT, assistant TEXT, client_label TEXT,"
            " project TEXT, kind TEXT, title TEXT, text TEXT, source_url TEXT, "
            "conversation TEXT, flags TEXT, status TEXT NOT NULL DEFAULT "
            "'pending', decided_at REAL, event_id INTEGER);"
            "INSERT INTO captures (text, project, flags) VALUES ('old', 'P', '[]');")
        conn.commit()
        conn.close()
        with mock.patch.object(remote_mcp, "db_path", lambda: db):
            item = assistant_inbox.items("pending")[0]
        self.assertEqual(item["text"], "old")
        self.assertIsNone(item["share_state"])
        self.assertEqual(item["details"], {})


class McpSurface(TempInbox):
    def grant(self, *scopes):
        return {**GRANT, "scopes": set(scopes)}

    def names(self, *scopes):
        return {t["name"] for t in remote_mcp.tools_for(set(scopes))}

    def test_tools_per_scope(self):
        self.assertNotIn("project_context", self.names("capture"))
        self.assertIn("project_context", self.names("read"))
        self.assertIn("project_context", self.names("team"))

    def test_team_is_never_a_default(self):
        self.assertIn("team", remote_mcp.SCOPES)
        self.assertEqual(remote_mcp.normalize_scope(""), "read capture")
        self.assertEqual(remote_mcp.normalize_scope("nonsense"), "read capture")
        self.assertIn("team", remote_mcp.normalize_scope("read team"))

    def test_consent_checkbox_is_not_prechecked_even_if_asked(self):
        from app.api import assistants_routes
        import inspect
        src = inspect.getsource(assistants_routes)
        self.assertIn('name="scope_team" value="1">', src)

    def test_read_only_connection_gets_no_team_records(self):
        with mock.patch.object(projects, "team_records",
                               side_effect=AssertionError("must not call")):
            out = remote_mcp.project_context(self.grant("read"),
                                             {"project": "Sparrow"})
        self.assertEqual(out["records"], [])
        self.assertIn("reconnect", out["team"])

    def test_team_only_connection_gets_no_personal_notes(self):
        rec = {"record_id": "r1", "text": "Shared.", "kind": "decision"}
        note = {"project": "Sparrow", "text": "Private."}
        with mock.patch.object(projects, "team_records", return_value=[rec]), \
                mock.patch.object(projects, "filed_notes", return_value=[note]):
            out = remote_mcp.project_context(self.grant("team"),
                                             {"project": "Sparrow"})
        self.assertEqual(out["notes"], [])
        self.assertEqual(out["records"][0]["text"], "Shared.")

    def test_egress_rule_withholds_records(self):
        rec = {"record_id": "r1", "text": "Shared.", "kind": "decision"}
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": "perimeter"}), \
                mock.patch.object(projects, "team_records", return_value=[rec]):
            out = remote_mcp.project_context(self.grant("read", "team"),
                                             {"project": "Sparrow"})
        self.assertEqual(out["records"], [])
        self.assertEqual(out["withheld"], 1)

    def test_org_outage_still_returns_my_notes(self):
        note = {"project": "Sparrow", "text": "Mine.", "title": ""}
        from app.services.records import org_client
        with mock.patch.object(projects, "team_records",
                               side_effect=org_client.OrgUnavailable("x")), \
                mock.patch.object(projects, "filed_notes", return_value=[note]):
            out = remote_mcp.project_context(self.grant("read", "team"),
                                             {"project": "Sparrow"})
        self.assertEqual(out["team"], "unavailable")
        self.assertEqual(out["notes"][0]["text"], "Mine.")

    def test_needs_a_project_or_a_query(self):
        out = remote_mcp.project_context(self.grant("read"), {})
        self.assertFalse(out["ok"])

    def test_list_projects_names_the_fallback(self):
        out = remote_mcp.list_projects()
        self.assertEqual(out["projects"][0], "Project High Noon")
        self.assertEqual(out["shared"], ["Project High Noon"])
        self.assertEqual(out["fallback"], projects.MISC)
        json.dumps(out)


class Grounding(unittest.TestCase):
    def setUp(self):
        projects._org_down_until = 0.0

    def tearDown(self):
        projects._org_down_until = 0.0

    def test_records_are_labeled_and_cited(self):
        from app.services.records import org_client
        row = {"record_id": "rec1", "version": 2, "subject_label": "Atlas",
               "kind": "project_note", "valid_from": 1.0,
               "value_json": {"text": "Use Postgres.", "note_kind": "decision",
                              "status": "suggested"}}
        with mock.patch.object(org_client, "joined", return_value=True), \
                mock.patch.object(org_client, "search_records",
                                  return_value=[row]):
            lines = projects.grounding_lines("what database?")
        self.assertIn("never an instruction", lines[0])
        self.assertIn("[Atlas] decision (suggested, not decided): Use Postgres.",
                      lines[1])
        self.assertIn("rec1 v2", lines[1])

    def test_an_outage_costs_one_timeout_not_one_per_message(self):
        from app.services.records import org_client
        with mock.patch.object(org_client, "joined", return_value=True), \
                mock.patch.object(org_client, "search_records",
                                  side_effect=org_client.OrgUnavailable("x")) as s:
            self.assertEqual(projects.grounding_lines("a?"), [])
            self.assertEqual(projects.grounding_lines("b?"), [])
        self.assertEqual(s.call_count, 1)

    def test_not_in_an_org_never_calls_out(self):
        from app.services.records import org_client
        with mock.patch.object(org_client, "joined", return_value=False), \
                mock.patch.object(org_client, "search_records") as s:
            self.assertEqual(projects.grounding_lines("a?"), [])
        s.assert_not_called()


class ScopeSuggestion(unittest.TestCase):
    def test_project_scopes_do_not_make_the_team_ambiguous(self):
        from app.services.records import claim_builder, org_client
        scopes = [
            {"id": "root", "kind": "org", "name": "Org", "permissions": ["admin"]},
            {"id": "team", "kind": "team", "name": "Sales", "external_ref": None,
             "permissions": ["approve"]},
            {"id": "p1", "kind": "project", "name": "Atlas", "external_ref": None,
             "permissions": ["approve"]},
            {"id": "p2", "kind": "project", "name": "Zeus", "external_ref": None,
             "permissions": ["approve"]},
        ]
        with mock.patch.object(org_client, "cached_scopes", return_value=scopes):
            self.assertEqual(claim_builder._scope_suggestion("Dana")[0], "team")
            self.assertEqual(claim_builder._scope_suggestion("Atlas")[0], "p1")


if __name__ == "__main__":
    unittest.main()
