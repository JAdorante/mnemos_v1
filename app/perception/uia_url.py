"""Committed browser URL via UI Automation (WS2d, Windows only).

`l0_meta` has always emitted `browser_url: None` because a wrong URL is worse
than no URL: it binds attribution to the wrong project silently and survives
rebuilds. This module is the guarded read that earns the field back.

What it reads: the browser's DOCUMENT element ValuePattern — the COMMITTED
url. Reading the address-bar `Edit` element instead is the known trap; that
returns whatever the user has typed, which is a search query, not a location,
and belongs to a different privacy class.

Design constraints this file exists to satisfy:

  * **L0 must not block.** The 1 Hz poll thread is the liveness clock for gap
    detection — a blocking cross-process COM call there manufactures false
    `sleep` gaps. So `current_url()` NEVER waits: it answers from cache and
    schedules a refresh on a dedicated STA worker. A navigation therefore
    lands on a later tick (typically the next one, and the 500 ms L0 debounce
    usually hides it entirely), which is the right trade — a URL that is one
    second late is honest; a stalled liveness clock is not.
  * **Every failure resolves to None.** Timeout, wrong shape, focus moved,
    omnibox focused, agent-driven, unknown app: all return None and increment
    a counter. Nothing here guesses.
  * **Roughly one COM call per navigation.** The cache key is
    (hwnd, window_title), and browser titles change on tab switch and on
    navigation. Known gap: same-title SPA navigation is missed, which is
    harmless while only the registrable domain is consumed (same-domain SPA
    navigation has the same domain) and is why domain-only is the default.

Counters are per app_name so the exit gate can be read per browser — a 99%
aggregate can hide one browser at 80%.
"""
from __future__ import annotations

import os
import queue
import threading
import time

from app.perception import psl

# Only these get a read attempted. An arbitrary app's Document element is not
# a browser location and must never be treated as one.
ALLOWED_APPS = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "arc.exe",
})

_CACHE_MAX = 64
_QUEUE_MAX = 8

_lock = threading.Lock()
_cache: "dict[tuple[int, str], tuple[str | None, float]]" = {}
_inflight: set = set()
_stats: dict[str, dict[str, int]] = {}
_latency_ms: dict[str, list[float]] = {}

_worker: threading.Thread | None = None
_requests: "queue.Queue[tuple]" = queue.Queue(maxsize=_QUEUE_MAX)

# Replaceable seam: the raw COM read. Tests swap this; production leaves it.
_reader = None


# ------------------------------- settings ---------------------------------
def enabled() -> bool:
    """Env-first at call time (the codebase's runtime-knob convention)."""
    env = os.getenv("QUILL_PERCEPTION_URL")
    if env is not None:
        return env not in ("0", "false", "False", "")
    try:
        from app.config import settings
        return bool(settings.perception.url_enabled)
    except Exception:
        return False


def store_full_url() -> bool:
    env = os.getenv("QUILL_PERCEPTION_URL_FULL")
    if env is not None:
        return env not in ("0", "false", "False", "")
    try:
        from app.config import settings
        return bool(settings.perception.url_full)
    except Exception:
        return False


def _timeout_ms() -> int:
    try:
        from app.config import settings
        return int(settings.perception.url_timeout_ms)
    except Exception:
        return 500


# ------------------------------- counters ---------------------------------
_COUNTERS = ("url_attempts", "url_ok", "url_timeout", "url_rejected_shape",
             "url_rejected_focus", "url_suppressed_omnibox",
             "url_suppressed_agent", "url_unavailable")


def _bump(app: str, name: str, n: int = 1) -> None:
    key = (app or "unknown").lower()
    with _lock:
        row = _stats.setdefault(key, {c: 0 for c in _COUNTERS})
        row[name] = row.get(name, 0) + n


def _record_latency(app: str, ms: float) -> None:
    key = (app or "unknown").lower()
    with _lock:
        samples = _latency_ms.setdefault(key, [])
        samples.append(float(ms))
        if len(samples) > 500:
            del samples[: len(samples) - 500]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[idx], 1)


def stats() -> dict:
    """Per-browser counters + read latency, for GET /perception/status."""
    with _lock:
        out = {}
        for app, row in _stats.items():
            lat = list(_latency_ms.get(app) or ())
            out[app] = dict(row)
            out[app]["latency_p50_ms"] = _pct(lat, 0.50)
            out[app]["latency_p95_ms"] = _pct(lat, 0.95)
        return {"enabled": enabled(), "full_url": store_full_url(),
                "psl_version": psl.version(), "by_app": out,
                "cached": len(_cache)}


def reset_stats() -> None:
    with _lock:
        _stats.clear()
        _latency_ms.clear()


def reset_cache() -> None:
    with _lock:
        _cache.clear()
        _inflight.clear()


# -------------------------------- guards ----------------------------------
def _is_browser(app_name: str) -> bool:
    return (app_name or "").strip().lower() in ALLOWED_APPS


def _agent_active() -> bool:
    try:
        from app.services import agent_activity
        return agent_activity.browser_run_active()
    except Exception:
        return False


def valid_url(url: str | None) -> str | None:
    """Shape validation: an absolute http(s) URL whose host parses to a real
    registrable domain, else None. This is the only place a raw UIA string
    becomes something the rest of the system may believe."""
    s = (url or "").strip()
    if not s or len(s) > 2048:
        return None
    low = s.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return None
    if any(c in s for c in ("\n", "\r", "\t", " ")):
        return None
    if not psl.registrable_domain(s):
        return None
    return s


def strip_path_secrets(url: str) -> str:
    """Query and fragment are where signed URLs, magic links, reset tokens and
    session ids live. They never reach storage, on any flag."""
    return (url or "").split("?", 1)[0].split("#", 1)[0]


# ---------------------------- the COM read --------------------------------
_uia_module = None


def _uia() -> object | None:
    """The generated UIAutomationClient wrapper, or None.

    `comtypes.gen.UIAutomationClient` does not exist until GetModule() has
    generated it from the typelib at least once — on a clean Windows box the
    bare import fails, which would make every read return url_unavailable
    forever. Generate on first use and cache the module.
    """
    global _uia_module
    if _uia_module is not None:
        return _uia_module
    try:
        import comtypes.client  # type: ignore
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA  # type: ignore
        _uia_module = UIA
        return UIA
    except Exception as exc:
        print(f"[perception.uia_url] UIAutomation unavailable ({exc}).")
        return None


def _uia_read_url(hwnd: int) -> tuple[str | None, str | None]:
    """(url, rejection) — the raw Windows read. `rejection` is a counter name
    when the read was deliberately declined. Never raises."""
    if os.name != "nt":
        return None, "url_unavailable"
    UIA = _uia()
    if UIA is None:
        return None, "url_unavailable"
    try:
        import ctypes

        import comtypes.client  # type: ignore
        user32 = ctypes.windll.user32
        before = int(user32.GetForegroundWindow() or 0)
        if before != int(hwnd):
            return None, "url_rejected_focus"

        auto = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=UIA.IUIAutomation)

        # Omnibox suppression: a focused address-bar Edit means the string we
        # would read is user-typed text, not a location.
        try:
            focused = auto.GetFocusedElement()
            if focused is not None and int(
                    focused.CurrentControlType) == UIA.UIA_EditControlTypeId:
                return None, "url_suppressed_omnibox"
        except Exception:
            pass

        element = auto.ElementFromHandle(int(hwnd))
        if element is None:
            return None, "url_rejected_shape"
        cond = auto.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId, UIA.UIA_DocumentControlTypeId)
        doc = element.FindFirst(UIA.TreeScope_Descendants, cond)
        if doc is None:
            return None, "url_rejected_shape"
        value = doc.GetCurrentPattern(UIA.UIA_ValuePatternId)
        if value is None:
            return None, "url_rejected_shape"
        raw = value.QueryInterface(UIA.IUIAutomationValuePattern).CurrentValue

        after = int(user32.GetForegroundWindow() or 0)
        if after != int(hwnd):
            # The user moved on mid-read; whatever we hold may belong to the
            # window we just left.
            return None, "url_rejected_focus"
        return (str(raw) if raw else None), None
    except Exception:
        return None, "url_unavailable"


def _do_read(hwnd: int) -> tuple[str | None, str | None]:
    fn = _reader or _uia_read_url
    try:
        return fn(int(hwnd))
    except Exception:
        return None, "url_unavailable"


# ----------------------------- worker thread ------------------------------
def _worker_loop() -> None:
    """Dedicated STA thread. COM is initialized once here so no capture-path
    thread ever pays apartment setup, and a wedged call wedges only this."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # APARTMENTTHREADED
        except Exception:
            pass
    while True:
        try:
            item = _requests.get()
        except Exception:
            return
        if item is None:
            return
        hwnd, title, app = item
        started = time.time()
        url, rejection = _do_read(hwnd)
        _record_latency(app, (time.time() - started) * 1000.0)
        if rejection:
            _bump(app, rejection)
            url = None
        else:
            url = valid_url(url)
            if url is None:
                _bump(app, "url_rejected_shape")
            else:
                _bump(app, "url_ok")
        with _lock:
            _inflight.discard((int(hwnd), title))
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()          # bounded, and a browser refills it fast
            _cache[(int(hwnd), title)] = (url, time.time())


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, daemon=True,
                                   name="perception-uia-url")
        _worker.start()


# ------------------------------- public API -------------------------------
def read_url(hwnd: int, timeout_ms: int | None = None) -> str | None:
    """Blocking, validated read with a hard timeout. Used by the harness and
    by tests — NOT by the L0 tick, which must never wait (see current_url)."""
    if not enabled():
        return None
    done = threading.Event()
    box: dict = {}

    def _run() -> None:
        try:
            box["result"] = _do_read(hwnd)
        finally:
            done.set()

    t = threading.Thread(target=_run, daemon=True, name="perception-uia-once")
    t.start()
    if not done.wait(max(0.01, (timeout_ms or _timeout_ms()) / 1000.0)):
        return None                    # never extend the deadline
    url, rejection = box.get("result") or (None, "url_unavailable")
    return None if rejection else valid_url(url)


def current_url(hwnd: int | str | None, title: str,
                app_name: str) -> str | None:
    """Cache-backed, NON-BLOCKING committed URL for a foreground window.

    Returns the cached value for (hwnd, title) and schedules a background read
    when that key is new. A cache miss answers None — honest, and one tick
    later the value is there.
    """
    if not enabled() or not _is_browser(app_name):
        return None
    try:
        h = int(hwnd)
    except (TypeError, ValueError):
        return None
    if not h:
        return None
    t = title or ""
    app = (app_name or "").strip().lower()

    if _agent_active():
        _bump(app, "url_suppressed_agent")
        return None
    try:
        from app.services.surface_filters import is_self_window
        if is_self_window(t):
            return None
    except Exception:
        pass

    key = (h, t)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            return hit[0]
        if key in _inflight:
            return None
        _inflight.add(key)
    _bump(app, "url_attempts")
    _ensure_worker()
    try:
        _requests.put_nowait((h, t, app))
    except queue.Full:
        with _lock:
            _inflight.discard(key)
        _bump(app, "url_timeout")       # worker is behind; drop, never queue up
    return None


def current_domain(hwnd: int | str | None, title: str,
                   app_name: str) -> str | None:
    """Registrable domain for a foreground window, or None."""
    return psl.registrable_domain(current_url(hwnd, title, app_name) or "")


def storable_url(url: str | None) -> str | None:
    """What may be persisted as `browser_url`. None unless the full-URL flag
    is on; query and fragment stripped either way."""
    if not url or not store_full_url():
        return None
    trimmed = strip_path_secrets(url)
    try:
        from app.perception.redaction import TIER_SECRETS, redact_text
        trimmed, _hits = redact_text(trimmed, TIER_SECRETS)
    except Exception:
        pass
    return trimmed or None
