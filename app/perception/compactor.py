"""L2 disk compactor — age tiers + budget, pixels before text.

Degradation order (correctness criterion 8):
  1. Unpromoted full frames older than full_ttl_h (default 72h)
  2. Thumbnails older than thumb_ttl_d (default 30d)
  3. While frames-dir size exceeds disk budget: oldest unpromoted full,
     then oldest thumbs
  Never touches ocr_lines / FTS / ocr_blocks / meta_events / supervision.

Promoted captures keep their full frame across the age pass (and are skipped
in the budget full-drop pass until unpinned).

CAS digests are shared across identical captures: clear the row first, then
unlink the file only when ``sha_refcount`` hits zero.
"""
from __future__ import annotations

from app.perception import l2_frames
from app.perception.store import PerceptionStore, get_pstore


def _release_sha(st: PerceptionStore, sha: str | None, root) -> int:
    """Unlink CAS file iff no capture still references it. Returns bytes freed."""
    if not sha:
        return 0
    if st.sha_refcount(sha) > 0:
        return 0
    before = _file_size(sha, root)
    if l2_frames.unlink_sha(sha, root):
        return before
    return 0


def compact(store: PerceptionStore | None = None,
            *, root=None, now_ms: int | None = None,
            full_ttl_h: float | None = None,
            thumb_ttl_d: float | None = None,
            budget_bytes: int | None = None) -> dict:
    from app.config import settings
    from app.perception.schemas import now_ms as _now

    cfg = settings.perception
    st = store or get_pstore()
    now = int(now_ms if now_ms is not None else _now())
    full_ttl = (cfg.full_ttl_h if full_ttl_h is None else full_ttl_h) * 3600_000
    thumb_ttl = (cfg.thumb_ttl_d if thumb_ttl_d is None else thumb_ttl_d) * \
        86400_000
    budget = (cfg.disk_budget_bytes if budget_bytes is None else budget_bytes)

    manifest = {"full_aged": 0, "thumb_aged": 0, "full_budget": 0,
                "thumb_budget": 0, "bytes_freed": 0, "skipped_promoted": 0,
                "shared_sha_kept": 0}

    rows = st.list_captures_for_compaction()

    # --- age: full frames ---
    for row in rows:
        fsha = row.get("frame_sha256")
        if not fsha:
            continue
        if int(row.get("promoted") or 0):
            manifest["skipped_promoted"] += 1
            continue
        if now - int(row["ts_utc"]) < full_ttl:
            continue
        # Clear this capture first so shared siblings keep a live refcount.
        deg = "thumb" if row.get("thumb_sha256") else "text"
        st.clear_frame_sha(row["capture_id"], degradation=deg)
        freed = _release_sha(st, fsha, root)
        manifest["full_aged"] += 1
        if freed:
            manifest["bytes_freed"] += freed
        elif st.sha_refcount(fsha) > 0:
            manifest["shared_sha_kept"] += 1

    # Refresh after mutations.
    rows = st.list_captures_for_compaction()

    # --- age: thumbs ---
    for row in rows:
        tsha = row.get("thumb_sha256")
        if not tsha:
            continue
        if now - int(row["ts_utc"]) < thumb_ttl:
            continue
        st.clear_thumb_sha(row["capture_id"], degradation="text")
        freed = _release_sha(st, tsha, root)
        manifest["thumb_aged"] += 1
        if freed:
            manifest["bytes_freed"] += freed
        elif st.sha_refcount(tsha) > 0:
            manifest["shared_sha_kept"] += 1

    # --- budget ---
    if budget > 0:
        _enforce_budget(st, root, budget, manifest)

    size = l2_frames.dir_size_bytes(root)
    manifest["dir_bytes"] = size
    manifest["budget_bytes"] = budget
    print(f"[perception.compactor] {manifest}")
    return manifest


def _enforce_budget(st: PerceptionStore, root, budget: int,
                    manifest: dict) -> None:
    # Drop oldest unpromoted fulls, then oldest thumbs, until under budget.
    while l2_frames.dir_size_bytes(root) > budget:
        rows = st.list_captures_for_compaction()
        victim = next((r for r in rows
                       if r.get("frame_sha256")
                       and not int(r.get("promoted") or 0)), None)
        if victim is None:
            victim = next((r for r in rows if r.get("thumb_sha256")), None)
            if victim is None:
                break
            tsha = victim["thumb_sha256"]
            st.clear_thumb_sha(victim["capture_id"], degradation="text")
            freed = _release_sha(st, tsha, root)
            manifest["thumb_budget"] += 1
            if freed:
                manifest["bytes_freed"] += freed
            elif st.sha_refcount(tsha) > 0:
                manifest["shared_sha_kept"] += 1
            continue
        fsha = victim["frame_sha256"]
        deg = "thumb" if victim.get("thumb_sha256") else "text"
        st.clear_frame_sha(victim["capture_id"], degradation=deg)
        freed = _release_sha(st, fsha, root)
        manifest["full_budget"] += 1
        if freed:
            manifest["bytes_freed"] += freed
        elif st.sha_refcount(fsha) > 0:
            manifest["shared_sha_kept"] += 1


def _file_size(sha: str, root) -> int:
    try:
        p = l2_frames.path_for(sha, root)
        return int(p.stat().st_size) if p.is_file() else 0
    except Exception:
        return 0


def run_job(_payload: dict | None = None) -> None:
    """Worker handler entrypoint (kind=perception_compact)."""
    compact()
