"""Capture dock — the companion window that owns browser capture streams.

Two tiers:

* STRUCTURE (always): the dock serves, runs the SAME engine as /capture (one
  capture code path, not two), and /capture/status reports browser-side
  capture from the server's own socket state so every page sees it.

* LIVE (opt-in, MNEMOS_DOCK_SMOKE=1): a real chromium with a fake mic proves
  the guarantee the dock exists for — capture started in the dock SURVIVES a
  full navigation in the main window, which is exactly what capture started
  inline on an app page cannot do.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except (error.URLError, TimeoutError, OSError):
            time.sleep(0.25)
    return False


class DockStructureTests(unittest.TestCase):
    def test_dock_reuses_the_capture_engine(self) -> None:
        """One capture implementation: the dock embeds the shared core, so a
        fix to VAD/worklet/reconnect can never land on only one surface."""
        from app.api.capture_dock import DOCK_PAGE
        from app.api.capture_page import CAPTURE_CORE_JS, CAPTURE_PAGE
        from app.api.mnemos_theme import BRAND

        # Both pages carry the engine verbatim (modulo the brand token the
        # theme substitutes at render time).
        core = CAPTURE_CORE_JS.replace("@@BRAND@@", BRAND)
        self.assertIn(core, DOCK_PAGE)
        self.assertIn(core, CAPTURE_PAGE)
        # And the engine is the real thing, not a stub.
        for probe in ("class SourceChannel", "registerProcessor('pcm-feeder'",
                      "createVadWorker", "/ingest/audio"):
            self.assertIn(probe, CAPTURE_CORE_JS, probe)

    def test_dock_provides_every_id_the_engine_paints(self) -> None:
        from app.api.capture_dock import DOCK_PAGE

        for kind in ("mic", "tab", "screen"):
            for pre in ("dot-", "st-", "start-", "stop-", "meter-", "priv-"):
                self.assertIn(f'id="{pre}{kind}"', DOCK_PAGE, pre + kind)
        # Only the audio channels have a pause control (screen is sampled).
        for kind in ("mic", "tab"):
            self.assertIn(f'id="pause-{kind}"', DOCK_PAGE)
        for extra in ("offline", "no-tab-audio", "meet-title", "stop-all"):
            self.assertIn(f'id="{extra}"', DOCK_PAGE, extra)

    def test_dock_has_no_unrendered_template_markers(self) -> None:
        from app.api.capture_dock import DOCK_PAGE

        self.assertNotIn("@@", DOCK_PAGE)

    def test_engine_tolerates_ids_a_surface_omits(self) -> None:
        """The dock deliberately drops enrollment and the ticker; the engine
        must no-op on those ids instead of throwing."""
        from app.api.capture_page import CAPTURE_CORE_JS

        self.assertIn("document.getElementById(id) || _stub", CAPTURE_CORE_JS)

    def test_dock_route_serves_no_store(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.web_ingest import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        r = client.get("/capture/dock")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r.headers.get("cache-control", ""))
        self.assertIn("SourceChannel", r.text)

    def test_web_capture_state_reports_off_when_nothing_connected(self) -> None:
        from app.api.web_ingest import web_capture_state

        state = web_capture_state()
        for key in ("mic", "tab", "screen"):
            self.assertIn(key, state)
            self.assertEqual(state[key], "off")

    def test_web_capture_state_tracks_connection_not_warm_pipeline(self) -> None:
        """`running` stays true through the keep-warm grace after a dropped
        socket — the indicator must follow `connected`, or a closed dock
        would keep claiming the mic is live."""
        from app.api import web_ingest

        feed = web_ingest._RemoteFeed("mic")
        feed.running = True           # warm pipeline, nobody connected
        with web_ingest._feeds_lock:
            web_ingest._feeds["mic"] = feed
        try:
            self.assertEqual(web_ingest.web_capture_state()["mic"], "off")
            web_ingest._set_connected(feed, feed.epoch, True)
            self.assertEqual(web_ingest.web_capture_state()["mic"], "recording")
            feed.paused = True
            self.assertEqual(web_ingest.web_capture_state()["mic"], "paused")
            feed.paused = False
            web_ingest._set_connected(feed, feed.epoch, False)
            self.assertEqual(web_ingest.web_capture_state()["mic"], "off")
            # A stale epoch (superseded connection) must not flip state back.
            web_ingest._set_connected(feed, feed.epoch - 1, True)
            self.assertEqual(web_ingest.web_capture_state()["mic"], "off")
        finally:
            with web_ingest._feeds_lock:
                web_ingest._feeds.pop("mic", None)

    def test_capture_status_exposes_web_state(self) -> None:
        from app.api.routes import _web_capture_state

        state = _web_capture_state()
        self.assertEqual(state.get("mic"), "off")

    def test_ui_opens_the_dock_instead_of_navigating(self) -> None:
        """The whole point: hosted capture must not send the user to another
        page — a MediaStream dies with the document that created it."""
        from app.api.mnemos_ui import UI_JS

        self.assertIn("/capture/dock", UI_JS)
        self.assertIn("mnemosCaptureDock", UI_JS)        # one dock, focused
        self.assertIn("BroadcastChannel", UI_JS)
        # The hosted status chip is a dock opener now, not a link away.
        self.assertNotIn('class="rec-status hosted" href="/capture"', UI_JS)
        # Exactly one navigation to /capture survives: the popup-blocked
        # fallback, which is the only case where leaving the page is right.
        self.assertEqual(UI_JS.count("window.location.href = '/capture';"), 1)
        self.assertIn("Popup blocked", UI_JS)

    def test_reopening_never_reloads_a_capturing_dock(self) -> None:
        """window.open on an existing NAME navigates that window, which would
        kill the stream — while capture is live, re-open must only focus."""
        from app.api.mnemos_ui import UI_JS

        start = UI_JS.index("openDock(want) {")
        body = UI_JS[start:start + 500]
        self.assertIn("this.webLive() && this.dockOpen()", body)
        self.assertIn("dockCommand('focus')", body)

    def test_static_ui_bundle_is_synced(self) -> None:
        from app.api.mnemos_ui import UI_JS

        js = (ROOT / "app" / "static" / "js" / "mnemos-ui.js").read_text(
            encoding="utf-8")
        self.assertIn("/capture/dock", js)
        self.assertIn("openDockFromConsent", UI_JS)
        self.assertIn("openDockFromConsent", js)


# Every live conferencing surface the capture path claims to recognize:
# (label, a representative join URL, expected provider). The join URL is the
# real one a calendar invite carries.
LIVE_MEETING_SITES = [
    ("Zoom", "https://us02web.zoom.us/j/1234567890?pwd=abc", "zoom"),
    ("Zoom (gov)", "https://www.zoomgov.com/j/1610000000", "zoom"),
    ("Google Meet", "https://meet.google.com/abc-defg-hij", "meet"),
    ("Microsoft Teams",
     "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc", "teams"),
    ("Teams (live)", "https://teams.live.com/meet/9312345", "teams"),
    ("Webex", "https://acme.webex.com/acme/j.php?MTID=m123", "webex"),
    ("Whereby", "https://whereby.com/acme-standup", "whereby"),
    ("GoTo Meeting", "https://app.gotomeeting.com/?meetingId=123456789",
     "goto"),
    ("join.me", "https://join.me/acme-room", "goto"),
    ("Jitsi", "https://meet.jit.si/AcmeWeeklySync", "jitsi"),
    ("Amazon Chime", "https://app.chime.aws/meetings/1234567890", "chime"),
    ("BlueJeans", "https://bluejeans.com/123456789/1234", "bluejeans"),
    ("RingCentral", "https://meetings.ringcentral.com/j/1234567890",
     "ringcentral"),
    ("Zoho Meeting", "https://meeting.zoho.com/meeting/join?key=abc", "zoho"),
    ("Discord", "https://discord.gg/acme", "discord"),
    ("Slack huddle",
     "https://acme.slack.com/huddle/T0123/C0456", "slack"),
    ("Gather", "https://gather.town/app/abc/acme-office", "gather"),
    ("Around", "https://around.co/r/acme-standup", "around"),
    ("Livestorm", "https://app.livestorm.co/p/abc-123", "livestorm"),
    ("Riverside", "https://riverside.fm/studio/acme", "riverside"),
    ("StreamYard", "https://streamyard.com/abc123def", "streamyard"),
]


class LiveMeetingSiteCoverageTests(unittest.TestCase):
    """Which conferencing hosts the capture path recognizes IS the feature:
    an unrecognized host is never offered a meeting session at all."""

    def test_every_live_site_resolves_to_its_provider(self) -> None:
        from app.services.meeting_session import extract_conference_link

        for label, url, provider in LIVE_MEETING_SITES:
            with self.subTest(site=label):
                got_url, got_provider = extract_conference_link(
                    f"Join the call: {url}")
                self.assertEqual(got_provider, provider, f"{label} -> {url}")
                self.assertEqual(got_url, url)

    def test_subdomains_resolve_to_the_same_provider(self) -> None:
        from app.services.meeting_session import extract_conference_link

        for url, provider in (
                ("https://acme.zoom.us/j/1", "zoom"),
                ("https://acme.webex.com/meet/x", "webex"),
                ("https://team.whereby.com/room", "whereby"),
        ):
            with self.subTest(url=url):
                self.assertEqual(extract_conference_link(url)[1], provider)

    def test_vendor_domain_migrations_keep_working(self) -> None:
        """Found by the live sweep on 2026-09-09: Riverside moved .fm -> .com
        (the old host 301s). Invites exist for both, so both must classify."""
        from app.services.meeting_session import extract_conference_link

        for url in ("https://riverside.fm/studio/acme",
                    "https://riverside.com/studio/acme"):
            self.assertEqual(extract_conference_link(url)[1], "riverside", url)

    def test_retired_services_still_classify_old_invites(self) -> None:
        """BlueJeans is gone (the domain no longer resolves), but calendar
        history still holds its links — an archived invite should still read
        as a meeting rather than as an unknown URL."""
        from app.services.meeting_session import extract_conference_link

        self.assertEqual(
            extract_conference_link("https://bluejeans.com/123456789")[1],
            "bluejeans")

    def test_every_provider_is_declared(self) -> None:
        from app.services.meeting_session import PROVIDERS

        for _label, _url, provider in LIVE_MEETING_SITES:
            self.assertIn(provider, PROVIDERS, provider)

    def test_always_on_apps_are_not_calls_by_themselves(self) -> None:
        """Slack/Discord/Gather windows sit open all day — the bare app name
        must never trigger a meeting offer, or every user gets prompted
        forever. Only an unambiguous in-call word counts."""
        from app.services.meeting_session import provider_from_window

        for idle in ("Slack | general | Acme", "Discord",
                     "#announcements | Acme - Discord", "Gather"):
            self.assertIsNone(provider_from_window(idle), idle)
        self.assertEqual(
            provider_from_window("Huddle in #general - Slack"), "slack")

    def test_window_titles_of_live_sites_classify(self) -> None:
        from app.services.meeting_session import provider_from_window

        for title, provider in (
                ("Standup - Zoom Meeting", "zoom"),
                ("Microsoft Teams", "teams"),
                ("Cisco Webex Meetings", "webex"),
                ("Whereby - acme-standup", "whereby"),
                ("GoTo Meeting", "goto"),
                ("Jitsi Meet", "jitsi"),
                ("Amazon Chime", "chime"),
                ("BlueJeans", "bluejeans"),
                ("RingCentral Meetings", "ringcentral"),
                ("Riverside.fm Studio", "riverside"),
                ("StreamYard", "streamyard"),
        ):
            with self.subTest(title=title):
                self.assertEqual(provider_from_window(title), provider)


class ShareDiagnosticsTests(unittest.TestCase):
    """A missing audio track on a meeting share is almost never "this browser
    can't do it" — it is the wrong surface, or an unticked box. Telling the
    user the wrong cause is what makes meeting capture fail silently."""

    def test_advice_names_the_actual_cause_per_surface(self) -> None:
        from app.api.capture_page import CAPTURE_CORE_JS

        self.assertIn("function tabAudioAdvice(info)", CAPTURE_CORE_JS)
        self.assertIn("displaySurface", CAPTURE_CORE_JS)
        # Each real surface the picker can return has its own advice.
        for surface in ("'window'", "'monitor'", "'browser'"):
            self.assertIn(f"info.surface === {surface}", CAPTURE_CORE_JS)

    def test_picker_excludes_sparrows_own_window(self) -> None:
        from app.api.capture_page import CAPTURE_CORE_JS

        self.assertIn("selfBrowserSurface: 'exclude'", CAPTURE_CORE_JS)
        self.assertIn("surfaceSwitching: 'include'", CAPTURE_CORE_JS)

    def test_failed_share_releases_the_picked_surface(self) -> None:
        """Without this the browser keeps showing 'sharing' after a share we
        rejected for having no audio."""
        from app.api.capture_page import CAPTURE_CORE_JS

        idx = CAPTURE_CORE_JS.index("if (!info.hasAudio)")
        self.assertIn("stream.getTracks().forEach(t => t.stop())",
                      CAPTURE_CORE_JS[idx:idx + 400])

    def test_dock_shows_what_is_being_captured(self) -> None:
        from app.api.capture_dock import DOCK_PAGE

        self.assertIn('id="share-what"', DOCK_PAGE)
        self.assertIn("function onShareStarted(share)", DOCK_PAGE)
        self.assertIn("Capturing audio from: ", DOCK_PAGE)


@unittest.skipUnless(
    os.environ.get("MNEMOS_DOCK_SMOKE") == "1",
    "set MNEMOS_DOCK_SMOKE=1 to run the live dock/browser smoke",
)
class DockLiveSmokeTests(unittest.TestCase):
    """Real server + real chromium + fake mic."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("pip install playwright")
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = os.environ.copy()
        env["QUILL_PORT"] = str(cls.port)
        env.setdefault("QUILL_HOME", str(ROOT / "data"))
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if not _wait_health(f"{cls.base}/health"):
            err = (cls.proc.stderr.read() if cls.proc.stderr else b"").decode(
                "utf-8", "replace")
            cls.proc.kill()
            raise unittest.SkipTest(f"server failed to start: {err[-400:]}")

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "proc", None):
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()

    def _status(self) -> dict:
        import json
        with request.urlopen(f"{self.base}/capture/status", timeout=5) as r:
            return json.load(r)

    def test_live_meeting_sites_are_reachable_and_still_classify(self) -> None:
        """Network sweep: the real conferencing sites still resolve, still
        serve a page, and the host each one redirects to STILL classifies as
        its provider — a vendor moving to a new domain silently turns off
        meeting capture for that site, and nothing else would catch it."""
        from playwright.sync_api import sync_playwright
        from app.services.meeting_session import extract_conference_link

        # Public entry points (not real meeting rooms — nothing is joined).
        # BlueJeans is deliberately absent: Verizon retired it and the domain
        # no longer resolves, so it stays in the table for old invites only.
        ENTRY = [
            ("Zoom", "https://zoom.us/join", "zoom"),
            ("Google Meet", "https://meet.google.com/", "meet"),
            ("Microsoft Teams", "https://teams.microsoft.com/", "teams"),
            ("Webex", "https://www.webex.com/", "webex"),
            ("Whereby", "https://whereby.com/", "whereby"),
            ("GoTo", "https://www.gotomeeting.com/", "goto"),
            ("Jitsi", "https://meet.jit.si/", "jitsi"),
            ("RingCentral", "https://www.ringcentral.com/", "ringcentral"),
            ("Gather", "https://www.gather.town/", "gather"),
            ("Livestorm", "https://livestorm.co/", "livestorm"),
            ("Riverside", "https://riverside.fm/", "riverside"),
            ("StreamYard", "https://streamyard.com/", "streamyard"),
        ]
        problems: list[str] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for label, url, provider in ENTRY:
                    with self.subTest(site=label):
                        # A page per site: these marketing pages fire their own
                        # client-side redirects, which would otherwise abort
                        # the NEXT site's navigation and cascade.
                        page = browser.new_page()
                        try:
                            resp = page.goto(url, wait_until="commit",
                                             timeout=45_000)
                            page.wait_for_timeout(1200)   # let redirects land
                            landed = page.url
                        except Exception as exc:
                            problems.append(
                                f"{label}: unreachable ({str(exc)[:120]})")
                            continue
                        finally:
                            page.close()
                        if resp is not None and resp.status >= 500:
                            problems.append(f"{label}: HTTP {resp.status}")
                        # The host must still classify — either the one we
                        # asked for or the one it redirected to. A vendor
                        # domain migration (riverside.fm -> riverside.com,
                        # caught here) shows up as NEITHER classifying.
                        # Logged-out marketing redirects (meet.google.com ->
                        # workspace.google.com) are fine: the meeting host
                        # itself is unchanged.
                        entry_ok = extract_conference_link(url)[1] == provider
                        landed_ok = extract_conference_link(landed)[1] == provider
                        if not (entry_ok or landed_ok):
                            problems.append(
                                f"{label}: {url} redirected to {landed} and "
                                f"NEITHER classifies as {provider!r} — the "
                                "vendor likely moved domains")
            finally:
                browser.close()
        self.assertFalse(problems, "\n".join(problems))

    def test_ui_opens_the_dock_as_a_popup_carrying_the_ticked_sources(self) -> None:
        """The wiring itself: the app opens a real popup at /capture/dock,
        armed with what the user allowed — no tab-hunting, no re-choosing."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context()
            try:
                page = ctx.new_page()
                page.goto(f"{self.base}/today",
                          wait_until="domcontentloaded", timeout=30_000)
                page.evaluate(
                    """() => fetch('/capture/consent', {method: 'POST',
                         headers: {'Content-Type': 'application/json'},
                         body: JSON.stringify({consented: true, mic: true,
                                               system_audio: true})})""")
                page.wait_for_timeout(500)
                page.evaluate("() => window.MnemosCapture.tick()")
                page.wait_for_timeout(500)
                with page.expect_popup(timeout=15_000) as popup_info:
                    page.evaluate(
                        "() => window.MnemosCapture.openDockFromConsent()")
                dock = popup_info.value
                dock.wait_for_load_state("domcontentloaded")
                self.assertIn("/capture/dock", dock.url)
                # The sheet's ticks rode across as arm flags.
                self.assertIn("mic=1", dock.url)
                self.assertIn("tab=1", dock.url)
                dock.wait_for_selector("#start-mic", timeout=10_000)
                self.assertTrue(dock.evaluate(
                    """() => document.getElementById('card-mic')
                              .classList.contains('armed')"""))
            finally:
                browser.close()

    def test_dock_capture_survives_navigation_in_the_main_window(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ])
            ctx = browser.new_context(permissions=["microphone"])
            try:
                main = ctx.new_page()
                main.goto(f"{self.base}/today", wait_until="domcontentloaded",
                          timeout=30_000)
                # Allow mic capture, the way the Privacy sheet does.
                main.evaluate(
                    """() => fetch('/capture/consent', {method: 'POST',
                         headers: {'Content-Type': 'application/json'},
                         body: JSON.stringify({consented: true, mic: true})})
                       .then(r => r.json())""")

                dock = ctx.new_page()
                dock.goto(f"{self.base}/capture/dock?mic=1",
                          wait_until="domcontentloaded", timeout=30_000)
                dock.wait_for_selector("#start-mic", timeout=10_000)
                # Auto-arm should start the mic on its own; click as a
                # fallback if the browser declined the un-gestured start.
                try:
                    dock.wait_for_function(
                        "() => document.getElementById('st-mic')"
                        ".textContent.trim() === 'recording'", timeout=12_000)
                except Exception:
                    dock.click("#start-mic")
                    dock.wait_for_function(
                        "() => document.getElementById('st-mic')"
                        ".textContent.trim() === 'recording'", timeout=20_000)

                # The SERVER now sees a live browser mic...
                deadline = time.time() + 15
                while time.time() < deadline:
                    if self._status().get("web", {}).get("mic") == "recording":
                        break
                    time.sleep(0.5)
                self.assertEqual(self._status().get("web", {}).get("mic"),
                                 "recording", "server never saw the dock mic")

                # ...and it stays live across full navigations in the main
                # window — the guarantee inline capture cannot make.
                for route in ("/chat", "/memory", "/today"):
                    main.goto(f"{self.base}{route}",
                              wait_until="domcontentloaded", timeout=30_000)
                    main.wait_for_timeout(700)
                    self.assertEqual(
                        self._status().get("web", {}).get("mic"), "recording",
                        f"capture died when the main window went to {route}")
                self.assertEqual(
                    dock.evaluate(
                        "() => document.getElementById('st-mic').textContent.trim()"),
                    "recording")

                # Stop-all from the MAIN window reaches the dock and releases
                # the stream (so the browser's own indicator clears too).
                main.evaluate("() => window.MnemosCapture.stopAll(false)")
                dock.wait_for_function(
                    "() => document.getElementById('st-mic')"
                    ".textContent.trim() === 'off'", timeout=15_000)
                deadline = time.time() + 15
                while time.time() < deadline:
                    if self._status().get("web", {}).get("mic") == "off":
                        break
                    time.sleep(0.5)
                self.assertEqual(self._status().get("web", {}).get("mic"),
                                 "off", "stop-all did not release the mic")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
