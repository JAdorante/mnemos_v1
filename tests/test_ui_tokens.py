"""UI token enforcement — prevents theme bypass regressions (audit §4.1).

Static checks on page modules: every HTML surface must opt into mnemos_theme,
utility pages must not regress to ``color: gray`` or forked palettes, and
KaTeX must stay scoped to math pages.
"""
from __future__ import annotations

import re
import unittest
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "app" / "api"

PAGE_SOURCES = sorted(API.glob("*_page.py")) + [
    API / "adoption.py",
    ROOT / "exec_webapp.py",
]

# Dead archaeology — still imports theme but is unrouted.
THEME_EXEMPT: set[Path] = {API / "home_page.py"}

# Utility pages that must stay copper/hex-free until tokens expand (C-3).
STRICT_UTILITY = {
    API / "auth_page.py",
    API / "triggers_page.py",
    API / "changes_page.py",
    API / "selfreport_page.py",
    API / "trace_page.py",
    API / "adoption.py",
}

THEME_CALL = re.compile(
    r"mnemos_theme\.(apply|apply_plain)"
    r"|apply as _mnemos|apply_plain as _plain"
    r"|_mnemos\(|_plain\(",
)
GRAY_COLOR = re.compile(r"color:\s*gray\b")
COPPER_RGBA = re.compile(r"rgba\(184,115,51")
FORKED_MUT = "#6B6F76"
CANONICAL_MUT = "--mut:#555960"
HEX_COLOR = re.compile(
    r"(?<![\w-])#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b",
)
HTML_MARKER = re.compile(r"<!doctype html", re.I)


def _ships_html(src: str) -> bool:
    return bool(HTML_MARKER.search(src))


class UiTokenEnforcementTests(unittest.TestCase):
    def test_page_modules_opt_into_theme(self) -> None:
        for path in PAGE_SOURCES:
            if path in THEME_EXEMPT:
                continue
            src = path.read_text(encoding="utf-8")
            if not _ships_html(src):
                continue
            self.assertRegex(
                src,
                THEME_CALL,
                f"{path.name} ships HTML but never calls mnemos_theme.apply/apply_plain",
            )

    def test_no_literal_gray_on_themed_pages(self) -> None:
        for path in PAGE_SOURCES:
            if path in THEME_EXEMPT:
                continue
            src = path.read_text(encoding="utf-8")
            if not THEME_CALL.search(src):
                continue
            for num, line in enumerate(src.splitlines(), 1):
                if GRAY_COLOR.search(line):
                    self.fail(f"{path.name}:{num} uses color: gray — use var(--mut)")

    def test_strict_utility_pages_have_no_raw_copper(self) -> None:
        for path in STRICT_UTILITY:
            src = path.read_text(encoding="utf-8")
            for num, line in enumerate(src.splitlines(), 1):
                if COPPER_RGBA.search(line):
                    self.fail(
                        f"{path.name}:{num} has raw copper rgba — "
                        "tokenize in mnemos_theme (C-3)",
                    )

    def test_strict_utility_pages_have_no_stray_hex_colors(self) -> None:
        for path in STRICT_UTILITY:
            src = path.read_text(encoding="utf-8")
            for num, line in enumerate(src.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                for match in HEX_COLOR.finditer(line):
                    self.fail(
                        f"{path.name}:{num} has hex literal {match.group()} "
                        "outside the token file — use var(--*)",
                    )

    def test_exec_webapp_uses_canonical_mut(self) -> None:
        src = (ROOT / "exec_webapp.py").read_text(encoding="utf-8")
        self.assertIn("apply_plain", src)
        self.assertIn("@@ROOT@@", src)
        self.assertNotIn(FORKED_MUT, src)
        try:
            from exec_webapp import PAGE  # noqa: WPS433 — optional runtime check
        except ModuleNotFoundError:
            return
        self.assertIn(CANONICAL_MUT, PAGE)

    def test_katex_not_in_global_font_links(self) -> None:
        from app.api.mnemos_theme import FONT_LINKS, KATEX_LINKS

        self.assertNotIn("katex.min.js", FONT_LINKS)
        self.assertNotIn("fonts.googleapis.com", FONT_LINKS)
        self.assertIn("/static/fonts/mnemos-fonts.css", FONT_LINKS)
        self.assertIn("/static/katex/katex.min.js", KATEX_LINKS)
        self.assertNotIn("cdn.jsdelivr.net", KATEX_LINKS)

    def test_extracted_pages_import(self) -> None:
        from app.api.auth_page import AUTH_PAGE
        from app.api.chat_page import CHAT_PAGE
        from app.api.console_page import CONSOLE_PAGE
        from app.api.desktop_access_page import DESKTOP_ACCESS_PAGE

        for name, page in (
            ("auth", AUTH_PAGE),
            ("chat", CHAT_PAGE),
            ("console", CONSOLE_PAGE),
            ("desktop", DESKTOP_ACCESS_PAGE),
        ):
            self.assertIn("<!doctype html", page.lower(), msg=name)
            self.assertIn(CANONICAL_MUT, page, msg=name)
        self.assertIn("katex.min.js", CHAT_PAGE)
        self.assertIn("katex.min.js", CONSOLE_PAGE)
        self.assertNotIn("katex.min.js", AUTH_PAGE)

    def test_chat_hides_operational_noise(self) -> None:
        from app.api.chat_page import CHAT_PAGE

        self.assertIn("kind==='progress'", CHAT_PAGE)
        self.assertIn("Agent ready|Fast lane ready|Offer expired", CHAT_PAGE)
        self.assertIn("Sources", CHAT_PAGE)
        self.assertNotIn("Remembered", CHAT_PAGE)

    def test_mnemos_chat_stream_in_ui_bundle(self) -> None:
        from app.api.mnemos_ui import UI_JS
        from pathlib import Path

        self.assertIn("window.MnemosChatStream", UI_JS)
        self.assertIn("/chat/stream", UI_JS)
        ui_js = Path(__file__).resolve().parents[1] / "app/static/js/mnemos-ui.js"
        self.assertIn("window.MnemosChatStream", ui_js.read_text(encoding="utf-8"))

    @unittest.skip(
        "/chat/stream cannot be driven from TestClient without wedging the "
        "suite — see the docstring; the client half is covered by "
        "test_mnemos_chat_stream_in_ui_bundle")
    def test_chat_stream_route(self) -> None:
        """Left skipped deliberately, with what was learned, rather than deleted.

        The original imported `app` from `exec_webapp` — the browser agent's
        FLASK app, which has no such endpoint. Handing a WSGI app to starlette's
        TestClient fails during lifespan startup with
        `Flask.__call__() missing 1 required positional argument`, which names
        nothing useful; that is why this sat broken.

        Fixing the import to `app.main` makes the route resolve and the handler
        run — and then the test hangs. `_events()` in app/api/routes.py loops
        7200 times at 0.5 s, and closing the response does not cancel it, so
        TestClient's teardown waits on a generator with an hour left to run.
        Measured: 240 s and 200 s timeouts hit exactly, and a full suite run
        went from ~5 minutes to killed at 25. Not reading a chunk does not help;
        the wait is in teardown, not the read.

        Route-existence introspection is not an alternative — this app serves
        `/today` with 200 while `app.routes` lists nine entries and no chat
        paths, so the table is not where its routes are visible.

        To make this testable, the endpoint would have to become bounded — an
        immediate first frame plus a way to stop the generator. That is a change
        to the route, not to the test, so it is left as a decision rather than
        made silently here.
        """

    def test_mnemos_dialog_in_ui_bundle(self) -> None:
        from app.api.mnemos_ui import UI_JS
        from pathlib import Path

        self.assertIn("window.MnemosDialog", UI_JS)
        self.assertIn("e.key !== 'Tab'", UI_JS)
        ui_js = Path(__file__).resolve().parents[1] / "app/static/js/mnemos-ui.js"
        self.assertTrue(ui_js.is_file(), "run scripts/sync_ui_static.py")
        self.assertIn("window.MnemosDialog", ui_js.read_text(encoding="utf-8"))

    def test_theme_static_assets_synced(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sys_path = root
        import sys

        if str(sys_path) not in sys.path:
            sys.path.insert(0, str(sys_path))
        from app.api.approval_partial import APPROVAL_CSS, APPROVAL_JS
        from app.api.mnemos_theme import CHROME_CSS, INK_CSS
        from app.api.mnemos_ui import UI_JS

        spec = importlib.util.spec_from_file_location(
            "_sync_ui_static", root / "scripts/sync_ui_static.py"
        )
        sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync)

        pairs = (
            (root / "app/static/css/mnemos-ink.css", INK_CSS),
            (root / "app/static/css/mnemos-chrome.css", CHROME_CSS),
            (root / "app/static/css/mnemos-approval.css", APPROVAL_CSS),
            (root / "app/static/js/mnemos-ui.js", sync._unwrap_script(UI_JS)),
            (root / "app/static/js/mnemos-approval.js", sync._unwrap_script(APPROVAL_JS)),
        )
        for path, expected in pairs:
            self.assertTrue(path.is_file(), f"missing {path.relative_to(root)}")
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                expected,
                f"{path.name} out of sync — run scripts/sync_ui_static.py",
            )

    def test_ssr_pages_are_not_cached(self) -> None:
        """SSR shells embed inline JS; a cached copy runs old client code."""
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            for route in ("/today", "/meetings"):
                cc = client.get(route).headers.get("cache-control", "")
                self.assertIn("no-store", cc, f"{route} may be served stale")

    def test_static_js_is_not_html_wrapped(self) -> None:
        """A .js file starting with "<script>" is dropped whole by the browser."""
        root = Path(__file__).resolve().parents[1]
        for name in ("mnemos-ui.js", "mnemos-approval.js"):
            text = (root / "app/static/js" / name).read_text(encoding="utf-8")
            self.assertNotIn("<script", text.lower(), f"{name} still carries a <script> tag")
            self.assertNotIn("</script", text.lower(), f"{name} still carries a </script> tag")

    def test_shared_ui_scripts_run_before_inline_page_code(self) -> None:
        """@@UI_JS@@ sits just above inline page scripts that call MnemosMemory
        at top level, so the bundle must not be deferred past document parse."""
        from app.api.mnemos_theme import THEME_SCRIPT_LINKS

        self.assertIn("/static/js/mnemos-ui.js", THEME_SCRIPT_LINKS)
        self.assertNotIn("defer", THEME_SCRIPT_LINKS)
        self.assertNotIn("async", THEME_SCRIPT_LINKS)

    def test_hold_captures_pointer_and_ignores_leave(self) -> None:
        """Hold-to-seal must survive the cursor slipping off the button.

        Windows precision-touchpads fire pointerleave / pointercancel on a
        1px slip; aborting the hold there made approval look broken.
        """
        from app.api.mnemos_ui import UI_JS

        hold_src = UI_JS.split("window.MnemosHold")[1].split("window.MnemosSeal")[0]
        self.assertIn("setPointerCapture", hold_src)
        self.assertIn("pointercancel", hold_src)
        self.assertNotIn("pointerleave", hold_src)

    def test_responsive_pages_have_media_queries(self) -> None:
        pages = [
            API / "org_page.py",
            API / "meeting_page.py",
            API / "org_network_page.py",
            API / "desktop_access_page.py",
            API / "onboarding_page.py",
            API / "shell_page.py",
            API / "peer_page.py",
            API / "phone_page.py",
            API / "chat_page.py",
            API / "profile_page.py",
            API / "console_page.py",
        ]
        for path in pages:
            src = path.read_text(encoding="utf-8")
            self.assertIn("@media", src, f"{path.name} missing responsive @media rules")

    def test_apply_uses_static_theme_links(self) -> None:
        from app.api.mnemos_theme import apply

        page = apply("<!doctype html><head>@@FONTS@@</head><body>@@UI_JS@@</body></html>")
        self.assertIn("/static/css/mnemos-ink.css", page)
        self.assertIn("/static/js/mnemos-approval.js", page)
        self.assertNotIn("inkDraw", page)

    def test_self_hosted_ui_static_present(self) -> None:
        root = Path(__file__).resolve().parents[1]
        required = [
            root / "app/static/fonts/mnemos-fonts.css",
            root / "app/static/katex/katex.min.css",
            root / "app/static/katex/katex.min.js",
            root / "app/static/katex/auto-render.min.js",
        ]
        for path in required:
            self.assertTrue(path.is_file(), f"missing {path.relative_to(root)}")

    def test_routes_no_longer_embeds_page_constants(self) -> None:
        src = (API / "routes.py").read_text(encoding="utf-8")
        self.assertNotIn("_CHAT_PAGE = ", src)
        self.assertNotIn("_CONSOLE_PAGE = ", src)
        self.assertIn("from app.api.chat_page import CHAT_PAGE", src)

    def test_trace_page_render_injects_tokens(self) -> None:
        from app.api.trace_page import render_trace_page

        html = render_trace_page(
            "test-corr",
            {"events": [], "candidates": [], "facts": [], "agent_runs": []},
        )
        self.assertIn(CANONICAL_MUT, html)
        self.assertNotIn("color: gray", html)

    def test_z_scale_tokens_defined(self) -> None:
        from app.api.mnemos_theme import ROOT_TOKENS

        for name in (
            "--z-base", "--z-raised", "--z-rail", "--z-banner",
            "--z-float", "--z-popover", "--z-system", "--z-modal",
            "--chrome-h", "--composer-h", "--dock-clear",
        ):
            self.assertIn(name, ROOT_TOKENS)

    def test_no_raw_z_index_outside_theme(self) -> None:
        """Raw z-index integers outside mnemos_theme.py are a lint failure."""
        raw_z = re.compile(r"z-index\s*:\s*\d+")
        scan_roots = [
            API,
            ROOT / "app" / "static" / "css",
            ROOT / "app" / "static" / "js",
            ROOT / "exec_webapp.py",
        ]
        theme = (API / "mnemos_theme.py").resolve()
        offenders: list[str] = []
        for root in scan_roots:
            if not root.exists():
                continue
            paths = [root] if root.is_file() else list(root.rglob("*"))
            for path in paths:
                if not path.is_file():
                    continue
                if path.suffix not in {".py", ".css", ".js"}:
                    continue
                if path.resolve() == theme:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                for num, line in enumerate(text.splitlines(), 1):
                    if raw_z.search(line):
                        offenders.append(f"{path.relative_to(ROOT)}:{num}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "raw z-index integers — use var(--z-*) from ROOT_TOKENS:\n"
            + "\n".join(offenders),
        )

    def test_fixed_elements_use_dock_or_modal(self) -> None:
        """New position:fixed must be dock, modal layer, or chrome measurement."""
        fixed_re = re.compile(r"position\s*:\s*fixed", re.I)
        allow = re.compile(
            r"#mnemosDockBR|#mnemosPrivacy|#spotlight|mnemos-hold-tip"
            r"|--z-modal|--z-float|inset\s*:\s*0",
            re.I,
        )
        offenders: list[str] = []
        candidates = list(API.glob("*.py"))
        for d in (ROOT / "app" / "static" / "css", ROOT / "app" / "static" / "js"):
            if d.is_dir():
                candidates.extend(d.glob("*"))
        for path in candidates:
            if not path.is_file() or path.suffix not in {".py", ".css", ".js"}:
                continue
            if path.name in {"mnemos_theme.py", "mnemos-chrome.css"}:
                # Dock + modal templates live here by design.
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            for num, line in enumerate(lines, 1):
                if not fixed_re.search(line):
                    continue
                window = "\n".join(lines[max(0, num - 3) : num + 2])
                if allow.search(window) or allow.search(line):
                    continue
                if "mnemosHoldTip" in window or "mnemos-hold-tip" in window:
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{num}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "position:fixed outside dock/modal allowlist:\n" + "\n".join(offenders),
        )

    def test_no_magic_chrome_height_offsets(self) -> None:
        """Fixed/absolute top/bottom must not encode another element's height."""
        # Canonical bugs: top:72px (header), bottom:120px (composer).
        magic = re.compile(
            r"(?:top|bottom)\s*:\s*(?:72|120|150)px\b",
            re.I,
        )
        offenders: list[str] = []
        for path in list(API.glob("*_page.py")) + [API / "routes.py"]:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for num, line in enumerate(text.splitlines(), 1):
                if magic.search(line):
                    offenders.append(f"{path.name}:{num}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "magic chrome-height offsets — use layout or var(--chrome-h):\n"
            + "\n".join(offenders),
        )

    def test_dock_and_chrome_helpers_in_ui_bundle(self) -> None:
        from app.api.mnemos_ui import UI_JS

        self.assertIn("window.MnemosDock", UI_JS)
        self.assertIn("window.MnemosChrome", UI_JS)
        self.assertIn("window.MnemosLayer", UI_JS)
        self.assertIn("lockScroll", UI_JS)


if __name__ == "__main__":
    unittest.main()
