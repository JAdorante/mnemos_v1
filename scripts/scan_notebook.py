"""Scan a notebook page with the webcam and run it through the understand->act
pipeline (real VLM read -> structured, routed actions).

    python scripts/scan_notebook.py

Captures one frame from the configured webcam (V4L2 on Linux, DirectShow on
Windows — same open path as the vision pipeline), saves it to
data/notebook_scan.jpg so you can see exactly what it read, then prints the
structured actions + the chat offer it would surface. Nothing is executed or
sent — this is read + decide only.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

import cv2

from app.config import settings
from app.services.camera import open_camera
from app.services.notebook import process_notebook, offer_text


def _page_score(gray) -> float:
    """How much this frame looks like a held-up page of text: lots of edges
    (handwriting) AND sharp (not motion-blurred). A blank wall or a face scores
    far lower than a page covered in writing."""
    edges = cv2.Canny(gray, 60, 160)
    edge_density = float((edges > 0).mean())          # fraction of edge pixels
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return edge_density * min(sharp, 400.0)


def _capture(seconds: float = 8.0) -> bytes | None:
    """Burst-capture for a few seconds and keep the best 'page of text' frame, so
    it doesn't depend on grabbing at exactly the right instant."""
    cfg = settings.vision
    cap = open_camera(cv2, cfg.camera_index, cfg.capture_backend)
    if cap is None or not cap.isOpened():
        return None
    try:
        cap.set(cv2.CAP_PROP_CONVERT_RGB, 1.0)
        fourcc = (cfg.capture_fourcc or "").strip()
        if fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc[:4]))
        if cfg.capture_width and cfg.capture_height:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.capture_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.capture_height)
        for _ in range(15):          # warm up / auto-expose
            cap.read()
            time.sleep(0.03)
        print(f"[scan] hold the page up — capturing best frame over {seconds:.0f}s...")
        best, best_score, n = None, -1.0, 0
        t0 = time.time()
        while time.time() - t0 < seconds:
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.03)
                continue
            n += 1
            score = _page_score(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            if score > best_score:
                best_score, best = score, frame
            time.sleep(0.04)
        if best is None:
            return None
        out = ROOT / "data" / "notebook_scan.jpg"
        cv2.imwrite(str(out), best)
        print(f"[scan] picked best of {n} frames (score={best_score:.1f}) -> {out}")
        ok, buf = cv2.imencode(".jpg", best)
        return buf.tobytes() if ok else None
    finally:
        cap.release()


def main() -> None:
    print("[scan] capturing from the webcam...")
    jpg = _capture()
    if jpg is None:
        print("[scan] could not capture a frame (camera busy or unavailable).")
        return

    res = process_notebook(image_bytes=jpg)
    if res.get("error"):
        print("[scan] error:", res["error"])
        return

    if res.get("preprocessed_jpg"):
        pre = ROOT / "data" / "notebook_pre.jpg"
        pre.write_bytes(res["preprocessed_jpg"])
        print(f"[scan] preprocessed page -> {pre}")

    print("\n=== WHAT THE VLM READ (verbatim) ===")
    print(res.get("read_text") or "(nothing legible)")
    print("\nPAGE TYPE:", res.get("page_type"))

    actions = res.get("actions") or []
    if not actions:
        print("\n(no actionable items found)")
    for i, a in enumerate(actions, 1):
        print(f"\n=== ACTION {i} ===")
        for k in ("kind", "recipient", "subject", "body", "goal",
                  "surface", "risk_level", "approval_required", "actionable"):
            if a.get(k) not in (None, ""):
                print(f"  {k:16}: {a[k]}")
        print("  CHAT OFFER ->", offer_text(a))

    if res.get("notes_summary"):
        print("\n=== NOTES SUMMARY ===")
        print(res["notes_summary"])


if __name__ == "__main__":
    main()
