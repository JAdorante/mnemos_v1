"""Sign-in handoff on every install kind, and the connector lane.

Live failure this pins (2026-09-22, hosted seat): the agent hit Gmail's
sign-in wall on a headless browser, asked three times for a sign-in the user
had no window to perform, the pane's "reveal" answered "runs fully headless
here" ten times — while the Google connector on that seat already held the
inbox headers.
"""
from __future__ import annotations

import queue
import threading
import types
import unittest
from unittest import mock

from browser_agent import ghost


class SigninHandoffKindTests(unittest.TestCase):
    """browser_agent.ghost.signin_handoff is the ONE decision every caller reads."""

    def test_headless_is_takeover(self) -> None:
        from browser_agent import config as bcfg
        with mock.patch.object(ghost, "can_reveal", return_value=False), \
             mock.patch.object(bcfg, "GHOST_MODE", "headless"):
            self.assertEqual(ghost.signin_handoff()["kind"], "takeover")

    def test_parked_window_is_reveal(self) -> None:
        with mock.patch.object(ghost, "can_reveal", return_value=True):
            self.assertEqual(ghost.signin_handoff()["kind"], "reveal")

    def test_hidden_without_display_is_none_with_reason(self) -> None:
        from browser_agent import config as bcfg
        with mock.patch.object(ghost, "can_reveal", return_value=False), \
             mock.patch.object(bcfg, "GHOST_MODE", "hidden"):
            h = ghost.signin_handoff()
        self.assertEqual(h["kind"], "none")
        self.assertIn("X11", h["reason"])


class AskHintTests(unittest.TestCase):
    """The model's own 'sign in in the browser window' ask gets the truth
    appended for the install it is running on."""

    def _hint(self, kind: str, q: str) -> str:
        from app.services import agent_bridge
        with mock.patch.object(ghost, "signin_handoff", return_value={"kind": kind}):
            return agent_bridge._signin_handoff_hint(q)

    def test_non_signin_ask_untouched(self) -> None:
        q = "Which of the two flights do you prefer?"
        self.assertEqual(self._hint("takeover", q), q)

    def test_takeover_wording(self) -> None:
        out = self._hint("takeover", "Could you sign in to Gmail in the browser window?")
        self.assertIn("take over", out)
        self.assertIn("hand back", out)
        self.assertNotIn("reveal", out)

    def test_reveal_wording(self) -> None:
        out = self._hint("reveal", "Please log in to your account.")
        self.assertIn("“reveal”", out)
        self.assertIn("“park”", out)

    def test_none_points_at_connectors_not_a_button(self) -> None:
        out = self._hint("none", "Enter your password to continue.")
        self.assertIn("Setup → Connectors", out)
        self.assertNotIn("reveal", out)
        self.assertNotIn("take over", out)


def _bare_agent():
    from browser_agent.orchestrator import Agent
    a = Agent.__new__(Agent)
    a._signin_hint_hosts = set()
    a.logs = []
    a._log = a.logs.append
    return a


_WALL_SCAN = {"url": "https://accounts.google.com/v3/signin/identifier",
              "title": "Sign in", "page_text": "Sign in to continue to Gmail",
              "elements": [{"id": 1, "role": "textbox", "name": "Email"},
                           {"id": 2, "role": "password", "name": "Password"}]}


class OrchestratorWallTests(unittest.TestCase):
    def test_no_handoff_stops_with_plain_message(self) -> None:
        from browser_agent import orchestrator as orch
        a = _bare_agent()
        with mock.patch.object(orch, "get_creds", return_value=None), \
             mock.patch.object(ghost, "signin_handoff",
                               return_value={"kind": "none", "reason": "r"}), \
             mock.patch.object(orch, "_connector_for_host",
                               return_value={"id": "google", "label": "Google (Gmail + Calendar)",
                                             "connected": True}):
            msg = a._maybe_signin_handoff_hint(_WALL_SCAN)
        self.assertIsNotNone(msg)
        self.assertIn("accounts.google.com", msg)
        self.assertIn("no browser window I can hand to you", msg)
        self.assertIn("already connected", msg)
        self.assertNotIn("sign in in the browser window", msg)

    def test_no_handoff_unconnected_connector_names_setup(self) -> None:
        from browser_agent import orchestrator as orch
        a = _bare_agent()
        with mock.patch.object(orch, "get_creds", return_value=None), \
             mock.patch.object(ghost, "signin_handoff",
                               return_value={"kind": "none", "reason": "r"}), \
             mock.patch.object(orch, "_connector_for_host",
                               return_value={"id": "outlook", "label": "Outlook",
                                             "connected": False}):
            msg = a._maybe_signin_handoff_hint(_WALL_SCAN)
        self.assertIn("Connect Outlook under Setup → Connectors", msg)

    def test_takeover_hints_once_and_continues(self) -> None:
        from browser_agent import orchestrator as orch
        a = _bare_agent()
        with mock.patch.object(orch, "get_creds", return_value=None), \
             mock.patch.object(ghost, "signin_handoff", return_value={"kind": "takeover"}):
            self.assertIsNone(a._maybe_signin_handoff_hint(_WALL_SCAN))
            self.assertIsNone(a._maybe_signin_handoff_hint(_WALL_SCAN))
        hints = [l for l in a.logs if "take over" in l]
        self.assertEqual(len(hints), 1, a.logs)

    def test_stored_creds_never_block(self) -> None:
        from browser_agent import orchestrator as orch
        a = _bare_agent()
        with mock.patch.object(orch, "get_creds", return_value={"user": "u", "pass": "p"}), \
             mock.patch.object(ghost, "signin_handoff",
                               return_value={"kind": "none", "reason": "r"}):
            self.assertIsNone(a._maybe_signin_handoff_hint(_WALL_SCAN))

    def test_not_a_wall_is_ignored(self) -> None:
        a = _bare_agent()
        scan = {"url": "https://example.com/", "page_text": "Welcome",
                "elements": [{"id": 1, "role": "link", "name": "Sign in"}]}
        self.assertIsNone(a._maybe_signin_handoff_hint(scan))
        self.assertEqual(a.logs, [])


class _FakeConnector:
    id = "google"
    label = "Google (Gmail + Calendar)"
    tool_names = ("Gmail", "Google Calendar")
    availability = "ready"
    kind = "directory"

    def __init__(self, items=None, fail=False):
        self.items = items or []
        self.fail = fail
        self.calls = []

    def connected(self) -> bool:
        return True

    def fetch_items(self, *, cursor=None, now=None):
        self.calls.append(cursor)
        if self.fail:
            raise RuntimeError("token expired")
        return list(self.items), {"since": now}


class ConnectorLaneTests(unittest.TestCase):
    def test_wants_reads_only(self) -> None:
        from app.services.connectors import lane
        w = lane.wants({"intent": "check_email", "site": "gmail", "surface": "browser",
                        "requires_browser": True})
        self.assertEqual(w, {"mail": True, "calendar": False, "site": "google"})
        w = lane.wants({"intent": "list_meetings_today", "site": "", "surface": "browser",
                        "requires_browser": True})
        self.assertTrue(w and w["calendar"])
        self.assertIsNone(w["site"])
        for intent in ("draft_email", "send_email", "schedule_meeting", "reply_to_email"):
            self.assertIsNone(lane.wants({"intent": intent, "site": "gmail",
                                          "surface": "browser", "requires_browser": True}),
                              intent)
        self.assertIsNone(lane.wants({"intent": "research_company", "site": "web",
                                      "surface": "browser", "requires_browser": True}))

    def test_context_block_from_connector(self) -> None:
        from app.services.connectors import lane, registry, session
        fake = _FakeConnector(items=[
            {"kind": "mail", "ts": 1_800_000_000.0, "title": "Q3 numbers",
             "from": "Dave <dave@example.com>"},
            {"kind": "mail", "ts": 1_800_000_500.0, "title": "Lunch?",
             "from": "Hugh <hugh@example.com>"},
            {"kind": "calendar", "start": "2026-09-24T10:00:00Z",
             "end": "2026-09-24T11:00:00Z", "title": "All In Meeting 3",
             "people": ["Dave", "Hugh"]},
        ])
        with mock.patch.object(registry, "all", return_value=[fake]), \
             mock.patch.object(session, "active_ids", return_value={"google"}):
            out = lane.context_block({"intent": "check_email_and_calendar", "site": "gmail",
                                      "surface": "browser", "requires_browser": True},
                                     now=1_800_001_000.0)
        self.assertIsNotNone(out)
        block, meta = out
        self.assertEqual(meta["mail"], 2)
        self.assertEqual(meta["calendar"], 1)
        self.assertEqual(meta["ids"], ["google"])
        # newest first, no bodies
        self.assertLess(block.index("Lunch?"), block.index("Q3 numbers"))
        self.assertIn("All In Meeting 3", block)
        self.assertIn("no message bodies", block)
        self.assertIn("since", fake.calls[0])

    def test_toggled_off_connector_not_consulted(self) -> None:
        from app.services.connectors import lane, registry, session
        fake = _FakeConnector(items=[{"kind": "mail", "ts": 1.0, "title": "x"}])
        with mock.patch.object(registry, "all", return_value=[fake]), \
             mock.patch.object(session, "active_ids", return_value=set()):
            self.assertIsNone(lane.context_block(
                {"intent": "check_email", "site": "gmail", "surface": "browser",
                 "requires_browser": True}))
        self.assertEqual(fake.calls, [])

    def test_site_mismatch_takes_browser(self) -> None:
        from app.services.connectors import lane, registry, session
        fake = _FakeConnector()
        with mock.patch.object(registry, "all", return_value=[fake]), \
             mock.patch.object(session, "active_ids", return_value={"google"}):
            self.assertIsNone(lane.context_block(
                {"intent": "check_email", "site": "outlook", "surface": "browser",
                 "requires_browser": True}))

    def test_fetch_failure_falls_back_to_stored_and_says_so(self) -> None:
        from app.services.connectors import lane, registry, session
        fake = _FakeConnector(fail=True)
        with mock.patch.object(registry, "all", return_value=[fake]), \
             mock.patch.object(session, "active_ids", return_value={"google"}), \
             mock.patch.object(lane, "_stored_items",
                               return_value=[{"kind": "mail", "ts": 5.0,
                                              "title": "landed earlier", "from": "a@b"}]):
            block, meta = lane.context_block(
                {"intent": "check_email", "site": "gmail", "surface": "browser",
                 "requires_browser": True})
        self.assertIn("landed earlier", block)
        self.assertIn("token expired", block)
        self.assertEqual(len(meta["errors"]), 1)

    def test_connector_for_host(self) -> None:
        from app.services.connectors import lane, registry
        fake = _FakeConnector()
        with mock.patch.object(registry, "get", side_effect=lambda cid: fake if cid == "google" else None):
            self.assertEqual(lane.connector_for_host("mail.google.com")["id"], "google")
            self.assertTrue(lane.connector_for_host("accounts.google.com")["connected"])
            self.assertIsNone(lane.connector_for_host("example.com"))
            self.assertIsNone(lane.connector_for_host("outlook.live.com"))


class OrchestratorLaneWiringTests(unittest.TestCase):
    """A mail read for a connected connector never starts Playwright."""

    def test_lane_preempts_browser(self) -> None:
        from browser_agent import orchestrator as orch
        from browser_agent.orchestrator import Agent
        a = Agent.__new__(Agent)
        a.dry_run = "draft"
        a.logs = []
        a._log = a.logs.append
        a.session_id = "s"
        a.transcript = []
        a.last_route = None
        a.last_distill_id = None
        a._recorder = types.SimpleNamespace(annotate_run=lambda **kw: None)
        a.mem = types.SimpleNamespace(log_event=lambda *args: None)
        route = {"intent": "check_email", "site": "gmail", "surface": "browser",
                 "requires_browser": True, "tool": "browser_agent", "rationale": ""}
        calls = {}

        def direct_answer(goal, ctx, mode_guidance=""):
            calls["ctx"] = ctx
            return "You have two new emails."

        a.llm = types.SimpleNamespace(route=lambda g, c: dict(route),
                                      direct_answer=direct_answer, last_distill_id=None)
        a._ensure_browser = lambda: (_ for _ in ()).throw(AssertionError("browser started"))
        with mock.patch.object(Agent, "_study_block", return_value=""), \
             mock.patch.object(Agent, "_build_ctx", return_value="CTX\n"), \
             mock.patch.object(orch, "_connector_lane",
                               return_value=("\n\nCONNECTOR DATA: Lunch?\n",
                                             {"ids": ["google"], "labels": ["Google"],
                                              "mail": 2, "calendar": 0, "days": 3.0,
                                              "errors": []})):
            ans, status = a._run_goal_inner("check my gmail")
        self.assertEqual(status, "answered_no_browser")
        self.assertEqual(ans, "You have two new emails.")
        self.assertIn("CONNECTOR DATA", calls["ctx"])
        self.assertEqual(a.last_route["tool"], "connector")
        self.assertTrue(any("connector lane" in l for l in a.logs), a.logs)

    def test_no_lane_goes_to_browser(self) -> None:
        from browser_agent import orchestrator as orch
        from browser_agent.orchestrator import Agent
        a = Agent.__new__(Agent)
        a.dry_run = "draft"
        a._log = lambda s: None
        a.session_id = "s"
        a.transcript = []
        a.last_route = None
        a._recorder = types.SimpleNamespace(annotate_run=lambda **kw: None)
        a.mem = types.SimpleNamespace(log_event=lambda *args: None)
        route = {"intent": "research_company", "site": "web", "surface": "browser",
                 "requires_browser": True, "tool": "browser_agent", "rationale": ""}
        a.llm = types.SimpleNamespace(route=lambda g, c: dict(route))

        class _Started(Exception):
            pass

        def _start():
            raise _Started()
        a._ensure_browser = _start
        with mock.patch.object(Agent, "_study_block", return_value=""), \
             mock.patch.object(Agent, "_build_ctx", return_value=""), \
             mock.patch.object(orch, "_connector_lane", return_value=None):
            with self.assertRaises(_Started):
                a._run_goal_inner("find Acme's careers page")


class _FakePage:
    def __init__(self):
        self.log = []
        self.url = "https://accounts.google.com/"
        self.mouse = types.SimpleNamespace(
            click=lambda x, y, button="left", click_count=1: self.log.append(("click", x, y, button, click_count)),
            wheel=lambda dx, dy: self.log.append(("wheel", dx, dy)))
        self.keyboard = types.SimpleNamespace(
            type=lambda t, delay=0: self.log.append(("type", t)),
            press=lambda k: self.log.append(("press", k)))

    def wait_for_timeout(self, ms):
        pass


class HumanInputRelayTests(unittest.TestCase):
    def _driver(self):
        from browser_agent.browser import BrowserDriver
        d = BrowserDriver(headless=True)
        d.page = _FakePage()
        d.pixel_scale = 0.5
        d.published = 0
        d._publish_frame = lambda *a, **k: setattr(d, "published", d.published + 1)
        d._sync_active_page = lambda: None
        return d

    def test_click_maps_frame_pixels_like_click_at(self) -> None:
        d = self._driver()
        r = d.human_input({"type": "click", "x": 200, "y": 100})
        self.assertTrue(r["ok"], r)
        self.assertEqual(d.page.log, [("click", 100.0, 50.0, "left", 1)])
        self.assertEqual(d.published, 1)

    def test_type_key_scroll_frame(self) -> None:
        d = self._driver()
        d.human_input({"type": "type", "text": "me@example.com"})
        d.human_input({"type": "key", "key": "enter"})
        d.human_input({"type": "key", "key": "ctrl+a"})
        d.human_input({"type": "scroll", "dy": 99999})
        d.human_input({"type": "frame"})
        self.assertEqual(d.page.log, [("type", "me@example.com"), ("press", "Enter"),
                                      ("press", "Control+a"), ("wheel", 0, 4000.0)])
        self.assertEqual(d.published, 5)

    def test_unknown_and_no_page(self) -> None:
        d = self._driver()
        self.assertFalse(d.human_input({"type": "drag"})["ok"])
        d.page = None
        self.assertIn("not open", d.human_input({"type": "click", "x": 1, "y": 1})["reason"])

    def test_text_is_capped(self) -> None:
        from browser_agent.browser import HUMAN_TEXT_MAX
        d = self._driver()
        d.human_input({"type": "type", "text": "x" * (HUMAN_TEXT_MAX + 50)})
        self.assertEqual(len(d.page.log[0][1]), HUMAN_TEXT_MAX)


class BridgeRelayQueueTests(unittest.TestCase):
    """Request threads hand events to the Playwright thread and wait."""

    def _worker(self, *, open_=True):
        from app.services.agent_bridge import AgentWorker
        w = AgentWorker()
        seen = []
        drv = types.SimpleNamespace(page=object() if open_ else None,
                                    human_input=lambda ev: (seen.append(ev) or {"ok": True}))
        w.agent = types.SimpleNamespace(_browser_started=open_, driver=drv)
        return w, seen

    def test_browser_not_open_refused_before_queueing(self) -> None:
        w, seen = self._worker(open_=False)
        r = w.submit_ghost_input({"type": "click", "x": 1, "y": 1}, timeout_s=0.2)
        self.assertFalse(r["ok"])
        self.assertIn("not open", r["reason"])
        self.assertTrue(w._ghost_in_q.empty())

    def test_mid_task_refused(self) -> None:
        w, seen = self._worker()
        w.busy, w.awaiting = True, False
        r = w.submit_ghost_input({"type": "frame"}, timeout_s=0.2)
        self.assertIn("mid-task", r["reason"])
        self.assertEqual(seen, [])

    def test_serviced_while_idle_or_awaiting(self) -> None:
        w, seen = self._worker()
        def _drain_soon():
            threading.Event().wait(0.05)   # let the submit land first
            w._drain_ghost_input()
        for busy, awaiting in ((False, False), (True, True)):
            w.busy, w.awaiting = busy, awaiting
            t = threading.Thread(target=_drain_soon, daemon=True)
            t.start()
            r = w.submit_ghost_input({"type": "key", "key": "Tab"}, timeout_s=2.0)
            t.join(1.0)
            self.assertTrue(r["ok"], (busy, awaiting, r))
        self.assertEqual([e["type"] for e in seen], ["key", "key"])

    def test_timeout_when_nobody_drains(self) -> None:
        w, seen = self._worker()
        r = w.submit_ghost_input({"type": "frame"}, timeout_s=0.1)
        self.assertIn("timed out", r["reason"])

    def test_ask_wait_drains_relay(self) -> None:
        """_on_ask blocks the Playwright thread — the relay must run inside it."""
        w, seen = self._worker()
        w._emit = lambda *a, **k: None
        answered = threading.Event()

        def _human():
            r = w.submit_ghost_input({"type": "type", "text": "hello"}, timeout_s=2.0)
            self.assertTrue(r["ok"], r)
            w.submit_answer("done")
            answered.set()
        threading.Thread(target=_human, daemon=True).start()
        ans = w._on_ask("Please sign in.")
        self.assertTrue(answered.wait(2.0))
        self.assertEqual(ans, "done")
        self.assertEqual(seen, [{"type": "type", "text": "hello"}])


class GhostInputEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def test_status_carries_handoff_and_browser_state(self) -> None:
        j = self.client.get("/agent/ghost/status").json()
        self.assertIn(j["handoff"], ("reveal", "takeover", "none"))
        self.assertIn("browser_open", j)
        self.assertIn("awaiting", j)

    def test_unknown_kind_and_missing_coords(self) -> None:
        r = self.client.post("/agent/ghost/input", json={"type": "drag"}).json()
        self.assertFalse(r["ok"])
        r = self.client.post("/agent/ghost/input", json={"type": "click", "x": 3}).json()
        self.assertIn("x and y", r["reason"])

    def test_no_browser_is_an_honest_reason(self) -> None:
        from app.services import agent_bridge
        with mock.patch.object(agent_bridge.worker, "browser_open", return_value=False):
            r = self.client.post("/agent/ghost/input",
                                 json={"type": "click", "x": 3, "y": 4}).json()
        self.assertFalse(r["ok"])
        self.assertIn("not open", r["reason"])

    def test_chat_page_offers_takeover(self) -> None:
        html = self.client.get("/chat").text
        self.assertIn('id="ghosttake"', html)
        self.assertIn("/agent/ghost/input", html)
        self.assertIn('id="ghostkeys"', html)


if __name__ == "__main__":
    unittest.main()
