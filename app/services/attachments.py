"""Chat attachments — documents and photos the user attaches for context *and*
long-term learning.

Unlike the sticky-notes Context textarea (one-shot, not persisted), files here
are saved under data/uploads/, turned into Events, mined for facts, and indexed
into semantic memory so later chat turns can retrieve them. Provenance is always
source='chat.attach' so the Memory Console can review/reverse them.

Fact mining runs on the durable job worker's text lane (extractor.run_text_once),
not on the request and not on a fire-and-forget thread: doing it inline on
`/chat/attach` blocked uvicorn's event loop for multi-chunk PDFs, and a daemon
thread lost its work (and its errors) with the process. `GET
/events/{id}/extraction` reports when the document has been understood.

Re-attaching the same bytes returns the existing event (pipeline fix list F06)
instead of a second event and a second round of mining.
"""
from __future__ import annotations

import hashlib
import re
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
    """Best-effort semantic index so attachments (and every other producer
    that inserts directly) surface in memory search: the event row plus the
    content chunk rows that make a document findable by any passage (F02)."""
    try:
        from app.services.memory import memory
        memory.index_event(eid, ev)
    except Exception as exc:
        print(f"[attachments] index skipped ({exc}).")


def _queue_fact_mine(anchor: int, *, label: str = "attach") -> int | None:
    """Hand the saved event to the worker's text lane (F03/F07). Returns the
    lane job id, or None when extraction is off in this process."""
    try:
        from app.services.worker import nudge
        jid = nudge("extract_text")
    except Exception as exc:
        print(f"[attachments] {label} extract enqueue skipped ({exc}).")
        return None
    if jid is None:
        print(f"[attachments] {label} event {anchor} saved; extraction is off "
              "in this process, so it stays unmined.")
    return jid


def _find_duplicate(store, digest: str) -> int | None:
    """F06: an upload with these exact bytes already in memory."""
    finder = getattr(store, "find_event_by_content_sha1", None)
    if not callable(finder):
        return None
    try:
        return finder(digest, sources=(SOURCE, "onboarding.upload"))
    except Exception:
        return None


def _document_context(original_name: str, text: str) -> str:
    """The attached file as the next chat turn's SUBJECT (see
    _ingest_document for why this window has to carry the file itself)."""
    limit = settings.documents.attach_context_chars
    truncated = len(text) > limit
    snippet = text if not truncated else text[:limit] + "…"
    head = (f"[ATTACHED DOCUMENT — the user just uploaded this file and it "
            f"is the subject of their message. Answer from ITS content "
            f"below, not from retrieved memories.]\nFilename: {original_name}\n")
    # `text` is itself capped at documents.max_chars, so for a long file the
    # honest denominator is "at least this much", not len(text).
    capped_at_ingest = len(text) >= settings.documents.max_chars
    if truncated:
        total = f"{len(text):,}+" if capped_at_ingest else f"{len(text):,}"
        head += (f"Note: this is the first {limit:,} of {total} characters — "
                 f"say so if the answer needs a part you cannot see.\n")
    elif capped_at_ingest:
        head += ("Note: long file — this is the leading section, not the whole "
                 "document. Say so if the answer needs a part you cannot see.\n")
    return f"{head}\n{snippet}"


def _duplicate_result(store, event_id: int, original_name: str) -> dict:
    """The response for a re-attached file: the existing event, nothing
    re-mined, and the same context window built from what is stored."""
    row = store.get_event(int(event_id)) or {}
    raw = str(row.get("raw") or "")
    photo = str(row.get("modality") or "") == "vision"
    if photo:
        context = f"[Attached photo: {original_name}]\n{raw}".strip()
    else:
        context = _document_context(original_name, raw)
    return {
        "ok": True, "kind": "photo" if photo else "document",
        "name": original_name, "event_id": int(event_id), "duplicate": True,
        "chars": len(raw), "facts": 0, "facts_pending": False,
        "summary": str(row.get("summary") or ""), "context": context,
        "path": ((row.get("meta") or {}) if isinstance(row.get("meta"), dict)
                 else {}).get("path"),
    }


def _ingest_document(path: Path, original_name: str, data: bytes, *,
                     source: str = SOURCE, section: str = "chat.attach") -> dict:
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
    label = "upload" if source != SOURCE else "attach"
    ev = Event(
        time=now, modality=Modality.DOCUMENT, raw=text,
        summary=f"[{label}] {original_name} ({len(text)} chars)",
        source=source,
        meta={"section": section, "path": str(path), "title": original_name,
              "ext": path.suffix.lower(), "bytes": len(data),
              "content_sha1": _content_key(data)},
    )
    _conf.attach(ev, _conf.OBSERVED, capture=1.0)
    anchor = store.insert(ev)
    _index_event(anchor, ev)

    # Do not block the HTTP response on N LLM extract calls (one per chunk):
    # the worker's text lane mines it, durably, on its next cycle.
    job_id = _queue_fact_mine(anchor)

    # Text for the current chat turn. Fact mining (above) reads the whole
    # document in the background, but the turn right after the upload — "what
    # is this about?" — is answered from THIS window only, so it has to
    # carry enough of the file to answer from. Labelled as the turn's
    # SUBJECT, not a sticky note: retrieval also runs on the goal and returns
    # unrelated memories, and an unlabelled excerpt loses to them.
    return {
        "ok": True, "kind": "document", "name": original_name,
        "path": str(path), "event_id": anchor, "chars": len(text),
        "facts": 0, "facts_pending": job_id is not None, "job_id": job_id,
        "summary": ev.summary,
        "context": _document_context(original_name, text),
    }


def _ingest_image(path: Path, original_name: str, data: bytes, *,
                  source: str = SOURCE, section: str = "chat.attach") -> dict:
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
            context={"frame_path": str(path), "source": source, "modality": "vision"},
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
        source=source,
        entities=list(vision_meta.get("objects") or []),
        meta={"section": section, "frame_path": str(path),
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

    # Mine OCR / description off the request path (same hang risk as PDFs):
    # the text lane picks the photo up by its source.
    facts_pending = False
    job_id = None
    mine = "\n\n".join(p for p in (ocr, description) if p).strip()
    if mine and len(mine) > 20:
        job_id = _queue_fact_mine(anchor, label="attach-photo")
        facts_pending = job_id is not None

    ctx_bits = [f"[Attached photo: {original_name}]"]
    if description:
        ctx_bits.append(description)
    if ocr:
        ctx_bits.append("Visible text:\n" + (ocr if len(ocr) <= 3000 else ocr[:3000] + "…"))
    return {
        "ok": True, "kind": "photo", "name": original_name,
        "path": str(path), "event_id": anchor, "facts": 0,
        "facts_pending": facts_pending, "job_id": job_id,
        "summary": summary, "description": description,
        "context": "\n".join(ctx_bits),
    }


def ingest_bytes(filename: str, data: bytes, *,
                 source: str = SOURCE,
                 section: str | None = None,
                 allow_images: bool = True) -> dict:
    """Save one uploaded file and ingest it into memory for learning.

    Returns a result dict with ok/kind/name/context (for the chat turn) and
    event_id/facts when successful.

    `source` / `section` default to chat.attach; onboarding upload passes
    onboarding.upload so Memory can review/reverse those separately.
    `allow_images=False` keeps hosted setup on documents only (no VLM photos).
    """
    if not data:
        return {"ok": False, "error": "empty file", "name": filename}
    if len(data) > _MAX_UPLOAD_BYTES:
        return {"ok": False, "error": f"file too large (max {_MAX_UPLOAD_BYTES} bytes)",
                "name": filename}

    original = _safe_name(filename)
    ext = Path(original).suffix.lower()
    allowed = set(_DOC_EXTS)
    if allow_images:
        allowed |= _IMAGE_EXTS
    if ext not in allowed:
        return {"ok": False, "error": f"unsupported type {ext or '(none)'}; "
                f"allowed: {', '.join(sorted(allowed))}", "name": original}

    # F06: the same bytes again (re-attached, or attached under a new name)
    # are the memory already made from them — no second event, no second
    # round of mining, no second copy on disk.
    full_digest = _content_key(data)
    try:
        from app.storage import get_store
        dup = _find_duplicate(get_store(), full_digest)
        if dup:
            return _duplicate_result(get_store(), dup, original)
    except Exception as exc:
        print(f"[attachments] duplicate probe skipped ({exc}).")

    # Content-addressed filename keeps duplicates from stacking; still unique
    # enough via short uuid prefix if the same bytes arrive under a new name.
    digest = full_digest[:12]
    stored = f"{int(time.time())}_{digest}_{original}"
    path = upload_dir() / stored
    try:
        path.write_bytes(data)
    except Exception as exc:
        return {"ok": False, "error": f"save failed: {exc}", "name": original}

    sec = section or source
    try:
        if ext in _IMAGE_EXTS:
            return _ingest_image(path, original, data, source=source, section=sec)
        return _ingest_document(path, original, data, source=source, section=sec)
    except Exception as exc:
        print(f"[attachments] ingest failed for {original}: {exc}")
        return {"ok": False, "error": str(exc), "name": original, "path": str(path)}


def allowed_accept() -> str:
    """HTML <input accept=...> value for the chat file picker."""
    return ",".join(sorted(_DOC_EXTS | _IMAGE_EXTS))


def allowed_doc_accept() -> str:
    """HTML accept= for document-only pickers (hosted onboarding upload)."""
    return ",".join(sorted(_DOC_EXTS))
