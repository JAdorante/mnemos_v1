"""WS-C — version check: semver, offline safety, the zero-request guarantee.

Two things must hold no matter what: a tester with no network boots exactly as
fast and as successfully as one with network, and a tester who turned the check
off makes literally zero requests.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import update_check as uc
from app.version import __version__


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_uc_"))
        self.env = patch.dict(os.environ, {
            "QUILL_DATA_DIR": str(self.tmp),
            "QUILL_UPDATE_CHECK": "1",
            "QUILL_UPDATE_MANIFEST_URL": "https://example.invalid/manifest.json",
        }, clear=False)
        self.env.start()
        self.manifest = {"latest": "0.4.2", "url": "https://example.invalid/z.zip",
                         "notes": "faster search", "min_supported": "0.3.0"}
        self.calls: list = []

    def tearDown(self) -> None:
        self.env.stop()

    def transport(self, manifest=None, raises=None):
        def _t(url, timeout):
            self.calls.append((url, timeout))
            if raises:
                raise raises
            return dict(manifest if manifest is not None else self.manifest)
        return _t


class SemverTests(unittest.TestCase):
    def test_basic_ordering(self) -> None:
        self.assertTrue(uc.is_newer("0.4.2", "0.4.0"))
        self.assertTrue(uc.is_newer("1.0.0", "0.9.9"))
        self.assertTrue(uc.is_newer("0.4.10", "0.4.9"))     # not string order
        self.assertFalse(uc.is_newer("0.4.0", "0.4.0"))
        self.assertFalse(uc.is_newer("0.3.9", "0.4.0"))

    def test_prereleases_sort_below_their_release(self) -> None:
        self.assertTrue(uc.is_newer("0.4.1", "0.4.1rc1"))
        self.assertFalse(uc.is_newer("0.4.1rc1", "0.4.1"))
        self.assertTrue(uc.is_newer("0.4.1rc2", "0.4.1rc1"))
        self.assertTrue(uc.is_newer("0.5.0a1", "0.4.9"))
        self.assertFalse(uc.is_newer("0.4.0.dev1", "0.4.0"))

    def test_unparseable_versions_never_claim_an_update(self) -> None:
        for bad in (None, "", "latest", "v-nine", "0.4.x", 4.2, {}):
            self.assertFalse(uc.is_newer(bad, "0.4.0"))
            self.assertFalse(uc.is_newer("0.4.2", bad))
            self.assertFalse(uc.is_unsupported("0.4.0", bad))

    def test_min_supported(self) -> None:
        self.assertTrue(uc.is_unsupported("0.2.9", "0.3.0"))
        self.assertFalse(uc.is_unsupported("0.3.0", "0.3.0"))
        self.assertFalse(uc.is_unsupported("0.4.0", "0.3.0"))


class BannerMatrixTests(unittest.TestCase):
    def m(self, latest, min_supported="0.3.0"):
        return {"latest": latest, "url": "u", "notes": "n",
                "min_supported": min_supported}

    def test_no_banner_when_current(self) -> None:
        self.assertIsNone(uc.banner(self.m("0.4.0"), current="0.4.0"))
        self.assertIsNone(uc.banner(self.m("0.3.9"), current="0.4.0"))
        self.assertIsNone(uc.banner(None, current="0.4.0"))
        self.assertIsNone(uc.banner("not a dict", current="0.4.0"))

    def test_info_banner_when_newer_exists(self) -> None:
        b = uc.banner(self.m("0.4.2"), current="0.4.0")
        self.assertEqual(b["level"], "info")
        self.assertFalse(b["unsupported"])
        self.assertIn("0.4.2", b["message"])

    def test_critical_banner_below_min_supported(self) -> None:
        b = uc.banner(self.m("0.4.2", min_supported="0.4.0"), current="0.3.5")
        self.assertEqual(b["level"], "critical")
        self.assertTrue(b["unsupported"])
        self.assertIn("minimum supported", b["message"])

    def test_critical_even_when_latest_equals_current(self) -> None:
        """A floor above your build still nags even if 'latest' is not newer."""
        b = uc.banner(self.m("0.3.5", min_supported="0.4.0"), current="0.3.5")
        self.assertIsNotNone(b)
        self.assertTrue(b["unsupported"])

    def test_dismiss_key_is_per_version(self) -> None:
        a = uc.banner(self.m("0.4.2"), current="0.4.0")["dismiss_key"]
        b = uc.banner(self.m("0.4.3"), current="0.4.0")["dismiss_key"]
        self.assertNotEqual(a, b)


class DisabledTests(_Base):
    def test_disabled_flag_makes_zero_requests(self) -> None:
        with patch.dict(os.environ, {"QUILL_UPDATE_CHECK": "0"}, clear=False):
            out = uc.check(transport=self.transport(), force=True)
            uc.start_background()
        self.assertEqual(self.calls, [])
        self.assertEqual(out["reason"], "disabled")
        self.assertEqual(out["state"], "disabled")

    def test_user_toggle_overrides_the_env_default(self) -> None:
        uc.set_enabled(False)
        self.assertFalse(uc.enabled())
        uc.check(transport=self.transport(), force=True)
        self.assertEqual(self.calls, [])
        uc.set_enabled(True)
        uc.check(transport=self.transport(), force=True)
        self.assertEqual(len(self.calls), 1)

    def test_no_url_means_no_request(self) -> None:
        with patch.dict(os.environ, {"QUILL_UPDATE_MANIFEST_URL": ""},
                        clear=False):
            out = uc.check(transport=self.transport(), force=True)
            uc.start_background()
        self.assertEqual(self.calls, [])
        self.assertEqual(out["state"], "unconfigured")


class CacheTests(_Base):
    def test_manifest_is_cached_for_24h(self) -> None:
        uc.check(now=1000.0, transport=self.transport())
        self.assertEqual(len(self.calls), 1)
        for later in (1000.0, 1000.0 + 3600, 1000.0 + 86_000):
            out = uc.check(now=later, transport=self.transport())
            self.assertEqual(out["reason"], "cached")
        self.assertEqual(len(self.calls), 1)
        uc.check(now=1000.0 + 86_401, transport=self.transport())
        self.assertEqual(len(self.calls), 2)

    def test_force_bypasses_the_cache(self) -> None:
        uc.check(now=1000.0, transport=self.transport())
        uc.check(now=1001.0, transport=self.transport(), force=True)
        self.assertEqual(len(self.calls), 2)

    def test_status_survives_a_restart(self) -> None:
        uc.check(now=1000.0, transport=self.transport())
        self.assertEqual(uc.status()["latest"], "0.4.2")
        self.assertEqual(uc.status()["state"], "update_available")
        # Nothing cached in memory: a fresh read of data/ gives the same answer.
        self.assertEqual(json.loads(uc.cache_path().read_text())["manifest"]
                         ["latest"], "0.4.2")


class OfflineTests(_Base):
    def test_network_failures_never_raise(self) -> None:
        for exc in (OSError("dns"), TimeoutError(), ValueError("bad json"),
                    json.JSONDecodeError("x", "y", 0)):
            out = uc.check(transport=self.transport(raises=exc), force=True)
            self.assertTrue(out["ok"])
        self.assertIsNotNone(uc.status()["error"])

    def test_offline_reports_unknown_not_a_false_current(self) -> None:
        """Never reaching the manifest must not read as 'you are up to date'."""
        out = uc.check(transport=self.transport(raises=OSError("offline")),
                       force=True)
        self.assertEqual(out["state"], "unknown")
        self.assertIsNone(out["banner"])
        self.assertIsNone(out["latest"])

    def test_a_failed_recheck_keeps_the_last_good_manifest(self) -> None:
        uc.check(now=1000.0, transport=self.transport())
        uc.check(now=200_000.0, transport=self.transport(raises=OSError("down")))
        st = uc.status()
        self.assertEqual(st["latest"], "0.4.2")
        self.assertEqual(st["state"], "update_available")
        self.assertIn("OSError", st["error"])

    def test_garbage_manifest_is_ignored(self) -> None:
        out = uc.check(transport=self.transport(
            manifest={"latest": "not-a-version"}), force=True)
        self.assertIsNone(out["banner"])
        self.assertEqual(out["state"], "current")

    def test_start_background_does_not_block_boot(self) -> None:
        """A dead network must be indistinguishable from a live one at boot."""
        import time
        def slow(url, timeout):
            time.sleep(0.5)
            raise TimeoutError()
        with patch("app.services.update_check._fetch", slow):
            t0 = time.monotonic()
            uc.start_background()
            elapsed = time.monotonic() - t0
        uc.stop_background()
        self.assertLess(elapsed, 0.2)


class PrivacyTests(_Base):
    def test_the_request_carries_nothing_about_this_install(self) -> None:
        """An unconditional GET of a static file: no params, no id, no version."""
        captured = {}

        class FakeResp:
            def read(self, *_a): return json.dumps(
                {"latest": "0.4.2", "min_supported": "0.3.0"}).encode()
            def __enter__(self): return self
            def __exit__(self, *_a): return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.header_items())
            captured["data"] = req.data
            return FakeResp()

        with patch("urllib.request.urlopen", fake_urlopen):
            uc.check(force=True)

        self.assertEqual(captured["method"], "GET")
        self.assertIsNone(captured["data"])
        self.assertNotIn("?", captured["url"])
        blob = json.dumps(captured).lower()
        from app.services.usage_ledger import install_id
        for leak in (install_id().lower(), __version__, "mnemos", "sparrow"):
            self.assertNotIn(leak.lower(), blob)


class VersionSurfaceTests(unittest.TestCase):
    def test_version_is_semver_and_single_sourced(self) -> None:
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+")
        from packaging.version import Version
        Version(__version__)   # raises if malformed

    def test_packaging_reads_the_same_constant(self) -> None:
        """The installer must not be able to drift from the app (WS-C)."""
        root = Path(__file__).resolve().parent.parent
        spec = (root / "packaging" / "mnemos.spec").read_text(encoding="utf-8")
        iss = (root / "packaging" / "mnemos.iss").read_text(encoding="utf-8")
        self.assertIn("app/version.py", spec)
        self.assertIn("VERSION.txt", spec)
        self.assertIn("VERSION.txt", iss)
        # No hard-coded version literal survives in the Inno script.
        self.assertNotRegex(iss, r'#define MyAppVersion "\d')
        # And the spec's extractor really parses our file.
        text = (root / "app" / "version.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        self.assertEqual(m.group(1), __version__)

    def test_health_and_crash_report_carry_the_version(self) -> None:
        from app.api.routes import health
        self.assertEqual(health()["version"], __version__)
        tmp = Path(tempfile.mkdtemp(prefix="quill_ucv_"))
        with patch.dict(os.environ, {"QUILL_DATA_DIR": str(tmp)}, clear=False):
            from app.services import crash_report
            import zipfile
            out = crash_report.write_report(note="hi")
            with zipfile.ZipFile(out["path"]) as zf:
                man = json.loads(zf.read("manifest.json"))
        self.assertEqual(man["app_version"], __version__)


if __name__ == "__main__":
    unittest.main()
