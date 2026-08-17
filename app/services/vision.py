"""M2 — live vision pipeline.

    Webcam  ->  OpenCV  ->  frame selection  ->  Claude vision  ->  VISION Event  ->  EventBus

Frame selection (don't send every frame to the model — that's slow and costly):
  * capture continuously at a low rate,
  * pick a frame when the scene *changes* (mean abs pixel diff over a threshold),
    rate-limited to at most one analysis per `min_interval_s`,
  * and force one at least every `max_interval_s` even if nothing moved.

Selected frames are saved as JPEGs (linked from the event's meta) and sent to
the VLM for a structured extraction that becomes a memory event.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from app.config import settings
from app.events import Event, Modality, bus
from app.services import confidence as _conf
from app.services import frame_quality
from app.services.camera import camera_backend_hint, open_camera

VCfg = settings.vision


class VisionPipeline:
    def __init__(self, sink=None) -> None:
        self.cfg = VCfg
        self._sink = sink or (lambda ev: bus.publish_nowait(ev))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cap = None
        self._prev_gray: np.ndarray | None = None
        self._last_analysis = 0.0
        self._vlm_broken = False
        self._health_reasons: tuple[str, ...] = ()   # () == frames look healthy
        Path(self.cfg.frame_dir).mkdir(parents=True, exist_ok=True)

    # ------------------------------ selection ----------------------------
    def _motion(self, gray: np.ndarray) -> float:
        if self._prev_gray is None or self._prev_gray.shape != gray.shape:
            self._prev_gray = gray
            return 255.0  # first frame always "changed"
        diff = float(np.mean(np.abs(gray.astype(np.int16) - self._prev_gray.astype(np.int16))))
        self._prev_gray = gray
        return diff

    def _should_analyze(self, motion: float, now: float) -> bool:
        since = now - self._last_analysis
        if since < self.cfg.min_interval_s:
            return False
        if motion >= self.cfg.motion_threshold:
            return True
        return since >= self.cfg.max_interval_s

    # ------------------------------ loop ---------------------------------
    def _run(self) -> None:
        import cv2

        fails = 0
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok or frame is None:
                # The camera opened but isn't delivering frames (another app has
                # it, a flaky backend, or it was unplugged). Back off instead of
                # hammering read() — which is what floods the MSMF warnings — and
                # try reopening once in a while so it recovers on its own.
                fails += 1
                if fails == 1:
                    print("[vision] camera opened but not delivering frames; "
                          "retrying quietly. If it persists: close other apps "
                          "using the webcam, check camera privacy settings, or set "
                          f"QUILL_CAMERA_INDEX / QUILL_CAMERA_BACKEND "
                          f"({camera_backend_hint()}). "
                          "python scripts/diagnose_camera.py")
                if fails % 50 == 0:                 # ~every 25s, try a fresh open
                    self._reopen(cv2)
                time.sleep(0.5)
                continue
            if fails:
                print("[vision] camera recovered.")
                fails = 0
            # Frame-health gate (#6): score the raw pixels first. A dead frame —
            # uniform, near-black, or a low-detail single-color cast (the green
            # placeholder glitch) — is the camera-broken class: skip the VLM and
            # flag it as a SYSTEM camera-health event instead of paying to
            # describe nothing. Subsumes the old brightness-only dark check.
            fq = frame_quality.score(frame)
            now = time.time()
            if not fq["analyzable"]:
                self._note_camera_health(fq, now)
                self._prev_gray = None
                time.sleep(0.3)
                continue
            self._note_camera_health(fq, now)     # emits 'recovered' on transition
            gray = cv2.cvtColor(cv2.resize(frame, (160, 120)), cv2.COLOR_BGR2GRAY)
            motion = self._motion(gray)
            if not self._should_analyze(motion, now):
                time.sleep(0.2)
                continue
            self._last_analysis = now
            self._analyze(frame, motion, now, fq)
        # end loop
        if self._cap is not None:
            self._cap.release()

    def _note_camera_health(self, fq: dict, ts: float) -> None:
        """Emit a SYSTEM camera-health event on a health-state TRANSITION (not
        every frame). Unhealthy -> a record that the camera is producing unusable
        frames (and why); back to healthy -> a recovery note. This is what makes
        'the camera is broken' visible instead of a silent stream of green frames."""
        reasons = tuple(fq.get("reasons") or ()) if not fq.get("analyzable") else ()
        if reasons == self._health_reasons:
            return                                  # no change — stay quiet
        self._health_reasons = reasons
        if reasons:
            msg = (f"Camera producing unusable frames ({fq.get('quality')}): "
                   f"{', '.join(reasons)}. Pausing analysis - not spending VLM "
                   f"calls on it. Check the lens / privacy settings / "
                   f"QUILL_CAMERA_BACKEND.")
            healthy = False
        else:
            msg = "Camera frames recovered; resuming analysis."
            healthy = True
        print(f"[vision] {msg}")
        try:
            self._sink(Event(
                time=ts, modality=Modality.SYSTEM, raw=msg, summary=msg,
                source="vision.camera_health",
                meta={"frame_quality": fq, "camera_index": self.cfg.camera_index,
                      "healthy": healthy}))
        except Exception as exc:
            print(f"[vision] camera-health emit skipped ({exc}).")

    def _analyze(self, frame, motion: float, ts: float, fq: dict | None = None) -> None:
        import cv2

        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
        if not ok:
            return
        jpeg = buf.tobytes()
        path = str(Path(self.cfg.frame_dir) / f"{ts:.3f}.jpg")
        try:
            Path(path).write_bytes(jpeg)
        except Exception as exc:
            print(f"[vision] frame save error: {exc}")

        cap_q = (fq or {}).get("capture_quality")
        meta = {"frame_path": path, "motion": round(motion, 1)}
        if fq is not None:
            meta["frame_quality"] = fq
        raw = "[frame captured]"
        summary = raw
        people: list[str] = []
        entities: list[str] = []
        model_conf = None

        if not self._vlm_broken:
            try:
                from app.services.vlm import vlm, align_item_confidences

                # Pass the frame's capture quality so the router can escalate a
                # marginal-capture content page to the accurate reader (#6c).
                res = vlm.describe(
                    jpeg, capture_quality=cap_q,
                    context={"frame_path": path, "source": "vision.webcam",
                             "modality": "vision"})
                raw = res.get("ocr_text") or res.get("description", raw)
                summary = res.get("description", summary)
                entities = list(res.get("objects", []))
                model_conf = res.get("confidence")
                meta["vision"] = res
                # Surface the page classification in the timeline + as a searchable
                # tag, and store the discrete items so the agent can act on them.
                ctype = res.get("content_type") or "none"
                if ctype and ctype != "none":
                    title = (res.get("title") or "").strip()
                    items = res.get("items") or []
                    n = len(items)
                    label = ctype.replace("_", " ")
                    summary = (f"{label}"
                               + (f" — {title}" if title else "")
                               + (f" ({n} item{'s' if n != 1 else ''})" if n else "")
                               + f": {res.get('description', '')}")
                    entities.append(ctype)
                    meta["content_type"] = ctype
                    if items:
                        meta["items"] = items
                        # Item-level confidence (#6d): fold each item's own model
                        # confidence with the frame's capture quality into a per-
                        # item action-readiness, so a smudged line enters weaker
                        # than a crisp one and the action gate can tell them apart.
                        raw_item_conf = align_item_confidences(res)
                        meta["item_confidences"] = [
                            _conf.readiness(
                                _conf.facets(model=ic, capture=cap_q), _conf.EXTRACTED)
                            for ic in raw_item_conf]
            except Exception as exc:
                self._vlm_broken = True
                print(f"[vision] VLM unavailable ({exc}). Saving frames without "
                      f"analysis. Set ANTHROPIC_API_KEY to enable descriptions.")

        ev = Event(
            time=ts, modality=Modality.VISION, raw=raw, summary=summary,
            source="vision.claude", people=people, entities=entities, meta=meta,
        )
        # Stamp the confidence contract (#3): a VISION event is model-EXTRACTED
        # from an observed frame — capture_quality from frame_quality, model_
        # confidence from the VLM. Mirrors what audio.py does for transcripts.
        _conf.attach(ev, _conf.EXTRACTED, capture=cap_q,
                     model=(float(model_conf) if isinstance(model_conf, (int, float))
                            else None))
        label = summary if summary != "[frame captured]" else "frame captured"
        print(f"[vision] {label}")
        self._sink(ev)

    # ------------------------------ lifecycle ----------------------------
    def _open_capture(self, cv2):
        """Open the webcam with the configured backend, then Linux V4L2, then
        CAP_ANY. DirectShow (dshow) remains the reliable Windows default."""
        cap = open_camera(cv2, self.cfg.camera_index, self.cfg.capture_backend)
        if cap is not None and cap.isOpened():
            self._configure(cap, cv2)
        return cap

    def _configure(self, cap, cv2) -> None:
        """Pin the pixel format so OpenCV decodes real frames, not raw-buffer
        noise. MJPG + CONVERT_RGB is the fix for the Windows 'colored static'
        frame (a YUY2/NV12 stride mismatch); a known resolution forces the
        camera to renegotiate its format."""
        try:
            cap.set(cv2.CAP_PROP_CONVERT_RGB, 1.0)
        except Exception:
            pass
        fourcc = (self.cfg.capture_fourcc or "").strip()
        if fourcc:
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc[:4]))
            except Exception:
                pass
        if self.cfg.capture_width and self.cfg.capture_height:
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.capture_width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.capture_height)
            except Exception:
                pass

    def _warmup(self, cv2) -> None:
        """Read and discard a few frames so the sensor auto-exposes before we
        analyze — otherwise the first frames come back black."""
        for _ in range(max(0, self.cfg.warmup_frames)):
            self._cap.read()
            time.sleep(0.05)

    def _reopen(self, cv2) -> None:
        try:
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
        self._cap = self._open_capture(cv2)
        if self._cap is not None and self._cap.isOpened():
            self._warmup(cv2)

    def start(self) -> None:
        import cv2

        # Silence OpenCV's per-frame MSMF/videoio WARN spam; we handle failures.
        for _set in (lambda: cv2.utils.logging.setLogLevel(
                        cv2.utils.logging.LOG_LEVEL_ERROR),
                     lambda: cv2.setLogLevel(3)):
            try:
                _set()
                break
            except Exception:
                continue

        self._cap = self._open_capture(cv2)
        if self._cap is None or not self._cap.isOpened():
            raise RuntimeError(
                f"could not open camera index {self.cfg.camera_index} "
                f"(set QUILL_CAMERA_INDEX, or QUILL_CAMERA_BACKEND="
                f"{camera_backend_hint()}; try python scripts/diagnose_camera.py)"
            )
        self._warmup(cv2)   # let auto-exposure settle before the first analysis
        self._stop.clear()
        self._prev_gray = None
        self._last_analysis = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[vision] watching camera {self.cfg.camera_index} "
              f"(analyze every {self.cfg.min_interval_s}-{self.cfg.max_interval_s}s "
              f"or on motion). Ctrl+C to stop.")

    def health(self) -> dict:
        """Live camera-health snapshot for the console (#6): whether the last
        frame read as usable, and the reasons if not."""
        return {"healthy": not self._health_reasons,
                "reasons": list(self._health_reasons),
                "camera_index": self.cfg.camera_index}

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[vision] stopping ...")
        finally:
            self.stop()
