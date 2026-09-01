"""Passive desktop observation — screen frames + mouse clicks (no keystrokes).

Mirrors the webcam VisionPipeline shape:

    Screen  ->  (VLM describe  OR  L1 OCR when QUILL_PERCEPTION_L1=1)  ->  VISION Event
    Mouse clicks  ->  (x,y) + window + crop  ->  INPUT Event  (+ optional VLM)

Exactly one screen producer may run: the legacy VLM `_screen_loop`, or the
perception L1 text layer. Dual-write is refused at start().

Opt-in via QUILL_DESKTOP_CAPTURE=1. Independent of QUILL_DESKTOP_UI (agent control).
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

from app.config import settings
from app.events import Event, Modality, bus
from app.services import confidence as _conf
from app.services import frame_quality

# Linux click hooks: prefer Xorg so pynput does not require compiling evdev.
if sys.platform.startswith("linux"):
    os.environ.setdefault("PYNPUT_BACKEND", "xorg")

DCfg = settings.desktop_capture


def _foreground_window() -> dict:
    """Best-effort foreground window title (Windows / Linux X11)."""
    os = __import__("os")
    if os.name == "nt":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return {}
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = (buf.value or "").strip()
            out: dict = {"hwnd": int(hwnd)}
            if title:
                out["window"] = title
            return out
        except Exception:
            return {}
    if __import__("sys").platform.startswith("linux"):
        try:
            from desktop_agent import x11_util

            return x11_util.active_window()
        except Exception:
            return {}
    return {}


def _grab_rgb() -> tuple[np.ndarray, tuple[int, int]]:
    """Primary-monitor RGB uint8 array and (width, height)."""
    try:
        import mss
        from PIL import Image

        with mss.mss() as sct:
            mon = sct.monitors[1]
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            arr = np.asarray(img)
            return arr, (int(shot.width), int(shot.height))
    except Exception:
        # pyautogui is a Windows-only dep (see requirements.txt); Linux uses mss.
        if not sys.platform.startswith("win"):
            raise
        import pyautogui  # type: ignore

        img = pyautogui.screenshot()
        arr = np.asarray(img.convert("RGB"))
        h, w = arr.shape[:2]
        return arr, (w, h)


def _rgb_to_jpeg(rgb: np.ndarray, quality: int) -> bytes:
    from PIL import Image

    img = Image.fromarray(rgb)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=max(40, min(95, int(quality))))
    return buf.getvalue()


def _downscale(rgb: np.ndarray, max_width: int) -> np.ndarray:
    h, w = rgb.shape[:2]
    if max_width <= 0 or w <= max_width:
        return rgb
    from PIL import Image

    nh = max(1, int(h * (max_width / w)))
    return np.asarray(Image.fromarray(rgb).resize((max_width, nh), Image.BILINEAR))


def _crop_around(rgb: np.ndarray, x: int, y: int, size: int) -> np.ndarray:
    h, w = rgb.shape[:2]
    half = max(40, size // 2)
    x0 = max(0, int(x) - half)
    y0 = max(0, int(y) - half)
    x1 = min(w, int(x) + half)
    y1 = min(h, int(y) + half)
    return rgb[y0:y1, x0:x1]


class DesktopCapturePipeline:
    """Background screen observer + mouse-click observer."""

    def __init__(self, sink=None) -> None:
        self.cfg = DCfg
        self._sink = sink or (lambda ev: bus.publish_nowait(ev))
        self._stop = threading.Event()
        self._screen_thread: threading.Thread | None = None
        self._click_listener = None
        self._prev_gray: np.ndarray | None = None
        self._last_screen_analysis = 0.0
        self._last_click_vlm = 0.0
        self._last_click: tuple[float, int, int, str] | None = None  # ts, x, y, btn
        self._screen_vlm_broken = False
        self._lock = threading.Lock()
        Path(self.cfg.frame_dir).mkdir(parents=True, exist_ok=True)

    # -------------------------- screen selection -------------------------
    def _motion(self, gray: np.ndarray) -> float:
        if self._prev_gray is None or self._prev_gray.shape != gray.shape:
            self._prev_gray = gray
            return 255.0
        diff = float(np.mean(np.abs(gray.astype(np.int16) - self._prev_gray.astype(np.int16))))
        self._prev_gray = gray
        return diff

    def _should_analyze(self, motion: float, now: float) -> bool:
        since = now - self._last_screen_analysis
        if since < self.cfg.min_interval_s:
            return False
        if motion >= self.cfg.motion_threshold:
            return True
        return since >= self.cfg.max_interval_s

    def _screen_loop(self) -> None:
        while not self._stop.is_set():
            try:
                rgb, _size = _grab_rgb()
            except Exception as exc:
                print(f"[desktop_capture] screen grab failed: {exc}")
                time.sleep(1.0)
                continue

            # frame_quality expects BGR (OpenCV); convert RGB -> BGR for scoring.
            bgr = rgb[:, :, ::-1].copy()
            fq = frame_quality.score(bgr)
            now = time.time()
            if not fq.get("analyzable"):
                time.sleep(0.4)
                continue

            # Tiny gray for motion (cheap).
            small = _downscale(rgb, 160)
            gray = np.mean(small, axis=2).astype(np.uint8)
            motion = self._motion(gray)
            if not self._should_analyze(motion, now):
                time.sleep(0.25)
                continue
            self._last_screen_analysis = now
            self._analyze_screen(rgb, motion, now, fq)
        print("[desktop_capture] screen loop stopped.")

    def _analyze_screen(self, rgb: np.ndarray, motion: float, ts: float,
                        fq: dict) -> None:
        from app.services.surface_filters import (
            is_console_window, scrub_vision_result, should_ingest_screen,
            strip_noise_lines,
        )

        win = _foreground_window()
        window_title = str(win.get("window") or "")
        # Dedicated terminals: never VLM / never publish — don't take the noise in.
        if is_console_window(window_title):
            print(f"[desktop_capture] skip console intake: {window_title[:80]!r}")
            return

        # Pre-pixel privacy gate (perception Phase A): credential/financial
        # surfaces are blocked BEFORE the frame is encoded, saved, or shown
        # to any model — the only gate that can run before the cloud call
        # when the local VLM is down and there is no OCR pass to check
        # first. A match writes a LABELED captures(kind='excluded') row so
        # the timeline shows a redaction, not an unexplained hole. The rule
        # id (never the title — it may itself be the secret) is all we log.
        from app.perception.privacy_gate import gate as privacy_gate
        rule = privacy_gate.check(window_title)
        if rule:
            print(f"[desktop_capture] privacy gate blocked frame ({rule}).")
            privacy_gate.record_exclusion(
                rule, window_id=str(win.get("hwnd") or ""),
                ts_ms=int(ts * 1000))
            return

        scaled = _downscale(rgb, self.cfg.max_width)
        jpeg = _rgb_to_jpeg(scaled, self.cfg.jpeg_quality)
        path = str(Path(self.cfg.frame_dir) / f"screen_{ts:.3f}.jpg")
        try:
            Path(path).write_bytes(jpeg)
        except Exception as exc:
            print(f"[desktop_capture] frame save error: {exc}")

        cap_q = fq.get("capture_quality")
        meta: dict = {
            "frame_path": path,
            "motion": round(motion, 1),
            "frame_quality": fq,
            "surface": "desktop",
            **win,
        }
        raw = "[desktop screen captured]"
        summary = raw
        entities: list[str] = ["desktop_screen"]
        model_conf = None

        # Phase 2: the frame_keep head. Judged on the window title plus the
        # motion scalar already computed above — the only signals that exist
        # before the VLM runs. OFF by default; in shadow mode `skip` is always
        # False, so the VLM runs exactly as before and the head only predicts.
        from app.services import fast_heads as _heads
        _fk = _heads.consult("frame_keep", window_title,
                             extra={"motion": min(float(motion) / 32.0, 1.0)})
        if _fk.get("skip"):
            print(f"[desktop_capture] frame skipped by head "
                  f"(p={_fk['p']:.3f}, motion={motion:.1f})")
            return

        if not self._screen_vlm_broken:
            try:
                from app.services.vlm import vlm, align_item_confidences

                # Screen is the paid-worthy path: allow Claude escalate.
                res = vlm.describe(
                    jpeg, capture_quality=cap_q, escalate=True,
                    context={"frame_path": path, "source": "desktop.screen",
                             "modality": "vision"})
                orig_ocr = str(res.get("ocr_text") or "")
                orig_desc = str(res.get("description") or "")
                ctype = str(res.get("content_type") or "")
                if not should_ingest_screen(
                        window_title, ocr=orig_ocr, summary=orig_desc,
                        content_type=ctype):
                    # The VLM ran and produced nothing worth keeping — the
                    # head was right to want to skip it.
                    _heads.record_outcome(_fk, needed_model=False)
                    print("[desktop_capture] skip CLI/log-only screen intake")
                    return
                # Strip CLI/log lines before anything is stored or mined.
                scrubbed = scrub_vision_result(res)
                if scrubbed is None:
                    _heads.record_outcome(_fk, needed_model=False)
                    print("[desktop_capture] skip empty screen after CLI scrub")
                    return
                res = scrubbed
                # Survived both filters: this frame genuinely needed the VLM.
                _heads.record_outcome(_fk, needed_model=True)
                raw = res.get("ocr_text") or res.get("description", raw)
                summary = res.get("description", summary) or raw
                entities = list(res.get("objects", [])) or entities
                if "desktop_screen" not in entities:
                    entities.append("desktop_screen")
                model_conf = res.get("confidence")
                meta["vision"] = res
                ctype = res.get("content_type") or "none"
                if ctype and ctype != "none":
                    title = (res.get("title") or "").strip()
                    items = res.get("items") or []
                    n = len(items)
                    label = ctype.replace("_", " ")
                    summary = (
                        f"desktop {label}"
                        + (f" — {title}" if title else "")
                        + (f" ({n} item{'s' if n != 1 else ''})" if n else "")
                        + f": {res.get('description', '')}"
                    )
                    entities.append(ctype)
                    meta["content_type"] = ctype
                    if items:
                        meta["items"] = items
                        raw_item_conf = align_item_confidences(res)
                        meta["item_confidences"] = [
                            _conf.readiness(
                                _conf.facets(model=ic, capture=cap_q), _conf.EXTRACTED)
                            for ic in raw_item_conf]
            except Exception as exc:
                # Only hard-disable screen VLM on persistent auth/config failures,
                # not transient local timeouts (those already fall back inside vlm).
                msg = str(exc).lower()
                if "api_key" in msg or "auth" in msg or "credit" in msg:
                    self._screen_vlm_broken = True
                print(f"[desktop_capture] screen VLM unavailable ({exc}). "
                      f"Saving frame without analysis.")

        # Final text hygiene even when VLM was skipped/failed.
        raw = strip_noise_lines(raw) or raw
        summary = strip_noise_lines(summary) or summary
        if not should_ingest_screen(window_title, ocr=raw, summary=summary,
                                    content_type=str(meta.get("content_type") or "")):
            print("[desktop_capture] skip empty/noisy screen intake")
            return

        if win.get("window"):
            summary = f"[{win['window']}] {summary}"

        ev = Event(
            time=ts, modality=Modality.VISION, raw=raw, summary=summary,
            source="desktop.screen", entities=entities, meta=meta,
        )
        _conf.attach(ev, _conf.EXTRACTED, capture=cap_q,
                     model=(float(model_conf) if isinstance(model_conf, (int, float))
                            else None))
        # WS2: verbatim identifiers from the frame text — best-effort.
        try:
            from app.perception import identifiers as _idents
            _idents.stamp_event(ev)
        except Exception as exc:
            print(f"[desktop_capture] identifier stamp skipped ({exc}).")
        print(f"[desktop_capture] screen: {summary[:160]}")
        self._sink(ev)

    # ------------------------------ clicks -------------------------------
    def _on_click(self, x: int, y: int, button, pressed: bool) -> None:
        if not pressed or self._stop.is_set():
            return
        # Run off the pynput callback thread so we never block the hook.
        threading.Thread(
            target=self._handle_click, args=(int(x), int(y), str(button)),
            daemon=True,
        ).start()

    def _is_duplicate_click(self, ts: float, x: int, y: int, btn: str) -> bool:
        prev = self._last_click
        if prev is None:
            return False
        pts, px, py, pbtn = prev
        if btn != pbtn:
            return False
        if (ts - pts) > self.cfg.click_dedup_s:
            return False
        return abs(x - px) <= self.cfg.click_dedup_px and abs(y - py) <= self.cfg.click_dedup_px

    def _handle_click(self, x: int, y: int, button: str) -> None:
        ts = time.time()
        btn = button.replace("Button.", "").lower()
        with self._lock:
            if self._is_duplicate_click(ts, x, y, btn):
                return
            self._last_click = (ts, x, y, btn)

        win = _foreground_window()
        window_title = str(win.get("window") or "")
        # Same intake filters as screen frames — don't flood memory with
        # clicks on the Mnemos console / credential surfaces.
        from app.services.surface_filters import is_console_window
        if is_console_window(window_title):
            print(f"[desktop_capture] skip console click: {window_title[:80]!r}")
            return
        from app.perception.privacy_gate import gate as privacy_gate
        rule = privacy_gate.check(window_title)
        if rule:
            print(f"[desktop_capture] privacy gate blocked click ({rule}).")
            privacy_gate.record_exclusion(
                rule, window_id=str(win.get("hwnd") or ""),
                ts_ms=int(ts * 1000))
            return

        meta: dict = {
            "kind": "click",
            "x": x, "y": y,
            "button": btn,
            "surface": "desktop",
            **win,
        }
        what = ""
        model_conf = None
        cap_q = None

        try:
            rgb, (sw, sh) = _grab_rgb()
            meta["screen_size"] = [sw, sh]
            crop = _crop_around(rgb, x, y, self.cfg.click_crop)
            if crop.size > 0:
                jpeg = _rgb_to_jpeg(crop, self.cfg.jpeg_quality)
                crop_path = str(Path(self.cfg.frame_dir) / f"click_{ts:.3f}.jpg")
                Path(crop_path).write_bytes(jpeg)
                meta["frame_path"] = crop_path

                # Opt-in, local-only, rate-limited — never Claude for click crops.
                do_vlm = (
                    self.cfg.click_vlm
                    and (ts - self._last_click_vlm) >= self.cfg.click_vlm_min_interval_s
                )
                if do_vlm:
                    with self._lock:
                        self._last_click_vlm = ts
                    bgr = crop[:, :, ::-1].copy()
                    fq = frame_quality.score(bgr)
                    cap_q = fq.get("capture_quality")
                    meta["frame_quality"] = fq
                    if fq.get("analyzable"):
                        try:
                            from app.services.vlm import vlm

                            res = vlm.describe(
                                jpeg, capture_quality=cap_q, escalate=False)
                            what = (res.get("description") or "").strip()
                            ocr = (res.get("ocr_text") or "").strip()
                            meta["vision"] = res
                            model_conf = res.get("confidence")
                            if ocr and len(ocr) < 200:
                                meta["ocr_text"] = ocr
                        except Exception as exc:
                            print(f"[desktop_capture] click VLM skipped ({exc}).")
        except Exception as exc:
            print(f"[desktop_capture] click capture failed: {exc}")

        win_label = win.get("window") or "desktop"
        if what:
            summary = f"click {btn} at ({x},{y}) on {win_label}: {what}"
            raw = what
            epistemic = _conf.EXTRACTED
        else:
            summary = f"click {btn} at ({x},{y}) on {win_label}"
            raw = summary
            epistemic = _conf.OBSERVED

        ev = Event(
            time=ts, modality=Modality.INPUT, raw=raw, summary=summary,
            source="desktop.click",
            entities=["click", btn],
            meta=meta,
        )
        _conf.attach(
            ev, epistemic, capture=cap_q,
            model=(float(model_conf) if isinstance(model_conf, (int, float)) else None),
        )
        print(f"[desktop_capture] {summary[:160]}")
        self._sink(ev)

    # ---------------------------- lifecycle ------------------------------
    def start(self) -> None:
        # Refresh from live settings so consent hot-patches (clicks on/off)
        # take effect without process restart.
        self.cfg = settings.desktop_capture
        if not self.cfg.enabled:
            raise RuntimeError(
                "desktop capture disabled (set QUILL_DESKTOP_CAPTURE=1 to enable)")
        plat = __import__("sys").platform
        if not (plat.startswith("win") or plat.startswith("linux")):
            raise RuntimeError(
                "desktop capture is currently Windows/Linux-only "
                "(macOS meeting path does not include screen/clicks)")
        if plat.startswith("linux"):
            # mss + pynput need a real X11 DISPLAY; XWayland is unreliable.
            try:
                from desktop_agent import x11_util

                if not x11_util.session_ok():
                    raise RuntimeError(
                        "desktop capture needs an X11 session "
                        "(DISPLAY set, XDG_SESSION_TYPE!=wayland)")
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"desktop capture X11 check failed ({exc})") from exc
        if self._screen_thread and self._screen_thread.is_alive():
            return
        # Also refuse restart while L1 owns the screen producer.
        if self._l1_owns_screen() and self._l1_running():
            return

        self._stop.clear()
        self._prev_gray = None
        self._last_screen_analysis = 0.0
        self._last_click = None
        self._screen_vlm_broken = False

        started = []
        if self.cfg.screen:
            if self._l1_owns_screen():
                # Phase B cutover: L1 is the sole desktop.screen producer.
                # Never start the VLM _screen_loop alongside it.
                if self._vlm_screen_alive():
                    raise RuntimeError(
                        "desktop capture: refuse dual screen producers "
                        "(VLM loop still alive while QUILL_PERCEPTION_L1=1)")
                self._start_l1()
                started.append("screen(l1)")
            else:
                if self._l1_running():
                    raise RuntimeError(
                        "desktop capture: refuse dual screen producers "
                        "(L1 still alive while QUILL_PERCEPTION_L1=0)")
                self._screen_thread = threading.Thread(
                    target=self._screen_loop, daemon=True, name="desktop-screen")
                self._screen_thread.start()
                started.append("screen")

        if self.cfg.clicks:
            try:
                from pynput import mouse

                self._click_listener = mouse.Listener(on_click=self._on_click)
                self._click_listener.start()
                started.append("clicks")
            except Exception as exc:
                print(f"[desktop_capture] click listener failed ({exc}). "
                      f"Install pynput: pip install pynput "
                      f"(on Linux: pip install pynput --no-deps if evdev "
                      f"build fails; Xorg backend uses python-xlib)")

        if not started:
            raise RuntimeError(
                "desktop capture enabled but both screen and clicks are off "
                "(QUILL_DESKTOP_CAPTURE_SCREEN / _CLICKS)")
        # Pilot ledger (WS-A): accrue desktop capture minutes while running.
        # Counted only past the guards above, so a refused start counts zero.
        try:
            from app.services.usage_ledger import usage
            usage.capture_started("desktop")
        except Exception as exc:
            print(f"[usage] desktop capture start not counted ({exc}).")
        click_mode = ("click-vlm=local-only" if self.cfg.click_vlm
                      else "click-vlm=off (coords+crop)")
        print(f"[desktop_capture] watching {', '.join(started)} "
              f"(screen interval {self.cfg.min_interval_s}-"
              f"{self.cfg.max_interval_s}s; {click_mode}). Opt-in capture active.")

    def stop(self) -> None:
        try:
            from app.services.usage_ledger import usage
            usage.capture_stopped("desktop")
        except Exception as exc:
            print(f"[usage] desktop capture stop not counted ({exc}).")
        self._stop.set()
        self._stop_l1()
        if self._click_listener is not None:
            try:
                self._click_listener.stop()
            except Exception:
                pass
            self._click_listener = None

    def running(self) -> dict:
        screen_alive = bool(self._screen_thread and self._screen_thread.is_alive()) \
            or self._l1_running()
        clicks_alive = bool(
            self._click_listener is not None and getattr(self._click_listener, "running", False)
        )
        return {
            "enabled": self.cfg.enabled,
            "screen_running": screen_alive,
            "screen_producer": ("l1" if self._l1_owns_screen() and self._l1_running()
                                else ("vlm" if bool(self._screen_thread
                                      and self._screen_thread.is_alive())
                                      else "none")),
            "clicks_running": clicks_alive,
            "running": screen_alive or clicks_alive,
        }

    # ------------------------ L1 / VLM producer mutex ---------------------
    @staticmethod
    def _l1_owns_screen() -> bool:
        from app.config import settings
        return bool(settings.perception.enabled
                    and settings.perception.l1_enabled)

    def _vlm_screen_alive(self) -> bool:
        return bool(self._screen_thread and self._screen_thread.is_alive())

    @staticmethod
    def _l1_running() -> bool:
        try:
            from app.perception.l1_capture import monitor as l1
            return bool(l1.running())
        except Exception:
            return False

    def _start_l1(self) -> None:
        from app.perception.l1_capture import monitor as l1
        # Share this pipeline's sink so tests / custom sinks keep working.
        l1._sink = self._sink
        l1.start()

    @staticmethod
    def _stop_l1() -> None:
        try:
            from app.perception.l1_capture import monitor as l1
            if l1.running():
                l1.stop()
        except Exception as exc:
            print(f"[desktop_capture] L1 stop skipped ({exc}).")
