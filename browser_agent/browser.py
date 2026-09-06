"""Playwright actuator. Each semantic action maps to one deterministic
browser call (FR-ACT-2). The LLM never writes selectors — it passes an
element_id, which we resolve to a locator tagged during the last scan.

Two launch modes:
  - ephemeral (default): a fresh isolated context, no cookies, no persistence.
  - persistent (user_data_dir set): a dedicated on-disk profile so a login
    survives across runs — session reuse (FR-SEC-2). Pair with channel="chrome"
    to drive real installed Chrome, which login providers rarely block.
"""
import hashlib
import os
import struct
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import config as cfg
from .perception import SCAN_JS, READ_JS
from .surfaces import inside_surface, pixel_surface

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
        self._ghost = "off"       # resolved from cfg.GHOST_MODE in start()
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        # Pixel fallback state, refreshed by every scan(): the graphics surface
        # coordinates are confined to, and screenshot-px -> CSS-px scale.
        self.pixel_surface = None
        self.pixel_scale = 1.0
        self.shot_size = (0, 0)

    def _publish_frame(self, png: bytes | None = None, url: str = "",
                       title: str = "") -> None:
        """Drop the current view into the ghost relay (best-effort, never raises).
        Runs on the Playwright thread — the only place a screenshot is legal."""
        if self._ghost == "off":
            return
        try:
            from . import ghost
            if png is None:
                png = self.page.screenshot(full_page=False)
            ghost.publish(png, url=url or self.page.url, title=title)
        except Exception:
            pass

    def start(self):
        self._pw = sync_playwright().start()

        # Ghost mode: the agent's view streams into the chat pane instead of
        # taking the user's screen. 'headless' drops the window entirely;
        # 'hidden' keeps a real headed window (login-provider friendly) parked
        # off-screen. Attach mode (user's own Chrome) is never ghosted.
        self._ghost = cfg.GHOST_MODE if not self.cdp_url else "off"
        if self._ghost == "headless":
            self.headless = True

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

        launch_args = ["--disable-blink-features=AutomationControlled"]
        # Hosted containers run as root, where Chromium's user-namespace
        # sandbox cannot start. Opt-in only — never weaken desktop installs.
        if os.environ.get("QUILL_BROWSER_NO_SANDBOX", "").strip() in ("1", "true", "True"):
            launch_args.append("--no-sandbox")
        hidden = self._ghost == "hidden" and not self.headless
        if hidden:
            # Chromium clamps --window-position back onto the display, so the
            # real hide happens post-launch (ghost.hide_new_windows). These keep
            # the off-screen window rendering so the ghost pane stays live.
            launch_args += [
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ]
        win_snapshot = None
        if hidden:
            try:
                from . import ghost
                win_snapshot = ghost.snapshot_windows()
            except Exception:
                pass
        launch = dict(
            headless=self.headless,
            args=launch_args,
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
        if hidden and win_snapshot is not None:
            # Zero visible presence: off-screen AND no taskbar button.
            # Best-effort; reveal_window() undoes both for sign-in handoffs.
            try:
                from . import ghost
                res = ghost.hide_new_windows(win_snapshot)
                if not res.get("ok"):
                    print(f"[ghost] window hide skipped ({res.get('reason')})")
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
        # no interactive elements at all == almost certainly still rendering,
        # UNLESS the page is a rendered graphics surface (a canvas game draws
        # no DOM at all) — waiting three more times there is pure latency.
        return s.get("count", 0) == 0 and not (s.get("surfaces") or [])

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
                    s = s2
                    break
                s = s2
        self._augment_clickables_cdp(s)
        self.pixel_surface = pixel_surface(s) if cfg.BROWSER_PIXEL else None
        if self.pixel_surface:
            # The DOM signature can't see a canvas move; a hash of the rendered
            # pixels can. Costs one screenshot, and only on graphics pages.
            png = self._grab()
            if png:
                s["pixel_hash"] = hashlib.sha1(png).hexdigest()[:12]
                self._publish_frame(png=png, url=s.get("url", ""),
                                    title=s.get("title", ""))
                return s
        self._publish_frame(url=s.get("url", ""), title=s.get("title", ""))
        return s

    # Candidates for the CDP listener probe: visible, not already listed (the
    # semantic scan + pointer sweep tag data-agent-id), and small enough to be
    # a discrete control — listener owners above ~30% of the viewport are
    # delegation containers (document/board-level handlers), where clicking the
    # element's center means nothing; the 'dom' surface covers those with
    # coordinates instead.
    _CDP_CANDIDATES_JS = """
    (max) => {
      const vw = window.innerWidth, vh = window.innerHeight;
      const out = [];
      for (const el of document.body ? document.body.querySelectorAll('*') : []) {
        if (out.length >= max) break;
        if (el.closest('[data-agent-id]')) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8) continue;
        if (r.width * r.height > 0.3 * vw * vh) continue;
        if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) continue;
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') continue;
        if (parseFloat(cs.opacity || '1') === 0) continue;
        out.push(el);
      }
      return out;
    }
    """

    _CDP_TAG_JS = """
    function(n) {
      // Re-checked at TAG time (not just collection time): earlier probes in
      // this same pass may have tagged an ancestor/descendant, and a container
      // wrapping listed elements (a pile div with a delegation handler around
      // its cards) would otherwise be listed again under its children's text.
      const near = this.closest('[data-agent-id]');
      if (near && near !== this) return null;
      if (this.querySelector('[data-agent-id]')) return null;
      this.setAttribute('data-agent-id', String(n));
      let nm = this.getAttribute('aria-label') || this.getAttribute('title')
        || (this.innerText || this.textContent || '');
      nm = (nm || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
      if (!nm) {
        const cls = (typeof this.className === 'string' && this.className.trim())
          ? '.' + this.className.trim().split(/\\s+/).slice(0, 2).join('.')
          : (this.id ? '#' + this.id : '');
        nm = this.tagName.toLowerCase() + cls;
      }
      const sel = typeof this.className === 'string'
        && /(?:^|\\s)(?:selected|active|current|chosen)(?:\\s|$)/.test(this.className);
      return [nm, this.tagName.toLowerCase(), sel];
    }
    """

    _CDP_CLICK_EVENTS = frozenset(
        {"click", "mousedown", "mouseup", "pointerdown", "pointerup", "touchstart"})

    def _augment_clickables_cdp(self, s: dict) -> None:
        """Ask Chrome (DOMDebugger.getEventListeners) which visible elements
        have click listeners that neither the semantic scan nor the pointer
        sweep caught, tag them, and append them as 'clickable' element_ids.

        This is the ground truth the cursor heuristic approximates: a JS-wired
        control with cursor:default is invisible to CSS but not to the debugger.
        Sparse pages only (busy pages are already well described), main frame
        only, and every failure degrades silently to the heuristic-only scan.
        """
        # Gate on the SEMANTIC count: the pointer sweep having found pieces is
        # exactly the situation where listener-only targets are likely too.
        semantic = s.get("semantic_count", s.get("count", 0))
        if not cfg.CDP_LISTENERS or semantic >= cfg.CDP_SPARSE_AT:
            return
        try:
            sess = getattr(self, "_cdp_sess", None)
            if sess is None or getattr(self, "_cdp_page", None) is not self.page:
                sess = self.page.context.new_cdp_session(self.page)
                self._cdp_sess, self._cdp_page = sess, self.page
            ev = sess.send("Runtime.evaluate", {
                "expression": f"({self._CDP_CANDIDATES_JS})({cfg.CDP_MAX_NODES})",
                "objectGroup": "agent-scan"})
            arr_id = (ev.get("result") or {}).get("objectId")
            if not arr_id:
                return
            props = sess.send("Runtime.getProperties", {
                "objectId": arr_id, "ownProperties": True})
            added = 0
            next_id = len(s.get("elements", []))
            for prop in props.get("result", []):
                if not prop.get("name", "").isdigit():
                    continue
                obj_id = (prop.get("value") or {}).get("objectId")
                if not obj_id:
                    continue
                try:
                    ls = sess.send("DOMDebugger.getEventListeners",
                                   {"objectId": obj_id})
                except Exception:
                    continue
                if not any(l.get("type") in self._CDP_CLICK_EVENTS
                           for l in ls.get("listeners", [])):
                    continue
                tag_res = sess.send("Runtime.callFunctionOn", {
                    "objectId": obj_id,
                    "functionDeclaration": self._CDP_TAG_JS,
                    "arguments": [{"value": next_id}],
                    "returnByValue": True})
                val = (tag_res.get("result") or {}).get("value")
                if not val:   # tag JS refused: overlaps an already-listed element
                    continue
                nm, tag, sel = (val + [False])[:3] if len(val) < 3 else val
                item = {"id": next_id, "role": "clickable", "name": nm,
                        "tag": tag, "editable": False, "via": "listener"}
                if sel:
                    item["selected"] = True
                s.setdefault("elements", []).append(item)
                next_id += 1
                added += 1
            if added:
                s["count"] = len(s["elements"])
            try:
                sess.send("Runtime.releaseObjectGroup",
                          {"objectGroup": "agent-scan"})
            except Exception:
                pass
        except Exception:
            # CDP unavailable (attach mode quirks, page swap mid-scan, old
            # Playwright): the scan stays heuristic-only.
            self._cdp_sess = None

    def read(self, element_id=None) -> str:
        self._sync_active_page()
        txt = self.page.evaluate(READ_JS, element_id)
        return (txt or "")[:4000]

    def screenshot(self, path: str):
        try:
            self.page.screenshot(path=path, full_page=False)
        except Exception:
            pass

    def screenshot_bytes(self):
        """PNG bytes of the current viewport, or None. Used to feed the executor
        vision alongside the accessibility tree (and saved as the step artifact).

        This is also the coordinate space the model measures pixel actions in,
        so it records the screenshot -> CSS-pixel scale it hands back."""
        png = self._grab()
        if png is None:
            return None
        self._publish_frame(png=png)
        return png

    def _grab(self):
        """Viewport PNG, fitted for grounding, with self.pixel_scale updated."""
        try:
            png = self.page.screenshot(full_page=False)
        except Exception:
            return None
        try:
            png = self._fit_for_grounding(png)
        except Exception:
            pass
        return png

    @staticmethod
    def _png_size(png: bytes) -> tuple[int, int]:
        """(width, height) from the IHDR chunk — no image library needed."""
        if not png or len(png) < 24 or not png.startswith(b"\x89PNG"):
            return (0, 0)
        w, h = struct.unpack(">II", png[16:24])
        return int(w), int(h)

    def _fit_for_grounding(self, png: bytes) -> bytes:
        """Keep the screenshot inside Claude's vision resize limit and record the
        scale from its pixels back to CSS pixels.

        The model measures coordinates on the image it is shown. If that image
        is a different size than the page (a HiDPI display doubles it, and the
        API downsizes anything past ~1568px), every coordinate it returns is off
        by that factor — so the mapping is computed here rather than assumed."""
        iw, ih = self._png_size(png)
        self.shot_size = (iw, ih)
        if not iw or not ih:
            self.pixel_scale = 1.0
            return png
        css_w = iw
        try:
            css_w = int(self.page.evaluate("() => window.innerWidth")) or iw
        except Exception:
            pass
        longest = max(iw, ih)
        cap = max(320, cfg.PIXEL_SHOT_MAX_EDGE)
        if longest > cap:
            try:
                import io

                from PIL import Image

                img = Image.open(io.BytesIO(png))
                ratio = cap / float(longest)
                img = img.resize((max(1, int(iw * ratio)), max(1, int(ih * ratio))))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                png = buf.getvalue()
                iw, ih = self._png_size(png)
                self.shot_size = (iw, ih)
            except Exception:
                pass  # no Pillow — send it full size and scale from its width
        self.pixel_scale = (css_w / float(iw)) if iw else 1.0
        return png

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
            elif name in ("click_at", "drag", "press_key"):
                return self._pixel_action(name, args)
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
            self._publish_frame()
            return {"ok": True, "detail": "ok"}
        except Exception as e:
            self._publish_frame()
            return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}

    # --- pixel fallback ----------------------------------------------------
    @staticmethod
    def _coord(v):
        """Model coordinate -> float. Tolerates "412", 412.0, and the recurring
        malformed shape where both numbers arrive packed in one field
        ("412, 630") — the intent is unambiguous, so don't burn a retry."""
        if isinstance(v, str):
            v = v.strip().strip("()")
            if "," in v:
                v = v.split(",")[0]
        return float(v)

    def _to_css(self, x, y) -> tuple[float, float]:
        """Screenshot pixels (what the model measured) -> CSS pixels (what the
        browser clicks)."""
        sc = self.pixel_scale or 1.0
        return self._coord(x) * sc, self._coord(y) * sc

    def _pixel_action(self, name: str, args: dict) -> dict:
        """click_at / drag / press_key — the coordinate path for canvas UIs.

        Refuses coordinates outside the graphics surface from the last scan:
        that region is exactly the part of the page the DOM cannot describe,
        and everything around it has an element_id to click instead. So a
        misread screenshot can't blind-click a Send/Buy/Delete button."""
        if not cfg.BROWSER_PIXEL:
            return {"ok": False, "detail": "pixel actions are disabled"}
        p = self.page
        if name == "press_key":
            key = str(args.get("key") or "").strip()
            if not key:
                return {"ok": False, "detail": "press_key needs a key"}
            # Playwright wants "Control+z"; models write "ctrl+z".
            parts = [k for k in key.replace(" ", "+").split("+") if k]
            alias = {"ctrl": "Control", "control": "Control", "cmd": "Meta",
                     "command": "Meta", "meta": "Meta", "alt": "Alt",
                     "option": "Alt", "shift": "Shift", "esc": "Escape",
                     "return": "Enter", "del": "Delete",
                     # Playwright spells these camel-case; models don't.
                     "left": "ArrowLeft", "right": "ArrowRight",
                     "up": "ArrowUp", "down": "ArrowDown",
                     "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
                     "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
                     "pageup": "PageUp", "pagedown": "PageDown"}
            norm = "+".join(alias.get(k.lower(), k if len(k) == 1 else k.capitalize())
                            for k in parts)
            p.keyboard.press(norm)
            p.wait_for_timeout(200)
            self._publish_frame()
            return {"ok": True, "detail": f"pressed {norm}"}

        surf = self.pixel_surface
        if not surf:
            return {"ok": False, "detail": (
                "no graphics surface on this page — act by element_id. If a "
                "game/widget just loaded, it may appear as a surface on the "
                "next scan (any action triggers one).")}
        try:
            if name == "click_at":
                pts = [self._to_css(args.get("x"), args.get("y"))]
            else:
                pts = [self._to_css(args.get("from_x"), args.get("from_y")),
                       self._to_css(args.get("to_x"), args.get("to_y"))]
        except (TypeError, ValueError):
            return {"ok": False, "detail": f"bad coordinates: {args}"}
        for (cx, cy) in pts:
            if not inside_surface(surf, cx, cy, pad=cfg.PIXEL_EDGE_PAD):
                sc = self.pixel_scale or 1.0
                return {"ok": False, "detail": (
                    f"({cx / sc:.0f}, {cy / sc:.0f}) is outside the graphics "
                    f"surface ({surf['x'] / sc:.0f}, {surf['y'] / sc:.0f}) to "
                    f"({(surf['x'] + surf['w']) / sc:.0f}, "
                    f"{(surf['y'] + surf['h']) / sc:.0f}) in screenshot pixels — "
                    "use element_id for controls outside it")}
        if name == "click_at":
            (cx, cy) = pts[0]
            btn = str(args.get("button") or "left").lower()
            if btn not in ("left", "right", "middle"):
                btn = "left"
            try:
                clicks = max(1, min(2, int(args.get("clicks") or 1)))
            except (TypeError, ValueError):
                clicks = 1
            p.mouse.click(cx, cy, button=btn, click_count=clicks)
            detail = f"clicked ({cx:.0f}, {cy:.0f})" + (" x2" if clicks == 2 else "")
        else:
            (fx, fy), (tx, ty) = pts
            # Stepped move: canvas drag handlers track mousemove, and a single
            # jump from press to release reads as a click, not a drag.
            p.mouse.move(fx, fy)
            p.mouse.down()
            steps = max(2, cfg.PIXEL_DRAG_STEPS)
            for i in range(1, steps + 1):
                p.mouse.move(fx + (tx - fx) * i / steps,
                             fy + (ty - fy) * i / steps)
            p.mouse.up()
            detail = f"dragged ({fx:.0f}, {fy:.0f}) -> ({tx:.0f}, {ty:.0f})"
        p.wait_for_timeout(300)
        self._publish_frame()
        return {"ok": True, "detail": detail}

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
