"""OpenCV webcam backends — platform map shared by vision + diagnose scripts.

Windows defaults to DirectShow; Linux to V4L2. Named APIs whose OpenCV cap id
is 0 (not built into this wheel) are skipped when sweeping.
"""
from __future__ import annotations

import os
import sys


def default_capture_backend(*, platform: str | None = None,
                             os_name: str | None = None) -> str:
    """QUILL_CAMERA_BACKEND default for this OS."""
    plat = sys.platform if platform is None else platform
    name = os.name if os_name is None else os_name
    if name == "nt" or plat == "win32":
        return "dshow"
    if plat.startswith("linux"):
        return "v4l2"
    return "any"


def camera_backend_hint(*, platform: str | None = None,
                        os_name: str | None = None) -> str:
    """Hint string for error text (pipe-separated backend names)."""
    plat = sys.platform if platform is None else platform
    name = os.name if os_name is None else os_name
    if name == "nt" or plat == "win32":
        return "dshow|msmf|any"
    if plat.startswith("linux"):
        return "v4l2|gstreamer|any"
    return "any"


def capture_backend_ids(cv2) -> dict[str, int]:
    """Name -> OpenCV CAP_* id. Missing attrs become 0."""
    return {
        "dshow": int(getattr(cv2, "CAP_DSHOW", 0) or 0),
        "msmf": int(getattr(cv2, "CAP_MSMF", 0) or 0),
        "v4l2": int(getattr(cv2, "CAP_V4L2", 0) or 0),
        "v4l": int(getattr(cv2, "CAP_V4L", 0) or 0),
        "gstreamer": int(getattr(cv2, "CAP_GSTREAMER", 0) or 0),
        "any": int(cv2.CAP_ANY),
    }


def platform_diag_backends(cv2, *, platform: str | None = None,
                           os_name: str | None = None) -> list[tuple[str, int]]:
    """(name, id) pairs worth sweeping on this OS. Skip named APIs with id 0."""
    plat = sys.platform if platform is None else platform
    name = os.name if os_name is None else os_name
    if name == "nt" or plat == "win32":
        names = ("dshow", "msmf", "any")
    elif plat.startswith("linux"):
        names = ("v4l2", "gstreamer", "any")
    else:
        names = ("any",)
    ids = capture_backend_ids(cv2)
    out: list[tuple[str, int]] = []
    seen: set[int] = set()
    for n in names:
        bid = ids.get(n, 0)
        if n != "any" and bid == 0:
            continue
        if bid in seen and n != "any":
            continue
        seen.add(bid)
        out.append((n, bid))
    if not out:
        out.append(("any", int(cv2.CAP_ANY)))
    return out


def open_fallback_ids(cv2, backend: str, *,
                      platform: str | None = None) -> list[int]:
    """Ordered CAP ids: configured backend, then Linux V4L2, then ANY."""
    plat = sys.platform if platform is None else platform
    ids = capture_backend_ids(cv2)
    chosen_name = (backend or "any").lower()
    chosen = ids.get(chosen_name, int(cv2.CAP_ANY))
    order: list[int] = [chosen]
    if plat.startswith("linux"):
        v4l2 = ids.get("v4l2", 0)
        if v4l2 and v4l2 not in order:
            order.append(v4l2)
    any_id = int(cv2.CAP_ANY)
    if any_id not in order:
        order.append(any_id)
    return order


def open_camera(cv2, index: int, backend: str, *,
                platform: str | None = None):
    """Open `index` trying `open_fallback_ids`. Returns the last handle even if
    it did not open (caller checks `isOpened()`)."""
    cap = None
    for bid in open_fallback_ids(cv2, backend, platform=platform):
        cap = cv2.VideoCapture(index, bid)
        if cap is not None and cap.isOpened():
            return cap
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
    return cap
