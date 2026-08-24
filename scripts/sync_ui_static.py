#!/usr/bin/env python3
"""Sync theme CSS and UI JS from Python modules into app/static/ (audit Q-2).

Run after editing mnemos_theme.py, approval_partial.py, or mnemos_ui.py:
  python3 scripts/sync_ui_static.py
"""
from __future__ import annotations

import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS_DIR = ROOT / "app" / "static" / "css"
JS_DIR = ROOT / "app" / "static" / "js"


def _unwrap_script(text: str) -> str:
    """Strip the <script>...</script> wrapper the Python constants carry.

    UI_JS / APPROVAL_JS are authored for inline injection via @@UI_JS@@, so they
    include the tags. A .js file must not: the browser parses the leading "<" as
    JavaScript and drops the whole file.
    """
    body = text.strip()
    body = re.sub(r"(?is)\A<script\b[^>]*>", "", body)
    body = re.sub(r"(?is)</script\s*>\Z", "", body)
    return body.strip() + "\n"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from app.api.approval_partial import APPROVAL_CSS, APPROVAL_JS
    from app.api.mnemos_theme import CHROME_CSS, INK_CSS
    from app.api.mnemos_ui import UI_JS

    CSS_DIR.mkdir(parents=True, exist_ok=True)
    JS_DIR.mkdir(parents=True, exist_ok=True)
    targets = (
        (CSS_DIR / "mnemos-ink.css", INK_CSS),
        (CSS_DIR / "mnemos-chrome.css", CHROME_CSS),
        (CSS_DIR / "mnemos-approval.css", APPROVAL_CSS),
        (JS_DIR / "mnemos-ui.js", _unwrap_script(UI_JS)),
        (JS_DIR / "mnemos-approval.js", _unwrap_script(APPROVAL_JS)),
    )
    for path, content in targets:
        path.write_text(content, encoding="utf-8")
        print(f"  wrote {path.relative_to(ROOT)} ({len(content)} bytes)")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
