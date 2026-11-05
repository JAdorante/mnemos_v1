"""Deployment profiles instead of flags: one file names each deployment
shape and the stages it runs, and /deployment answers "what is running for
this seat" from the live environment."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from app import profiles


class _Base(unittest.TestCase):
    def setUp(self):
        # Every flag any profile or stage touches starts unset, and the
        # module's record of what it applied starts empty.
        keys = {f for _, flags in profiles.STAGES.values() for f, _, _ in flags}
        for p in profiles.PROFILES.values():
            keys |= set(p["env"])
        keys |= {"QUILL_PROFILE", "QUILL_VISION_LOCAL"}
        self._saved = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        self._applied = mock.patch.object(profiles, "_applied", {})
        self._name = mock.patch.object(profiles, "_profile_name", "")
        self._applied.start()
        self._name.start()

    def tearDown(self):
        self._applied.stop()
        self._name.stop()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class ProfileTests(_Base):
    def test_every_profile_names_only_known_shapes(self):
        self.assertTrue({"desktop-solo", "hosted-pilot", "org"}
                        <= set(profiles.PROFILES))
        for name, p in profiles.PROFILES.items():
            self.assertTrue(p["about"], name)

    def test_learning_loop_runs_without_knowing_six_flags(self):
        for name in ("desktop-solo", "hosted-pilot", "org"):
            with self.subTest(name=name):
                self.tearDown()
                self.setUp()
                os.environ["QUILL_PROFILE"] = name
                profiles.apply()
                on = set(profiles.running()["on"])
                self.assertTrue({"learning.pairs", "learning.local_text",
                                 "learning.exemplars", "learning.router"}
                                <= on, sorted(on))

    def test_explicit_env_overrides_the_profile(self):
        os.environ["QUILL_PROFILE"] = "hosted-pilot"
        os.environ["QUILL_FLEET"] = "0"
        set_keys = profiles.apply()
        self.assertNotIn("QUILL_FLEET", set_keys)
        st = profiles.running()["stages"]["network.fleet"]
        self.assertFalse(st["on"])
        self.assertEqual(st["flags"][0]["source"], "override")
        rec = profiles.running()["stages"]["network.records"]
        self.assertEqual(rec["flags"][0]["source"], "profile")

    def test_org_turns_on_the_network(self):
        profiles.apply("org")
        on = set(profiles.running()["on"])
        self.assertTrue({"network.org", "network.records", "network.fleet",
                         "network.fleet_exemplars"} <= on)

    def test_no_profile_reports_code_defaults(self):
        r = profiles.running()
        self.assertIsNone(r["profile"])
        st = r["stages"]["learning.exemplars"]
        self.assertFalse(st["on"])
        self.assertEqual(st["flags"][0]["source"], "default")
        self.assertTrue(r["stages"]["memory.semantic"]["on"])

    def test_unknown_profile_applies_nothing(self):
        self.assertEqual(profiles.apply("prod-ish"), {})

    def test_tester_keeps_its_pins(self):
        from app import config
        os.environ["QUILL_PROFILE"] = "tester"
        with mock.patch.object(config, "_ollama_reachable", return_value=False):
            config.apply_tester_profile()
        self.assertEqual(os.environ.get("QUILL_FIRST_RUN_MODE"), "meeting")
        self.assertEqual(os.environ.get("QUILL_TEXT_LOCAL"), "0")
        self.assertEqual(os.environ.get("QUILL_VISION_LOCAL"), "0")

    def test_stage_flags_exist_in_the_code(self):
        # A stage that names a flag nothing reads would report a fiction.
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "app"
        src = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                        for p in root.rglob("*.py")
                        if p.name != "profiles.py")
        for _, flags in profiles.STAGES.values():
            for flag, _, _ in flags:
                self.assertIn(f'"{flag}"', src, flag)


if __name__ == "__main__":
    unittest.main()
