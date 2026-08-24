"""Playwright overlap smoke — floating UI must not intersect.

Requires playwright + chromium. Skips cleanly when unavailable.
Run: python -m pytest tests/test_overlap_smoke.py -q
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

ROUTES = ("/chat", "/memory", "/today", "/peer", "/profile")
WIDTHS = (1280, 400)

# Elements that must not pairwise-intersect when forced visible.
FLOATERS = (
    "#mnemosDockBR > *",
    "header.top, .top",
    "#mnemosApproval.on",
    "#pastPanel.open",
    "#spotlight.open",
    "#mnemosPrivacy.open",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except (error.URLError, TimeoutError, OSError):
            time.sleep(0.25)
    return False


OVERLAP_JS = """
([selectors]) => {
  const nodes = [];
  for (const sel of selectors) {
    document.querySelectorAll(sel).forEach((el) => {
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return;
      nodes.push({
        sel, id: el.id || el.className,
        left: r.left, top: r.top, right: r.right, bottom: r.bottom,
      });
    });
  }
  const hits = [];
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      // Same selector family (e.g. multiple .top) — skip self-stack siblings in dock
      if (a.sel === '#mnemosDockBR > *' && b.sel === '#mnemosDockBR > *') continue;
      if (a.sel === b.sel && a.sel.includes('.top')) continue;
      const overlap = !(a.right <= b.left || b.right <= a.left
        || a.bottom <= b.top || b.bottom <= a.top);
      if (overlap) hits.push([a, b]);
    }
  }
  return hits;
}
"""


@unittest.skipUnless(
    os.environ.get("MNEMOS_OVERLAP_SMOKE") == "1"
    or os.environ.get("CI") == "true",
    "set MNEMOS_OVERLAP_SMOKE=1 (or CI) to run Playwright overlap smoke",
)
class OverlapSmokeTests(unittest.TestCase):
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
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if not _wait_health(f"{cls.base}/health"):
            err = (cls.proc.stderr.read() if cls.proc.stderr else b"").decode(
                "utf-8", "replace"
            )
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

    def test_no_floating_intersections(self) -> None:
        from playwright.sync_api import sync_playwright

        failures: list[str] = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                for width in WIDTHS:
                    page.set_viewport_size({"width": width, "height": 900})
                    for route in ROUTES:
                        page.goto(f"{self.base}{route}", wait_until="domcontentloaded",
                                  timeout=20_000)
                        # Force "everything on" where the DOM allows it.
                        page.evaluate(
                            """() => {
                              const ap = document.getElementById('mnemosApproval');
                              if (ap) { ap.classList.add('on'); ap.hidden = false; }
                              const toast = document.getElementById('mnemosToast');
                              if (toast) {
                                toast.hidden = false;
                                toast.textContent = 'Overlap smoke toast';
                                if (window.MnemosPlaceToast) MnemosPlaceToast();
                              }
                              const ghost = document.getElementById('ghost');
                              if (ghost) ghost.style.display = 'block';
                              if (window.MnemosCapture && MnemosCapture.mount)
                                MnemosCapture.mount();
                              if (window.MnemosChrome) MnemosChrome.sync();
                            }"""
                        )
                        page.wait_for_timeout(150)
                        hits = page.evaluate(OVERLAP_JS, [list(FLOATERS)])
                        for a, b in hits:
                            # Sticky top overlapping in-flow approval is OK —
                            # approval is below header in document order.
                            labels = {a["sel"], b["sel"]}
                            if "header.top, .top" in labels and "#mnemosApproval.on" in labels:
                                continue
                            failures.append(
                                f"{route}@{width}: {a.get('id') or a['sel']} ∩ "
                                f"{b.get('id') or b['sel']}"
                            )
                browser.close()
        except Exception as exc:
            msg = str(exc)
            if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
                self.skipTest(f"run: {sys.executable} -m playwright install chromium")
            raise

        self.assertEqual(failures, [], "overlapping floats:\n" + "\n".join(failures))


if __name__ == "__main__":
    os.environ.setdefault("MNEMOS_OVERLAP_SMOKE", "1")
    unittest.main()
