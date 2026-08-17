"""Webcam diagnostic — find a backend + pixel-format combo that gives a real
image (not black, not colored-noise).

    python scripts/diagnose_camera.py            # sweep camera index 0
    python scripts/diagnose_camera.py 1          # sweep camera index 1

For each (backend x FOURCC) combo it opens the camera, warms it up, grabs a
frame, saves it to data/cam_diag/, and prints the resolution + brightness/detail
stats. Then OPEN THAT FOLDER and see which file shows you (not static). Whatever
backend+fourcc produced it, set:

    QUILL_CAMERA_BACKEND=<backend>   QUILL_CAMERA_FOURCC=<fourcc>

in your .env (fourcc can be empty). 'noise?' flags a frame that statistically
looks like random static rather than a scene.

Linux sweeps v4l2 / gstreamer / any. Windows sweeps dshow / msmf / any.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import cv2
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"OpenCV not available: {exc}")

from app.services.camera import platform_diag_backends


def _looks_like_noise(frame) -> bool:
    """Random static has almost no correlation between neighboring pixels; a real
    scene does. Compare the frame's detail to a 2x-downsampled-then-upsampled
    version: for noise the two differ a lot; for a real image they're close."""
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    small = cv2.resize(g, (g.shape[1] // 2, g.shape[0] // 2))
    blur = cv2.resize(small, (g.shape[1], g.shape[0]))
    hi_freq = float(np.mean(np.abs(g - blur)))     # energy lost to downsampling
    return hi_freq > 18.0 and float(g.std()) > 40  # lots of pixel-scale detail


def _try(index, backend_name, backend_id, fourcc, out_dir):
    cap = cv2.VideoCapture(index, backend_id)
    if not cap.isOpened():
        return f"  {backend_name:10} {fourcc or '(none)':6}  -> could not open"
    try:
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 1.0)
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc[:4]))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        for _ in range(15):          # warm up / let format settle
            cap.read()
            time.sleep(0.03)
        ok, frame = cap.read()
        if not ok or frame is None:
            return f"  {backend_name:10} {fourcc or '(none)':6}  -> opened but no frame"
        h, w = frame.shape[:2]
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean, std = float(g.mean()), float(g.std())
        noise = _looks_like_noise(frame)
        fn = out_dir / f"{backend_name}_{fourcc or 'none'}.jpg"
        cv2.imwrite(str(fn), frame)
        verdict = ("DARK" if mean < 8 else "noise?" if noise else "looks OK")
        return (f"  {backend_name:10} {fourcc or '(none)':6}  -> {w}x{h}  "
                f"mean={mean:5.1f} std={std:5.1f}  [{verdict}]  saved {fn.name}")
    finally:
        cap.release()


def main(argv):
    index = int(argv[1]) if len(argv) > 1 else 0
    out_dir = Path("data") / "cam_diag"
    out_dir.mkdir(parents=True, exist_ok=True)
    backends = platform_diag_backends(cv2)
    # Empty FOURCC first on Linux (leave the driver format alone). Windows
    # still prefers MJPG to avoid the green/static stride mismatch.
    fourccs = ["", "MJPG", "YUY2"] if sys.platform.startswith("linux") else [
        "MJPG", "YUY2", ""]
    print(f"[diag] sweeping camera index {index}. Saving frames to {out_dir}/\n")
    for bname, bid in backends:
        for fcc in fourccs:
            print(_try(index, bname, bid, fcc, out_dir))
    print(f"\n[diag] Open {out_dir}/ and see which .jpg shows a real image.")
    print("[diag] Then set QUILL_CAMERA_BACKEND / QUILL_CAMERA_FOURCC in .env to match.")
    print("[diag] If every frame is static, try a different index (e.g. "
          "`python scripts/diagnose_camera.py 1`).")


if __name__ == "__main__":
    main(sys.argv)
