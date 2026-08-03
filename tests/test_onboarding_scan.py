"""Onboarding phase-1 system scan: app/git/project signals -> reviewable draft.

Pure/injectable helpers are unit-tested with temp dirs and a fake git runner;
scan() is tested for shape, source vetoes, the disabled flag, and — the load-
bearing contract — that it NEVER ingests (returns a draft only).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from app.services import onboarding_scan as scan


class CleanAppNamesTests(unittest.TestCase):
    def test_filters_noise_and_dedups(self) -> None:
        got = scan.clean_app_names([
            "Google Chrome", "Uninstall Google Chrome", "Cursor",
            "cursor", "Chrome Update", "Node.js Documentation", "  ",
            "Visual Studio Code", "Report a Bug",
        ])
        self.assertEqual(got, ["Cursor", "Google Chrome", "Visual Studio Code"])

    def test_caps_length(self) -> None:
        many = [f"App {i:03d}" for i in range(100)]
        self.assertEqual(len(scan.clean_app_names(many)), scan._MAX_APPS)

    def test_drops_windows_builtins(self) -> None:
        got = scan.clean_app_names([
            "Command Prompt", "Control Panel", "Character Map", "Slack"])
        self.assertEqual(got, ["Slack"])


class SystemFolderTests(unittest.TestCase):
    def test_shortcuts_under_system_folders_skipped(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "Administrative Tools").mkdir()
            (root / "Administrative Tools" / "ODBC.lnk").write_text("x", "utf-8")
            (root / "Accessories").mkdir()
            (root / "Accessories" / "Paint.lnk").write_text("x", "utf-8")
            (root / "Slack.lnk").write_text("x", "utf-8")
            got = scan.installed_apps([root])
        self.assertEqual(got, ["Slack"])   # system-folder shortcuts excluded


class InstalledAppsTests(unittest.TestCase):
    def test_reads_shortcut_stems(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "Sub").mkdir()
            for n in ("Slack.lnk", "Sub/Postman.lnk", "Uninstall Foo.lnk",
                      "notes.txt"):
                (root / n).write_text("x", encoding="utf-8")
            got = scan.installed_apps([root])
        self.assertIn("Slack", got)
        self.assertIn("Postman", got)          # nested .lnk found
        self.assertNotIn("Uninstall Foo", got)  # noise filtered
        self.assertNotIn("notes", got)          # non-.lnk ignored

    def test_missing_root_is_empty(self) -> None:
        self.assertEqual(scan.installed_apps([Path("/no/such/dir/xyz")]), [])


class GitIdentityTests(unittest.TestCase):
    def test_parses_name_and_email(self) -> None:
        def run(args):
            return {("config", "--global", "user.name"): "Ada Lovelace",
                    ("config", "--global", "user.email"): "ada@example.com"
                    }.get(tuple(args), "")
        ident = scan.git_identity(run=run)
        self.assertEqual(ident["name"], "Ada Lovelace")
        self.assertEqual(ident["email"], "ada@example.com")

    def test_drops_bogus_email(self) -> None:
        ident = scan.git_identity(run=lambda a: "not-an-email"
                                  if "email" in a[-1] else "Grace")
        self.assertEqual(ident["name"], "Grace")
        self.assertNotIn("email", ident)

    def test_no_git_config_empty(self) -> None:
        self.assertEqual(scan.git_identity(run=lambda a: ""), {})


class DevProjectsTests(unittest.TestCase):
    def test_detects_marker_dirs_only(self) -> None:
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "webapp" / ".git").mkdir(parents=True)
            (root / "api").mkdir()
            (root / "api" / "pyproject.toml").write_text("x", encoding="utf-8")
            (root / "notes").mkdir()                 # no marker -> skipped
            (root / "node_modules").mkdir()          # junk name -> skipped
            (root / "node_modules" / "package.json").write_text("{}", "utf-8")
            got = scan.dev_projects([root])
        names = {p["name"] for p in got}
        self.assertEqual(names, {"webapp", "api"})
        self.assertTrue(all(p["kind"] == "project" for p in got))

    def test_dedups_across_roots(self) -> None:
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            for base in (a, b):
                (Path(base) / "shared" / ".git").mkdir(parents=True)
            got = scan.dev_projects([Path(a), Path(b)])
        self.assertEqual([p["name"] for p in got], ["shared"])


class RankAppsTests(unittest.TestCase):
    def test_no_usage_keeps_order(self) -> None:
        names = ["Zoom", "Cursor", "Slack"]
        self.assertEqual(scan.rank_apps(names, {}), names)

    def test_usage_lifts_used_apps_by_time(self) -> None:
        names = ["Alpha App", "Cursor", "Slack", "Zoom"]
        usage = {"Cursor": 3600.0, "Zoom": 120.0}
        got = scan.rank_apps(names, usage)
        # Used first (Cursor > Zoom), then the rest alphabetically.
        self.assertEqual(got, ["Cursor", "Zoom", "Alpha App", "Slack"])

    def test_substring_match_from_window_app(self) -> None:
        # Activity app names come from window titles ("Google Chrome").
        got = scan.rank_apps(["Google Chrome", "Notepad"],
                             {"Google Chrome": 500.0})
        self.assertEqual(got[0], "Google Chrome")

    def test_ranking_before_cap_keeps_used_late_app(self) -> None:
        # A heavily-used late-alphabet app survives the cap because ranking runs
        # first (regression guard for the whole point of phase 2).
        names = [f"App {i:02d}" for i in range(59)] + ["Zoom"]
        ranked = scan.rank_apps(names, {"Zoom": 9999.0})
        self.assertEqual(ranked[0], "Zoom")
        self.assertIn("Zoom", ranked[:scan._MAX_APPS])


class BookmarkToolsTests(unittest.TestCase):
    def _write(self, path, urls):
        node = {"roots": {"bookmark_bar": {"children": [
            {"type": "url", "url": u} for u in urls]}}}
        Path(path).write_text(__import__("json").dumps(node), encoding="utf-8")

    def test_only_recognized_domains_surface(self) -> None:
        with TemporaryDirectory() as td:
            f = Path(td) / "Bookmarks"
            self._write(f, [
                "https://github.com/me/repo",
                "https://www.notion.so/workspace",
                "https://my-therapist-portal.example.com/login",  # private
                "https://en.wikipedia.org/wiki/Cats",             # unmapped
            ])
            got = scan.bookmark_tools([f])
        self.assertIn("GitHub", got)
        self.assertIn("Notion", got)
        self.assertEqual(len(got), 2)   # private + unmapped never surface

    def test_ordered_by_frequency(self) -> None:
        with TemporaryDirectory() as td:
            f = Path(td) / "Bookmarks"
            self._write(f, ["https://figma.com/a", "https://github.com/1",
                            "https://github.com/2", "https://github.com/3"])
            got = scan.bookmark_tools([f])
        self.assertEqual(got[0], "GitHub")   # 3 beats Figma's 1

    def test_missing_or_bad_file_empty(self) -> None:
        self.assertEqual(scan.bookmark_tools([Path("/no/such/Bookmarks")]), [])


class MergeToolsTests(unittest.TestCase):
    def test_dedup_case_insensitive_preserve_order(self) -> None:
        got = scan._merge_tools(["GitHub", "Slack"], ["slack", "Cursor"])
        self.assertEqual(got, ["GitHub", "Slack", "Cursor"])


class ScanAssemblyTests(unittest.TestCase):
    def _cfg(self, enabled=True, sources=("apps", "git", "projects")):
        return SimpleNamespace(onboarding=SimpleNamespace(
            scan_enabled=enabled, scan_sources=frozenset(sources)))

    def test_disabled_returns_error_no_work(self) -> None:
        with mock.patch.object(scan, "settings", self._cfg(enabled=False)), \
             mock.patch.object(scan, "installed_apps",
                               side_effect=AssertionError("scanned while off")):
            out = scan.scan()
        self.assertFalse(out["ok"])
        self.assertIn("disabled", out["error"])

    def test_source_veto_skips_that_scanner(self) -> None:
        with mock.patch.object(scan, "settings", self._cfg()), \
             mock.patch.object(scan, "installed_apps", return_value=["Slack"]), \
             mock.patch.object(scan, "_app_usage", return_value={}), \
             mock.patch.object(scan, "git_identity",
                               side_effect=AssertionError("git ran")), \
             mock.patch.object(scan, "dev_projects", return_value=[]):
            out = scan.scan(sources={"apps"})
        self.assertEqual(out["sources"], ["apps"])
        self.assertEqual(out["profile"]["tools"], ["Slack"])

    def test_full_draft_shape(self) -> None:
        with mock.patch.object(scan, "settings", self._cfg()), \
             mock.patch.object(scan, "installed_apps",
                               return_value=["Cursor", "Slack"]), \
             mock.patch.object(scan, "_app_usage", return_value={}), \
             mock.patch.object(scan, "git_identity",
                               return_value={"name": "Ada",
                                             "email": "ada@x.com"}), \
             mock.patch.object(scan, "dev_projects",
                               return_value=[{"name": "webapp", "kind": "project",
                                              "aliases": [], "note": ""}]):
            out = scan.scan()
        self.assertTrue(out["ok"])
        p = out["profile"]
        self.assertEqual(p["identity"]["name"], "Ada")
        self.assertEqual(p["tools"], ["Cursor", "Slack"])
        self.assertEqual(p["projects"][0]["name"], "webapp")
        self.assertIn("ada@x.com", p["notes"])
        self.assertEqual(out["found"]["tools"], 2)
        self.assertEqual(out["found"]["projects"], 1)
        self.assertTrue(out["found"]["identity"])

    def test_bookmarks_opt_in_merges_and_leads(self) -> None:
        with mock.patch.object(scan, "settings", self._cfg()), \
             mock.patch.object(scan, "installed_apps", return_value=["Cursor"]), \
             mock.patch.object(scan, "_app_usage", return_value={}), \
             mock.patch.object(scan, "git_identity", return_value={}), \
             mock.patch.object(scan, "dev_projects", return_value=[]), \
             mock.patch.object(scan, "bookmark_tools",
                               return_value=["GitHub", "Cursor"]):
            out = scan.scan(sources={"apps", "bookmarks"})
        # Bookmark tools lead; app tools follow; dedup drops the repeat "Cursor".
        self.assertEqual(out["profile"]["tools"], ["GitHub", "Cursor"])
        self.assertEqual(out["found"]["bookmark_tools"], 2)
        self.assertIn("bookmarks", out["sources"])

    def test_apps_ranked_by_usage(self) -> None:
        with mock.patch.object(scan, "settings", self._cfg()), \
             mock.patch.object(scan, "installed_apps",
                               return_value=["Cursor", "Slack", "Zoom"]), \
             mock.patch.object(scan, "_app_usage",
                               return_value={"Zoom": 5000.0}), \
             mock.patch.object(scan, "git_identity", return_value={}), \
             mock.patch.object(scan, "dev_projects", return_value=[]):
            out = scan.scan(sources={"apps"})
        self.assertEqual(out["profile"]["tools"][0], "Zoom")  # usage lifts it

    def test_scan_never_ingests(self) -> None:
        # The whole design hinges on this: a scan produces a draft, never a
        # memory write. onboarding_scan must not import/call the ingest path.
        import app.services.onboarding as onb
        with mock.patch.object(scan, "settings", self._cfg()), \
             mock.patch.object(scan, "installed_apps", return_value=[]), \
             mock.patch.object(scan, "_app_usage", return_value={}), \
             mock.patch.object(scan, "git_identity", return_value={}), \
             mock.patch.object(scan, "dev_projects", return_value=[]), \
             mock.patch.object(onb, "ingest",
                               side_effect=AssertionError("scan ingested!")):
            scan.scan()   # must not raise


class UsedAppsTests(unittest.TestCase):
    def test_only_apps_with_usage(self) -> None:
        got = scan.used_apps(["Cursor", "Slack", "Zoom"],
                             {"Zoom": 200.0, "Cursor": 900.0})
        self.assertEqual(got, ["Cursor", "Zoom"])   # used, most-used first

    def test_empty_usage_yields_nothing(self) -> None:
        self.assertEqual(scan.used_apps(["A", "B"], {}), [])


class EnrichTests(unittest.TestCase):
    """enrich() sources scan signals into memory as OBSERVED context — never
    the ACCEPTED tier the survey uses, never approved, never form-fill."""

    def setUp(self) -> None:
        from app.storage import Store
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_scan_"))
        self._prev = os.environ.get("QUILL_ONBOARDING_SCAN_STATE")
        os.environ["QUILL_ONBOARDING_SCAN_STATE"] = str(self.tmp / "scan_state.json")
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("QUILL_ONBOARDING_SCAN_STATE", None)
        else:
            os.environ["QUILL_ONBOARDING_SCAN_STATE"] = self._prev

    def _run(self, sources={"apps", "git", "projects"}):
        with mock.patch.object(scan, "dev_projects",
                               return_value=[{"name": "alpaca_bot", "kind": "project",
                                              "aliases": [], "note": ""}]), \
             mock.patch.object(scan, "installed_apps",
                               return_value=["Cursor", "Slack", "@RISK"]), \
             mock.patch.object(scan, "_app_usage",
                               return_value={"Cursor": 900.0}), \
             mock.patch.object(scan, "bookmark_tools", return_value=["GitHub"]), \
             mock.patch.object(scan, "git_identity",
                               return_value={"name": "Ada", "email": "ada@x.com"}):
            return scan.enrich(sources=sources, store=self.store)

    def test_seeds_entities_and_observed_claims(self) -> None:
        res = self._run({"apps", "git", "projects", "bookmarks"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["projects"], 1)
        self.assertEqual(res["identity"], 1)
        # Only the USED app (Cursor) + bookmark tool (GitHub) enrich — not @RISK.
        ents = {e["name"]: e["kind"] for e in self.store.recent_entities(50)}
        self.assertEqual(ents.get("alpaca_bot"), "project")
        self.assertEqual(ents.get("Cursor"), "tool")
        self.assertEqual(ents.get("GitHub"), "tool")
        self.assertNotIn("@RISK", ents)      # installed-but-unused -> not enriched
        self.assertNotIn("Slack", ents)

    def test_claims_are_observed_and_unreviewed(self) -> None:
        self._run()
        claims = self.store.list_facts(kind="claim", limit=100)
        self.assertTrue(claims)
        # NOT approved — this is observed context, not a human-accepted fact.
        self.assertTrue(all(c["review"] != "approved" for c in claims))
        evs = [ev for _, ev in self.store.all_with_ids()
               if ev.source == scan.ENRICH_SOURCE]
        self.assertTrue(evs)
        self.assertTrue(all(ev.epistemic == "observed" for ev in evs))

    def test_does_not_complete_onboarding(self) -> None:
        # Enrichment must not mark the survey done — that's the user's step.
        prev = os.environ.get("QUILL_ONBOARDING_STATE")
        os.environ["QUILL_ONBOARDING_STATE"] = str(self.tmp / "onb_state.json")
        try:
            self._run()
            from app.services import onboarding
            self.assertFalse(onboarding.status()["completed"])
        finally:
            if prev is None:
                os.environ.pop("QUILL_ONBOARDING_STATE", None)
            else:
                os.environ["QUILL_ONBOARDING_STATE"] = prev

    def test_idempotent_second_run_adds_nothing(self) -> None:
        self._run()
        before = self.store.fact_count()
        res = self._run()
        self.assertEqual(self.store.fact_count(), before)
        self.assertEqual(res["added"], 0)
        self.assertGreater(res["skipped"], 0)

    def test_returns_no_profile_never_form_fill(self) -> None:
        res = self._run()
        # enrich() returns counts, never a draft profile to pre-fill the form.
        self.assertNotIn("profile", res)


class GeneralityTests(unittest.TestCase):
    """Proof the scan is GENERAL-PURPOSE, not tailored to the dev's machine.

    Drives the REAL scanner logic against a synthetic *different* person's
    machine (Bob) and asserts it learns Bob — his projects, his tools, his
    identity — with zero leakage of the developer's own data. This is the
    market guarantee: the same code learns whoever runs it.
    """

    def setUp(self) -> None:
        from app.storage import Store
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_gen_"))
        self._prev = os.environ.get("QUILL_ONBOARDING_SCAN_STATE")
        os.environ["QUILL_ONBOARDING_SCAN_STATE"] = str(self.tmp / "state.json")
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("QUILL_ONBOARDING_SCAN_STATE", None)
        else:
            os.environ["QUILL_ONBOARDING_SCAN_STATE"] = self._prev

    def test_learns_a_completely_different_person(self) -> None:
        import json as _json
        # --- build "Bob's machine" as synthetic inputs -----------------------
        sm = self.tmp / "StartMenu"
        sm.mkdir()
        for n in ("Blender.lnk", "Inkscape.lnk", "Uninstall Blender.lnk"):
            (sm / n).write_text("x", encoding="utf-8")
        code = self.tmp / "code"
        (code / "bobs_blog" / ".git").mkdir(parents=True)
        (code / "weather_cli").mkdir(parents=True)
        (code / "weather_cli" / "pyproject.toml").write_text("x", encoding="utf-8")
        bm = self.tmp / "Bookmarks"
        bm.write_text(_json.dumps({"roots": {"bar": {"children": [
            {"type": "url", "url": "https://figma.com/f"},
            {"type": "url", "url": "https://notion.so/n"}]}}}), encoding="utf-8")

        def bobs_git(args):
            return {("config", "--global", "user.name"): "Bob Smith",
                    ("config", "--global", "user.email"): "bob@example.com"
                    }.get(tuple(args), "")

        # Route enrich's internal calls through the REAL functions on Bob's data.
        real_installed = scan.installed_apps
        real_projects = scan.dev_projects
        real_git = scan.git_identity
        real_bm = scan.bookmark_tools
        with mock.patch.object(scan, "installed_apps",
                               lambda cap=None: real_installed([sm], cap=cap)), \
             mock.patch.object(scan, "dev_projects",
                               lambda: real_projects([code])), \
             mock.patch.object(scan, "git_identity",
                               lambda: real_git(run=bobs_git)), \
             mock.patch.object(scan, "bookmark_tools",
                               lambda: real_bm([bm])), \
             mock.patch.object(scan, "_app_usage",
                               return_value={"Blender": 1000.0}):
            res = scan.enrich(sources={"apps", "git", "projects", "bookmarks"},
                              store=self.store)

        self.assertEqual(res["projects"], 2)
        self.assertEqual(res["identity"], 1)

        ents = {e["name"] for e in self.store.recent_entities(50)}
        self.assertLessEqual({"bobs_blog", "weather_cli", "Blender", "Figma",
                              "Notion"}, ents)
        self.assertNotIn("Inkscape", ents)   # installed but unused -> not learned

        blob = " || ".join(c["text"] for c in
                           self.store.list_facts(kind="claim", limit=100))
        self.assertIn("Bob Smith", blob)
        self.assertIn("bobs_blog", blob)
        # Zero leakage of the developer's own machine into a different user's run.
        for leak in ("Justin", "Adorante", "alpaca", "dtc", "Venture Pulse",
                     "villanova"):
            self.assertNotIn(leak.lower(), blob.lower())


if __name__ == "__main__":
    unittest.main()
