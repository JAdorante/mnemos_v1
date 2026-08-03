"""Attention mode conditioning (A3 §9)."""
from __future__ import annotations

import unittest

from app.services import attention_mode as am


class AttentionModeTests(unittest.TestCase):
    def tearDown(self):
        am.set_manual("auto")

    def test_manual_overrides_and_clears(self):
        cur = am.set_manual("coding")
        self.assertEqual(cur["id"], "coding")
        self.assertEqual(cur["source"], "manual")
        self.assertEqual(cur["confidence"], 1.0)
        cleared = am.set_manual("auto")
        self.assertNotEqual(cleared["source"], "manual")

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            am.set_manual("spaceship")

    def test_apply_boosts_tools_in_coding(self):
        ranked = [
            {"id": "person:1", "kind": "person", "gravity": 1.0, "pinned": False,
             "prospective_risk": 0},
            {"id": "entity:1", "kind": "tool", "gravity": 1.0, "pinned": False,
             "prospective_risk": 0},
        ]
        am.set_manual("coding")
        out = am.apply_to_candidates(ranked, am.current())
        by = {n["id"]: n for n in out}
        self.assertGreater(by["entity:1"]["gravity"], by["person:1"]["gravity"])

    def test_quiet_dims_non_urgent(self):
        ranked = [
            {"id": "fact:1", "kind": "task", "gravity": 1.0, "pinned": False,
             "prospective_risk": 0.1},
            {"id": "fact:2", "kind": "commitment", "gravity": 1.0, "pinned": False,
             "prospective_risk": 0.9},
            {"id": "person:1", "kind": "person", "gravity": 1.0, "pinned": True,
             "prospective_risk": 0},
        ]
        am.set_manual("off")
        out = am.apply_to_candidates(ranked, am.current())
        by = {n["id"]: n for n in out}
        self.assertLess(by["fact:1"]["gravity"], 0.5)
        self.assertGreater(by["fact:2"]["gravity"], by["fact:1"]["gravity"])
        self.assertGreater(by["person:1"]["gravity"], 0.5)

    def test_meeting_boosts_people_over_coding(self):
        node = {"id": "person:1", "kind": "person", "gravity": 1.0,
                "pinned": False, "prospective_risk": 0}
        am.set_manual("meeting")
        g_meet = am.apply_to_candidates([dict(node)], am.current())[0]["gravity"]
        am.set_manual("coding")
        g_code = am.apply_to_candidates([dict(node)], am.current())[0]["gravity"]
        self.assertGreater(g_meet, g_code)


if __name__ == "__main__":
    unittest.main()
