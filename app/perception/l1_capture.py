"""L1 — change-triggered OCR text layer (Phase B).

Triggers (in order): L0 foreground/app/title change → 700 ms settle;
perceptual dHash every 5 s (Hamming > 10); max-interval fallback 120 s.
Scroll suppression: ≥70 % of lines already in the window's rolling hash
cache → kind='scroll_delta', store only novel lines.

Pipeline: foreground-window grab → privacy gate (pre-pixel) → local OCR →
secrets redaction → delta persist (ocr_lines + frame_line_map + FTS) →
embed merged blocks ≥20 chars into LanceDB ocr_blocks → emit one
desktop.screen Event with meta.capture_id.

No VLM / no network in this path. No full-frame JPEG persist (Phase C).
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque
from typing import Callable

import numpy as np

from app.perception.dhash import dhash64, hamming64
from app.perception.ocr import NullOcr, OcrEngine, OcrLineResult, get_default_engine
from app.perception.redaction import TIER_SECRETS, redact_text, secret_kinds
from app.perception.schemas import Capture, OcrLine, now_ms
from app.perception.store import PerceptionStore, get_pstore


def _line_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _foreground_info() -> dict:
    """hwnd, title, app exe — Win32; empty elsewhere."""
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
        return {
            "hwnd": int(hwnd),
            "window": title,
            "window_id": str(int(hwnd)),
            "app_exe": exe,
            "app_name": (exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
                         if exe else ""),
        }
    except Exception:
        return {}


def grab_foreground_rgb() -> tuple[np.ndarray | None, dict]:
    """Grab the foreground window client area as RGB. Returns (rgb|None, info)."""
    info = _foreground_info()
    hwnd = info.get("hwnd")
    if not hwnd:
        return None, info
    try:
        import ctypes
        from ctypes import wintypes
        from PIL import Image

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None, info
        w = int(rect.right - rect.left)
        h = int(rect.bottom - rect.top)
        if w < 8 or h < 8:
            return None, info

        hwndDC = user32.GetDC(hwnd)
        if not hwndDC:
            return None, info
        memDC = gdi32.CreateCompatibleDC(hwndDC)
        bmp = gdi32.CreateCompatibleBitmap(hwndDC, w, h)
        gdi32.SelectObject(memDC, bmp)
        # PW_RENDERFULLCONTENT = 2 — better for DirectComposition windows.
        ok = user32.PrintWindow(hwnd, memDC, 2)
        if not ok:
            ok = gdi32.BitBlt(memDC, 0, 0, w, h, hwndDC, 0, 0, 0x00CC0020)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h  # top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0
        buf = (ctypes.c_ubyte * (w * h * 4))()
        gdi32.GetDIBits(memDC, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memDC)
        user32.ReleaseDC(hwnd, hwndDC)
        if not ok:
            return None, info
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        rgb = arr[:, :, 2::-1].copy()  # BGRA → RGB
        return rgb, info
    except Exception as exc:
        print(f"[perception.l1] foreground grab failed ({exc}).")
        return None, info


def merge_blocks(lines: list[OcrLineResult], min_chars: int = 20) -> list[str]:
    """Contiguous lines whose joined text is ≥ min_chars → embeddable blocks."""
    blocks: list[str] = []
    buf: list[str] = []
    for ln in lines:
        t = (ln.text or "").strip()
        if not t:
            if buf:
                joined = "\n".join(buf)
                if len(joined) >= min_chars:
                    blocks.append(joined)
                buf = []
            continue
        buf.append(t)
    if buf:
        joined = "\n".join(buf)
        if len(joined) >= min_chars:
            blocks.append(joined)
    return blocks


class L1Capture:
    def __init__(self, store: PerceptionStore | None = None,
                 ocr: OcrEngine | None = None,
                 sink: Callable | None = None, *,
                 settle_ms: int | None = None,
                 dhash_every_s: float | None = None,
                 dhash_hamming: int | None = None,
                 max_interval_s: float | None = None,
                 min_conf: float | None = None,
                 line_cache: int | None = None,
                 scroll_overlap: float | None = None,
                 poll_s: float = 0.25) -> None:
        from app.config import settings
        cfg = settings.perception
        self._store = store
        self._ocr = ocr  # lazy default
        self._sink = sink
        self.settle_ms = cfg.l1_settle_ms if settle_ms is None else settle_ms
        self.dhash_every_s = (cfg.l1_dhash_every_s if dhash_every_s is None
                              else dhash_every_s)
        self.dhash_hamming = (cfg.l1_dhash_hamming if dhash_hamming is None
                              else dhash_hamming)
        self.max_interval_s = (cfg.l1_max_interval_s if max_interval_s is None
                               else max_interval_s)
        self.min_conf = cfg.l1_min_conf if min_conf is None else min_conf
        self.line_cache_n = cfg.l1_line_cache if line_cache is None else line_cache
        self.scroll_overlap = (cfg.l1_scroll_overlap if scroll_overlap is None
                               else scroll_overlap)
        self.poll_s = poll_s
        self._now = time.time

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._caches: dict[str, deque[str]] = defaultdict(
            lambda: deque(maxlen=self.line_cache_n))
        self._cache_loaded: set[str] = set()

        self._last_fg: tuple | None = None
        self._pending_change_since: float | None = None
        self._last_capture_ts: float = 0.0
        self._last_dhash_ts: float = 0.0
        self._last_dhash: int | None = None
        self._ocr_warned = False

    def store(self) -> PerceptionStore:
        if self._store is None:
            self._store = get_pstore()
        return self._store

    def ocr(self) -> OcrEngine:
        if self._ocr is None:
            self._ocr = get_default_engine()
        return self._ocr

    def _emit_sink(self):
        if self._sink is not None:
            return self._sink
        from app.events import bus
        return lambda ev: bus.publish_nowait(ev)

    # ------------------------------ lifecycle -----------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self.ocr().available():
                if not self._ocr_warned:
                    print("[perception.l1] OCR engine unavailable — L1 will "
                          "not capture until one is present.")
                    self._ocr_warned = True
            self._stop.clear()
            self._last_fg = None
            self._pending_change_since = None
            self._last_capture_ts = 0.0
            self._last_dhash = None
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="perception-l1")
            self._thread.start()
            print("[perception.l1] text layer started "
                  f"(settle={self.settle_ms}ms, dhash={self.dhash_every_s}s, "
                  f"max={self.max_interval_s}s).")

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=max(2.0, self.poll_s * 8))
        self._thread = None
        print("[perception.l1] text layer stopped.")

    def running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def status(self) -> dict:
        return {"running": self.running(),
                "ocr_available": self.ocr().available(),
                "last_capture_ts": self._last_capture_ts or None,
                "cached_windows": len(self._caches)}

    # ------------------------------ the loop ------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                print(f"[perception.l1] tick failed ({exc}).")
            self._stop.wait(self.poll_s)

    def _tick(self, now: float | None = None) -> None:
        now = self._now() if now is None else now
        info = _foreground_info()
        fg = (info.get("app_name") or "", info.get("window_id") or "",
              info.get("window") or "")
        trigger = None

        if self._last_fg is None:
            self._last_fg = fg
            # First tick: arm settle so we capture shortly after start.
            self._pending_change_since = now
        elif fg != self._last_fg:
            self._last_fg = fg
            self._pending_change_since = now
            self._last_dhash = None  # force re-hash after context switch
        elif (self._pending_change_since is not None
              and (now - self._pending_change_since) * 1000 >= self.settle_ms):
            trigger = "l0_change"
            self._pending_change_since = None

        if trigger is None and (now - self._last_dhash_ts) >= self.dhash_every_s:
            self._last_dhash_ts = now
            rgb, info2 = grab_foreground_rgb()
            if rgb is not None:
                h = dhash64(rgb)
                if (self._last_dhash is not None
                        and hamming64(h, self._last_dhash) > self.dhash_hamming):
                    trigger = "dhash"
                    info = {**info, **info2}
                    # Reuse the grab we already paid for.
                    self._capture(rgb, info, trigger, now)
                    self._last_dhash = h
                    return
                self._last_dhash = h

        if trigger is None and self._last_capture_ts > 0 \
                and (now - self._last_capture_ts) >= self.max_interval_s:
            trigger = "max_interval"
        elif trigger is None and self._last_capture_ts == 0 \
                and self._pending_change_since is None:
            # Cold start without a pending settle — wait for first change/settle.
            pass

        if trigger is None:
            return
        rgb, info2 = grab_foreground_rgb()
        if rgb is None:
            return
        info = {**info, **info2}
        self._capture(rgb, info, trigger, now)

    # ------------------------------ one capture ---------------------------
    def _capture(self, rgb: np.ndarray, info: dict, trigger: str,
                 now: float) -> dict | None:
        from app.perception.privacy_gate import gate as privacy_gate
        from app.services.surface_filters import is_console_window

        title = str(info.get("window") or "")
        window_id = str(info.get("window_id") or info.get("hwnd") or "")
        if is_console_window(title):
            return None

        rule = privacy_gate.check(title, app_exe=str(info.get("app_exe") or ""))
        if rule:
            privacy_gate.record_exclusion(
                rule, window_id=window_id, ts_ms=int(now * 1000))
            print(f"[perception.l1] privacy gate blocked ({rule}).")
            self._last_capture_ts = now
            return None

        if not self.ocr().available():
            return None

        result = self.ocr().recognize(rgb)
        kept: list[OcrLineResult] = []
        dropped = 0
        redaction_hits = 0
        for ln in result.lines:
            if float(ln.conf) < self.min_conf:
                dropped += 1
                continue
            text, hits = redact_text(ln.text, TIER_SECRETS)
            if hits:
                redaction_hits += len(hits)
            # Secret-shaped: prefer dropping the line over storing a mask that
            # still hints at credentials on screen.
            if secret_kinds(ln.text):
                redaction_hits += 1
                continue
            if not (text or "").strip():
                continue
            kept.append(OcrLineResult(
                text=text.strip(), bbox_x=ln.bbox_x, bbox_y=ln.bbox_y,
                bbox_w=ln.bbox_w, bbox_h=ln.bbox_h, conf=ln.conf))

        ordered_hashes = [_line_hash(ln.text) for ln in kept]
        self._ensure_cache(window_id)
        cache = self._caches[window_id]
        cache_set = set(cache)
        novel_idxs = [i for i, h in enumerate(ordered_hashes) if h not in cache_set]
        total = len(ordered_hashes)
        overlap = (1.0 - (len(novel_idxs) / total)) if total else 1.0
        kind = ("scroll_delta" if total and overlap >= self.scroll_overlap
                else "full")
        novel_lines = [kept[i] for i in novel_idxs]
        novel_hashes = [ordered_hashes[i] for i in novel_idxs]

        frame_sha = thumb_sha = None
        thumb_path = None
        degradation = "text"
        try:
            from app.config import settings
            if settings.perception.l2_enabled:
                from app.perception import l2_frames
                cas = l2_frames.put_rgb(rgb)
                frame_sha = cas["frame_sha256"]
                thumb_sha = cas["thumb_sha256"]
                thumb_path = cas["thumb_path"]
                degradation = "full"
        except Exception as exc:
            print(f"[perception.l1] L2 frame write skipped ({exc}).")

        cap = Capture(
            ts_utc=int(now * 1000), window_id=window_id, kind=kind,
            trigger=trigger, ocr_engine=result.engine,
            ocr_version=result.version or None,
            ocr_mean_conf=(round(sum(ln.conf for ln in kept) / len(kept), 4)
                           if kept else None),
            dropped_low_conf=dropped, redaction_hits=redaction_hits,
            novel_line_count=len(novel_lines), total_line_count=total,
            frame_sha256=frame_sha, thumb_sha256=thumb_sha,
            degradation=degradation)
        st = self.store()
        st.insert_capture(cap)
        if novel_lines:
            st.upsert_ocr_lines([
                OcrLine(line_hash=novel_hashes[i], window_id=window_id,
                        first_capture_id=cap.capture_id,
                        text=novel_lines[i].text,
                        bbox_x=novel_lines[i].bbox_x,
                        bbox_y=novel_lines[i].bbox_y,
                        bbox_w=novel_lines[i].bbox_w,
                        bbox_h=novel_lines[i].bbox_h,
                        conf=novel_lines[i].conf)
                for i in range(len(novel_lines))])
            for h in novel_hashes:
                cache.append(h)
        # Full visible text map — even for scroll_delta (reconstruction).
        st.set_frame_line_map(cap.capture_id, ordered_hashes)

        text = st.reconstruct_text(cap.capture_id)
        # Embed merged novel-ish blocks from the visible lines (redacted).
        blocks = merge_blocks(kept)
        if blocks:
            try:
                from app.perception import ocr_blocks
                ocr_blocks.add_blocks(cap.capture_id, blocks, ts=now)
            except Exception as exc:
                print(f"[perception.l1] embed skipped ({exc}).")

        self._publish_event(cap, text, info, now, thumb_path=thumb_path)
        self._last_capture_ts = now
        self._last_dhash = dhash64(rgb)
        return {"capture_id": cap.capture_id, "kind": kind, "text": text,
                "novel": len(novel_lines), "total": total,
                "frame_sha256": frame_sha, "thumb_sha256": thumb_sha}

    def _ensure_cache(self, window_id: str) -> None:
        if not window_id or window_id in self._cache_loaded:
            return
        hashes = self.store().load_window_line_hashes(
            window_id, limit=self.line_cache_n)
        dq = self._caches[window_id]
        for h in hashes:
            dq.append(h)
        self._cache_loaded.add(window_id)

    def _publish_event(self, cap: Capture, text: str, info: dict,
                       now: float, *, thumb_path: str | None = None) -> None:
        from app.events import Event, Modality
        from app.services import confidence as _conf

        raw = text or ""
        title = str(info.get("window") or "")
        summary = (f"[{title}] {raw[:240]}" if title else raw[:240]) or \
            "[desktop screen captured]"
        meta = {
            "surface": "desktop",
            "capture_id": cap.capture_id,
            "window": title,
            "hwnd": info.get("hwnd"),
            "frame_sha256": cap.frame_sha256,
            "thumb_sha256": cap.thumb_sha256,
            "degradation": cap.degradation,
            "l1": {
                "kind": cap.kind,
                "trigger": cap.trigger,
                "novel_line_count": cap.novel_line_count,
                "total_line_count": cap.total_line_count,
                "ocr_engine": cap.ocr_engine,
                "ocr_mean_conf": cap.ocr_mean_conf,
                "redaction_hits": cap.redaction_hits,
            },
        }
        if thumb_path:
            meta["frame_path"] = thumb_path
        ev = Event(
            time=now, modality=Modality.VISION, raw=raw, summary=summary,
            source="desktop.screen",
            entities=["desktop_screen", "l1_ocr"],
            meta=meta,
        )
        _conf.attach(ev, _conf.EXTRACTED,
                     model=float(cap.ocr_mean_conf)
                     if cap.ocr_mean_conf is not None else None)
        try:
            self._emit_sink()(ev)
            print(f"[perception.l1] {cap.kind}/{cap.trigger}: "
                  f"{summary[:140]}")
        except Exception as exc:
            print(f"[perception.l1] event publish failed ({exc}).")

    # Test seam: run one capture without the loop.
    def capture_once(self, rgb: np.ndarray, info: dict,
                     trigger: str = "test") -> dict | None:
        return self._capture(rgb, info, trigger, self._now())


monitor = L1Capture()
