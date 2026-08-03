"""One-shot vision test — can Claude read what's in front of the camera?

    python scripts/test_vision.py                 # capture one webcam frame
    python scripts/test_vision.py notes.jpg       # or test an existing image

Prints the structured extraction (description, OCR text, objects, scene type).
Needs ANTHROPIC_API_KEY (or an `ant auth login` profile).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.services.vlm import vlm


def capture_frame() -> bytes:
    import cv2

    idx = settings.vision.camera_index
    cap = cv2.VideoCapture(idx)
    if not cap.isOpened():
        raise SystemExit(f"Could not open camera {idx}. Set QUILL_CAMERA_INDEX in .env")
    # Request the highest resolution the webcam supports — small handwriting
    # needs the pixels. The camera clamps to its real max if 1080p isn't available.
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[test] camera resolution: {w}x{h}")
    # Warm up — the first few frames are often black while the sensor adjusts.
    print("[test] warming up camera (hold your notes up to the webcam) ...")
    for _ in range(30):
        cap.read()
        time.sleep(0.05)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise SystemExit("Failed to read a frame from the camera.")
    out = Path("data") / "test_capture.jpg"
    out.parent.mkdir(exist_ok=True)
    cv2.imwrite(str(out), frame)
    print(f"[test] captured frame saved to {out}")
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return buf.tobytes()


def main(argv: list[str]) -> None:
    if len(argv) > 1:
        jpeg = Path(argv[1]).read_bytes()
        print(f"[test] analyzing image: {argv[1]}")
    else:
        jpeg = capture_frame()

    print(f"[test] sending to {settings.vision.model} ...")
    res = vlm.describe(jpeg)
    print("\n=== Claude vision result ===")
    print("description  :", res.get("description"))
    print("scene_type   :", res.get("scene_type"))
    print("people       :", res.get("people_count"))
    print("objects      :", ", ".join(res.get("objects", [])))
    print("\n--- page understanding ---")
    print("content_type :", res.get("content_type"))
    print("title        :", res.get("title") or "(none)")
    items = res.get("items") or []
    print(f"items ({len(items)}):")
    for it in items:
        print("   -", it)
    print("\n--- OCR / readable text ---")
    print(res.get("ocr_text") or "(none detected)")
    print("\n[raw]", json.dumps(res))


if __name__ == "__main__":
    main(sys.argv)
