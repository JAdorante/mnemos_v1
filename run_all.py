"""Launch the whole Mnemos system with one command.

    python run_all.py

Starts the whole Mnemos system:
  * the Memory Engine (SQLite + semantic index),
  * the FastAPI server + API/docs at http://127.0.0.1:8000/docs,
  * the Exec.AI browser agent web UI at http://127.0.0.1:5000

Capture (mic / webcam / screen / system-audio) stays OFF until the user
opts in via the in-UI Privacy controls (see capture_consent). After consent,
those pipelines run in the FastAPI process. The browser agent runs as a
child process (sync Playwright needs its own process, isolated from the async
server). Ctrl+C stops everything.

Flags:
    --no-audio      force-disable mic even if previously consented
    --no-vision     force-disable webcam even if previously consented
    --no-notifications  don't capture Phone Link / Windows notifications
    --desktop-capture   request screen observation (still needs UI consent)
    --system-audio      request loopback transcription (still needs UI consent)
    --no-browser    don't start the Exec.AI browser agent
    --browser-headless   hide the agent's browser window
    --port 8000     API server port
    --browser-port 5000  browser-agent UI port
    --host 127.0.0.1
"""
from __future__ import annotations

import argparse
import atexit
import os
import subprocess
import sys

# Load .env BEFORE argparse reads defaults: QUILL_HOST/QUILL_PORT set there must
# shape the actual uvicorn binding, not just app.config's view of it (they
# disagreed once: page said LAN-open, socket was still 127.0.0.1).
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass


def _kill_tree(proc: subprocess.Popen) -> None:
    """Terminate a child process and everything it spawned (e.g. Chromium)."""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        # taskkill /T kills the whole tree — the Flask worker AND the browser it launched.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _start_browser_agent(port: int, headless: bool) -> subprocess.Popen | None:
    """Launch the Exec.AI browser agent web UI as a child process."""
    cmd = [sys.executable, "exec_webapp.py", "--port", str(port)]
    if headless:
        cmd.append("--headless")
    # Ghost mode ('hidden') is the chat agent's default, but the Exec.AI child
    # has no reveal endpoint — keep its window visible unless the user opted
    # the whole system in via QUILL_GHOST_BROWSER.
    env = dict(os.environ)
    env.setdefault("QUILL_GHOST_BROWSER", "off")
    try:
        return subprocess.Popen(cmd, env=env,
                                cwd=os.path.dirname(os.path.abspath(__file__)))
    except Exception as exc:
        print(f"[launch] browser agent failed to start: {exc}")
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Launch all of Mnemos at once")
    ap.add_argument("--no-audio", action="store_true",
                    help="force-disable mic (overrides prior consent for this run)")
    ap.add_argument("--no-vision", action="store_true",
                    help="force-disable webcam (overrides prior consent for this run)")
    ap.add_argument("--no-notifications", action="store_true",
                    help="don't capture Phone Link / Windows notifications")
    ap.add_argument("--desktop-capture", action="store_true",
                    help="request screen observation (UI consent still required)")
    ap.add_argument("--system-audio", action="store_true",
                    help="request loopback transcription (UI consent still required)")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't start the Exec.AI browser agent")
    ap.add_argument("--browser-headless", action="store_true",
                    help="hide the agent's browser window")
    ap.add_argument("--host", default=os.environ.get("QUILL_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("QUILL_PORT", "8000")))
    ap.add_argument("--browser-port", type=int,
                    default=int(os.environ.get("QUILL_BROWSER_PORT", "5000")))
    args = ap.parse_args()

    os.environ["QUILL_AUTOSTART"] = "1"
    # Capture pipelines stay off until capture_consent allows them. These flags
    # only force sources OFF for this process (or request capability in env).
    if args.no_audio:
        os.environ["QUILL_AUTOSTART_AUDIO"] = "0"
    if args.no_vision:
        os.environ["QUILL_AUTOSTART_VISION"] = "0"
    if args.no_notifications:
        os.environ["QUILL_AUTOSTART_NOTIFICATIONS"] = "0"
    if args.desktop_capture:
        os.environ["QUILL_DESKTOP_CAPTURE"] = "1"
    if args.system_audio:
        os.environ["QUILL_SYSTEM_AUDIO"] = "1"

    browser_proc = None
    if not args.no_browser:
        browser_proc = _start_browser_agent(args.browser_port, args.browser_headless)
        # Ensure the child (and its Chromium) dies with us, however we exit.
        atexit.register(_kill_tree, browser_proc)

    print("=" * 62)
    print("  Mnemos — launching everything")
    print(f"  API + docs:      http://{args.host}:{args.port}/docs")
    if browser_proc:
        print(f"  Browser agent:   http://127.0.0.1:{args.browser_port}")
    print("  Capture is off until Privacy consent in the UI.")
    print("  Ctrl+C to stop.")
    print("=" * 62)

    import uvicorn

    try:
        uvicorn.run("app.main:app", host=args.host, port=args.port,
                    log_level="warning", reload=False)
    finally:
        if browser_proc and browser_proc.poll() is None:
            print("\n[launch] stopping browser agent ...")
            _kill_tree(browser_proc)


if __name__ == "__main__":
    main()
