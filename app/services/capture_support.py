"""Which capture sources this machine can actually run (WS-F).

The Console used to offer every capture toggle on every OS. On macOS and Linux
that means a tester ticks "Screen", gets an HTTP 503 from `_resume_source`, and
is left to guess whether they did something wrong, the build is broken, or the
feature is simply Windows-only. The 503 is the correct backstop; it is not an
honest answer to the question the checkbox asked.

This is the answer: a pure, platform-parameterised map of what each source can
do here, surfaced in ``GET /capture/status`` so the Privacy sheet can disable
what cannot work and say why, before anything is clicked.

Three states, deliberately distinct:

``available``   the source works on this OS.
``setup``       the OS can do it, but this machine needs something installed
                first (macOS system audio needs a loopback device). Offered,
                with the requirement stated.
``unsupported`` not implemented for this OS. Shown disabled with the reason,
                never silently hidden — a tester comparing notes with a Windows
                colleague should be able to see that the difference is expected.

Nothing here enforces anything: consent gating and the 503 remain the security
and correctness boundary. This exists so the UI stops lying.
"""
from __future__ import annotations

import sys
from typing import Any

# Sources the Privacy sheet can offer, in its display order.
SOURCES = ("mic", "webcam", "screen", "clicks", "system_audio", "save_audio")

AVAILABLE = "available"
SETUP = "setup"
UNSUPPORTED = "unsupported"

# macOS desktop capture stays Windows/Linux-only — docs/macos-meeting.md scopes
# the Mac build to the meeting path. Linux uses mss + pynput under X11
# (see desktop_capture.start() and desktop_agent/x11_util.py).
_DESKTOP_ONLY_ON_WINDOWS = (
    "Screen capture is Windows/Linux-only in this build. Meetings, memory, "
    "search and the Console all work here.")
_CLICKS_ONLY_ON_WINDOWS = (
    "Mouse-click capture is Windows/Linux-only in this build.")
_MAC_SYSTEM_AUDIO = (
    "Needs a loopback device to hear the other side of a call. Install "
    "BlackHole, set it as the output (or a Multi-Output Device with your "
    "speakers), then point QUILL_SYSTEM_AUDIO_DEVICE at it. Without it your "
    "mic still records the meeting — remote voices are just quieter.")
_LINUX_SYSTEM_AUDIO = (
    "Needs a PulseAudio/PipeWire monitor source. Set QUILL_SYSTEM_AUDIO_DEVICE "
    "to the monitor of your output device.")
_LINUX_DESKTOP_NOTE = (
    "Needs an X11 session (mss + pynput). Wayland screen/click capture is "
    "not supported yet.")
# Hosted (QUILL_HEADLESS=1): the server has no devices at all — the user's
# BROWSER is the capture surface. Mic and meeting-tab audio arrive over
# WS /ingest/audio from the /capture page; everything device-shaped is off.
_HOSTED_MIC = (
    "Captured from your browser: open the Capture page and start the mic "
    "there.")
_HOSTED_TAB = (
    "Your meeting tab's audio, shared from the Capture page (“Share "
    "meeting tab” + “Also share tab audio”). Desktop browsers "
    "only — phones can't share tab audio.")
_HOSTED_SCREEN = (
    "Share a tab, window, or screen from the Capture page — the browser "
    "shows a sharing indicator the whole time. Desktop browsers only.")
_HOSTED_NO_DEVICE = (
    "This hosted instance runs on a server with no local devices — "
    "capture happens in your browser, which offers mic, meeting-tab "
    "audio, and screen sharing only.")


def _norm(platform: str | None) -> str:
    plat = (platform if platform is not None else sys.platform).lower()
    if plat.startswith("win"):
        return "windows"
    if plat == "darwin":
        return "macos"
    return "linux"


def _headless_env() -> bool:
    import os
    return os.environ.get("QUILL_HEADLESS", "") in ("1", "true", "True")


def support(platform: str | None = None,
            headless: bool | None = None) -> dict[str, dict[str, Any]]:
    """Per-source capability for this OS. Pure — pass `platform`/`headless`
    to test; `headless` defaults to QUILL_HEADLESS."""
    os_name = _norm(platform)
    out: dict[str, dict[str, Any]] = {}

    def put(key: str, state: str, reason: str = "") -> None:
        out[key] = {"state": state, "available": state != UNSUPPORTED,
                    "reason": reason}

    if _headless_env() if headless is None else headless:
        # Server OS is irrelevant here: the browser is the capture surface.
        put("mic", AVAILABLE, _HOSTED_MIC)
        put("system_audio", AVAILABLE, _HOSTED_TAB)
        put("save_audio", AVAILABLE)
        put("screen", AVAILABLE, _HOSTED_SCREEN)
        put("webcam", UNSUPPORTED, _HOSTED_NO_DEVICE)
        put("clicks", UNSUPPORTED, _HOSTED_NO_DEVICE)
        return out

    # Mic and camera are portable: sounddevice/PortAudio and OpenCV both work
    # on all three. macOS gates them behind a TCC permission prompt, which is
    # the OS asking the user, not an incapability.
    put("mic", AVAILABLE)
    put("webcam", AVAILABLE)
    put("save_audio", AVAILABLE)

    if os_name == "windows":
        put("screen", AVAILABLE)
        put("clicks", AVAILABLE)
        put("system_audio", AVAILABLE)
    elif os_name == "linux":
        # Offered; start() still fails soft if DISPLAY/X11 is missing.
        put("screen", AVAILABLE, _LINUX_DESKTOP_NOTE)
        put("clicks", AVAILABLE, _LINUX_DESKTOP_NOTE)
        put("system_audio", SETUP, _LINUX_SYSTEM_AUDIO)
    else:
        put("screen", UNSUPPORTED, _DESKTOP_ONLY_ON_WINDOWS)
        put("clicks", UNSUPPORTED, _CLICKS_ONLY_ON_WINDOWS)
        put("system_audio", SETUP, _MAC_SYSTEM_AUDIO)
    return out


def status(platform: str | None = None,
           headless: bool | None = None) -> dict[str, Any]:
    """What ``GET /capture/status`` embeds under ``"support"``."""
    os_name = _norm(platform)
    hosted = _headless_env() if headless is None else headless
    per_source = support(platform, headless=hosted)
    if hosted:
        note = ("Hosted instance: capture happens in your browser on the "
                "Capture page — mic, meeting-tab audio, and screen sharing.")
    elif os_name == "windows":
        note = ""
    elif os_name == "linux":
        note = ("Screen and mouse-click capture work under X11. "
                "Wayland is not supported yet.")
    else:
        note = ("This build captures meetings, not your screen. "
                "Screen and mouse-click capture are Windows/Linux-only.")
    return {
        "os": os_name,
        "sources": per_source,
        "unsupported": sorted(k for k, v in per_source.items()
                              if v["state"] == UNSUPPORTED),
        "needs_setup": sorted(k for k, v in per_source.items()
                              if v["state"] == SETUP),
        # A one-line summary the Console can show without walking the map.
        "note": note,
    }


def is_available(source: str, platform: str | None = None) -> bool:
    return bool(support(platform).get(source, {}).get("available", True))
