"""WS2d — browser URL anchor (Windows).

Covers the parts that are platform-independent by construction: the
registrable-domain parse, the guard/cache layer around the UIA read (the COM
call itself is a replaceable seam), the agent-suppression flag, the identifier
truncation + host-collision suppression, and the two flagged consumption paths.

The COM read is NOT exercised here — it cannot be, off Windows. What is
exercised is every decision made around it, which is where the failure modes
that matter (a wrong domain, a leaked path, a stalled tick) actually live.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.perception import identifiers, psl, uia_url
from app.perception.schemas import MetaEvent
from app.services import agent_activity


class _EnvMixin:
    def _env(self, **kv):
        """Set env for the test and restore after — the runtime knobs read
        env-first at call time, so this never touches the frozen settings."""
        old = {k: os.environ.get(k) for k in kv}

        def _restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        for k, v in kv.items():
            os.environ[k] = v
        self.addCleanup(_restore)


# --------------------------------------------------------------------------
class PublicSuffixTests(unittest.TestCase):
    def test_registrable_domain_beats_last_two_labels(self):
        # Each of these is wrong under the old last-two-labels split.
        self.assertEqual(psl.registrable_domain("https://www.bbc.co.uk/news"),
                         "bbc.co.uk")
        self.assertEqual(psl.registrable_domain("owner.github.io"),
                         "owner.github.io")
        self.assertEqual(psl.registrable_domain("a.b.mnemos.dev"), "mnemos.dev")
        self.assertEqual(psl.registrable_domain("x.s3.amazonaws.com"),
                         "x.s3.amazonaws.com")

    def test_no_domain_is_none_not_a_guess(self):
        # NB: psl is scheme-agnostic by design — rejecting non-http schemes
        # is uia_url.valid_url's job, and it is tested there.
        for bad in ("co.uk", "http://192.168.1.4/x", "localhost", "",
                    "https://[::1]/x", "a"):
            self.assertIsNone(psl.registrable_domain(bad), bad)

    def test_userinfo_port_case_and_path_are_stripped(self):
        self.assertEqual(
            psl.registrable_domain("https://user:pw@Sub.Example.COM:8443/p?q=1#f"),
            "example.com")

    def test_wildcard_and_exception_rules(self):
        self.assertEqual(psl.registrable_domain("www.ck"), "www.ck")  # !www.ck
        self.assertIsNone(psl.registrable_domain("foo.ck"))           # *.ck

    def test_sld_label_is_the_matchable_surface(self):
        self.assertEqual(psl.sld_label("https://acme.co.uk/x"), "acme")
        self.assertEqual(psl.sld_label("owner.github.io"), "owner")

    def test_list_is_vendored_and_pinned(self):
        self.assertGreater(psl.rule_count(), 5000)
        self.assertNotEqual(psl.version(), "unknown")


# --------------------------------------------------------------------------
class UrlShapeTests(unittest.TestCase):
    def test_only_absolute_http_urls_with_a_real_domain(self):
        self.assertEqual(uia_url.valid_url("https://github.com/a/b"),
                         "https://github.com/a/b")
        for bad in ("chrome://settings", "about:blank", "file:///c:/x",
                    "https://co.uk/x", "not a url", "", None,
                    "https://a.com/x\nhttps://b.com"):
            self.assertIsNone(uia_url.valid_url(bad), bad)

    def test_query_and_fragment_never_reach_storage(self):
        self.assertEqual(
            uia_url.strip_path_secrets("https://a.com/reset?token=abc#t=1"),
            "https://a.com/reset")


class StorableUrlTests(_EnvMixin, unittest.TestCase):
    def test_domain_only_is_the_default(self):
        self._env(QUILL_PERCEPTION_URL_FULL="0")
        self.assertIsNone(uia_url.storable_url("https://a.com/p"))

    def test_full_flag_stores_a_stripped_path(self):
        self._env(QUILL_PERCEPTION_URL_FULL="1")
        self.assertEqual(uia_url.storable_url("https://a.com/p?token=x#y"),
                         "https://a.com/p")


# --------------------------------------------------------------------------
class UiaGuardTests(_EnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        uia_url.reset_cache()
        uia_url.reset_stats()
        agent_activity.reset()
        self.addCleanup(uia_url.reset_cache)
        self.addCleanup(uia_url.reset_stats)
        self.addCleanup(agent_activity.reset)

    def _reader(self, url, rejection=None):
        calls = []

        def _fn(hwnd):
            calls.append(hwnd)
            return url, rejection
        return _fn, calls

    def _drain(self):
        """The worker is a real thread; wait for it to settle."""
        for _ in range(200):
            uia_url._requests.join() if False else None
            time.sleep(0.005)
            with uia_url._lock:
                if not uia_url._inflight:
                    return
        self.fail("uia_url worker never drained")

    def test_disabled_by_default_reads_nothing(self):
        self._env(QUILL_PERCEPTION_URL="0")
        fn, calls = self._reader("https://a.com/x")
        with patch.object(uia_url, "_reader", fn):
            self.assertIsNone(uia_url.current_url(1, "t", "chrome.exe"))
        self.assertEqual(calls, [])

    def test_non_browser_apps_are_never_read(self):
        self._env(QUILL_PERCEPTION_URL="1")
        fn, calls = self._reader("https://a.com/x")
        with patch.object(uia_url, "_reader", fn):
            self.assertIsNone(uia_url.current_url(1, "t", "notepad.exe"))
        self.assertEqual(calls, [])

    def test_first_call_is_non_blocking_and_the_next_one_has_it(self):
        # The L0 tick must never wait on COM: a miss answers None NOW and the
        # value lands on a later tick.
        self._env(QUILL_PERCEPTION_URL="1")
        fn, calls = self._reader("https://github.com/JAdorante/mnemos_v1")
        with patch.object(uia_url, "_reader", fn):
            self.assertIsNone(uia_url.current_url(7, "repo — Chrome",
                                                  "chrome.exe"))
            self._drain()
            self.assertEqual(
                uia_url.current_url(7, "repo — Chrome", "chrome.exe"),
                "https://github.com/JAdorante/mnemos_v1")
            # Same (hwnd, title) key → still exactly one COM call.
            uia_url.current_url(7, "repo — Chrome", "chrome.exe")
        self.assertEqual(len(calls), 1)

    def test_a_title_change_invalidates_the_cache(self):
        self._env(QUILL_PERCEPTION_URL="1")
        fn, calls = self._reader("https://a.com/x")
        with patch.object(uia_url, "_reader", fn):
            uia_url.current_url(7, "tab one", "chrome.exe")
            self._drain()
            uia_url.current_url(7, "tab two", "chrome.exe")
            self._drain()
        self.assertEqual(len(calls), 2)

    def test_a_rejected_shape_caches_none_not_a_guess(self):
        self._env(QUILL_PERCEPTION_URL="1")
        fn, _ = self._reader("chrome://newtab")
        with patch.object(uia_url, "_reader", fn):
            uia_url.current_url(7, "t", "chrome.exe")
            self._drain()
            self.assertIsNone(uia_url.current_url(7, "t", "chrome.exe"))
        by_app = uia_url.stats()["by_app"]["chrome.exe"]
        self.assertEqual(by_app["url_rejected_shape"], 1)
        self.assertEqual(by_app["url_ok"], 0)

    def test_reader_rejections_are_counted_per_browser(self):
        self._env(QUILL_PERCEPTION_URL="1")
        fn, _ = self._reader(None, "url_rejected_focus")
        with patch.object(uia_url, "_reader", fn):
            uia_url.current_url(7, "t", "firefox.exe")
            self._drain()
        stats = uia_url.stats()["by_app"]
        self.assertEqual(stats["firefox.exe"]["url_rejected_focus"], 1)
        self.assertNotIn("chrome.exe", stats)

    def test_agent_driven_browsing_is_suppressed(self):
        self._env(QUILL_PERCEPTION_URL="1")
        agent_activity.set_browser_run(True)
        fn, calls = self._reader("https://a.com/x")
        with patch.object(uia_url, "_reader", fn):
            self.assertIsNone(uia_url.current_url(7, "t", "chrome.exe"))
        self.assertEqual(calls, [])
        self.assertEqual(
            uia_url.stats()["by_app"]["chrome.exe"]["url_suppressed_agent"], 1)

    def test_current_domain_parses_through_psl(self):
        self._env(QUILL_PERCEPTION_URL="1")
        fn, _ = self._reader("https://www.bbc.co.uk/news/uk-1")
        with patch.object(uia_url, "_reader", fn):
            uia_url.current_url(7, "t", "chrome.exe")
            self._drain()
            self.assertEqual(uia_url.current_domain(7, "t", "chrome.exe"),
                             "bbc.co.uk")


# --------------------------------------------------------------------------
class AgentActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        agent_activity.reset()
        self.addCleanup(agent_activity.reset)

    def test_set_and_clear(self):
        self.assertFalse(agent_activity.browser_run_active())
        agent_activity.set_browser_run(True)
        self.assertTrue(agent_activity.browser_run_active())
        agent_activity.set_browser_run(False)
        self.assertFalse(agent_activity.browser_run_active())

    def test_a_crashed_run_expires_instead_of_suppressing_forever(self):
        t0 = 1_000_000.0
        with patch.dict(os.environ, {"QUILL_AGENT_RUN_TTL_S": "60"}):
            agent_activity.set_browser_run(True, now=t0)
            self.assertTrue(agent_activity.browser_run_active(now=t0 + 59))
            self.assertFalse(agent_activity.browser_run_active(now=t0 + 61))

    def test_heartbeat_extends_a_live_run(self):
        t0 = 1_000_000.0
        with patch.dict(os.environ, {"QUILL_AGENT_RUN_TTL_S": "60"}):
            agent_activity.set_browser_run(True, now=t0)
            agent_activity.heartbeat(now=t0 + 50)
            self.assertTrue(agent_activity.browser_run_active(now=t0 + 100))


# --------------------------------------------------------------------------
class TrustedUrlIdentifierTests(unittest.TestCase):
    def _kinds(self, idents, kind):
        return [i for i in idents if i["kind"] == kind]

    def test_repo_slug_from_the_trusted_url(self):
        got = identifiers.extract_identifiers(
            "", browser_url="https://github.com/JAdorante/mnemos_v1/pull/12")
        repo = self._kinds(got, "repo")
        self.assertEqual(repo[0]["value"], "JAdorante/mnemos_v1")
        self.assertEqual(repo[0]["norm"], "mnemos_v1")
        self.assertEqual(repo[0]["src"], "browser_url")

    def test_the_full_url_flag_has_no_back_door_through_identifiers(self):
        # `value` persists to quill.db. Truncation happens unconditionally, so
        # a deep path cannot be stored under QUILL_PERCEPTION_URL_FULL=0.
        got = identifiers.extract_identifiers(
            "", browser_url="https://acme.com/orgs/acme/settings/billing"
                            "?token=secret#frag")
        url = self._kinds(got, "url")[0]
        self.assertEqual(url["value"], "https://acme.com/orgs")
        for ident in got:
            self.assertNotIn("token", ident["value"])
            self.assertNotIn("billing", ident["value"])

    def test_the_trusted_url_takes_the_first_cap_slot(self):
        got = identifiers.extract_identifiers(
            "github.com/other/thing " * 5,
            browser_url="https://github.com/JAdorante/mnemos_v1")
        self.assertEqual(got[0]["src"], "browser_url")

    def test_same_host_ocr_hits_are_suppressed(self):
        # An address-bar misread produces a different norm, so _seen_add
        # cannot dedupe it — both would reach the resolver at score 1.0.
        got = identifiers.extract_identifiers(
            "github.com/JAdorante/mnemos_vl",
            browser_url="https://github.com/JAdorante/mnemos_v1")
        norms = {i["norm"] for i in got}
        self.assertIn("mnemos_v1", norms)
        self.assertNotIn("mnemos_vl", norms)

    def test_www_prefix_does_not_defeat_the_collision_rule(self):
        got = identifiers.extract_identifiers(
            "github.com/JAdorante/mnemos_vl",
            browser_url="https://www.github.com/JAdorante/mnemos_v1")
        self.assertNotIn("mnemos_vl", {i["norm"] for i in got})

    def test_a_different_host_survives(self):
        # A link in the page body is a real identifier, not a misread.
        got = identifiers.extract_identifiers(
            "see gitlab.com/team/widget for the port",
            browser_url="https://github.com/JAdorante/mnemos_v1")
        self.assertIn("widget", {i["norm"] for i in got})

    def test_a_host_misread_is_a_named_residual_not_a_closed_case(self):
        # Documented limitation: a misread of the HOST collides with nothing.
        got = identifiers.extract_identifiers(
            "githuh.com/JAdorante/mnemos_vl",
            browser_url="https://github.com/JAdorante/mnemos_v1")
        self.assertIn("githuh.com/JAdorante", {i["norm"] for i in got})

    def test_domain_identifier_uses_the_matchable_label(self):
        got = identifiers.extract_identifiers(
            "", browser_url="https://acme.co.uk/portal/x")
        dom = self._kinds(got, "domain")[0]
        self.assertEqual((dom["value"], dom["norm"]), ("acme.co.uk", "acme"))

    def test_infrastructure_domains_never_become_candidates(self):
        for url in ("https://www.google.com/search/x",
                    "https://github.com/a/b",
                    "https://app.slack.com/client/x"):
            got = identifiers.extract_identifiers("", browser_url=url)
            self.assertEqual(self._kinds(got, "domain"), [], url)

    def test_a_bad_url_yields_nothing_rather_than_a_guess(self):
        for bad in ("chrome://newtab", "co.uk", "", None, "javascript:void(0)"):
            got = identifiers.extract_identifiers("", browser_url=bad)
            self.assertEqual([i for i in got if i.get("src") == "browser_url"],
                             [], repr(bad))

    def test_ocr_only_behaviour_is_unchanged(self):
        got = identifiers.extract_identifiers("see github.com/JAdorante/mnemos_v1")
        self.assertIn("mnemos_v1", {i["norm"] for i in got})
        self.assertTrue(all(i["src"] == "ocr" for i in got))


class StampEventTests(unittest.TestCase):
    def test_domain_only_capture_still_yields_a_domain_candidate(self):
        from app.events import Event, Modality
        ev = Event(time=1.0, modality=Modality.VISION, raw="hello",
                   source="desktop.screen",
                   meta={"window": "x — Chrome", "url_domain": "acme.co.uk"})
        identifiers.stamp_event(ev)
        kinds = {i["kind"]: i for i in ev.meta.get("identifiers") or []}
        self.assertEqual(kinds["domain"]["norm"], "acme")


# --------------------------------------------------------------------------
class PrivacyGateDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pg_dom_"))
        from app.perception.privacy_gate import PrivacyGate
        self.gate = PrivacyGate(blocklist_path=self.tmp / "blocklist.json")

    def test_banking_domain_blocks_once_the_field_is_populated(self):
        self.assertIsNone(self.gate.check("Accounts", app_exe="chrome.exe"))
        self.assertEqual(
            self.gate.check("Accounts", app_exe="chrome.exe",
                            url_domain="chase.com"),
            "builtin:banking_domain")
        self.assertEqual(
            self.gate.check("Accounts", url_domain="secure.chase.com"),
            "builtin:banking_domain")

    def test_user_domain_rules_round_trip(self):
        self.gate.add_user_rule("domains", "payroll.example")
        self.assertEqual(self.gate.check("x", url_domain="payroll.example"),
                         "user:domain:payroll.example")
        self.gate.remove_user_rule("domains", "payroll.example")
        self.assertIsNone(self.gate.check("x", url_domain="payroll.example"))


# --------------------------------------------------------------------------
class DomainSegmentationTests(_EnvMixin, unittest.TestCase):
    def _events(self):
        from app.events import Event, Modality

        def ev(t, dom):
            return Event(time=t, modality=Modality.VISION, raw="",
                         source="desktop.screen",
                         meta={"window": "page — Chrome", "url_domain": dom})
        return [(1, ev(0.0, "github.com")), (2, ev(1.0, "github.com")),
                (3, ev(2.0, "mail.example")), (4, ev(3.0, "mail.example"))]

    def test_off_by_default_one_app_is_one_block(self):
        self._env(QUILL_CONTEXT_DOMAIN_SEGMENT="0")
        from app.services.activity import group_activities
        self.assertEqual(len(group_activities(self._events(), 60.0)), 1)

    def test_on_the_domain_splits_the_block(self):
        self._env(QUILL_CONTEXT_DOMAIN_SEGMENT="1")
        from app.services.activity import group_activities
        acts = group_activities(self._events(), 60.0)
        self.assertEqual([a.domain for a in acts],
                         ["github.com", "mail.example"])

    def test_a_missing_domain_never_splits(self):
        self._env(QUILL_CONTEXT_DOMAIN_SEGMENT="1")
        from app.events import Event, Modality
        from app.services.activity import group_activities

        def ev(t, meta):
            return Event(time=t, modality=Modality.VISION, raw="",
                         source="desktop.screen", meta=meta)
        rows = [(1, ev(0.0, {"window": "p — Chrome", "url_domain": "a.com"})),
                (2, ev(1.0, {"window": "p — Chrome"})),
                (3, ev(2.0, {"window": "p — Chrome", "url_domain": "a.com"}))]
        self.assertEqual(len(group_activities(rows, 60.0)), 1)

    def test_app_grouping_is_untouched_when_off(self):
        # app_of() and the existing block key must behave exactly as before.
        self._env(QUILL_CONTEXT_DOMAIN_SEGMENT="0")
        from app.events import Event, Modality
        from app.services.activity import group_activities

        def ev(t, win):
            return Event(time=t, modality=Modality.VISION, raw="",
                         source="desktop.screen", meta={"window": win})
        rows = [(1, ev(0.0, "a — Chrome")), (2, ev(1.0, "b — Cursor"))]
        self.assertEqual([a.app for a in group_activities(rows, 60.0)],
                         ["Chrome", "Cursor"])


class L3DomainBlockTests(_EnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        import app.perception.store as store_mod
        from app.perception.store import PerceptionStore
        self.tmp = Path(tempfile.mkdtemp(prefix="perc_ws2d_"))
        self.ps = PerceptionStore(self.tmp / "perception.db")
        store_mod._pstore = self.ps

        def _reset():
            try:
                self.ps.close()
            except Exception:
                pass
            store_mod._pstore = None
        self.addCleanup(_reset)

    def _metas(self, t0):
        return [
            MetaEvent(session_id="S", seq=1, ts_utc=t0, app_name="chrome.exe",
                      url_domain="github.com"),
            MetaEvent(session_id="S", seq=2, ts_utc=t0 + 60_000,
                      app_name="chrome.exe", url_domain="github.com"),
            MetaEvent(session_id="S", seq=3, ts_utc=t0 + 400_000,
                      app_name="chrome.exe", url_domain="mail.example"),
            MetaEvent(session_id="S", seq=4, ts_utc=t0 + 460_000,
                      app_name="chrome.exe", url_domain="mail.example"),
        ]

    def test_dominant_domain_is_written_when_segmentation_is_on(self):
        self._env(QUILL_CONTEXT_DOMAIN_SEGMENT="1")
        from app.perception.l3_workers import run_segment
        t0 = 1_700_000_000_000
        self.ps.insert_meta_batch(self._metas(t0))
        run_segment({"lookback_ms": 10 ** 12}, store=self.ps)
        blocks = self.ps.list_activity_blocks(limit=50)
        domains = sorted(b["dominant_domain"] or "" for b in blocks)
        self.assertIn("github.com", domains)

    def test_off_leaves_one_browser_block_and_no_domain(self):
        self._env(QUILL_CONTEXT_DOMAIN_SEGMENT="0")
        from app.perception.l3_workers import run_segment
        t0 = 1_700_000_000_000
        self.ps.insert_meta_batch(self._metas(t0))
        run_segment({"lookback_ms": 10 ** 12}, store=self.ps)
        blocks = self.ps.list_activity_blocks(limit=50)
        self.assertTrue(all(not (b["dominant_domain"] or "") for b in blocks))


# --------------------------------------------------------------------------
class DomainAnchorFlagTests(_EnvMixin, unittest.TestCase):
    """kind="domain" reaches attribution through exactly one of the three
    sibling kind filters, and only when its own flag is on."""

    class _FakeStore:
        def events_in_window(self, *a, **k):
            return [{"meta": {"identifiers": [
                {"kind": "domain", "norm": "acme", "src": "browser_url"},
                {"kind": "repo", "norm": "mnemos_v1", "src": "browser_url"},
                {"kind": "url", "norm": "acme.com/x", "src": "ocr"},
            ]}}]

    def test_off_by_default(self):
        self._env(QUILL_CONTEXT_DOMAIN_ANCHOR="0")
        from app.services.context_anchor import _identifier_norms
        self.assertEqual(_identifier_norms(self._FakeStore(), 0, 1),
                         ["mnemos_v1"])

    def test_on_adds_the_domain_and_still_drops_kind_url(self):
        self._env(QUILL_CONTEXT_DOMAIN_ANCHOR="1")
        from app.services.context_anchor import _identifier_norms
        self.assertEqual(sorted(_identifier_norms(self._FakeStore(), 0, 1)),
                         ["acme", "mnemos_v1"])

    def test_the_other_two_filters_are_not_widened(self):
        idents = list(self._FakeStore().events_in_window()[0]["meta"]["identifiers"])
        self.assertEqual(identifiers.entity_candidate_names(idents),
                         ["mnemos_v1"])
        from app.services import identifier_rollup
        src = Path(identifier_rollup.__file__).read_text()
        self.assertIn('kind not in ("repo", "title_segment", "path")', src)


class TwoStoreErasureTests(unittest.TestCase):
    """§6.4 — URL data lives in BOTH stores, so the erasure claim has to be
    asserted against both.

    `erasure.erase_window` step 1 calls PerceptionStore.erase_range (the
    `url_domain` column) and step 2 calls Store.erase_events_window (where
    meta["identifiers"] holds the URL-derived `value`). Asserting only the
    first would pass while proving nothing about the store that actually holds
    the paths. These are those two cascade steps, exercised against real
    stores rather than the mocked Store the phase-A cascade test uses."""

    def test_url_data_is_gone_from_perception_db_and_quill_db(self):
        import app.perception.store as store_mod
        from app.events import Event, Modality
        from app.perception.store import PerceptionStore
        from app.storage import Store

        tmp = Path(tempfile.mkdtemp(prefix="ws2d_erase_"))
        ps = PerceptionStore(tmp / "perception.db")
        store_mod._pstore = ps
        self.addCleanup(lambda: setattr(store_mod, "_pstore", None))
        self.addCleanup(ps.close)
        store = Store(db_path=tmp / "quill.db", audio_dir=tmp / "audio")
        self.addCleanup(store.close)

        t0_ms = 1_700_000_000_000
        ps.insert_meta_batch([MetaEvent(session_id="S", seq=1, ts_utc=t0_ms,
                                        app_name="chrome.exe",
                                        url_domain="acme.co.uk")])
        ev = Event(time=t0_ms / 1000.0, modality=Modality.VISION, raw="",
                   source="desktop.screen",
                   meta={"window": "x — Chrome", "url_domain": "acme.co.uk"})
        identifiers.stamp_event(ev)
        self.assertTrue(any(i["kind"] == "domain"
                            for i in ev.meta["identifiers"]))
        store.insert(ev)

        def _url_rows():
            with store._lock:
                return store._conn.execute(
                    "SELECT COUNT(*) FROM events WHERE meta LIKE ?",
                    ("%acme.co.uk%",)).fetchone()[0]

        self.assertEqual(_url_rows(), 1)
        self.assertEqual(ps.counts()["meta_events"], 1)

        ps.erase_range(t0_ms - 1000, t0_ms + 1000)
        store.erase_events_window((t0_ms - 1000) / 1000.0,
                                  (t0_ms + 1000) / 1000.0)

        self.assertEqual(ps.counts()["meta_events"], 0)
        self.assertEqual(_url_rows(), 0, "URL-derived identifier survived in "
                                         "quill.db after the cascade")


class HarnessScoringTests(unittest.TestCase):
    """The Phase 0 gate is computed by scripts/ws2d/ground_truth.py, so the
    scoring rules are pinned here. Both of these were real bugs found while
    running the harness against a live Chromium on Linux."""

    def _result(self):
        from scripts.ws2d.ground_truth import Result
        return Result()

    def test_a_redirect_is_not_scored_as_a_precision_failure(self):
        # bbc.co.uk/news serves bbc.com/news. The driver and the accessibility
        # layer do not see the switch at the same instant; either observation
        # is a legitimate truth.
        r = self._result()
        r.add("navigate", "https://www.bbc.co.uk/news",
              "https://www.bbc.com/news", 40.0, "https://www.bbc.com/news")
        self.assertTrue(r.rows[0]["correct"])
        self.assertEqual(r.rows[0]["truth_domains"], ["bbc.co.uk", "bbc.com"])

    def test_a_genuinely_wrong_read_still_fails(self):
        r = self._result()
        r.add("navigate", "https://github.com/a/b", "https://evil.example/x",
              10.0, "https://github.com/a/b")
        self.assertFalse(r.rows[0]["correct"])
        self.assertEqual(r.summary("chromium", "atspi")["precision"], 0.0)

    def test_spa_staleness_shows_as_path_not_domain(self):
        # The (hwnd, title) cache key misses same-title SPA routing. Harmless
        # at domain level — which is the argument for the domain-only default.
        r = self._result()
        r.add("spa_same_title", "https://spa.example.com/route-1",
              "https://spa.example.com/", 10.0, "https://spa.example.com/route-1")
        self.assertTrue(r.rows[0]["correct"])
        self.assertFalse(r.rows[0]["path_correct"])

    def test_no_read_is_a_recall_hole_not_a_precision_hit(self):
        r = self._result()
        r.add("mid_load", "https://a.com/x", None, 5.0, "https://a.com/x")
        r.add("navigate", "https://a.com/y", "https://a.com/y", 5.0,
              "https://a.com/y")
        summary = r.summary("chromium", "atspi")
        self.assertEqual(summary["precision"], 1.0)
        self.assertEqual(summary["recall"], 0.5)

    def test_latency_is_gated_only_for_the_shipped_reader(self):
        r = self._result()
        r.add("navigate", "https://a.com/x", "https://a.com/x", 900.0,
              "https://a.com/x")
        self.assertNotIn("p95_le_150ms", r.summary("chromium", "atspi")["gate"])
        self.assertIn("p95_le_150ms", r.summary("chrome", "uia")["gate"])

    def test_phase_minus_one_verdict_bands(self):
        from scripts.bench_browser_a11y import _verdict
        self.assertEqual(_verdict(0.4, 20.0), "negligible")
        self.assertEqual(_verdict(1.97, 90.5), "bounded")   # measured here
        self.assertEqual(_verdict(9.0, 400.0), "severe")


class LivePilotNoiseTests(_EnvMixin, unittest.TestCase):
    """Four precision bugs found by reading identifiers off the six live GB10
    pilot containers on 2026-09-09. Every string below is verbatim from a
    `desktop.screen` row that a real user's shared screen produced."""

    def _norms(self, text, kind=None, **kw):
        got = identifiers.extract_identifiers(text, **kw)
        return {i["norm"] for i in got if kind is None or i["kind"] == kind}

    # 1. language pairs mined as repo slugs, at score 1.0 into attribution
    def test_syntax_picker_pairs_are_not_repositories(self):
        for bad in ("JavaScript/JSON", "TypeScript/JSON", "JavaScript/Node.js",
                    "JavaScript/TypeScript", "JSON/some"):
            self.assertEqual(self._norms(bad, "repo"), set(), bad)

    def test_a_repo_from_a_real_url_path_still_survives(self):
        # The bare-slug stoplist must not touch host-derived repos: a path
        # under github.com is authoritative where a prose slug is a guess.
        self.assertIn("app", self._norms("https://github.com/docker/app.git",
                                         "repo"))
        self.assertIn("memos", self._norms(
            "https://github.com/Aduarte/memos/memos_v3/README", "repo"))

    # 2. OCR-garbled private IPs stamped as url identifiers
    def test_ip_garbage_never_becomes_an_identifier(self):
        for bad in ("http://192.168", "http://172.19", "http://127.65",
                    "https://102.168.120.8000", "http://192.168.38/~jb.8086"):
            self.assertEqual(self._norms(bad), set(), bad)

    def test_psl_rejects_a_numeric_public_suffix(self):
        for bad in ("192.168", "172.19", "127.65", "192.168.120.8000"):
            self.assertIsNone(psl.registrable_domain(bad), bad)
        self.assertEqual(psl.registrable_domain("github.com"), "github.com")

    # 3. the pilot's own tunnel address mined into the graph
    def test_our_own_tunnel_is_not_memory(self):
        self.assertEqual(self._norms(
            "mac-cheapest-experiment-division.trycloudflare.com/memory"), set())

    def test_the_configured_public_host_is_suppressed_too(self):
        self._env(QUILL_PUBLIC_HOST="sparrow.example.org")
        self.assertEqual(self._norms("https://sparrow.example.org/console"),
                         set())

    # 4. a URL's own path mined a second time as kind="path"
    def test_a_url_is_not_a_filesystem_path(self):
        # kind="path" IS consumed by all three filters, so "github.com" was
        # binding as a path root on every frame showing a github URL.
        self.assertEqual(
            self._norms("https://github.com/docker/app.git", "path"), set())
        self.assertEqual(
            self._norms("https://www.google.com/home", "path"), set())

    def test_real_paths_are_untouched(self):
        self.assertEqual(
            self._norms("see /home/jb/mnemos/app/perception/psl.py", "path"),
            {"mnemos"})
        self.assertEqual(
            self._norms(r"opened C:\Users\jb\projects\nexus_v1\storage.py",
                        "path"),
            {"nexus_v1"})


if __name__ == "__main__":
    unittest.main()
