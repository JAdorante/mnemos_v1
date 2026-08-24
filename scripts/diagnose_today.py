#!/usr/bin/env python3
"""Diagnose the /today "Couldn't reach Mnemos" banner.

Run it while `python run_all.py` is up, then paste the output:

  python scripts/diagnose_today.py
"""
from __future__ import annotations

import argparse
import collections
import time
import urllib.error
import urllib.request


def _get(url: str, timeout: float = 30.0):
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:600], time.time() - t0
    except Exception as e:  # connection refused, timeout, DNS...
        return f"{type(e).__name__}: {e}", b"", time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--count", type=int, default=40)
    args = ap.parse_args()

    print(f"base = {args.base}\n")

    # 1. Is the page the browser gets actually the current build?
    status, body, _ = _get(f"{args.base}/today")
    html = body.decode("utf-8", "replace")
    print(f"GET /today -> {status}")
    if isinstance(status, int) and status == 200:
        print(f"  fresh client JS (shellErrMsg present): {'shellErrMsg' in html}")
        print(f"  real-retry logic (_shellFails present): {'_shellFails' in html}")
    print()

    # 2. Does the endpoint the banner watches ever fail?
    codes: collections.Counter = collections.Counter()
    slow, fails = 0.0, []
    for i in range(args.count):
        status, body, dt = _get(f"{args.base}/today/state?limit=28")
        codes[status] += 1
        slow = max(slow, dt)
        if status != 200 and len(fails) < 3:
            fails.append((status, body[:400].decode("utf-8", "replace")))
        time.sleep(0.5)

    print(f"GET /today/state x{args.count} -> {dict(codes)}")
    print(f"  slowest: {slow:.2f}s")
    for status, body in fails:
        print(f"  FAIL {status}: {body}")
    print()

    if set(codes) == {200}:
        print("Server is healthy. The failure is in the browser:")
        print("  - hard-reload (Ctrl+Shift+R); the banner then names the reason")
        print("  - if 'fresh client JS' was False above, you are on cached JS")
    else:
        print("Server-side failure reproduced above — paste this output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
