"""Chat attachments — documents and photos the user attaches for context *and*
long-term learning.

Unlike the sticky-notes Context textarea (one-shot, not persisted), files here
are saved under data/uploads/, turned into Events, mined for facts, and indexed
into semantic memory so later chat turns can retrieve them. Provenance is always
source='chat.attach' so the Memory Console can review/reverse them.

Fact mining (Claude / local extract) is deferred to a background thread: doing it
inline on `/chat/attach` blocked uvicorn's event loop for multi-chunk PDFs and
froze the live run (audio/UI/API) until the upload finished or timed out.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

from app.config import settings

SOURCE = "chat.attach"

_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})
_DOC_EXTS = settings.documents.exts | frozenset({".rst", ".text", ".log"})
# Hard ceiling for a single chat upload (env can tighten documents.max_bytes).
_MAX_UPLOAD_BYTES = max(int(settings.documents.max_bytes), 8_000_000)


def upload_dir() -> Path:
    p = Path(settings.storage.data_dir) / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_name(name: str) -> str:
    base = Path(name or "file").name
    base = re.sub(r"[^\w.\- ()\[\]]+", "_", base).strip(" ._") or "file"
    return base[:180]


def _content_key(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _index_event(eid: int, ev) -> None:
    """Best-effort semantic index so attachments surface in memory search."""
    try:
        from app.services.memory import memory
        vectors = memory._ensure_vectors()
        if not vectors:
            return
        text = (ev.summary or ev.raw or "").strip()
        if not text:
            return
        vectors.add(eid, ev.time, ev.modality.value, text, memory._embed(text))
        with memory._lock:
            memory._events.append(ev)
    except Exception as exc:
        print(f"[attachments] index skipped ({exc}).")


def _mine_document_facts(original_name: str, text: str, anchor: int,
                         now: float) -> None:
    """LLM fact extract for an already-saved attach event (background only)."""
    from app.services.documents import _persist_facts, chunk_text
    from app.services.extractor import extractor
    from app.storage import get_store

    store = get_store()
    facts_n = 0
    for ch in chunk_text(text):
        try:
            facts = extractor._extract_text(ch)
        except Exception as exc:
            print(f"[attachments] extract error on {original_name} ({exc}).")
            continue
        facts_n += _persist_facts(store, facts, anchor, ch, now)
    try:
        store.mark_extracted([anchor], now)
    except Exception:
        pass
    print(f"[attachments] mined {facts_n} fact(s) from {original_name} "
          f"(event {anchor}).")


def _schedule_fact_mine(fn, *args, label: str = "attach") -> None:
    t = threading.Thread(target=fn, args=args, daemon=True,
                         name=f"{label}-extract")
    t.start()


def _ingest_document(path: Path, original_name: str, data: bytes) -> dict:
    from app.events import Event, Modality
    from app.services import confidence as _conf
    from app.services.documents import extract_text
    from app.storage import get_store

    text = extract_text(path)
    if not text:
        # Fallback: treat as UTF-8 if the typed reader failed (e.g. odd .txt).
        try:
            text = data.decode("utf-8", errors="ignore")[: settings.documents.max_chars].strip()
        except Exception:
            text = ""
    if not text:
        return {"ok": False, "error": "could not read text from document",
                "name": original_name, "kind": "document"}

    store = get_store()
    now = time.time()
    ev = Event(
        time=now, modality=Modality.DOCUMENT, raw=text,
        summary=f"[attach] {original_name} ({len(text)} chars)",
        source=SOURCE,
        meta={"section": "chat.attach", "path": str(path), "title": original_name,
              "ext": path.suffix.lower(), "bytes": len(data),
              "content_sha1": _content_key(data)},
    )
    _conf.attach(ev, _conf.OBSERVED, capture=1.0)
    anchor = store.insert(ev)
    _index_event(anchor, ev)

    # Do not block the HTTP response on N LLM extract calls (one per chunk).
    _schedule_fact_mine(_mine_document_facts, original_name, text, anchor, now)

    # Snippet for the current chat turn's sticky context.
    snippet = text if len(text) <= 4000 else text[:4000] + "…"
    return {
        "ok": True, "kind": "document", "name": original_name,
        "path": str(path), "event_id": anchor, "chars": len(text),
        "facts": 0, "facts_pending": True,
        "summary": ev.summary,
        "context": f"[Attached document: {original_name}]\n{snippet}",
    }


def _ingest_image(path: Path, original_name: str, data: bytes) -> dict:
    from app.events import Event, Modality
    from app.services import confidence as _conf
    from app.storage import get_store

    # Prefer JPEG bytes for the VLM when possible; pass raw otherwise.
    jpeg = data
    if path.suffix.lower() not in (".jpg", ".jpeg"):
        try:
            import cv2
            import numpy as np
            arr = np.frombuffer(data, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
                if ok:
                    jpeg = buf.tobytes()
        except Exception:
            pass

    description = ""
    ocr = ""
    vision_meta: dict = {}
    model_conf = None
    try:
        from app.services.vlm import vlm
        res = vlm.describe(
            jpeg,
            context={"frame_path": str(path), "source": SOURCE, "modality": "vision"},
        ) or {}
        description = (res.get("description") or "").strip()
        ocr = (res.get("ocr_text") or "").strip()
        vision_meta = res
        model_conf = res.get("confidence")
    except Exception as exc:
        print(f"[attachments] VLM describe failed ({exc}).")

    if not description and not ocr:
        description = f"User-attached photo: {original_name}"

    raw_parts = [p for p in (description, ocr) if p]
    raw = "\n\n".join(raw_parts)
    summary = f"[attach photo] {original_name}: {description or '(no description)'}"

    store = get_store()
    now = time.time()
    ev = Event(
        time=now, modality=Modality.VISION, raw=raw, summary=summary,
        source=SOURCE,
        entities=list(vision_meta.get("objects") or []),
        meta={"section": "chat.attach", "frame_path": str(path),
              "title": original_name, "ext": path.suffix.lower(),
              "bytes": len(data), "content_sha1": _content_key(data),
              "vision": vision_meta,
              "content_type": vision_meta.get("content_type") or "none",
              "items": vision_meta.get("items") or []},
    )
    _conf.attach(
        ev, _conf.EXTRACTED, capture=1.0,
        model=(float(model_conf) if isinstance(model_conf, (int, float)) else None),
    )
    anchor = store.insert(ev)
    _index_event(anchor, ev)

    # Mine OCR / description off the request path (same hang risk as PDFs).
    facts_pending = False
    mine = "\n\n".join(p for p in (ocr, description) if p).strip()
    if mine and len(mine) > 20:
        facts_pending = True
        _schedule_fact_mine(_mine_document_facts, original_name, mine, anchor, now,
                            label="attach-photo")

    ctx_bits = [f"[Attached photo: {original_name}]"]
    if description:
        ctx_bits.append(description)
    if ocr:
        ctx_bits.append("Visible text:\n" + (ocr if len(ocr) <= 3000 else ocr[:3000] + "…"))
    return {
        "ok": True, "kind": "photo", "name": original_name,
        "path": str(path), "event_id": anchor, "facts": 0,
        "facts_pending": facts_pending,
        "summary": summary, "description": description,
        "context": "\n".join(ctx_bits),
    }


def ingest_bytes(filename: str, data: bytes) -> dict:
    """Save one uploaded file and ingest it into memory for learning.

    Returns a result dict with ok/kind/name/context (for the chat turn) and
    event_id/facts when successful.
    """
    if not data:
        return {"ok": False, "error": "empty file", "name": filename}
    if len(data) > _MAX_UPLOAD_BYTES:
        return {"ok": False, "error": f"file too large (max {_MAX_UPLOAD_BYTES} bytes)",
                "name": filename}

    original = _safe_name(filename)
    ext = Path(original).suffix.lower()
    if ext not in _DOC_EXTS and ext not in _IMAGE_EXTS:
        allowed = ", ".join(sorted(_DOC_EXTS | _IMAGE_EXTS))
        return {"ok": False, "error": f"unsupported type {ext or '(none)'}; "
                f"allowed: {allowed}", "name": original}

    # Content-addressed filename keeps duplicates from stacking; still unique
    # enough via short uuid prefix if the same bytes arrive under a new name.
    digest = _content_key(data)[:12]
    stored = f"{int(time.time())}_{digest}_{original}"
    path = upload_dir() / stored
    try:
        path.write_bytes(data)
    except Exception as exc:
        return {"ok": False, "error": f"save failed: {exc}", "name": original}

    try:
        if ext in _IMAGE_EXTS:
            return _ingest_image(path, original, data)
        return _ingest_document(path, original, data)
    except Exception as exc:
        print(f"[attachments] ingest failed for {original}: {exc}")
        return {"ok": False, "error": str(exc), "name": original, "path": str(path)}


def allowed_accept() -> str:
    """HTML <input accept=...> value for the chat file picker."""
    return ",".join(sorted(_DOC_EXTS | _IMAGE_EXTS))
