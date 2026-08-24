#!/usr/bin/env python3
"""Live smoke test for Linux ghost desktop + AT-SPI (X11 session required).

    python scripts/test_linux_desktop.py
    python scripts/test_linux_desktop.py --app gedit --keep

Launches an allowlisted editor on a jailed temp file, parks it off-screen,
scans controls via AT-SPI, sets text, captures a frame, and publishes to the
ghost relay. Cleans up the launched process unless --keep is passed.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="Linux ghost desktop smoke test")
    ap.add_argument("--app", default="gedit")
    ap.add_argument("--keep", action="store_true", help="leave the app running")
    args = ap.parse_args()

    from desktop_agent import a11y, config as cfg, ghost, x11_util
    from desktop_agent.driver import DesktopDriver

    if not x11_util.session_ok():
        print("SKIP: need X11 (DISPLAY set, XDG_SESSION_TYPE != wayland)")
        return 0
    if not a11y.available():
        print("FAIL: AT-SPI unavailable (install gir1.2-atspi-2.0 / enable a11y)")
        return 1
    if not ghost.enabled():
        print("FAIL: ghost desktop disabled (QUILL_GHOST_DESKTOP=0?)")
        return 1

    d = DesktopDriver(on_log=print, on_approve=lambda *a, **k: True)
    mk = d.make_dir("ghost_smoke")
    if not mk.get("ok"):
        print("FAIL: could not create jail dir:", mk)
        return 1
    target = Path(mk["path"]) / "ghost_smoke.txt"
    target.write_text("before\n", encoding="utf-8")
    print(f"jail file: {target}")
    print(f"ghost enabled: {ghost.enabled()}  a11y: {a11y.available()}")

    before = ghost.snapshot_windows()
    launch = d.launch_app(args.app, [str(target)])
    print("launch:", launch)
    if not launch.get("ok"):
        return 1
    time.sleep(1.0)

    scan = d.ui_scan(args.app)
    print("ui_scan ok:", scan.get("ok"), "detail head:", (scan.get("detail") or "")[:200])
    if not scan.get("ok"):
        return 1

    s = a11y.scan(args.app)
    editable_id = None
    for c in s.get("controls", []):
        if "set_text" not in c.get("patterns", []):
            continue
        if "Search" in (c.get("name") or ""):
            continue
        editable_id = c["id"]
        break
    if editable_id is None:
        print("WARN: no set_text control found; skipping ui_set_text")
    else:
        wrote = d.ui_set_text(editable_id, "Hello from Linux ghost desktop!")
        print("ui_set_text:", wrote)

    hwnd = a11y.last_window_hwnd()
    parked = ghost.parked_apps()
    print(f"window xid={hwnd} parked={hwnd in parked if hwnd else False}")
    if hwnd and hwnd in parked:
        png = ghost.window_png(hwnd)
        print(f"frame png bytes: {len(png or b'')}")
        ghost.publish_frame(hwnd)
        try:
            from browser_agent import ghost as relay
            fr = relay.latest()
            print(f"relay published: {fr is not None}")
        except Exception as exc:
            print(f"relay check skipped: {exc}")

    if not args.keep:
        # Best-effort terminate the launched app (same pid family).
        for _xid, pid, title in x11_util.client_windows():
            if args.app in x11_util.exe_for_pid(pid).lower() or args.app in (title or "").lower():
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        try:
            subprocess.run(["pkill", "-f", str(target)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
