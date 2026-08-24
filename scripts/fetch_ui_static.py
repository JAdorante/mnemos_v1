#!/usr/bin/env python3
"""Download self-hosted UI assets (fonts, KaTeX) into app/static/.

Run before release or when bumping font/KaTeX versions:
  python3 scripts/fetch_ui_static.py
"""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
FONTS_DIR = STATIC / "fonts"
KATEX_DIR = STATIC / "katex"
KATEX_VERSION = "0.16.11"
KATEX_BASE = f"https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist"
GOOGLE_FONTS_CSS = (
    "https://fonts.googleapis.com/css2?"
    "family=IBM+Plex+Mono:wght@400;500"
    "&family=Instrument+Serif:ital@0;1"
    "&family=Inter:wght@400;500;600;700"
    "&display=swap"
)
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"  wrote {path.relative_to(ROOT)} ({len(data)} bytes)")


def fetch_fonts() -> None:
    print("Fetching Google Fonts…")
    css = fetch(GOOGLE_FONTS_CSS).decode("utf-8")
    urls = sorted(set(re.findall(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", css)))
    local_css: list[str] = []
    for block in re.split(r"(?=@font-face)", css):
        if not block.strip():
            continue
        out = block
        for url in re.findall(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", block):
            name = url.rsplit("/", 1)[-1].split("?")[0]
            rel = f"woff2/{name}"
            write(FONTS_DIR / rel, fetch(url))
            out = out.replace(f"url({url})", f"url(/static/fonts/{rel})")
        local_css.append(out.strip())
    css_path = FONTS_DIR / "mnemos-fonts.css"
    css_path.write_text("\n\n".join(local_css) + "\n", encoding="utf-8")
    print(f"  wrote {css_path.relative_to(ROOT)}")


def fetch_katex() -> None:
    print(f"Fetching KaTeX {KATEX_VERSION}…")
    for name in ("katex.min.css", "katex.min.js"):
        data = fetch(f"{KATEX_BASE}/{name}")
        write(KATEX_DIR / name, data)
    write(KATEX_DIR / "auto-render.min.js", fetch(f"{KATEX_BASE}/contrib/auto-render.min.js"))
    css = (KATEX_DIR / "katex.min.css").read_text(encoding="utf-8")
    for url in sorted(set(re.findall(r"url\((fonts/[^)]+)\)", css))):
        rel = url.replace("fonts/", "")
        write(KATEX_DIR / "fonts" / rel, fetch(f"{KATEX_BASE}/{url}"))
    fixed = re.sub(
        r"url\(fonts/",
        "url(/static/katex/fonts/",
        css,
    )
    (KATEX_DIR / "katex.min.css").write_text(fixed, encoding="utf-8")
    print(f"  patched {KATEX_DIR / 'katex.min.css'}")


def main() -> int:
    fetch_fonts()
    fetch_katex()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
