"""LanceDB vector store — the semantic index over the memory timeline.

Embedded/file-based (no server), stored under `data/lance/`. Holds one row per
event: its SQLite id, time, modality, text, and embedding vector. Cosine
similarity search returns the ids of the closest memories, which the Memory
Engine then hydrates from SQLite.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from app.config import settings


class VectorStore:
    def __init__(self, path: str | None = None, dim: int | None = None) -> None:
        self.path = Path(path or settings.memory.lance_dir)
        self.path.mkdir(parents=True, exist_ok=True)
        self._dim = dim
        self._lock = threading.Lock()
        self._db = None
        self._table = None
        # Self-maintenance: every commit leaves an immutable version behind, and
        # each version manifest lists ALL fragments — so an unmaintained
        # high-churn table grows quadratically (observed: 106 GB of manifests
        # over 145 MB of vectors). Compact + prune every N commits.
        self._optimize_every = max(0, int(settings.memory.lance_optimize_every))
        self._commits_since_optimize = 0

    def _ensure(self, dim: int):
        if self._table is not None:
            return self._table
        import lancedb
        import pyarrow as pa

        self._dim = dim
        self._db = lancedb.connect(str(self.path))
        schema = pa.schema([
            pa.field("id", pa.int64()),
            pa.field("time", pa.float64()),
            pa.field("modality", pa.string()),
            pa.field("text", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ])
        if "events" in self._db.table_names():
            self._table = self._db.open_table("events")
            # A version backlog on disk (accumulated while maintenance was off,
            # or by a process that died before its Nth commit) is cleared once
            # at open, so the cap holds across restarts, not just in-process.
            try:
                if (self._optimize_every
                        and len(self._table.list_versions()) > self._optimize_every):
                    self._optimize()
            except Exception as exc:
                print(f"[vectorstore] open-time optimize skipped ({exc}).")
        else:
            self._table = self._db.create_table("events", schema=schema)
        return self._table

    def _optimize(self) -> None:
        """Compact fragments and drop superseded versions (call under _lock).
        Keeps every current row; only append history is discarded."""
        from datetime import timedelta

        t0 = time.time()
        self._table.optimize(cleanup_older_than=timedelta(0))
        self._commits_since_optimize = 0
        print(f"[vectorstore] index optimized (fragments compacted, old versions "
              f"pruned) in {time.time() - t0:.1f}s.")

    def _after_commit(self) -> None:
        """Count a commit; optimize when the version budget is spent. Best-effort
        — maintenance must never fail an ingest (call under _lock)."""
        if not self._optimize_every:
            return
        self._commits_since_optimize += 1
        if self._commits_since_optimize >= self._optimize_every:
            try:
                self._optimize()
            except Exception as exc:
                self._commits_since_optimize = 0  # don't retry on every commit
                print(f"[vectorstore] periodic optimize skipped ({exc}).")

    def add(self, id: int, time_: float, modality: str, text: str, vector: np.ndarray) -> None:
        table = self._ensure(len(vector))
        with self._lock:
            table.add([{
                "id": int(id), "time": float(time_), "modality": modality,
                "text": text or "", "vector": vector.tolist(),
            }])
            self._after_commit()

    def add_many(self, rows: list[dict]) -> None:
        """Index many rows in a SINGLE commit. The backfill used to call add()
        once per event — one LanceDB version + fragment per row — which is what
        let the index balloon to tens of thousands of files and made every boot
        slower than the last. One commit fixes that. Each row is a dict with
        keys: id, time, modality, text, vector (list[float])."""
        if not rows:
            return
        table = self._ensure(len(rows[0]["vector"]))
        with self._lock:
            table.add(rows)
            self._after_commit()

    def delete_ids(self, ids: list[int]) -> int:
        """Delete rows by id (best-effort). Cleanup tooling calls this to drop the
        vectors of facts/events removed from the store, so orphaned rows don't
        linger in search. Opens the on-disk table directly (no dim needed)."""
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        with self._lock:
            try:
                if getattr(self, "_table", None) is None:
                    import lancedb
                    if getattr(self, "_db", None) is None:
                        self._db = lancedb.connect(str(self.path))
                    if "events" not in self._db.table_names():
                        return 0
                    self._table = self._db.open_table("events")
                self._table.delete(f"id IN ({','.join(str(i) for i in ids)})")
                self._after_commit()
                return len(ids)
            except Exception as exc:
                print(f"[vectorstore] delete skipped ({exc}).")
                return 0

    def search(self, vector: np.ndarray, k: int = 8, modality: str | None = None) -> list[dict]:
        table = self._ensure(len(vector))
        with self._lock:
            q = table.search(vector.tolist()).metric("cosine").limit(k)
            if modality:
                q = q.where(f"modality = '{modality}'")
            rows = q.to_list()
        # lancedb returns _distance (cosine distance); similarity = 1 - distance
        for r in rows:
            r["score"] = round(1.0 - float(r.get("_distance", 1.0)), 4)
        return rows

    def list_ids(self) -> set[int]:
        """All vector row ids currently on disk (events + fact-offset ids).

        Used by MemoryEngine reconciliation so a partial index can be repaired
        instead of treating any non-empty table as 'fully indexed forever'.
        """
        with self._lock:
            try:
                if self._table is None:
                    import lancedb

                    if self._db is None:
                        self._db = lancedb.connect(str(self.path))
                    if "events" not in self._db.table_names():
                        return set()
                    self._table = self._db.open_table("events")
                # Column-only scan — avoid pulling embedding vectors into RAM.
                try:
                    lance_tbl = self._table.to_lance()
                    col = lance_tbl.to_table(columns=["id"])["id"]
                    return {int(x) for x in col.to_pylist()}
                except Exception:
                    rows = self._table.to_pandas()
                    if rows is None or rows.empty:
                        return set()
                    return {int(x) for x in rows["id"].tolist()}
            except Exception as exc:
                print(f"[vectorstore] list_ids skipped ({exc}).")
                return set()

    def count(self) -> int:
        """Real row count of the persisted table, opening it from disk if this
        process hasn't yet. Returns 0 only when no table exists on disk.

        The old version short-circuited to 0 whenever `_dim` was unset — which
        is the case on every fresh boot — so the Memory Engine's "already
        indexed?" guard never fired and it re-indexed the entire timeline (as
        thousands of single-row commits) on every startup."""
        with self._lock:
            if self._table is None:
                try:
                    import lancedb

                    if self._db is None:
                        self._db = lancedb.connect(str(self.path))
                    if "events" not in self._db.table_names():
                        return 0
                    self._table = self._db.open_table("events")
                except Exception:
                    return 0
            try:
                return int(self._table.count_rows())
            except Exception:
                return 0

    def force_optimize(self) -> dict:
        """Manual Lance compact+prune (console / recovery). Never raises."""
        t0 = time.time()
        with self._lock:
            try:
                if self._table is None:
                    import lancedb
                    if self._db is None:
                        self._db = lancedb.connect(str(self.path))
                    if "events" not in self._db.table_names():
                        return {"ok": True, "skipped": True, "reason": "no_table"}
                    self._table = self._db.open_table("events")
                n_ver = None
                try:
                    n_ver = len(self._table.list_versions())
                except Exception:
                    pass
                self._optimize()
                return {
                    "ok": True,
                    "versions_before": n_ver,
                    "elapsed_s": round(time.time() - t0, 2),
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def lance_status(self) -> dict:
        """Lightweight index health for the economy console."""
        with self._lock:
            try:
                if self._table is None:
                    import lancedb
                    if self._db is None:
                        self._db = lancedb.connect(str(self.path))
                    if "events" not in self._db.table_names():
                        return {"ok": True, "exists": False}
                    self._table = self._db.open_table("events")
                n_ver = len(self._table.list_versions())
                n_rows = int(self._table.count_rows())
                return {
                    "ok": True, "exists": True,
                    "versions": n_ver, "rows": n_rows,
                    "optimize_every": self._optimize_every,
                    "commits_since_optimize": self._commits_since_optimize,
                    "path": str(self.path),
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc)}


_vs: VectorStore | None = None
_vs_lock = threading.Lock()


def get_vectorstore() -> VectorStore:
    global _vs
    if _vs is None:
        with _vs_lock:
            if _vs is None:
                _vs = VectorStore()
    return _vs
