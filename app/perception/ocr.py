"""OCR engine interface for L1 — Windows.Media.Ocr default.

Behind a narrow interface so PaddleOCR/Tesseract can swap later. Soft-fails
when winsdk / OCR is unavailable (non-Windows, missing package): callers see
`available() is False` and skip the capture rather than inventing text or
falling back to a VLM (no inline model calls in the capture path).
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass
class OcrLineResult:
    text: str
    bbox_x: float = 0.0
    bbox_y: float = 0.0
    bbox_w: float = 0.0
    bbox_h: float = 0.0
    conf: float = 1.0


@dataclass
class OcrResult:
    lines: list[OcrLineResult]
    engine: str = "none"
    version: str = ""


class OcrEngine(Protocol):
    def available(self) -> bool: ...
    def recognize(self, rgb: np.ndarray) -> OcrResult: ...


class NullOcr:
    """Always-unavailable stub (tests / non-Windows)."""

    def available(self) -> bool:
        return False

    def recognize(self, rgb: np.ndarray) -> OcrResult:
        return OcrResult(lines=[], engine="null", version="")


class WindowsMediaOcr:
    """winsdk Windows.Media.Ocr wrapper. One engine instance, reused."""

    def __init__(self) -> None:
        self._engine = None
        self._lock = threading.Lock()
        self._warned = False
        self.engine_name = "windows.media.ocr"
        self.version = ""

    def available(self) -> bool:
        return self._ensure() is not None

    def _ensure(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            try:
                import os
                if os.name != "nt":
                    return None
                from winsdk.windows.media.ocr import OcrEngine
                eng = OcrEngine.try_create_from_user_profile_languages()
                if eng is None:
                    eng = OcrEngine.try_create_from_language(
                        # en-US fallback when profile languages lack OCR.
                        __import__("winsdk.windows.globalization",
                                   fromlist=["Language"]).Language("en-US"))
                if eng is None:
                    if not self._warned:
                        print("[perception.ocr] Windows.Media.Ocr unavailable "
                              "(no language pack?).")
                        self._warned = True
                    return None
                self._engine = eng
                try:
                    self.version = str(eng.recognizer_name or "")
                except Exception:
                    self.version = ""
            except Exception as exc:
                if not self._warned:
                    print(f"[perception.ocr] winsdk OCR init failed ({exc}).")
                    self._warned = True
                return None
        return self._engine

    def recognize(self, rgb: np.ndarray) -> OcrResult:
        eng = self._ensure()
        if eng is None or rgb is None or getattr(rgb, "size", 0) == 0:
            return OcrResult(lines=[], engine=self.engine_name,
                             version=self.version)
        try:
            software_bitmap = _rgb_to_software_bitmap(rgb)
            if software_bitmap is None:
                return OcrResult(lines=[], engine=self.engine_name,
                                 version=self.version)
            result = _await(eng.recognize_async(software_bitmap))
            lines: list[OcrLineResult] = []
            if result is None:
                return OcrResult(lines=[], engine=self.engine_name,
                                 version=self.version)
            for line in (result.lines or []):
                text = (line.text or "").strip()
                if not text:
                    continue
                rect = getattr(line, "words", None)
                # Aggregate word rects into a line bbox when available.
                xs, ys, x2s, y2s, confs = [], [], [], [], []
                for w in (rect or []):
                    b = getattr(w, "bounding_rect", None)
                    if b is not None:
                        xs.append(float(b.x)); ys.append(float(b.y))
                        x2s.append(float(b.x + b.width))
                        y2s.append(float(b.y + b.height))
                    # Windows OCR does not expose per-word confidence reliably;
                    # treat recognized lines as high-confidence (1.0) so the
                    # 0.55 floor does not silently drop everything.
                    confs.append(1.0)
                if xs:
                    x0, y0 = min(xs), min(ys)
                    lines.append(OcrLineResult(
                        text=text, bbox_x=x0, bbox_y=y0,
                        bbox_w=max(x2s) - x0, bbox_h=max(y2s) - y0,
                        conf=float(sum(confs) / len(confs))))
                else:
                    lines.append(OcrLineResult(text=text, conf=1.0))
            return OcrResult(lines=lines, engine=self.engine_name,
                             version=self.version)
        except Exception as exc:
            print(f"[perception.ocr] recognize failed ({exc}).")
            return OcrResult(lines=[], engine=self.engine_name,
                             version=self.version)


def _await(op):
    """Run a WinRT IAsyncOperation from a sync thread."""
    try:
        return op.get()
    except Exception:
        pass
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_wrap_async(op))
        finally:
            loop.close()
    except Exception:
        return None


async def _wrap_async(op):
    return await op


def _rgb_to_software_bitmap(rgb: np.ndarray):
    """RGB uint8 HxWx3 → SoftwareBitmap (BGRA8) for Windows.Media.Ocr."""
    try:
        from winsdk.windows.graphics.imaging import (
            BitmapBufferAccessMode, BitmapPixelFormat, SoftwareBitmap,
        )
        h, w = int(rgb.shape[0]), int(rgb.shape[1])
        if rgb.ndim != 3 or rgb.shape[2] < 3:
            return None
        # BGRA
        bgra = np.empty((h, w, 4), dtype=np.uint8)
        bgra[:, :, 0] = rgb[:, :, 2]
        bgra[:, :, 1] = rgb[:, :, 1]
        bgra[:, :, 2] = rgb[:, :, 0]
        bgra[:, :, 3] = 255
        sb = SoftwareBitmap(BitmapPixelFormat.BGRA8, w, h)
        buf = sb.lock_buffer(BitmapBufferAccessMode.WRITE)
        try:
            ref = buf.create_reference()
            # MemoryBufferByteAccess via winsdk buffer protocol.
            mv = memoryview(ref)
            flat = bgra.reshape(-1)
            n = min(len(mv), flat.nbytes)
            mv[:n] = flat.tobytes()[:n]
        finally:
            buf.close()
        return sb
    except Exception as exc:
        # Fallback: encode PNG via PIL and use BitmapDecoder (more reliable
        # across winsdk versions than lock_buffer byte poking).
        try:
            import io
            from PIL import Image
            from winsdk.windows.graphics.imaging import BitmapDecoder
            from winsdk.windows.storage.streams import (
                DataWriter, InMemoryRandomAccessStream,
            )
            bio = io.BytesIO()
            Image.fromarray(rgb).save(bio, format="PNG")
            data = bio.getvalue()
            stream = InMemoryRandomAccessStream()
            writer = DataWriter(stream)
            writer.write_bytes(list(data))  # winsdk wants a list-like of bytes
            _await(writer.store_async())
            _await(writer.flush_async())
            stream.seek(0)
            decoder = _await(BitmapDecoder.create_async(stream))
            return _await(decoder.get_software_bitmap_async())
        except Exception as exc2:
            print(f"[perception.ocr] bitmap convert failed "
                  f"({exc}; fallback {exc2}).")
            return None


def get_default_engine() -> OcrEngine:
    eng = WindowsMediaOcr()
    if eng.available():
        return eng
    return NullOcr()
