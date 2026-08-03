"""L0 — the always-on desktop metadata stream (1 Hz, append-only).

Polls the foreground window at ~1 Hz and emits a MetaEvent only on state
change (500 ms debounce) or as a 60 s heartbeat. Input is COUNTS ONLY plus an
idle flag — key/mouse contents are never read, stored, or logged. A single
writer thread batches commits every ~2 s.

Honest gaps ("machine is on" is defined here, not inferred downstream):
  * the poll loop cannot run while the machine sleeps (S3 / modern standby /
    hibernate) or the process is stopped, so a tick-to-tick wall-clock jump
    beyond `gap_threshold_s` becomes gap(reason='sleep');
  * a start() that finds the previous session's last record older than 2x the
    heartbeat writes gap(reason='process_down') spanning the hole;
  * pause() writes an open-ended gap(reason='user_pause') that resume()
    closes (a crash mid-pause is closed by the next boot's reconcile).
Lock screen is NOT a gap: the session is alive and L0 keeps honestly
recording the lock surface as the foreground state.

`browser_url` is None in Phase A (url_unavailable) — the UIA URL read has
returned wrong content on this codebase before, and a missing URL is honest
where a wrong one is poison. Phase B adds the full-URL + registrable-domain
parse with the graceful unavailable path.
"""
from __future__ import annotations

import hashlib
import threading
import time

from app.perception.redaction import TIER_SECRETS, redact_text
from app.perception.schemas import (MetaEvent, new_ulid, now_ms,
                                    utc_offset_minutes)
from app.perception.store import PerceptionStore, get_pstore


# --------------------------- Windows providers ----------------------------
def win32_provider() -> dict:
    """Foreground window metadata via Win32 (no UIA, no hooks). Returns {} on
    any failure — an empty poll is a valid 'nothing readable' state."""
    import os
    if os.name != "nt":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {}
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = (buf.value or "").strip()

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        # PROCESS_QUERY_LIMITED_INFORMATION — enough for the image name and
        # works across integrity levels where PROCESS_QUERY_INFORMATION fails.
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if h:
            try:
                size = wintypes.DWORD(1024)
                pbuf = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(h, 0, pbuf,
                                                       ctypes.byref(size)):
                    exe = pbuf.value or ""
            finally:
                kernel32.CloseHandle(h)

        sm = user32.GetSystemMetrics
        topo = f"{sm(80)}|{sm(78)}x{sm(79)}"   # SM_CMONITORS|virtual WxH
        return {
            "app_name": (exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
                         if exe else ""),
            "app_exe_path": exe,
            "window_id": str(int(hwnd)),
            "window_title": title,
            "browser_url": None,       # Phase B (url_unavailable is honest)
            "doc_path": None,
            "display_hash": hashlib.sha1(topo.encode()).hexdigest()[:12],
        }
    except Exception:
        return {}


def win32_idle_age_s() -> float | None:
    """Seconds since last user input, via GetLastInputInfo (no hooks)."""
    import os
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount()
        return ((tick - li.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


class InputCounter:
    """Key/mouse COUNTS since last read — never contents. pynput hooks when
    available; otherwise counts stay 0 and idle comes from GetLastInputInfo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys = 0
        self._clicks = 0
        self._kb = None
        self._mouse = None

    def start(self) -> None:
        try:
            from pynput import keyboard, mouse

            def on_press(_key):          # count only — the key is discarded
                with self._lock:
                    self._keys += 1

            def on_click(_x, _y, _b, pressed):
                if pressed:
                    with self._lock:
                        self._clicks += 1

            self._kb = keyboard.Listener(on_press=on_press)
            self._mouse = mouse.Listener(on_click=on_click)
            self._kb.start()
            self._mouse.start()
        except Exception as exc:
            print(f"[perception.l0] input counters unavailable ({exc}); "
                  "idle flag still works via GetLastInputInfo.")

    def stop(self) -> None:
        for lst in (self._kb, self._mouse):
            try:
                if lst is not None:
                    lst.stop()
            except Exception:
                pass
        self._kb = self._mouse = None

    def take(self) -> tuple[int, int]:
        with self._lock:
            k, c = self._keys, self._clicks
            self._keys = self._clicks = 0
            return k, c


# ------------------------------ the monitor -------------------------------
class L0Monitor:
    def __init__(self, store: PerceptionStore | None = None,
                 provider=None, idle_age_fn=win32_idle_age_s, *,
                 poll_s: float | None = None, debounce_ms: int | None = None,
                 heartbeat_s: float | None = None,
                 batch_commit_s: float | None = None,
                 idle_s: float | None = None,
                 gap_threshold_s: float | None = None,
                 audit_every_s: float = 3600.0,
                 use_input_hooks: bool = True) -> None:
        from app.config import settings
        cfg = settings.perception
        self._store = store
        self.provider = provider or win32_provider
        self.idle_age_fn = idle_age_fn
        self.poll_s = cfg.poll_s if poll_s is None else poll_s
        self.debounce_ms = cfg.debounce_ms if debounce_ms is None else debounce_ms
        self.heartbeat_s = cfg.heartbeat_s if heartbeat_s is None else heartbeat_s
        self.batch_commit_s = (cfg.batch_commit_s if batch_commit_s is None
                               else batch_commit_s)
        self.idle_s = cfg.idle_s if idle_s is None else idle_s
        self.gap_threshold_s = (cfg.gap_threshold_s if gap_threshold_s is None
                                else gap_threshold_s)
        self.audit_every_s = audit_every_s
        self._use_hooks = use_input_hooks
        self._now = time.time              # patchable seam for tests

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._inputs = InputCounter()
        self._lock = threading.Lock()

        self.session_id = ""
        self.seq = 0
        self._pending: list[MetaEvent] = []
        self._last_tick: float | None = None
        self._last_flush: float = 0.0
        self._last_emit: float | None = None
        self._last_emitted_state: tuple | None = None
        self._candidate: tuple | None = None
        self._candidate_since: float = 0.0
        self._acc_keys = 0
        self._acc_mouse = 0
        self._utc_off = 0
        self._last_audit: float = 0.0
        self._paused = False
        self._pause_gap_id: int | None = None

    def store(self) -> PerceptionStore:
        if self._store is None:
            self._store = get_pstore()
        return self._store

    # ------------------------------ lifecycle -----------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            now_i = now_ms()
            st = self.store()
            # Reconcile a prior run: close crash-dangled gaps, and label the
            # hole since the last record if one exists.
            dangling = st.close_dangling_gaps(now_i)
            if dangling:
                print(f"[perception.l0] closed {dangling} dangling gap(s).")
            last = st.last_meta_ts()
            if last is not None and now_i - last > int(2 * self.heartbeat_s * 1000):
                st.add_gap(last, now_i, "process_down")
            self.session_id = new_ulid(now_i)
            self.seq = 0
            self._utc_off = utc_offset_minutes()
            self._last_tick = None
            self._last_emit = None
            self._last_emitted_state = None
            self._candidate = None
            self._acc_keys = self._acc_mouse = 0
            self._paused = False
            self._stop.clear()
            if self._use_hooks:
                self._inputs.start()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="perception-l0")
            self._thread.start()
            print("[perception.l0] metadata stream started "
                  f"(session {self.session_id[:10]}…, {1/self.poll_s:.0f} Hz).")

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=max(2.0, self.poll_s * 3))
        self._thread = None
        self._inputs.stop()
        self.flush(force=True)

    def pause(self) -> None:
        """User pause: stop capturing immediately and label the silence."""
        if self._paused:
            return
        self.stop()
        self._pause_gap_id = self.store().add_gap(now_ms(), None, "user_pause")
        self._paused = True
        print("[perception.l0] paused (gap opened).")

    def resume(self) -> None:
        if self._pause_gap_id is not None:
            try:
                self.store().close_gap(self._pause_gap_id, now_ms())
            except Exception as exc:
                print(f"[perception.l0] pause-gap close skipped ({exc}).")
            self._pause_gap_id = None
        self._paused = False
        self.start()

    def running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def status(self) -> dict:
        return {"running": self.running(), "paused": self._paused,
                "session_id": self.session_id, "seq": self.seq,
                "pending": len(self._pending),
                "last_emit_ms": (int(self._last_emit * 1000)
                                 if self._last_emit else None)}

    # ------------------------------ the loop ------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                print(f"[perception.l0] tick failed ({exc}).")
            self._stop.wait(self.poll_s)
        print("[perception.l0] metadata stream stopped.")

    def _tick(self, now: float | None = None) -> None:
        now = self._now() if now is None else now
        # Sleep detection: this loop cannot run while the machine sleeps, so
        # a large wall-clock jump between ticks IS the sleep interval.
        if (self._last_tick is not None
                and (now - self._last_tick) > self.gap_threshold_s):
            self.store().add_gap(int(self._last_tick * 1000),
                                 int(now * 1000), "sleep")
            self._last_emitted_state = None      # state may differ post-wake
            self._utc_off = utc_offset_minutes()  # timezone can change too
        self._last_tick = now

        try:
            info = self.provider() or {}
        except Exception:
            info = {}
        k, m = self._inputs.take()
        self._acc_keys += k
        self._acc_mouse += m

        state = (info.get("app_name") or "",
                 self._exe_hash(info.get("app_exe_path") or ""),
                 info.get("window_id") or "",
                 info.get("window_title") or "",
                 self._domain(info.get("browser_url")),
                 info.get("doc_path"),
                 info.get("display_hash") or "")

        emit = False
        if self._last_emit is None:
            emit = True                                        # session start
        elif state != self._last_emitted_state:
            if self._candidate != state:
                self._candidate = state                        # debounce arm
                self._candidate_since = now
            elif (now - self._candidate_since) * 1000 >= self.debounce_ms:
                emit = True
        else:
            self._candidate = None
        if not emit and self._last_emit is not None \
                and (now - self._last_emit) >= self.heartbeat_s:
            emit = True                    # heartbeat: liveness + input counts

        if emit:
            self._emit(state, info, now)
        if self._pending and (now - self._last_flush) >= self.batch_commit_s:
            self.flush(now=now)
        if (now - self._last_audit) >= self.audit_every_s:
            self._last_audit = now
            self._self_audit(now)

    def _emit(self, state: tuple, info: dict, now: float) -> None:
        idle_age = None
        try:
            idle_age = self.idle_age_fn() if self.idle_age_fn else None
        except Exception:
            pass
        # Secret-shaped window titles (an open .env's full path, a key file)
        # are masked even in local metadata — a title is text, and text goes
        # through the redaction stage before it lands anywhere durable.
        title, _hits = redact_text(state[3], TIER_SECRETS)
        self.seq += 1
        self._pending.append(MetaEvent(
            session_id=self.session_id, seq=self.seq,
            ts_utc=int(now * 1000), utc_offset_minutes=self._utc_off,
            app_name=state[0], app_exe_hash=state[1], window_id=state[2],
            window_title=title, browser_url=info.get("browser_url"),
            url_domain=state[4], doc_path=state[5],
            key_count=self._acc_keys, mouse_count=self._acc_mouse,
            is_idle=bool(idle_age is not None and idle_age >= self.idle_s),
            display_hash=state[6]))
        self._acc_keys = self._acc_mouse = 0
        self._last_emit = now
        self._last_emitted_state = state
        self._candidate = None

    def flush(self, *, force: bool = False, now: float | None = None) -> int:
        rows, self._pending = self._pending, []
        n = 0
        if rows:
            try:
                n = self.store().insert_meta_batch(rows)
            except Exception as exc:
                if force:
                    print(f"[perception.l0] final flush failed ({exc}).")
                else:
                    self._pending = rows + self._pending   # retry next tick
                    return 0
        self._last_flush = self._now() if now is None else now
        return n

    def _self_audit(self, now: float) -> None:
        """Hourly coverage self-audit (correctness criterion 1)."""
        try:
            end = int(now * 1000)
            res = self.store().coverage(end - 24 * 3600 * 1000, end)
            self.store().record_coverage_audit(res)
            if res["covered_pct"] < 100.0:
                print(f"[perception.l0] coverage audit: "
                      f"{res['covered_pct']:.2f}% of the last 24 h vouched "
                      f"for ({len(res['holes'])} unlabeled hole(s)).")
        except Exception as exc:
            print(f"[perception.l0] coverage audit skipped ({exc}).")

    # ------------------------------ helpers -------------------------------
    @staticmethod
    def _exe_hash(exe_path: str) -> str:
        if not exe_path:
            return ""
        return hashlib.sha1(exe_path.lower().encode()).hexdigest()[:16]

    @staticmethod
    def _domain(url: str | None) -> str | None:
        """Registrable-ish domain from a URL; None when unavailable. Phase B
        replaces this with a proper public-suffix parse."""
        if not url:
            return None
        try:
            host = url.split("//", 1)[-1].split("/", 1)[0].split("@")[-1]
            host = host.split(":")[0].strip().lower()
            parts = [p for p in host.split(".") if p]
            return ".".join(parts[-2:]) if len(parts) >= 2 else (host or None)
        except Exception:
            return None


monitor = L0Monitor()
