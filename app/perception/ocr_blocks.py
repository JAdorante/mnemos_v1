"""L1 OCR-block embeddings — LanceDB table keyed by capture_id (string).

Separate from the int-id `events` table so ULID capture_ids join cleanly to
Parquet / perception.db without colliding with event/fact id bands.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from app.config import settings

_TABLE = "ocr_blocks"
_lock = threading.Lock()
_db = None
_table = None


def _connect():
    global _db, _table
    import lancedb
    import pyarrow as pa

    path = Path(settings.memory.lance_dir)
    path.mkdir(parents=True, exist_ok=True)
    _db = lancedb.connect(str(path))
    if _TABLE in _db.table_names():
        _table = _db.open_table(_TABLE)
        return _table
    # dim filled on first add; create with a placeholder schema once we know dim
    return None


def _ensure(dim: int):
    global _table
    with _lock:
        if _table is not None:
            return _table
        import lancedb
        import pyarrow as pa

        path = Path(settings.memory.lance_dir)
        path.mkdir(parents=True, exist_ok=True)
        db = lancedb.connect(str(path))
        schema = pa.schema([
            pa.field("capture_id", pa.string()),
            pa.field("block_idx", pa.int32()),
            pa.field("time", pa.float64()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ])
        if _TABLE in db.table_names():
            _table = db.open_table(_TABLE)
        else:
            _table = db.create_table(_TABLE, schema=schema)
        return _table


def add_blocks(capture_id: str, texts: list[str], *,
               ts: float | None = None) -> int:
    """Embed and index merged OCR blocks for one capture. Best-effort."""
    texts = [t for t in (texts or []) if (t or "").strip()]
    if not texts or not capture_id:
        return 0
    try:
        from app.services.embeddings import embedder
        vecs = embedder.encode_many(texts)
        table = _ensure(int(vecs.shape[1]))
        now = float(ts if ts is not None else time.time())
        rows = [{
            "capture_id": str(capture_id),
            "block_idx": i,
            "time": now,
            "text": texts[i],
            "vector": vecs[i].tolist(),
        } for i in range(len(texts))]
        with _lock:
            table.add(rows)
        return len(rows)
    except Exception as exc:
        print(f"[perception.ocr_blocks] add skipped ({exc}).")
        return 0


def delete_capture_ids(capture_ids: list[str]) -> int:
    """Drop all ocr_blocks rows for the given capture_ids."""
    ids = [str(c) for c in (capture_ids or []) if c]
    if not ids:
        return 0
    try:
        with _lock:
            table = _table
            if table is None:
                import lancedb
                path = Path(settings.memory.lance_dir)
                if not path.is_dir():
                    return 0
                db = lancedb.connect(str(path))
                if _TABLE not in db.table_names():
                    return 0
                table = db.open_table(_TABLE)
            # Lance delete filter: OR of equality predicates.
            # Escape single quotes in ids (ULIDs have none, but be safe).
            parts = []
            for c in ids:
                safe = c.replace("'", "''")
                parts.append(f"capture_id = '{safe}'")
            table.delete(" OR ".join(parts))
        return len(ids)
    except Exception as exc:
        print(f"[perception.ocr_blocks] delete skipped ({exc}).")
        return 0
