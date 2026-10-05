"""Team page: fleet offers render as approval packets and Share quotes the
hash that was shown (so an edited signal needs a fresh approval).

Live tier in headless chromium with every endpoint mocked; skipped when
Playwright is not installed.
"""
from __future__ import annotations

import json
import unittest

from app.api.peer_page import PEER_PAGE

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

OFFER = {"offer_id": "of:abc", "sha256": "f" * 64, "status": "pending",
         "signal": {"producer": "agent:quant", "kind": "status_update",
                    "subject": "Atlas migration",
                    "body": {"status": "at_risk", "due": "Oct 16"},
                    "confidence": 0.7, "topic": "eng.status",
                    "summary": "<b>not markup</b> term premium"}}


class StaticTests(unittest.TestCase):
    def test_panel_is_present_and_domain_neutral(self) -> None:
        self.assertIn('id="fleetPanel"', PEER_PAGE)
        self.assertIn("/fleet/offers/", PEER_PAGE)
        self.assertIn("sha256:sha", PEER_PAGE)
        self.assertNotIn("instrument", PEER_PAGE)
        self.assertNotIn("trading", PEER_PAGE)


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class LiveTeamPageTests(unittest.TestCase):
    def _run(self, fleet_enabled: bool):
        errors, decided = [], []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.dismiss())

            def handle(route):
                url = route.request.url
                if "/fleet/status" in url:
                    body = {"ok": True, "enabled": fleet_enabled}
                elif "/fleet/offers/" in url and url.endswith("/decide"):
                    decided.append(json.loads(route.request.post_data))
                    body = {"ok": True, "offer": {**OFFER, "status": "sent"}}
                elif "/fleet/offers" in url:
                    body = {"ok": True, "offers": [] if decided else [OFFER]}
                elif "/peer/status" in url:
                    body = {"classes": [], "actions": [], "peers": [],
                            "pending_asks": [], "sent": [], "teams": [],
                            "loops": [], "packs": []}
                elif "/people/list" in url:
                    body = {"people": []}
                else:
                    body = {}
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(body))

            page.route("http://team.test/**", handle)
            page.route("http://team.test/peer", lambda r: r.fulfill(
                status=200, content_type="text/html", body=PEER_PAGE))
            page.goto("http://team.test/peer")
            if fleet_enabled:
                page.wait_for_selector("#fleetPanel:not([hidden]) .ask",
                                       timeout=5000)
                html = page.inner_html("#fleetBox")
                page.click("#fleetBox button.btn-sm >> text=Share")
                page.wait_for_function("document.querySelector('#fleetBox')"
                                       ".textContent.includes('Nothing')",
                                       timeout=5000)
            else:
                page.wait_for_timeout(800)
                html = ""
            hidden = page.eval_on_selector("#fleetPanel", "e => e.hidden")
            browser.close()
        return errors, decided, html, hidden

    def test_offer_renders_escaped_and_share_quotes_the_hash(self) -> None:
        errors, decided, html, hidden = self._run(True)
        self.assertEqual(errors, [])
        self.assertIn("agent:quant", html)
        self.assertIn("status: at_risk", html)
        self.assertIn("Atlas migration", html)
        self.assertNotIn("<b>not markup</b>", html)
        self.assertEqual(decided, [{"approve": True, "sha256": "f" * 64}])
        self.assertFalse(hidden)

    def test_panel_stays_hidden_when_the_fleet_is_off(self) -> None:
        errors, decided, _, hidden = self._run(False)
        self.assertEqual(errors, [])
        self.assertTrue(hidden)
        self.assertEqual(decided, [])


if __name__ == "__main__":
    unittest.main()
