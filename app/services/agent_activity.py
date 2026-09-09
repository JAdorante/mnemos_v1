"""Process-local "the agent is driving a browser right now" flag (WS2d).

`browser_agent` has two driver modes and only one of them is distinguishable
from the user's own browsing by process:

  * launch mode (`launch_persistent_context(channel="chrome")`) — a separate
    browser process with our own user_data_dir. A PID/profile discriminator
    exists.
  * attach mode (`connect_over_cdp`) — drives the USER'S running Chrome.
    Same process, same profile, same HWND as real browsing. There is no
    process-level discriminator at all, so suppression has to be temporal.

Hence this module: the orchestrator marks a browser run active, and the
perception side (uia_url) declines to read a URL while it is. Living here
rather than in `browser_agent` keeps `app/perception` from importing the
agent package.

The flag is deliberately TTL'd. A crashed run that never reaches
`Orchestrator.close()` would otherwise suppress URL reads for the rest of the
process lifetime — silent, permanent, and invisible. Expiring costs precision
nothing (a stale-expired flag only means we resume honest reads) and costs
recall only for a run that outlives the TTL without a heartbeat.
"""
from __future__ import annotations

import os
import threading
import time

_DEFAULT_TTL_S = 900.0

_lock = threading.Lock()
_active_until: float = 0.0
_active: bool = False


def _ttl_s() -> float:
    """Env-first at call time (the `memory.vector_gc` convention) so a tester
    can widen it without a restart."""
    try:
        return max(1.0, float(os.getenv("QUILL_AGENT_RUN_TTL_S",
                                        _DEFAULT_TTL_S)))
    except (TypeError, ValueError):
        return _DEFAULT_TTL_S


def set_browser_run(active: bool, *, now: float | None = None) -> None:
    """Mark an agent-driven browser run started (True) or finished (False)."""
    global _active, _active_until
    t = time.time() if now is None else now
    with _lock:
        _active = bool(active)
        _active_until = (t + _ttl_s()) if active else 0.0


def heartbeat(now: float | None = None) -> None:
    """Extend an active run's TTL. A long run may call this; not required."""
    global _active_until
    t = time.time() if now is None else now
    with _lock:
        if _active:
            _active_until = t + _ttl_s()


def browser_run_active(now: float | None = None) -> bool:
    """True while an agent browser run is known to be in flight."""
    global _active, _active_until
    t = time.time() if now is None else now
    with _lock:
        if not _active:
            return False
        if t >= _active_until:
            _active = False
            _active_until = 0.0
            print("[agent_activity] browser-run flag expired (TTL) — "
                  "resuming URL reads.")
            return False
        return True


def status() -> dict:
    with _lock:
        return {"browser_run_active": bool(_active),
                "expires_in_s": (round(max(0.0, _active_until - time.time()), 1)
                                 if _active else None)}


def reset() -> None:
    """Test hook."""
    global _active, _active_until
    with _lock:
        _active, _active_until = False, 0.0
