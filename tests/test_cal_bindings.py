"""CAL Stage 0 — the binding table and cold-start seeding.

`kg_node_keys` stops being a name-blocking table and becomes the durable,
O(1) map from observable identifier to graph node. Two contracts matter:

  1. Identity-class keys bind on first sight; convention-class keys must prove
     themselves across distinct days (CAL §3.1).
  2. A fresh install seeds bindings from what the machine already reveals
     BEFORE the first event, so day one doesn't escalate every event
     (CAL §13, failure #10).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.services import onboarding_scan as scan
from app.storage import Store

DAY = 86400.0


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_cal_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "a")
        self.eid = self.store.resolve_entity("Ravenry", "project", ts=time.time())


class BindingTableTests(_StoreCase):
    def test_identity_key_binds_on_first_observation(self) -> None:
        """A git remote is assigned by an authority — there is nothing to
        disambiguate, only something to learn."""
        self.assertTrue(self.store.bind_node_key(
            "entity", self.eid, "repo", "github.com/jadorante/mnemos_v1",
            strength=0.95, key_class="identity"))
        row = self.store.lookup_binding(
            "repo", "github.com/jadorante/mnemos_v1")[0]
        self.assertTrue(row["bindable"])
        self.assertEqual(row["node_id"], self.eid)

    def test_convention_key_needs_repeat_across_days(self) -> None:
        now = time.time()
        self.store.bind_node_key("entity", self.eid, "path", "/home/x/rav",
                                 strength=0.6, ts=now - 2 * DAY)
        self.assertFalse(self.store.lookup_binding(
            "path", "/home/x/rav")[0]["bindable"])
        self.store.bind_node_key("entity", self.eid, "path", "/home/x/rav",
                                 strength=0.6, ts=now - 2 * DAY)
        self.assertFalse(self.store.lookup_binding(
            "path", "/home/x/rav")[0]["bindable"],
            "three sightings in ONE day is one observation, not three")
        self.store.bind_node_key("entity", self.eid, "path", "/home/x/rav",
                                 strength=0.6, ts=now)
        row = self.store.lookup_binding("path", "/home/x/rav")[0]
        self.assertTrue(row["bindable"])
        self.assertEqual(row["n_obs"], 3)
        self.assertEqual(len(json.loads(row["seen_days"])), 2)

    def test_confirmation_short_circuits_the_gate(self) -> None:
        self.store.bind_node_key("entity", self.eid, "path", "/home/x/rav",
                                 strength=0.6, confirmed=True, origin="asserted")
        row = self.store.lookup_binding("path", "/home/x/rav")[0]
        self.assertTrue(row["bindable"])
        self.assertEqual(row["origin"], "asserted")

    def test_reobservation_never_weakens_a_binding(self) -> None:
        """A user correction outranks every later ambient sighting; if a weak
        observation could overwrite it, the same correction would be paid for
        every week (CAL §13, failure #13)."""
        self.store.bind_node_key("entity", self.eid, "repo", "r",
                                 strength=0.95, key_class="identity",
                                 origin="asserted", confirmed=True)
        self.store.bind_node_key("entity", self.eid, "repo", "r",
                                 strength=0.20, key_class="convention",
                                 origin="inferred")
        row = self.store.lookup_binding("repo", "r")[0]
        self.assertEqual(row["strength"], 0.95)
        self.assertEqual(row["key_class"], "identity")
        self.assertEqual(row["origin"], "asserted")
        self.assertEqual(row["confirmed"], 1)

    def test_ambiguity_is_returned_not_resolved(self) -> None:
        """One key mapping to several nodes is the resolver's input, not an
        error the store should paper over."""
        other = self.store.resolve_entity("Tea Leaf", "project", ts=time.time())
        self.store.bind_node_key("entity", self.eid, "path", "/shared",
                                 strength=0.6)
        self.store.bind_node_key("entity", other, "path", "/shared",
                                 strength=0.9)
        rows = self.store.lookup_binding("path", "/shared")
        self.assertEqual([r["node_id"] for r in rows], [other, self.eid],
                         "strongest first")

    def test_invalidate_hides_then_reobservation_revives(self) -> None:
        self.store.bind_node_key("entity", self.eid, "repo", "r",
                                 strength=0.95, key_class="identity")
        self.assertEqual(self.store.invalidate_node_key(
            "entity", self.eid, "repo", "r"), 1)
        self.assertEqual(self.store.lookup_binding("repo", "r"), [])
        self.store.bind_node_key("entity", self.eid, "repo", "r", strength=0.95)
        self.assertEqual(len(self.store.lookup_binding("repo", "r")), 1)

    def test_bindable_only_filters(self) -> None:
        self.store.bind_node_key("entity", self.eid, "path", "/home/x/rav",
                                 strength=0.6)
        self.assertEqual(
            self.store.lookup_binding("path", "/home/x/rav",
                                      bindable_only=True), [])

    def test_merge_carries_binding_strength(self) -> None:
        """Copying only the key/value pair would downgrade an inherited repo
        binding to the 0.5 default, and the winner would start re-escalating
        events the loser had already paid to resolve."""
        loser = self.store.resolve_entity("Ravenry v1", "project", ts=time.time())
        self.store.bind_node_key("entity", loser, "repo", "r", strength=0.95,
                                 key_class="identity", confirmed=True)
        self.store.copy_node_keys("entity", loser, self.eid)
        row = [r for r in self.store.lookup_binding("repo", "r")
               if r["node_id"] == self.eid][0]
        self.assertEqual(row["strength"], 0.95)
        self.assertEqual(row["key_class"], "identity")
        self.assertEqual(row["confirmed"], 1)

    def test_legacy_blocking_keys_keep_working(self) -> None:
        """Pre-CAL rows take the defaults, which is right for them: a
        normalized name is convention-class and ambiguity is expected."""
        rows = self.store.lookup_binding("norm_name", "ravenry")
        self.assertEqual(rows[0]["node_id"], self.eid)
        self.assertEqual(rows[0]["key_class"], "convention")
        self.assertFalse(rows[0]["bindable"])

    def test_stats_report_coverage(self) -> None:
        self.store.bind_node_key("entity", self.eid, "repo", "r",
                                 strength=0.95, key_class="identity")
        st = self.store.binding_stats(key_types=["repo"])
        self.assertEqual(st["by_type"]["repo"], {"n": 1, "identity": 1,
                                                 "confirmed": 0})


class SeedBindingsTests(_StoreCase):
    """The cold-start fix, driven against a synthetic machine.

    Mirrors the generality contract the rest of this scan is held to: the same
    code must learn whoever runs it, with no leakage of the developer's own
    machine into the assertions.
    """

    def _machine(self):
        code = self.tmp / "code"
        (code / "bobs_blog" / ".git").mkdir(parents=True, exist_ok=True)
        (code / "weather_cli").mkdir(parents=True, exist_ok=True)
        (code / "weather_cli" / "pyproject.toml").write_text("x", "utf-8")
        bm = self.tmp / "Bookmarks"
        bm.write_text(json.dumps({"roots": {"bar": {"children": [
            {"type": "url", "url": "https://figma.com/file/abc"},
            {"type": "url", "url": "https://docs.google.com/document/d/1"},
        ]}}}), encoding="utf-8")

        def fake_git(args):
            if args[:1] == ["-C"] and args[2:] == ["config", "--get",
                                                   "remote.origin.url"]:
                return ("git@github.com:bob/bobs_blog.git"
                        if args[1].endswith("bobs_blog") else "")
            if args[:1] == ["-C"] and args[2:] == ["rev-parse", "--abbrev-ref",
                                                   "HEAD"]:
                return "feature/dark-mode"
            return ""
        return code, bm, fake_git

    def _seed(self, sources=("projects", "bookmarks")):
        code, bm, fake_git = self._machine()
        real_projects, real_bm = scan.dev_projects, scan.bookmark_tools
        with mock.patch.object(scan, "_run_git", fake_git), \
             mock.patch.object(scan, "dev_projects",
                               lambda: real_projects([code])), \
             mock.patch.object(scan, "bookmark_tools",
                               lambda: real_bm([bm])):
            return scan.seed_bindings(sources=set(sources), store=self.store)

    def test_seeds_a_repo_before_any_event(self) -> None:
        res = self._seed()
        self.assertTrue(res["ok"])
        rows = self.store.lookup_binding("repo", "github.com/bob/bobs_blog")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["bindable"],
                        "an identity key must be usable on day one")
        self.assertEqual(rows[0]["node_id"],
                         self.store.resolve_entity("bobs_blog", "project"),
                         "the key must point at the project it names")

    def test_repo_root_path_is_identity_by_composition(self) -> None:
        """A repo root is the mount point of something globally unique, so it
        binds immediately; a marker-only folder is a habit and must not."""
        self._seed()
        code = self.tmp / "code"
        with_repo = self.store.lookup_binding(
            "path", os.path.normcase(str((code / "bobs_blog").resolve())))
        without = self.store.lookup_binding(
            "path", os.path.normcase(str((code / "weather_cli").resolve())))
        self.assertTrue(with_repo[0]["bindable"])
        self.assertEqual(with_repo[0]["key_class"], "identity")
        self.assertFalse(without[0]["bindable"])
        self.assertEqual(without[0]["key_class"], "convention")

    def test_branch_is_scoped_and_generic_branches_are_skipped(self) -> None:
        self._seed()
        self.assertEqual(
            len(self.store.lookup_binding(
                "branch", "github.com/bob/bobs_blog#feature/dark-mode")), 1)
        self.assertEqual(self.store.binding_stats(
            key_types=["branch"])["by_type"]["branch"]["n"], 1)

    def test_discriminating_tool_domain_binds_generic_one_does_not(self) -> None:
        """`figma.com` identifies Figma. `google.com` identifies nothing —
        writing it as a binding is how a bank statement ends up attributed to
        whatever was open at the time (CAL §3.2)."""
        self._seed()
        self.assertEqual(len(self.store.lookup_binding("domain", "figma.com")), 1)
        self.assertEqual(self.store.lookup_binding("domain", "google.com"), [])

    def test_is_idempotent_and_accrues_evidence(self) -> None:
        first = self._seed()
        second = self._seed()
        self.assertGreater(first["minted"], 0)
        self.assertEqual(second["minted"], 0, "a re-run mints nothing new")
        self.assertEqual(first["bindings"], second["bindings"])
        row = self.store.lookup_binding("repo", "github.com/bob/bobs_blog")[0]
        self.assertEqual(row["n_obs"], 2, "but it IS another observation")

    def test_source_veto_is_honoured(self) -> None:
        res = self._seed(sources=("projects",))
        self.assertEqual(self.store.lookup_binding("domain", "figma.com"), [])
        self.assertGreater(res["bindings"], 0)

    def test_learns_a_different_person_not_the_developer(self) -> None:
        self._seed()
        types = self.store.binding_stats()["by_type"]
        self.assertIn("repo", types)
        self.assertEqual(
            self.store.lookup_binding("repo", "github.com/jadorante/mnemos_v1"),
            [], "no leakage of the developer's own machine")


class ContactPointSeedTests(_StoreCase):
    """People the graph already knows, made resolvable in one indexed read.

    This is what lets an email at 11:03 land on the same person who spoke in
    Slack at 10:02 without a model being asked who they are.
    """

    def _person(self, name, addr, *, conf=0.9, status="attributed"):
        pid = self.store.resolve_person(name, ts=time.time())
        self.store.upsert_contact_point(
            person_id=pid, type_="email", value_display=addr,
            value_normalized=addr, confidence=conf,
            attribution_method="email_header", verification_status=status,
            source_event_id=None, evidence_quote=None, discourse_role="party",
            ts=time.time(), created_by="system", pipeline_version=1)
        return pid

    def _seed(self):
        with mock.patch.object(scan, "dev_projects", return_value=[]), \
             mock.patch.object(scan, "bookmark_tools", return_value=[]), \
             mock.patch.object(scan, "git_identity", return_value={}):
            return scan.seed_bindings(sources={"projects"}, store=self.store)

    def test_email_binds_to_its_person(self) -> None:
        pid = self._person("Sarah Brennan", "Sarah.B+news@Ravenry.AI")
        res = self._seed()
        self.assertEqual(res["by_type"].get("email"), 1)
        rows = self.store.lookup_binding("email", "sarah.b@ravenry.ai")
        self.assertEqual([r["node_id"] for r in rows], [pid])
        self.assertTrue(rows[0]["bindable"], "an address is an identity")

    def test_weak_attribution_caps_strength(self) -> None:
        """The ADDRESS is an identity; "this address is Sarah's" may not be."""
        self._person("Sarah Brennan", "s@ravenry.ai", conf=0.6)
        self._seed()
        self.assertEqual(
            self.store.lookup_binding("email", "s@ravenry.ai")[0]["strength"],
            0.6)

    def test_low_confidence_contact_is_skipped(self) -> None:
        self._person("Maybe Someone", "who@ravenry.ai", conf=0.2)
        self._seed()
        self.assertEqual(self.store.lookup_binding("email", "who@ravenry.ai"), [])

    def test_hidden_person_is_not_bound(self) -> None:
        pid = self._person("Ambient Name", "amb@ravenry.ai")
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE people SET hide_from_people=1 WHERE id=?", (pid,))
            self.store._conn.commit()
        self._seed()
        self.assertEqual(self.store.lookup_binding("email", "amb@ravenry.ai"), [])

    def test_absorbed_person_binds_to_the_survivor(self) -> None:
        """Binding to a row that has been merged away points the key at a node
        nothing else references."""
        survivor = self._person("Sarah Brennan", "sarah@ravenry.ai")
        dupe = self._person("S. Brennan", "s.brennan@ravenry.ai")
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE people SET canonical_person_id=? WHERE id=?",
                (survivor, dupe))
            self.store._conn.commit()
        self._seed()
        rows = self.store.lookup_binding("email", "s.brennan@ravenry.ai")
        self.assertEqual([r["node_id"] for r in rows], [survivor])


class SeedDisabledTests(_StoreCase):
    def test_disabled_flag_stops_the_machine_scan_not_the_projection(self) -> None:
        """`QUILL_ONBOARDING_SCAN=0` withholds what the machine reveals; it does
        not withhold addresses the user already gave us."""
        pid = self.store.resolve_person("Sarah", ts=time.time())
        self.store.upsert_contact_point(
            person_id=pid, type_="email", value_display="s@ravenry.ai",
            value_normalized="s@ravenry.ai", confidence=0.9,
            attribution_method="email_header", verification_status="attributed",
            source_event_id=None, evidence_quote=None, discourse_role="party",
            ts=time.time(), created_by="system", pipeline_version=1)
        cfg = mock.MagicMock()
        cfg.onboarding.scan_enabled = False
        with mock.patch.object(scan, "settings", cfg), \
             mock.patch.object(scan, "dev_projects") as projects:
            res = scan.seed_bindings(store=self.store)
        projects.assert_not_called()
        self.assertEqual(res["by_type"], {"email": 1})


class ScanDraftShapeTests(unittest.TestCase):
    def test_wizard_draft_keeps_its_original_fields(self) -> None:
        """The draft is posted straight back to /onboarding/ingest — path and
        remote are seeding inputs, not things to ask a human to review."""
        cfg = mock.MagicMock()
        cfg.onboarding.scan_enabled = True
        cfg.onboarding.scan_sources = ("projects",)
        with mock.patch.object(scan, "settings", cfg), \
             mock.patch.object(scan, "dev_projects", return_value=[
                 {"name": "webapp", "kind": "project", "aliases": [],
                  "note": "", "path": "/home/bob/code/webapp",
                  "remote": "git@github.com:bob/webapp.git",
                  "branch": "main"}]):
            out = scan.scan()
        self.assertEqual(set(out["profile"]["projects"][0]),
                         {"name", "kind", "aliases", "note"})


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
