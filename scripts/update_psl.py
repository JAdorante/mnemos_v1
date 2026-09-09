#!/usr/bin/env python3
"""Refresh the vendored public suffix list (WS2d). DEVELOPER COMMAND ONLY.

Nothing in app/perception calls this. Constraint 2 of the browser-URL brief is
no network in the capture path, so the list is vendored and pinned and only a
human refreshes it:

    python3 scripts/update_psl.py

Review the diff before committing — a bad list silently rewrites every
registrable domain the privacy gate and activity segmentation key on.
"""
from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

URL = "https://publicsuffix.org/list/public_suffix_list.dat"
DEST = Path(__file__).resolve().parents[1] / "app/perception/data"


def main() -> int:
    dat = DEST / "public_suffix_list.dat"
    try:
        with urllib.request.urlopen(URL, timeout=30) as r:
            body = r.read().decode("utf-8")
    except Exception as exc:
        print(f"fetch failed: {exc}")
        return 1
    rules = [l for l in body.splitlines()
             if l.strip() and not l.strip().startswith("//")]
    if len(rules) < 5000 or "===BEGIN ICANN DOMAINS===" not in body:
        print(f"refusing to write: list looks wrong ({len(rules)} rules)")
        return 1
    dat.write_text(body, encoding="utf-8")
    stamp = time.strftime("%Y-%m-%d")
    (DEST / "public_suffix_list.VERSION").write_text(
        f"source: {URL}\n"
        f"version: fetched {stamp} ({len(rules)} rules)\n"
        f"vendored: {stamp}\n"
        "license: MPL-2.0 (header retained in the .dat)\n"
        "refresh: python3 scripts/update_psl.py   # dev-time only, "
        "never at capture time\n", encoding="utf-8")
    print(f"wrote {len(rules)} rules to {dat}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
