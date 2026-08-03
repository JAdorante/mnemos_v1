"""L3 — async semantics on the existing jobs queue (Phase D).

Job kinds (registered when QUILL_PERCEPTION_L3=1):
  l3_segment      — activity_blocks from meta_events / captures
  l3_extract      — per-capture_id typed extract → KG + extractions
  l3_vlm_fallback — OCR-thin local VLM caption under spend cap
  (salience append is inline from extract / pin)

Does NOT collide with audio job kind ``extract``. When L3 is on,
``screen_extract`` must not be scheduled (see register_l3_jobs).
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.perception.redaction import TIER_EGRESS, TIER_SECRETS, redact_text, secret_kinds
from app.perception.schemas import (
    ActivityBlock, Extraction, SupervisionEvent, norm_span_key, now_ms,
)
from app.perception.store import PerceptionStore, get_pstore

# Reuse the screen-attribution prompt from the legacy miner.
from app.services.screen_extract import SOURCE, _SYSTEM


def _store(store: PerceptionStore | None = None) -> PerceptionStore:
    return store or get_pstore()


# ------------------------------ segmentation ------------------------------
def run_segment(payload: dict | None = None,
                store: PerceptionStore | None = None) -> dict:
    """Build activity_blocks from recent meta_events.

    A block ends on ≥ idle_gap of wall time between metas, or an app switch
    lasting ≥ switch_gap. Capture ids whose ts falls inside the block window
    are attached.
    """
    from app.config import settings
    cfg = settings.perception
    st = _store(store)
    idle_ms = int(cfg.l3_idle_gap_s * 1000)
    switch_ms = int(cfg.l3_switch_gap_s * 1000)
    lookback = int((payload or {}).get("lookback_ms", 24 * 3600 * 1000))
    since = now_ms() - lookback
    metas = st.recent_meta_ordered(since, limit=8000)
    if len(metas) < 2:
        return {"blocks": 0, "metas": len(metas)}

    caps = st.recent_captures(since, limit=8000)
    # oldest-first for assignment
    caps = sorted(caps, key=lambda c: int(c["ts_utc"]))

    blocks_out = 0
    cur_app = metas[0].get("app_name") or ""
    cur_start = int(metas[0]["ts_utc"])
    cur_end = cur_start
    keys = 0
    mice = 0
    switch_since: int | None = None

    def _flush(end_ts: int, app: str, k: int, m: int, start_ts: int) -> None:
        nonlocal blocks_out
        if end_ts <= start_ts:
            return
        member = [c["capture_id"] for c in caps
                  if start_ts <= int(c["ts_utc"]) < end_ts]
        intensity = (k + m) / max(1.0, (end_ts - start_ts) / 1000.0)
        block = ActivityBlock(
            ts_start=start_ts, ts_end=end_ts, dominant_app=app,
            input_intensity=round(intensity, 4), capture_ids=member,
            summary=f"{app or 'unknown'} ({len(member)} captures)")
        st.upsert_activity_block(block)
        blocks_out += 1

    for row in metas[1:]:
        ts = int(row["ts_utc"])
        app = row.get("app_name") or ""
        keys += int(row.get("key_count") or 0)
        mice += int(row.get("mouse_count") or 0)
        gap = ts - cur_end
        if gap >= idle_ms:
            _flush(cur_end, cur_app, keys, mice, cur_start)
            cur_app, cur_start, cur_end = app, ts, ts
            keys = mice = 0
            switch_since = None
            continue
        if app != cur_app:
            if switch_since is None:
                switch_since = cur_end
            if ts - switch_since >= switch_ms:
                _flush(switch_since, cur_app, keys, mice, cur_start)
                cur_app, cur_start, cur_end = app, ts, ts
                keys = mice = 0
                switch_since = None
                continue
        else:
            switch_since = None
        cur_end = ts
    _flush(cur_end, cur_app, keys, mice, cur_start)
    print(f"[perception.l3] segment: {blocks_out} block(s) from "
          f"{len(metas)} meta rows.")
    return {"blocks": blocks_out, "metas": len(metas)}


# ------------------------------ extraction --------------------------------
def _find_source_event_id(capture_id: str) -> int | None:
    """Best-effort: recent desktop.screen events whose meta.capture_id matches."""
    try:
        from app.storage import get_store
        store = get_store()
        # Scan a modest recent window — exact SQL JSON query varies by SQLite.
        rows = store.unextracted_events(limit=1, modality="vision",
                                        source=SOURCE)  # may be empty
        # Broader: query events table directly if available.
        with store._lock:
            cur = store._conn.execute(
                "SELECT id, meta FROM events WHERE source=? "
                "ORDER BY id DESC LIMIT 200", (SOURCE,))
            for r in cur.fetchall():
                try:
                    meta = json.loads(r["meta"] or "{}")
                except Exception:
                    continue
                if str(meta.get("capture_id") or "") == str(capture_id):
                    return int(r["id"])
    except Exception:
        return None
    return None


def _run_llm_extract(text: str) -> dict:
    from app.services.extractor import _SCHEMA, EXTRACTOR_MODEL
    from app.services.model_router import router
    cleaned, _ = redact_text(text, TIER_SECRETS)
    if secret_kinds(text):
        # Prefer skip over shipping secrets to any model.
        return {}
    out = router.complete_json(
        "extract", system=_SYSTEM,
        messages=[{"role": "user", "content": f"Screen text:\n\n{cleaned}"}],
        schema=_SCHEMA, max_tokens=1024, model=EXTRACTOR_MODEL)
    return out or {}


def _iter_candidates(facts: dict) -> list[tuple[str, dict, str, float]]:
    """Flatten extractor output into (type, payload, span, conf) tuples."""
    out: list[tuple[str, dict, str, float]] = []
    for key, typ in (("tasks", "task"), ("commitments", "commitment"),
                     ("claims", "claim")):
        for item in (facts.get(key) or []):
            if not isinstance(item, dict):
                continue
            span = str(item.get("source_span") or item.get("text")
                       or item.get("claim") or "")
            conf = float(item.get("confidence") or item.get("conf") or 0.5)
            out.append((typ, item, span, conf))
    for ent in (facts.get("entities") or []):
        if isinstance(ent, dict):
            span = str(ent.get("name") or ent.get("source_span") or "")
            out.append(("entity_mention", ent, span,
                        float(ent.get("confidence") or 0.5)))
    for rel in (facts.get("relations") or []):
        if isinstance(rel, dict):
            span = str(rel.get("source_span")
                       or f"{rel.get('subj')} {rel.get('pred')} {rel.get('obj')}")
            out.append(("decision" if (rel.get("pred") or "") == "decided"
                        else "entity_mention", rel, span,
                        float(rel.get("confidence") or 0.5)))
    return out


def run_extract(payload: dict | None = None,
                store: PerceptionStore | None = None) -> dict:
    """Extract one capture_id (or drain a few needing extract)."""
    st = _store(store)
    payload = payload or {}
    capture_ids = []
    if payload.get("capture_id"):
        capture_ids = [str(payload["capture_id"])]
    else:
        capture_ids = [c["capture_id"] for c in st.captures_needing_extract(
            limit=int(payload.get("limit", 5)))]
    if not capture_ids:
        return {"captures": 0, "facts": 0, "extractions": 0}

    total_facts = 0
    total_ext = 0
    for cid in capture_ids:
        if st.has_extractions(cid):
            continue
        text = st.reconstruct_text(cid) or ""
        # OCR-thin → try local VLM caption before/without the JSON extract.
        if len(text.strip()) < _vlm_thresh():
            try:
                run_vlm_fallback({"capture_id": cid}, store=st)
            except Exception as exc:
                print(f"[perception.l3] vlm_fallback skipped ({exc}).")
        if len(text.strip()) < 20:
            # Sentinel so we don't spin forever when VLM also no-ops.
            if not st.has_extractions(cid):
                st.insert_extraction(Extraction(
                    capture_id=cid, type="empty", payload_json="{}",
                    source_span="", norm_span_key="empty",
                    model="none", egress="local", ts_utc=now_ms()))
            continue
        try:
            facts = _run_llm_extract(text)
        except Exception as exc:
            print(f"[perception.l3] extract failed for {cid[:10]}… ({exc}).")
            st.add_supervision(SupervisionEvent(
                ts_utc=now_ms(), kind="extraction_reject",
                target_type="capture", target_id=cid,
                payload_json=json.dumps({"error": str(exc)[:200]})))
            continue

        anchor = _find_source_event_id(cid)
        n = 0
        if facts and anchor is not None:
            try:
                from app.services.documents import _persist_facts
                from app.storage import get_store
                n = _persist_facts(
                    get_store(), facts, anchor, text, time.time(),
                    event_source=SOURCE, window="")
            except Exception as exc:
                print(f"[perception.l3] persist_facts skipped ({exc}).")

        model = "extract"
        egress = "local"
        inserted = 0
        for typ, payload_obj, span, conf in _iter_candidates(facts):
            key = norm_span_key(span) or typ
            if st.insert_extraction(Extraction(
                    capture_id=cid, type=typ,
                    payload_json=json.dumps(payload_obj, default=str)[:4000],
                    confidence=conf, source_span=(span or "")[:1000],
                    norm_span_key=key, model=model, egress=egress,
                    ts_utc=now_ms())):
                inserted += 1
        if inserted == 0 and not facts:
            st.insert_extraction(Extraction(
                capture_id=cid, type="noop", payload_json="{}",
                norm_span_key="noop", model=model, egress=egress,
                ts_utc=now_ms()))
        # Crude salience from novel lines.
        cap = st.get_capture(cid) or {}
        score = min(1.0, float(cap.get("novel_line_count") or 0) / 40.0)
        st.add_salience(cid, score, features={"source": "l3_extract"},
                        model_version="l3-salience-v1")
        total_facts += n
        total_ext += inserted

    if total_facts:
        try:
            from app.services.worker import worker
            worker.enqueue("graph", unique=True)
        except Exception:
            pass
    print(f"[perception.l3] extract: {len(capture_ids)} capture(s), "
          f"{total_ext} extraction row(s), {total_facts} KG fact(s).")
    return {"captures": len(capture_ids), "facts": total_facts,
            "extractions": total_ext}


def _vlm_thresh() -> int:
    from app.config import settings
    return int(settings.perception.l3_vlm_ocr_chars)


# ------------------------------ VLM fallback ------------------------------
def run_vlm_fallback(payload: dict | None = None,
                     store: PerceptionStore | None = None) -> dict:
    """Local VLM caption when OCR is short and a thumb exists."""
    st = _store(store)
    cid = str((payload or {}).get("capture_id") or "")
    if not cid:
        return {"ok": False, "reason": "no_capture_id"}
    cap = st.get_capture(cid)
    if not cap:
        return {"ok": False, "reason": "missing"}
    text = st.reconstruct_text(cid)
    if len((text or "").strip()) >= _vlm_thresh():
        return {"ok": False, "reason": "ocr_sufficient"}
    thumb = cap.get("thumb_sha256")
    if not thumb:
        return {"ok": False, "reason": "no_thumb"}
    # Spend cap / local-only: escalate=False on VLM.
    try:
        from app.perception import l2_frames
        from app.perception.spend_cap import spend_cap
        path = l2_frames.path_for(thumb)
        if not path.is_file():
            return {"ok": False, "reason": "thumb_missing"}
        jpeg = path.read_bytes()
        # WebP bytes are fine for most VLM paths that accept image bytes;
        # if not, skip rather than re-encode in the hot path.
        if not spend_cap.allow("vision"):
            return {"ok": False, "reason": "budget_exhausted"}
        from app.services.vlm import vlm
        res = vlm.describe(jpeg, escalate=False,
                           context={"capture_id": cid, "source": "l3_vlm",
                                    "modality": "vision"})
        desc = str((res or {}).get("description") or "").strip()
        desc, _ = redact_text(desc, TIER_EGRESS)
        if not desc:
            return {"ok": False, "reason": "empty"}
        st.insert_extraction(Extraction(
            capture_id=cid, type="vlm_caption",
            payload_json=json.dumps({"description": desc})[:4000],
            confidence=float((res or {}).get("confidence") or 0.4),
            source_span=desc[:200], norm_span_key=norm_span_key(desc),
            model=str((res or {}).get("_provider") or "vlm"),
            egress="local", ts_utc=now_ms()))
        return {"ok": True, "caption": desc[:120]}
    except Exception as exc:
        print(f"[perception.l3] vlm_fallback failed ({exc}).")
        return {"ok": False, "reason": str(exc)}


# ------------------------------ job entrypoints ---------------------------
def job_segment(payload: dict | None = None) -> None:
    run_segment(payload)


def job_extract(payload: dict | None = None) -> None:
    run_extract(payload)


def job_vlm_fallback(payload: dict | None = None) -> None:
    run_vlm_fallback(payload)


def drain_extracts(limit: int = 20) -> dict:
    return run_extract({"limit": limit})


def register_l3_jobs(worker) -> dict:
    """Register L3 handlers. Returns which legacy paths must stay off."""
    from app.perception.export_parquet import run_export

    worker.register("l3_segment", job_segment)
    worker.register("l3_extract", job_extract)
    worker.register("l3_vlm_fallback", job_vlm_fallback)
    worker.register("perception_export", lambda p: run_export(p))
    worker.enqueue("l3_segment", unique=True)
    worker.enqueue("l3_extract", {"limit": 10})
    worker.enqueue("perception_export", unique=True)
    return {"screen_extract_disabled": True}


def should_schedule_screen_extract(*, l3_enabled: bool | None = None) -> bool:
    """False when L3 owns screen mining."""
    if l3_enabled is None:
        from app.config import settings
        l3_enabled = bool(settings.perception.l3_enabled)
    return not bool(l3_enabled)


def l3_cutover_plan(*, l3_enabled: bool, extract_on: bool) -> dict:
    """Pure wiring helper: L3 and screen_extract are mutually exclusive."""
    screen = bool(extract_on) and should_schedule_screen_extract(
        l3_enabled=l3_enabled)
    return {
        "register_l3": bool(l3_enabled),
        "register_screen_extract": screen,
        "chain_screen_extract_from_activity": screen,
        "enqueue_l3_from_captures": bool(l3_enabled),
    }


def enqueue_extract_for_event(ev, worker) -> None:
    """Bus helper: enqueue l3_extract when an L1 event carries capture_id."""
    from app.config import settings
    if not settings.perception.l3_enabled:
        return
    meta = ev.meta if isinstance(getattr(ev, "meta", None), dict) else {}
    cid = meta.get("capture_id")
    if not cid:
        return
    try:
        worker.enqueue("l3_extract", {"capture_id": str(cid)})
    except Exception as exc:
        print(f"[perception.l3] enqueue extract skipped ({exc}).")
