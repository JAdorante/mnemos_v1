"""Popup/dialog-aware observation rendering."""
from __future__ import annotations

import unittest

from browser_agent.perception import render_observation


def _scan(elements, modal=None):
    return {"url": "https://example.com", "title": "t", "count": len(elements),
            "truncated": False, "elements": elements, "modal": modal,
            "scrollY": 0, "scrollMax": 100}


class RenderObservationPopupTests(unittest.TestCase):
    def test_modal_banner_and_flags(self) -> None:
        obs = render_observation(_scan([
            {"id": 0, "role": "button", "name": "Not now", "dialog": True},
            {"id": 1, "role": "button", "name": "Search", "covered": True},
        ], modal="See results closer to you?"))
        self.assertIn('POPUP/DIALOG OPEN: "See results closer to you?"', obs)
        self.assertIn("[0] button: Not now [dialog]", obs)
        self.assertIn("[1] button: Search (covered)", obs)

    def test_roleless_overlay_warning(self) -> None:
        els = [{"id": i, "role": "link", "name": f"L{i}", "covered": True}
               for i in range(6)]
        els.append({"id": 6, "role": "button", "name": "No thanks"})
        obs = render_observation(_scan(els))
        self.assertIn("overlay/popup appears to cover the page", obs)
        self.assertIn("[6] button: No thanks", obs)
        self.assertNotIn("POPUP/DIALOG OPEN", obs)

    def test_clean_page_has_no_warnings(self) -> None:
        obs = render_observation(_scan([
            {"id": 0, "role": "button", "name": "Search"},
            {"id": 1, "role": "link", "name": "Result", "covered": True},
        ]))
        self.assertNotIn("POPUP/DIALOG OPEN", obs)
        self.assertNotIn("overlay/popup appears", obs)
        # An isolated covered element (e.g. under a sticky header) still shows.
        self.assertIn("[1] link: Result (covered)", obs)


if __name__ == "__main__":
    unittest.main()
