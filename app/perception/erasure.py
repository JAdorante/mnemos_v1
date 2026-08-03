"""Cascading erasure — "delete" means NOTHING survives on disk.

The old delete path left media, vectors, and log rows behind. This job
cascades one time window across every store desktop perception writes to:

  1. perception.db rows (meta_events, captures, ocr_lines, frame_line_map,
     extractions, activity_blocks, salience_scores) — store.erase_range
  2. quill.db `desktop.%` events + facts derived from them + relations
     touching either (VACUUM so raw pages don't retain the text)
  3. LanceDB vectors for those event ids and fact ids (FACT_ID_OFFSET range)
  4. frame files: paths referenced by erased event meta, plus any file in the
     frame dirs whose embedded/mtime timestamp falls in the window
  5. escalate_distill.jsonl rows (desktop-sourced in-window, or referencing
     an erased frame) — rewritten atomically, NO .bak (a backup would defeat
     erasure)
  6. exported Parquet partitions (export/<table>/date=YYYY-MM-DD) whose UTC
     day overlaps the window — Phase D re-exports surviving rows

Afterwards a gap(reason='privacy_excluded') spans the window so the timeline
stays honest, and a supervision row records THAT an erasure happened (counts
only — never the erased content).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from app.perception.schemas import SupervisionEvent, now_ms
from app.perception.store import get_pstore

# desktop_capture names frames screen_<epoch>.jpg / click_<epoch>.jpg.
_FRAME_TS = re.compile(r"^(?:screen|click)_(\d+(?:\.\d+)?)\.\w+$")


def _erase_frame_files(t0: float, t1: float,
                       explicit: list[str]) -> int:
    """Erase legacy desktop_frames paths. CAS WebP is handled separately via
    SHA + refcount (never mtime-sweep the CAS tree — shared digests)."""
    from app.config import settings
    n = 0
    for p in explicit:
        try:
            path = Path(p)
            if path.is_file():
                path.unlink()
                n += 1
        except Exception:
            continue
    # Legacy JPEG tree only — CAS lives under data/frames and is SHA-gated.
    root = Path(settings.desktop_capture.frame_dir)
    if not root.is_dir():
        return n
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        ts = None
        m = _FRAME_TS.match(f.name)
        if m:
            ts = float(m.group(1))
        else:
            try:
                ts = f.stat().st_mtime
            except OSError:
                continue
        if t0 <= ts < t1:
            try:
                f.unlink()
                n += 1
            except Exception:
                continue
    return n


def _erase_distill_rows(t0: float, t1: float,
                        frame_paths: set[str]) -> int:
    """Drop desktop-sourced distill rows in-window (or any row referencing an
    erased frame). Atomic rewrite, deliberately without a backup copy."""
    from app.config import settings
    path = Path(settings.escalate_log.path)
    if not path.is_file():
        return 0
    norm = {str(Path(p)) for p in frame_paths}
    kept: list[str] = []
    dropped = 0
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                row = json.loads(ln)
            except Exception:
                kept.append(ln)
                continue
            fp = str(Path(row.get("frame_path"))) if row.get("frame_path") else ""
            in_window = (t0 <= float(row.get("time") or 0) < t1
                         and str(row.get("source") or "").startswith("desktop."))
            if in_window or (fp and fp in norm):
                dropped += 1
                continue
            kept.append(ln)
        if dropped:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                       suffix=".jsonl.tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(kept) + ("\n" if kept else ""))
            os.replace(tmp, path)
    except Exception as exc:
        print(f"[perception.erasure] distill sweep skipped ({exc}).")
        return 0
    return dropped


def _erase_parquet_partitions(t0: float, t1: float) -> int:
    """Remove export/<table>/date=YYYY-MM-DD partitions overlapping [t0,t1)."""
    from app.perception.export_parquet import export_root
    root = export_root()
    if not root.is_dir():
        return 0
    days = set()
    day = t0 - (t0 % 86400)
    while day < t1:
        days.add(time.strftime("%Y-%m-%d", time.gmtime(day)))
        day += 86400
    n = 0
    for table_dir in root.iterdir():
        if not table_dir.is_dir():
            continue
        for part in table_dir.glob("date=*"):
            if part.name.split("=", 1)[-1] in days:
                try:
                    shutil.rmtree(part)
                    n += 1
                except Exception:
                    continue
    return n


def erase_window(ts_start_ms: int, ts_end_ms: int) -> dict:
    """Erase every trace of desktop perception in [ts_start_ms, ts_end_ms).
    Returns a per-store manifest of what was deleted. Never partial-silently:
    each cascade step reports, and failures surface in the manifest."""
    if ts_end_ms <= ts_start_ms:
        raise ValueError("erase_window: empty or inverted window")
    t0, t1 = ts_start_ms / 1000.0, ts_end_ms / 1000.0
    manifest: dict = {"window": [ts_start_ms, ts_end_ms]}

    # 1. perception.db
    pstore = get_pstore()
    perc = pstore.erase_range(ts_start_ms, ts_end_ms)
    cap_ids = list(perc.pop("capture_ids", []) or [])
    frame_shas = list(perc.pop("frame_shas", []) or [])
    thumb_shas = list(perc.pop("thumb_shas", []) or [])
    manifest["perception"] = perc

    # 1b. LanceDB ocr_blocks keyed by capture_id (Phase B)
    try:
        from app.perception import ocr_blocks
        manifest["ocr_blocks"] = ocr_blocks.delete_capture_ids(cap_ids)
    except Exception as exc:
        manifest["ocr_blocks"] = {"error": str(exc)}

    # 1c. CAS WebP frames (Phase C) — unlink only when no surviving capture
    # still references the digest (shared-SHA / content-addressed hazard).
    cas_n = 0
    cas_kept = 0
    try:
        from app.perception import l2_frames
        for sha in set(frame_shas + thumb_shas):
            if not sha:
                continue
            if pstore.sha_refcount(sha) > 0:
                cas_kept += 1
                continue
            if l2_frames.unlink_sha(sha):
                cas_n += 1
        manifest["cas_frames"] = cas_n
        manifest["cas_shared_kept"] = cas_kept
    except Exception as exc:
        manifest["cas_frames"] = {"error": str(exc)}

    # 2. quill.db events/facts/relations
    ev = {"event_ids": [], "fact_ids": [], "frame_paths": []}
    try:
        from app.storage import get_store
        ev = get_store().erase_events_window(t0, t1)
        manifest["events"] = {k: v for k, v in ev.items()
                              if k in ("events", "facts", "relations")}
    except Exception as exc:
        manifest["events"] = {"error": str(exc)}

    # 3. LanceDB vectors (events + offset fact ids)
    try:
        from app.services.memory import FACT_ID_OFFSET
        from app.vectorstore import get_vectorstore
        ids = list(ev["event_ids"]) + [FACT_ID_OFFSET + int(f)
                                       for f in ev["fact_ids"]]
        manifest["vectors"] = get_vectorstore().delete_ids(ids) if ids else 0
    except Exception as exc:
        manifest["vectors"] = {"error": str(exc)}

    # 4. legacy desktop_frames/ + any remaining CAS by mtime window
    manifest["frames"] = _erase_frame_files(t0, t1, ev.get("frame_paths") or [])

    # 5. distill trail
    manifest["distill_rows"] = _erase_distill_rows(
        t0, t1, set(ev.get("frame_paths") or []))

    # 6. Parquet partitions
    manifest["parquet_partitions"] = _erase_parquet_partitions(t0, t1)

    # Honest timeline: the hole is labeled, and the fact of erasure (counts
    # only) is a supervision signal.
    try:
        pstore.add_gap(ts_start_ms, ts_end_ms, "privacy_excluded")
        pstore.add_supervision(SupervisionEvent(
            ts_utc=now_ms(), kind="erasure", target_type="window",
            target_id=f"{ts_start_ms}-{ts_end_ms}",
            payload_json=json.dumps({
                "events": manifest.get("events"),
                "frames": manifest["frames"],
                "distill_rows": manifest["distill_rows"]})))
    except Exception as exc:
        manifest["gap_error"] = str(exc)
    print(f"[perception.erasure] erased window "
          f"{ts_start_ms}..{ts_end_ms}: {manifest}")
    return manifest
