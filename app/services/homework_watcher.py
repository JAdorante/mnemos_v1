"""Proactive homework watcher — screen problem → chat yes/no offer.

When the sticky study mode is *Homework help* and vision (webcam or desktop
screen) looks like a problem set / quiz / calculation / homework form, offer
in chat to start tutoring. Debounced. Does not inject UI into the browser tab.

Disable with QUILL_HOMEWORK_WATCH=0 (or QUILL_AGENT=0).
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

from app.events import Modality

_recent_offer: dict[str, float] = {}
_lock = threading.Lock()
_COOLDOWN_S = 300  # same problem signature within 5 min

_HOMEWORK_TYPES = frozenset({
    "questions", "calculation", "form",
})

# Soft signals when VLM says mixed/none but the page still looks like homework.
_HW_MARKERS = re.compile(
    r"\b(problem\s*\d|part\s*\([a-d]\)|webassign|chegg|homework|quiz\b|"
    r"worksheet|exercise\s*\d|find the (time|position|velocity)|"
    r"solve for|show that|calculate|determine)\b",
    re.I,
)
_SELF_WINDOW = re.compile(
    r"mnemos(?:\.ai)?|memory console|exec\.ai|/onboarding|nexus_v1\s*-\s*cursor|"
    r"/ui\b|localhost:\d+",
    re.I,
)
_SCHEMA_JUNK = frozenset({
    "description", "ocr_text", "content_type", "title", "items", "mixed", "none",
})


def _enabled() -> bool:
    return (
        os.environ.get("QUILL_HOMEWORK_WATCH", "1") not in ("0", "false", "False")
        and os.environ.get("QUILL_AGENT") not in ("0", "false", "False")
    )


def _study_is_homework() -> bool:
    try:
        from app.services import agent_chat_mode as _smode
        return _smode.current().get("id") == "homework"
    except Exception:
        return False


def _hash(key: str) -> str:
    return hashlib.sha1((key or "").strip().lower().encode()).hexdigest()


def _is_self_ui(ev) -> bool:
    meta = ev.meta or {}
    win = str(meta.get("window") or "")
    if _SELF_WINDOW.search(win):
        return True
    vision = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    blob = " ".join([
        str(vision.get("title") or ""),
        str(vision.get("ocr_text") or "")[:300],
        str(ev.summary or "")[:300],
    ]).lower()
    if "mnemos" in blob and ("homework help" in blob or "study mode" in blob):
        return True
    return False


def _snippet(ocr: str, limit: int = 900) -> str:
    text = re.sub(r"[ \t]+", " ", (ocr or "").strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _homework_payload(ev) -> dict | None:
    """Pull a homework-problem signature from a VISION event, or None."""
    if _is_self_ui(ev):
        return None

    meta = ev.meta or {}
    vision = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    ctype = (meta.get("content_type") or vision.get("content_type") or "none")
    ctype = str(ctype).strip().lower()
    title = (vision.get("title") or "").strip()
    ocr = (vision.get("ocr_text") or ev.raw or "") or ""
    summary = ev.summary or ""
    window = str(meta.get("window") or "")
    items = [str(x).strip() for x in (meta.get("items") or vision.get("items") or [])
             if str(x).strip() and str(x).strip().lower() not in _SCHEMA_JUNK]

    from app.services.surface_filters import is_log_or_cli_surface
    if is_log_or_cli_surface(window, title, ocr, summary):
        return None

    explicit = ctype in _HOMEWORK_TYPES
    soft = (not explicit) and bool(
        _HW_MARKERS.search(f"{title}\n{ocr[:500]}\n{summary}")
    )
    if not explicit and not soft:
        return None

    # Soft matches need a bit of substance so random pages don't fire.
    body = _snippet(ocr) or _snippet(summary, 400)
    if not body and not items:
        return None
    if soft and len(body) < 40 and not items:
        return None

    label = title or (items[0][:80] if items else "")
    if not label:
        # First non-empty OCR line as a short label.
        for line in (ocr or "").splitlines():
            line = line.strip()
            if len(line) >= 8:
                label = line[:80]
                break
    if not label:
        label = "Homework problem"

    return {
        "title": label,
        "content_type": ctype if explicit else "inferred",
        "ocr": body,
        "items": items[:8],
        "window": window,
        "frame_path": meta.get("frame_path"),
        "source": getattr(ev, "source", "") or "",
        "inferred": soft and not explicit,
    }


def _on_event(ev) -> None:
    try:
        if getattr(ev, "modality", None) != Modality.VISION:
            return
        if not _enabled():
            return
        if not _study_is_homework():
            return

        payload = _homework_payload(ev)
        if not payload:
            return

        now = time.time()
        # Signature: window + title + head of OCR (stable across minor OCR jitter).
        sig = _hash(
            f"{payload.get('window')}|{payload.get('title')}|"
            f"{(payload.get('ocr') or '')[:240]}"
        )
        with _lock:
            last = _recent_offer.get(sig)
            if last is not None and now - last < _COOLDOWN_S:
                return
            _recent_offer[sig] = now

        from app.services.agent_bridge import worker

        offered = worker.propose_homework(
            title=payload["title"],
            ocr=payload.get("ocr") or "",
            items=payload.get("items") or [],
            content_type=payload.get("content_type") or "",
            window=payload.get("window") or "",
            frame_path=payload.get("frame_path"),
            event_time=getattr(ev, "time", None),
        )
        if offered:
            print(
                f"[homework] offered help for “{(payload.get('title') or '')[:60]}” "
                f"({payload.get('content_type')}"
                f"{' inferred' if payload.get('inferred') else ''}) — reply yes/no."
            )
        else:
            print(
                f"[homework] queued help offer for "
                f"“{(payload.get('title') or '')[:60]}”."
            )
    except Exception as exc:
        print(f"[homework] watcher error: {exc}")


def attach() -> None:
    from app.events import bus

    bus.subscribe(_on_event)
    print("[homework] watching for homework pages while study mode is Homework help.")
