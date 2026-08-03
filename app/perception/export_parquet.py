"""Nightly / on-demand Parquet export of perception tables (Phase D).

Writes ``export/<table>/date=YYYY-MM-DD/part-<ts>.parquet`` for rows newer
than the per-table watermark in ``export_watermarks``. Erasure already
deletes overlapping partitions.
"""
from __future__ import annotations

import time
from pathlib import Path

from app.perception.store import PerceptionStore, get_pstore

_TABLES = (
    "meta_events", "gaps", "captures", "ocr_lines",
    "activity_blocks", "extractions", "supervision_events", "salience_scores",
)


def export_root(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    from app.config import settings
    return Path(settings.perception.export_dir)


def run_export(payload: dict | None = None,
               store: PerceptionStore | None = None,
               root: str | Path | None = None) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    st = store or get_pstore()
    base = export_root(root)
    base.mkdir(parents=True, exist_ok=True)
    limit = int((payload or {}).get("limit", 5000))
    manifest: dict = {"tables": {}, "root": str(base)}

    for table in _TABLES:
        last_ts, last_rowid = st.get_export_watermark(table)
        # ocr_lines uses rowid watermark in last_ts slot via last_rowid field.
        since = last_rowid if table == "ocr_lines" else last_ts
        rows = st.export_rows_since(table, since, limit=limit)
        if not rows:
            manifest["tables"][table] = {"rows": 0}
            continue
        # Drop internal _rowid from parquet payload but track max.
        max_ts = since
        max_rid = last_rowid
        clean = []
        for r in rows:
            rid = int(r.pop("_rowid", 0) or 0)
            max_rid = max(max_rid, rid)
            ts = int(r.get("ts_utc") or r.get("ts_start") or 0)
            max_ts = max(max_ts, ts)
            # stringify nested-ish
            clean.append({k: ("" if v is None else v) for k, v in r.items()})
        day = time.strftime(
            "%Y-%m-%d",
            time.gmtime((max_ts / 1000.0) if max_ts > 10_000_000_000 else max_ts or time.time()))
        part_dir = base / table / f"date={day}"
        part_dir.mkdir(parents=True, exist_ok=True)
        out = part_dir / f"part-{int(time.time())}-{max_rid}.parquet"
        table_pa = pa.Table.from_pylist(clean)
        pq.write_table(table_pa, out)
        if table == "ocr_lines":
            st.set_export_watermark(table, max_ts, max_rid)
        else:
            st.set_export_watermark(table, max_ts, max_rid)
        manifest["tables"][table] = {"rows": len(clean), "path": str(out)}
    print(f"[perception.export] {manifest}")
    return manifest
