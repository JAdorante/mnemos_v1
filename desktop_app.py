"""Mnemos as a desktop app — the packaged build's entry point.

`run_all.py` is the developer/scripted-install launcher: a console, uvicorn in
the foreground, and "now open a browser tab". That is the wrong first
impression for the pilot cohort, and the console window alone reads as
unfinished software to a non-technical tester.

This is the same server with a shell around it:

* **A window, not a tab.** `pywebview` hosts the existing UI in a native window
  — Edge WebView2 on Windows (present on every Windows 11 machine), WKWebView
  on macOS. Nothing about the pages changes; they are the same local HTTP
  surface, so there is no second UI to maintain.
* **A tray icon** with Open, *Stop capture*, and Quit. Stopping capture from
  outside the browser matters: the one control a tester must be able to reach
  in a hurry should not require finding the right tab first.
* **Per-user data.** A packaged install sits in Program Files and cannot write
  there, so `app.runtime` relocates the data directory before `app.config` is
  imported and freezes it.

Deliberately **not** started here, unlike `run_all.py`: the browser agent and
the Org Coordinator. Both are launched as `[sys.executable, "some_script.py"]`
children, which in a frozen build re-executes `Mnemos.exe` instead of running
the script. Rather than half-fix that, the desktop build ships the memory
product — capture, memory, people, chat, review — and leaves the agents to the
scripted install until they are packaged deliberately.

    python desktop_app.py                 # window + tray
    python desktop_app.py --no-window     # headless; what CI smoke-tests
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen

from app.runtime import apply_env_defaults, bundle_root, env_file, is_frozen

# Must happen before anything imports app.config: its dataclasses freeze at
# import, so a later QUILL_DATA_DIR is silently ignored.
_APPLIED = apply_env_defaults()

WINDOW_TITLE = "Mnemos"
HEALTH_TIMEOUT_S = 180.0


def _load_env() -> None:
    """Load the tester's .env from wherever this build keeps it."""
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    path = env_file()
    if path.is_file():
        load_dotenv(path, override=False)


def _serve(host: str, port: int, log_level: str) -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=host, port=port, log_level=log_level,
                access_log=(log_level in ("info", "debug")))


def wait_for_health(url: str, timeout_s: float = HEALTH_TIMEOUT_S) -> bool:
    """Block until the server answers. First boot builds indexes and is slow."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urlopen(f"{url}/health", timeout=3) as resp:
                if 200 <= int(getattr(resp, "status", 200)) < 500:
                    return True
        except (URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _icon_image():
    """The tray image, from the same logo the web UI uses."""
    try:
        from PIL import Image
        return Image.open(bundle_root() / "app" / "static" / "mnemos-logo.png")
    except Exception as exc:
        print(f"[desktop] tray icon image unavailable ({exc}).")
        return None


def _stop_capture() -> str:
    """Tray 'Stop capture' — the same call the recording bar's button makes."""
    try:
        from app.services import wipe
        out = wipe.stop_capture()
        return "Capture stopped." if out.get("ok") else "Capture stop had errors."
    except Exception as exc:
        return f"Could not stop capture: {exc}"


def start_tray(url: str, on_quit) -> "threading.Thread | None":
    """Run the tray icon on its own thread. Optional: absence is not fatal."""
    try:
        import pystray
    except Exception as exc:
        print(f"[desktop] tray unavailable ({exc}); window only.")
        return None

    import webbrowser

    def _open(_icon=None, _item=None) -> None:
        webbrowser.open(url)

    def _stop(_icon=None, _item=None) -> None:
        print(f"[desktop] {_stop_capture()}")

    def _quit(icon=None, _item=None) -> None:
        try:
            if icon is not None:
                icon.stop()
        finally:
            on_quit()

    image = _icon_image()
    menu = pystray.Menu(
        pystray.MenuItem("Open Mnemos", _open, default=True),
        pystray.MenuItem("Stop capture", _stop),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )
    icon = pystray.Icon("mnemos", image, WINDOW_TITLE, menu)
    t = threading.Thread(target=icon.run, name="tray", daemon=True)
    t.start()
    return t


def open_window(url: str) -> bool:
    """Native window via pywebview. Returns False if we had to fall back."""
    try:
        import webview
    except Exception as exc:
        print(f"[desktop] pywebview unavailable ({exc}); opening your browser.")
        import webbrowser
        webbrowser.open(url)
        return False
    webview.create_window(WINDOW_TITLE, url, width=1180, height=820,
                          min_size=(900, 600))
    # Blocks on the main thread until the window closes — which is the app's
    # lifetime, so everything else has to be a daemon thread.
    webview.start()
    return True


# The imports a working build must be able to make. `silero_vad` is the one
# that matters most and the one a spec is most likely to lose: it pulls torch at
# import time even when loading the ONNX model, so a build that excludes torch
# has no VAD — which means no utterances, no memory, and no error that says so.
CRITICAL_IMPORTS = (
    ("torch", "tensor runtime (VAD, speaker ID, embeddings)"),
    ("silero_vad", "voice-activity detection — no VAD means nothing is heard"),
    ("faster_whisper", "speech recognition"),
    ("sentence_transformers", "semantic search index"),
    ("speechbrain", "speaker identification"),
    ("sounddevice", "microphone input"),
    ("lancedb", "vector store"),
)


# The shell. Missing these is not fatal — desktop_app falls back to the default
# browser and no tray — but on the packaged build that fallback *is* the bug:
# the whole point is not being a browser tab. Reported, never fatal, because a
# working-but-plain app beats refusing to start.
SHELL_IMPORTS = (
    ("webview", "native window (Edge WebView2 on Windows)"),
    ("pystray", "tray icon: Open / Stop capture / Quit"),
)


def self_test() -> int:
    """Prove this build can load what it needs. Exit 0 when it can.

    Separate from "are the weights cached": a missing download is a first-run
    state a tester can fix, while a missing module is a broken build nobody can.
    """
    failed = []
    for name, why in CRITICAL_IMPORTS:
        try:
            __import__(name)
            print(f"  ok       {name:24} {why}", flush=True)
        except Exception as exc:
            failed.append(name)
            print(f"  MISSING  {name:24} {why}\n           -> {exc}", flush=True)
    for name, why in SHELL_IMPORTS:
        try:
            __import__(name)
            print(f"  ok       {name:24} {why}", flush=True)
        except Exception as exc:
            print(f"  degraded {name:24} {why}\n           -> {exc}\n"
                  f"           the app still runs, but as a browser tab",
                  flush=True)
    try:
        from app.services.model_fetch import check
        missing = check(log=lambda m: None)
        print(f"  weights  {'all cached' if not missing else 'to download: ' + ', '.join(missing)}",
              flush=True)
    except Exception as exc:
        print(f"  weights  could not be checked: {exc}", flush=True)
    if failed:
        print(f"\nFAIL — this build cannot run: {', '.join(failed)}", flush=True)
        return 1
    print("\nPASS — every required component loaded.", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mnemos desktop app")
    ap.add_argument("--self-test", action="store_true",
                    help="check this build can load what it needs, then exit")
    ap.add_argument("--no-window", action="store_true",
                    help="serve only; no window and no tray (CI smoke test)")
    ap.add_argument("--no-tray", action="store_true", help="skip the tray icon")
    ap.add_argument("--host", default=os.environ.get("QUILL_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("QUILL_PORT", "8000")))
    ap.add_argument("--log-level", default=os.environ.get("QUILL_LOG_LEVEL", "warning"),
                    choices=["critical", "error", "warning", "info", "debug"])
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    _load_env()
    os.environ["QUILL_AUTOSTART"] = "1"
    for key, value in _APPLIED.items():
        print(f"[desktop] {key}={value}", flush=True)
    if is_frozen():
        print(f"[desktop] frozen build; bundle at {bundle_root()}", flush=True)

    url = f"http://{args.host}:{args.port}"
    server = threading.Thread(
        target=_serve, args=(args.host, args.port, args.log_level),
        name="uvicorn", daemon=True)
    server.start()

    if not wait_for_health(url):
        print("[desktop] the server did not come up; see the log above.",
              file=sys.stderr, flush=True)
        return 1
    print(f"[desktop] ready at {url}", flush=True)

    if args.no_window:
        # CI and headless hosts: stay up until killed, like run_all.py.
        try:
            while server.is_alive():
                server.join(timeout=1.0)
        except KeyboardInterrupt:
            pass
        return 0

    done = threading.Event()
    if not args.no_tray:
        start_tray(url, on_quit=done.set)

    if not open_window(url):
        # Browser fallback: the window cannot tell us when to exit, so wait for
        # the tray's Quit (or Ctrl+C) instead of exiting and killing the server.
        try:
            while not done.is_set():
                done.wait(timeout=1.0)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
