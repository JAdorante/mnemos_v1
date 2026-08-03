"""Pixel-level desktop control — screenshot, click, type.

Used when QUILL_DESKTOP_UI=1. Coordinates match the primary monitor screenshot
(top-left origin). Mutating calls are gated by DesktopDriver's approval hook.
"""
from __future__ import annotations

import io
import os
import re
import time

from . import config as cfg

# pyautogui failsafe: fling mouse to a screen corner to abort.
_BLOCKED_KEYS = frozenset({
    "win", "winleft", "winright", "apps", "sleep", "hibernate",
})


def enabled() -> bool:
    if os.name != "nt":
        return False
    return cfg.PIXEL_UI


def _pyautogui():
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = max(0.05, cfg.PIXEL_PAUSE_S)
    return pyautogui


def screen_size() -> tuple[int, int]:
    pag = _pyautogui()
    w, h = pag.size()
    return int(w), int(h)


def screenshot_bytes() -> tuple[bytes, tuple[int, int]]:
    """Capture the primary monitor as PNG bytes and (width, height)."""
    if not enabled():
        raise RuntimeError("pixel UI disabled (QUILL_DESKTOP_UI=0)")
    try:
        import mss

        with mss.MSS() as sct:
            mon = sct.monitors[1]  # primary
            shot = sct.grab(mon)
            from PIL import Image

            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue(), shot.size
    except Exception:
        pag = _pyautogui()
        img = pag.screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), img.size


def coerce_coords(x, y) -> tuple[int, int]:
    """Best-effort (x, y) → ints, or raise ValueError.

    Tolerates the recurring malformed LLM shape where both coordinates are
    packed into one field as a string — {"x": "997, 749", "y": 749} — which
    otherwise burns a refuse/retry round-trip (screenshot + model call) on an
    action whose intent is unambiguous."""
    def _one(v):
        if isinstance(v, str):
            v = v.strip().strip("()")
        return int(round(float(v)))

    try:
        return _one(x), _one(y)
    except (TypeError, ValueError):
        pass
    for packed in (x, y):
        if isinstance(packed, str):
            parts = [p for p in re.split(r"[,\s]+", packed.strip().strip("()")) if p]
            if len(parts) == 2:
                try:
                    return _one(parts[0]), _one(parts[1])
                except (TypeError, ValueError):
                    continue
    raise ValueError(f"coordinates must be integers, got ({x!r}, {y!r})")


def check_coords(x: int, y: int) -> str | None:
    """Return an error string if coordinates are out of bounds."""
    try:
        x, y = coerce_coords(x, y)
    except (TypeError, ValueError):
        return f"coordinates must be integers, got ({x!r}, {y!r})"
    w, h = screen_size()
    if x < 0 or y < 0 or x >= w or y >= h:
        return f"coordinates ({x}, {y}) outside screen {w}x{h}"
    return None


def check_type_text(text: str) -> str | None:
    if text is None:
        return "empty text"
    if len(text) > cfg.PIXEL_MAX_TYPE_CHARS:
        return f"text too long ({len(text)} > {cfg.PIXEL_MAX_TYPE_CHARS} chars)"
    return None


def check_key(key: str) -> str | None:
    k = (key or "").strip().lower()
    if not k:
        return "empty key"
    parts = re.split(r"[+\s]+", k)
    for p in parts:
        if p in _BLOCKED_KEYS:
            return f"blocked key: {p!r}"
    if "alt" in parts and "f4" in parts:
        return "blocked key chord: alt+f4"
    return None


def click_at(x: int, y: int, button: str = "left") -> None:
    pag = _pyautogui()
    btn = (button or "left").lower()
    if btn not in ("left", "right", "middle"):
        btn = "left"
    pag.click(int(x), int(y), button=btn)
    time.sleep(cfg.PIXEL_PAUSE_S)


def type_text(text: str) -> None:
    pag = _pyautogui()
    # write() handles most characters; interval spaces keystrokes slightly.
    pag.write(text, interval=0.02)


def press_key(key: str) -> None:
    pag = _pyautogui()
    k = (key or "").strip().lower()
    if "+" in k or " " in k:
        parts = re.split(r"[+\s]+", k)
        pag.hotkey(*parts)
    else:
        pag.press(k)
