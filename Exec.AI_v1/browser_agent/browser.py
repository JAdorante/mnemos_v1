"""Playwright actuator. Each semantic action maps to one deterministic
browser call (FR-ACT-2). The LLM never writes selectors — it passes an
element_id, which we resolve to a locator tagged during the last scan.

Two launch modes:
  - ephemeral (default): a fresh isolated context, no cookies, no persistence.
  - persistent (user_data_dir set): a dedicated on-disk profile so a login
    survives across runs — session reuse (FR-SEC-2). Pair with channel="chrome"
    to drive real installed Chrome, which login providers rarely block.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import config as cfg
from .perception import SCAN_JS, READ_JS

# Trim the most obvious "I'm automated" tells so login providers (Google/MS)
# are less likely to hard-block sign-in. Not a bypass — just avoids the false
# "insecure browser" flag on a browser the user themselves is logging into.
_STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


class BrowserDriver:
    def __init__(self, headless: bool = True, user_data_dir=None, channel=None,
                 cdp_url=None):
        self.headless = headless
        self.user_data_dir = str(user_data_dir) if user_data_dir else None
        self.channel = channel
        self.cdp_url = cdp_url          # attach to a user's already-running Chrome
        self.attached = False
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self):
        self._pw = sync_playwright().start()

        # Attach mode: connect to the user's own Chrome (started with
        # --remote-debugging-port), reusing whatever they're already logged into.
        if self.cdp_url:
            self.browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
            self.context = (self.browser.contexts[0] if self.browser.contexts
                            else self.browser.new_context())
            self.page = (self.context.pages[-1] if self.context.pages
                         else self.context.new_page())
            self.attached = True
            try:
                self.context.add_init_script(_STEALTH_JS)
            except Exception:
                pass
            return

        launch = dict(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        if self.channel:
            launch["channel"] = self.channel
        if self.user_data_dir:
            # persistent profile == session reuse: cookies/localStorage on disk
            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
            self.context = self._pw.chromium.launch_persistent_context(
                self.user_data_dir, viewport={"width": 1280, "height": 800}, **launch)
            self.browser = None
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        else:
            self.browser = self._pw.chromium.launch(**launch)
            self.context = self.browser.new_context(viewport={"width": 1280, "height": 800})
            self.page = self.context.new_page()
        try:
            self.context.add_init_script(_STEALTH_JS)
        except Exception:
            pass

    @property
    def persistent(self) -> bool:
        return self.user_data_dir is not None

    # --- helpers -----------------------------------------------------------
    def _sync_active_page(self):
        """Follow newly opened tabs so the agent acts on what it sees."""
        if self.context and self.context.pages:
            self.page = self.context.pages[-1]
            try:
                self.page.bring_to_front()
            except Exception:
                pass

    def _loc(self, element_id):
        return self.page.locator(f'[data-agent-id="{element_id}"]').first

    # --- observation -------------------------------------------------------
    @staticmethod
    def _looks_blank(s):
        # no interactive elements at all == almost certainly still rendering
        return s.get("count", 0) == 0

    def scan(self) -> dict:
        self._sync_active_page()
        s = self.page.evaluate(SCAN_JS, cfg.MAX_ELEMENTS)
        # SPA/JS-heavy pages report blank right after navigation; wait for the DOM
        # to render interactive elements before we act on or verify the page.
        if self._looks_blank(s):
            for _ in range(cfg.RENDER_RETRIES):
                try:
                    self.page.wait_for_function(
                        "() => document.querySelectorAll("
                        "'a[href],button,input,select,textarea,[role]').length > 3",
                        timeout=cfg.RENDER_WAIT_MS)
                except Exception:
                    pass  # timed out waiting to render — rescan and check anyway
                s2 = self.page.evaluate(SCAN_JS, cfg.MAX_ELEMENTS)
                if not self._looks_blank(s2):
                    return s2
                s = s2
        return s

    def read(self, element_id=None) -> str:
        self._sync_active_page()
        txt = self.page.evaluate(READ_JS, element_id)
        return (txt or "")[:4000]

    def screenshot(self, path: str):
        try:
            self.page.screenshot(path=path, full_page=False)
        except Exception:
            pass

    def wait_briefly(self):
        try:
            self.page.wait_for_timeout(1200)
        except Exception:
            pass

    def goto(self, url: str):
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)

    # --- action execution --------------------------------------------------
    def execute(self, name: str, args: dict) -> dict:
        """Return {ok: bool, detail: str}. Never raises."""
        try:
            p = self.page
            if name == "click":
                self._loc(args["element_id"]).click(timeout=8000)
            elif name == "type":
                loc = self._loc(args["element_id"])
                try:
                    loc.fill(args["text"], timeout=8000)
                except Exception:
                    loc.click(timeout=8000)
                    p.keyboard.type(args["text"])
            elif name == "select":
                loc = self._loc(args["element_id"])
                try:
                    loc.select_option(label=args["option"], timeout=8000)
                except Exception:
                    loc.select_option(value=args["option"], timeout=8000)
            elif name == "scroll":
                amt = int(args.get("amount") or 600)
                p.mouse.wheel(0, amt if args.get("direction") != "up" else -amt)
                p.wait_for_timeout(300)
            elif name == "navigate":
                p.goto(args["url"], wait_until="domcontentloaded", timeout=30000)
            elif name == "go_back":
                p.go_back(timeout=15000)
            elif name == "wait_for":
                cond = str(args.get("condition", "")).lower()
                if "idle" in cond or "network" in cond:
                    p.wait_for_load_state("networkidle", timeout=10000)
                elif cond:
                    p.get_by_text(args["condition"]).first.wait_for(timeout=10000)
                else:
                    p.wait_for_timeout(1500)
            else:
                return {"ok": False, "detail": f"unknown action: {name}"}
            self._sync_active_page()
            return {"ok": True, "detail": "ok"}
        except Exception as e:
            return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}

    def close(self):
        if self.attached:
            # Leave the user's real browser running — just drop our connection.
            try:
                self._pw.stop()
            except Exception:
                pass
            return
        for fn in (
            lambda: self.context.close(),
            lambda: self.browser.close(),
            lambda: self._pw.stop(),
        ):
            try:
                fn()
            except Exception:
                pass
