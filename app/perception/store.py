"""perception.db — versioned SQLite (WAL) store for the perception layers.

Separate file from quill.db on purpose: real numbered migrations via
`PRAGMA user_version` from day 0 (quill.db predates that discipline), a
single-writer WAL connection so the 1 Hz L0 stream never contends with the
main store's lock, and an erasure cascade that can be audited table-by-table.
Cross-store joins (captures -> events/KG) happen by id in Python, the same
pattern the repo already uses for SQLite<->LanceDB.

All timestamps here are UTC milliseconds (int). The main store uses epoch
seconds (float); conversion happens at the boundary, never implicitly.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from app.perception.schemas import (Capture, Gap, MetaEvent, SCHEMA_VERSION,
                                    SupervisionEvent, now_ms)

# Numbered migration steps. Each entry runs when user_version < step, in
# order, then stamps user_version = step. NEVER edit a shipped step — add a
# new one (that is the whole point of the version stamp).
_MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        # --- L0 ------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS meta_events (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            ts_utc INTEGER NOT NULL,
            utc_offset_minutes INTEGER NOT NULL DEFAULT 0,
            app_name TEXT DEFAULT '',
            app_exe_hash TEXT DEFAULT '',
            window_id TEXT DEFAULT '',
            window_title TEXT DEFAULT '',
            browser_url TEXT,
            url_domain TEXT,
            doc_path TEXT,
            key_count INTEGER NOT NULL DEFAULT 0,
            mouse_count INTEGER NOT NULL DEFAULT 0,
            is_idle INTEGER NOT NULL DEFAULT 0,
            display_hash TEXT DEFAULT '',
            schema_version INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_meta_ts ON meta_events(ts_utc)",
        """CREATE TABLE IF NOT EXISTS gaps (
            id INTEGER PRIMARY KEY,
            ts_start INTEGER NOT NULL,
            ts_end INTEGER,
            reason TEXT NOT NULL CHECK(reason IN
              ('process_down','sleep','user_pause','privacy_excluded','crash')),
            schema_version INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_gaps_start ON gaps(ts_start)",
        # --- L1 (rows arrive in Phase B; 'excluded' rows arrive today) -----
        """CREATE TABLE IF NOT EXISTS captures (
            capture_id TEXT PRIMARY KEY,
            ts_utc INTEGER NOT NULL,
            window_id TEXT DEFAULT '',
            meta_event_id INTEGER,
            kind TEXT NOT NULL
              CHECK(kind IN ('full','scroll_delta','excluded','vlm_only')),
            trigger TEXT DEFAULT '',
            frame_sha256 TEXT,
            thumb_sha256 TEXT,
            ocr_engine TEXT,
            ocr_version TEXT,
            ocr_mean_conf REAL,
            dropped_low_conf INTEGER NOT NULL DEFAULT 0,
            redaction_hits INTEGER NOT NULL DEFAULT 0,
            exclusion_rule TEXT,
            novel_line_count INTEGER NOT NULL DEFAULT 0,
            total_line_count INTEGER NOT NULL DEFAULT 0,
            schema_version INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_captures_ts ON captures(ts_utc)",
        """CREATE TABLE IF NOT EXISTS ocr_lines (
            line_hash TEXT NOT NULL,
            window_id TEXT NOT NULL,
            first_capture_id TEXT,
            text TEXT NOT NULL,
            bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL,
            conf REAL,
            PRIMARY KEY (line_hash, window_id)
        )""",
        """CREATE TABLE IF NOT EXISTS frame_line_map (
            capture_id TEXT NOT NULL,
            line_hash TEXT NOT NULL,
            line_order INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_flm_capture ON frame_line_map(capture_id)",
        # --- L3 (Phase D) --------------------------------------------------
        """CREATE TABLE IF NOT EXISTS activity_blocks (
            block_id TEXT PRIMARY KEY,
            ts_start INTEGER NOT NULL,
            ts_end INTEGER NOT NULL,
            dominant_app TEXT, dominant_domain TEXT, dominant_doc TEXT,
            input_intensity REAL,
            capture_ids TEXT,
            summary TEXT,
            schema_version INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS extractions (
            extraction_id TEXT PRIMARY KEY,
            block_id TEXT, capture_id TEXT,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            confidence REAL,
            source_span TEXT,
            norm_span_key TEXT,
            model TEXT, model_version TEXT,
            egress TEXT NOT NULL DEFAULT 'local',
            ts_utc INTEGER NOT NULL,
            schema_version INTEGER NOT NULL
        )""",
        # Idempotency: re-running an L3 job must not mint duplicates.
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_extract_dedupe
            ON extractions(type, norm_span_key, capture_id)""",
        """CREATE TABLE IF NOT EXISTS salience_scores (
            capture_group_id TEXT NOT NULL,
            score REAL NOT NULL,
            features_json TEXT,
            model_version TEXT,
            ts_utc INTEGER NOT NULL
        )""",
        # --- supervision (training corpus, append-only) --------------------
        """CREATE TABLE IF NOT EXISTS supervision_events (
            id INTEGER PRIMARY KEY,
            ts_utc INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN
              ('query','query_click','extraction_confirm','extraction_reject',
               'extraction_edit','action_approved','action_rejected','pin',
               'unpin','exclusion_added','erasure')),
            target_type TEXT DEFAULT '',
            target_id TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}',
            schema_version INTEGER NOT NULL
        )""",
        # --- spend cap ledger (SECURITY #2) --------------------------------
        """CREATE TABLE IF NOT EXISTS spend_ledger (
            day TEXT NOT NULL,
            task_class TEXT NOT NULL,
            usd REAL NOT NULL DEFAULT 0,
            calls INTEGER NOT NULL DEFAULT 0,
            denied INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, task_class)
        )""",
        # --- coverage self-audit runs (correctness criterion 1) ------------
        """CREATE TABLE IF NOT EXISTS coverage_audits (
            id INTEGER PRIMARY KEY,
            ts_utc INTEGER NOT NULL,
            window_start INTEGER NOT NULL,
            window_end INTEGER NOT NULL,
            covered_pct REAL NOT NULL,
            hole_ms INTEGER NOT NULL,
            holes_json TEXT
        )""",
    ]),
    # Phase C — L2 promotion + degradation tracking on captures
    (2, [
        "ALTER TABLE captures ADD COLUMN promoted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE captures ADD COLUMN degradation TEXT NOT NULL DEFAULT 'full'",
        "CREATE INDEX IF NOT EXISTS idx_captures_promoted ON captures(promoted)",
    ]),
    # Phase D — Parquet export watermarks
    (3, [
        """CREATE TABLE IF NOT EXISTS export_watermarks (
            table_name TEXT PRIMARY KEY,
            last_ts_utc INTEGER NOT NULL DEFAULT 0,
            last_rowid INTEGER NOT NULL DEFAULT 0
        )""",
    ]),
]

# FTS5 is created outside the numbered steps: some Python builds lack the
# module and the Phase A floor must not depend on it. Recorded in `fts_ok`.
_FTS_DDL = ("CREATE VIRTUAL TABLE IF NOT EXISTS ocr_fts "
            "USING fts5(text, content='ocr_lines')")

# A meta record vouches for the time from the PREVIOUS record up to itself.
# The L0 monitor heartbeats every 60 s even with no state change, so any
# record-to-record distance beyond heartbeat + slack is an (unlabeled) hole
# the coverage audit must surface.
COVERAGE_MAX_STRIDE_MS = 150_000


class PerceptionStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            from app.config import settings
            db_path = Path(settings.storage.data_dir) / "perception.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Batch commits are the fsync unit (L0 commits every ~2 s).
        self._conn.execute("PRAGMA synchronous=FULL")
        self.fts_ok = False
        self._migrate()

    # ------------------------------ migrations ----------------------------
    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for step, ddl in _MIGRATIONS:
                if cur >= step:
                    continue
                for sql in ddl:
                    self._conn.execute(sql)
                self._conn.execute(f"PRAGMA user_version = {step}")
                self._conn.commit()
                cur = step
            try:
                self._conn.execute(_FTS_DDL)
                self._conn.commit()
                self.fts_ok = True
            except sqlite3.OperationalError as exc:
                print(f"[perception.store] FTS5 unavailable ({exc}); "
                      "text search degrades to LIKE in Phase B.")

    def user_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # ------------------------------ L0 writes -----------------------------
    def insert_meta_batch(self, rows: list[MetaEvent]) -> int:
        """Append a batch of L0 records in ONE commit (the fsync unit)."""
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO meta_events
                   (session_id, seq, ts_utc, utc_offset_minutes, app_name,
                    app_exe_hash, window_id, window_title, browser_url,
                    url_domain, doc_path, key_count, mouse_count, is_idle,
                    display_hash, schema_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(r.session_id, r.seq, r.ts_utc, r.utc_offset_minutes,
                  r.app_name, r.app_exe_hash, r.window_id, r.window_title,
                  r.browser_url, r.url_domain, r.doc_path, r.key_count,
                  r.mouse_count, int(r.is_idle), r.display_hash,
                  r.schema_version) for r in rows])
            self._conn.commit()
            return len(rows)

    def last_meta_ts(self) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts_utc) AS t FROM meta_events").fetchone()
            return int(row["t"]) if row and row["t"] is not None else None

    # ------------------------------ gaps ----------------------------------
    def add_gap(self, ts_start: int, ts_end: int | None, reason: str) -> int:
        Gap(ts_start=ts_start, ts_end=ts_end, reason=reason)  # validate
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO gaps (ts_start, ts_end, reason, schema_version) "
                "VALUES (?,?,?,?)", (ts_start, ts_end, reason, SCHEMA_VERSION))
            self._conn.commit()
            return int(cur.lastrowid)

    def close_gap(self, gap_id: int, ts_end: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE gaps SET ts_end=? WHERE id=?",
                               (ts_end, gap_id))
            self._conn.commit()

    def close_dangling_gaps(self, ts_end: int) -> int:
        """Close any open-ended gap a crash left behind (reconcile on boot)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE gaps SET ts_end=? WHERE ts_end IS NULL", (ts_end,))
            self._conn.commit()
            return cur.rowcount

    def list_gaps(self, since_ms: int | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT * FROM gaps"
        args: list = []
        if since_ms is not None:
            q += " WHERE ts_start >= ? OR ts_end IS NULL"
            args.append(since_ms)
        q += " ORDER BY ts_start DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    # ------------------------------ captures ------------------------------
    def insert_capture(self, cap: Capture) -> str:
        with self._lock:
            # promoted/degradation exist from migration step 2; tolerate step-1
            # DBs mid-test by catching OperationalError and retrying bare insert.
            cols = (
                """INSERT OR REPLACE INTO captures
                   (capture_id, ts_utc, window_id, meta_event_id, kind,
                    trigger, frame_sha256, thumb_sha256, ocr_engine,
                    ocr_version, ocr_mean_conf, dropped_low_conf,
                    redaction_hits, exclusion_rule, novel_line_count,
                    total_line_count, schema_version, promoted, degradation)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""")
            args = (cap.capture_id, cap.ts_utc, cap.window_id, cap.meta_event_id,
                    cap.kind, cap.trigger, cap.frame_sha256, cap.thumb_sha256,
                    cap.ocr_engine, cap.ocr_version, cap.ocr_mean_conf,
                    cap.dropped_low_conf, cap.redaction_hits, cap.exclusion_rule,
                    cap.novel_line_count, cap.total_line_count,
                    cap.schema_version, int(getattr(cap, "promoted", False)),
                    getattr(cap, "degradation", None) or "full")
            try:
                self._conn.execute(cols, args)
            except sqlite3.OperationalError:
                self._conn.execute(
                    """INSERT OR REPLACE INTO captures
                       (capture_id, ts_utc, window_id, meta_event_id, kind,
                        trigger, frame_sha256, thumb_sha256, ocr_engine,
                        ocr_version, ocr_mean_conf, dropped_low_conf,
                        redaction_hits, exclusion_rule, novel_line_count,
                        total_line_count, schema_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    args[:17])
            self._conn.commit()
            return cap.capture_id

    def update_capture_frames(self, capture_id: str,
                              frame_sha: str | None,
                              thumb_sha: str | None,
                              *, degradation: str | None = None) -> None:
        deg = degradation
        if deg is None:
            if frame_sha:
                deg = "full"
            elif thumb_sha:
                deg = "thumb"
            else:
                deg = "text"
        with self._lock:
            self._conn.execute(
                "UPDATE captures SET frame_sha256=?, thumb_sha256=?, "
                "degradation=? WHERE capture_id=?",
                (frame_sha, thumb_sha, deg, capture_id))
            self._conn.commit()

    def set_promoted(self, capture_id: str, promoted: bool = True) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE captures SET promoted=? WHERE capture_id=?",
                (1 if promoted else 0, capture_id))
            self._conn.commit()
            return cur.rowcount > 0

    def add_salience(self, capture_group_id: str, score: float,
                     features: dict | None = None,
                     model_version: str = "pin-v1",
                     ts_utc: int | None = None) -> None:
        import json as _json
        from app.perception.schemas import now_ms
        with self._lock:
            self._conn.execute(
                "INSERT INTO salience_scores "
                "(capture_group_id, score, features_json, model_version, ts_utc) "
                "VALUES (?,?,?,?,?)",
                (capture_group_id, float(score),
                 _json.dumps(features or {}), model_version,
                 int(ts_utc if ts_utc is not None else now_ms())))
            self._conn.commit()

    def list_captures_for_compaction(self) -> list[dict]:
        """All captures that still reference a CAS frame or thumb."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT capture_id, ts_utc, frame_sha256, thumb_sha256, "
                "COALESCE(promoted, 0) AS promoted, "
                "COALESCE(degradation, 'full') AS degradation "
                "FROM captures "
                "WHERE frame_sha256 IS NOT NULL OR thumb_sha256 IS NOT NULL "
                "ORDER BY ts_utc ASC").fetchall()
            return [dict(r) for r in rows]

    def clear_frame_sha(self, capture_id: str, *,
                        degradation: str = "thumb") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE captures SET frame_sha256=NULL, degradation=? "
                "WHERE capture_id=?", (degradation, capture_id))
            self._conn.commit()

    def clear_thumb_sha(self, capture_id: str, *,
                        degradation: str = "text") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE captures SET thumb_sha256=NULL, degradation=? "
                "WHERE capture_id=?", (degradation, capture_id))
            self._conn.commit()

    def sha_refcount(self, sha256: str) -> int:
        """How many capture rows still reference this CAS digest (frame or thumb).

        Content-addressed files are shared across identical captures — unlink
        only when this returns 0.
        """
        sha = (sha256 or "").strip()
        if not sha:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM captures "
                "WHERE frame_sha256=? OR thumb_sha256=?",
                (sha, sha)).fetchone()
            return int(row["n"] if row else 0)

    def recent_captures(self, since_ms: int, limit: int = 500) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM captures WHERE ts_utc >= ? "
                "ORDER BY ts_utc DESC LIMIT ?", (since_ms, limit)).fetchall()]

    def recent_meta(self, since_ms: int, limit: int = 2000) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM meta_events WHERE ts_utc >= ? "
                "ORDER BY ts_utc DESC LIMIT ?", (since_ms, limit)).fetchall()]

    def get_capture(self, capture_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM captures WHERE capture_id = ?",
                (capture_id,)).fetchone()
            return dict(row) if row else None

    def upsert_ocr_lines(self, lines: list) -> int:
        """Insert novel OCR lines (OR IGNORE on line_hash+window_id). Syncs
        FTS5 content rows when available. Returns count of newly inserted."""
        if not lines:
            return 0
        from app.perception.schemas import OcrLine
        rows = []
        for ln in lines:
            if isinstance(ln, OcrLine):
                rows.append(ln)
            else:
                rows.append(OcrLine(**ln) if isinstance(ln, dict) else ln)
        inserted = 0
        with self._lock:
            for ln in rows:
                cur = self._conn.execute(
                    """INSERT OR IGNORE INTO ocr_lines
                       (line_hash, window_id, first_capture_id, text,
                        bbox_x, bbox_y, bbox_w, bbox_h, conf)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (ln.line_hash, ln.window_id, ln.first_capture_id, ln.text,
                     ln.bbox_x, ln.bbox_y, ln.bbox_w, ln.bbox_h, ln.conf))
                if cur.rowcount:
                    inserted += 1
                    if self.fts_ok:
                        # content= external-content FTS: insert by rowid.
                        row = self._conn.execute(
                            "SELECT rowid FROM ocr_lines WHERE line_hash=? "
                            "AND window_id=?",
                            (ln.line_hash, ln.window_id)).fetchone()
                        if row:
                            try:
                                self._conn.execute(
                                    "INSERT INTO ocr_fts(rowid, text) "
                                    "VALUES (?,?)", (row["rowid"], ln.text))
                            except sqlite3.OperationalError:
                                pass
            self._conn.commit()
        return inserted

    def set_frame_line_map(self, capture_id: str,
                           ordered_hashes: list[str]) -> int:
        """Replace the ordered line-hash map for a capture (full visible text)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM frame_line_map WHERE capture_id = ?", (capture_id,))
            self._conn.executemany(
                "INSERT INTO frame_line_map (capture_id, line_hash, line_order) "
                "VALUES (?,?,?)",
                [(capture_id, h, i) for i, h in enumerate(ordered_hashes)])
            self._conn.commit()
        return len(ordered_hashes)

    def reconstruct_text(self, capture_id: str) -> str:
        """Byte-identical reconstruction of the OCR'd visible text for a
        capture via frame_line_map → ocr_lines (joins on line_hash; window
        comes from the capture row)."""
        with self._lock:
            cap = self._conn.execute(
                "SELECT window_id FROM captures WHERE capture_id = ?",
                (capture_id,)).fetchone()
            if not cap:
                return ""
            window_id = cap["window_id"] or ""
            rows = self._conn.execute(
                """SELECT ol.text FROM frame_line_map flm
                   JOIN ocr_lines ol ON ol.line_hash = flm.line_hash
                     AND ol.window_id = ?
                   WHERE flm.capture_id = ?
                   ORDER BY flm.line_order ASC""",
                (window_id, capture_id)).fetchall()
        return "\n".join(r["text"] for r in rows)

    def load_window_line_hashes(self, window_id: str,
                                limit: int = 2000) -> list[str]:
        """Most-recent line hashes for a window (crash-safe cache rebuild).
        Ordered by first_capture_id recency via captures.ts_utc when possible."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT ol.line_hash FROM ocr_lines ol
                   LEFT JOIN captures c ON c.capture_id = ol.first_capture_id
                   WHERE ol.window_id = ?
                   ORDER BY COALESCE(c.ts_utc, 0) DESC
                   LIMIT ?""", (window_id, int(limit))).fetchall()
        # Return oldest→newest so the rolling cache matches insertion order.
        return [r["line_hash"] for r in reversed(rows)]

    # ------------------------------ supervision ---------------------------
    def add_supervision(self, ev: SupervisionEvent) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO supervision_events (ts_utc, kind, target_type, "
                "target_id, payload_json, schema_version) VALUES (?,?,?,?,?,?)",
                (ev.ts_utc, ev.kind, ev.target_type, ev.target_id,
                 ev.payload_json, ev.schema_version))
            self._conn.commit()
            return int(cur.lastrowid)

    # ------------------------------ spend ledger --------------------------
    @staticmethod
    def _day(ts: float | None = None) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None
                                                     else time.time()))

    def add_spend(self, usd: float, task_class: str,
                  ts: float | None = None) -> None:
        day = self._day(ts)
        with self._lock:
            self._conn.execute(
                """INSERT INTO spend_ledger (day, task_class, usd, calls)
                   VALUES (?,?,?,1)
                   ON CONFLICT(day, task_class)
                   DO UPDATE SET usd = usd + excluded.usd, calls = calls + 1""",
                (day, task_class, float(usd)))
            self._conn.commit()

    def bump_denied(self, task_class: str, ts: float | None = None) -> None:
        day = self._day(ts)
        with self._lock:
            self._conn.execute(
                """INSERT INTO spend_ledger (day, task_class, denied)
                   VALUES (?,?,1)
                   ON CONFLICT(day, task_class)
                   DO UPDATE SET denied = denied + 1""", (day, task_class))
            self._conn.commit()

    def day_spend(self, ts: float | None = None) -> dict:
        day = self._day(ts)
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_class, usd, calls, denied FROM spend_ledger "
                "WHERE day = ?", (day,)).fetchall()
        by = {r["task_class"]: {"usd": float(r["usd"]), "calls": r["calls"],
                                "denied": r["denied"]} for r in rows}
        return {"day": day,
                "total_usd": round(sum(v["usd"] for v in by.values()), 6),
                "denied": sum(v["denied"] for v in by.values()),
                "by_task": by}

    # ------------------------------ coverage audit ------------------------
    def coverage(self, window_start: int, window_end: int) -> dict:
        """Fraction of [window_start, window_end) vouched for by
        meta_events ∪ gaps, and the unlabeled holes. 'Machine on' is defined
        as: the L0 monitor was writing records <= COVERAGE_MAX_STRIDE_MS
        apart, or an explicit gap row labels why it was not."""
        with self._lock:
            metas = [int(r["ts_utc"]) for r in self._conn.execute(
                "SELECT ts_utc FROM meta_events WHERE ts_utc >= ? AND "
                "ts_utc < ? ORDER BY ts_utc",
                (window_start - COVERAGE_MAX_STRIDE_MS, window_end)).fetchall()]
            gaps = [(int(r["ts_start"]),
                     int(r["ts_end"]) if r["ts_end"] is not None else window_end)
                    for r in self._conn.execute(
                        "SELECT ts_start, ts_end FROM gaps WHERE "
                        "(ts_end IS NULL OR ts_end > ?) AND ts_start < ?",
                        (window_start, window_end)).fetchall()]
        spans: list[tuple[int, int]] = list(gaps)
        for a, b in zip(metas, metas[1:]):
            if b - a <= COVERAGE_MAX_STRIDE_MS:
                spans.append((a, b))
        # First/last record edge: a record vouches a stride around itself.
        if metas:
            spans.append((max(window_start, metas[0] - COVERAGE_MAX_STRIDE_MS),
                          metas[0]))
            spans.append((metas[-1], min(window_end,
                                         metas[-1] + COVERAGE_MAX_STRIDE_MS)))
        # Merge and measure.
        spans = sorted((max(a, window_start), min(b, window_end))
                       for a, b in spans if b > window_start and a < window_end)
        merged: list[list[int]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        covered = sum(b - a for a, b in merged)
        total = max(1, window_end - window_start)
        holes: list[list[int]] = []
        cursor = window_start
        for a, b in merged:
            if a > cursor:
                holes.append([cursor, a])
            cursor = max(cursor, b)
        if cursor < window_end:
            holes.append([cursor, window_end])
        return {"window_start": window_start, "window_end": window_end,
                "covered_pct": round(100.0 * covered / total, 3),
                "hole_ms": total - covered, "holes": holes}

    def record_coverage_audit(self, result: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO coverage_audits (ts_utc, window_start, "
                "window_end, covered_pct, hole_ms, holes_json) "
                "VALUES (?,?,?,?,?,?)",
                (now_ms(), result["window_start"], result["window_end"],
                 result["covered_pct"], result["hole_ms"],
                 json.dumps(result["holes"][:50])))
            self._conn.commit()

    # ------------------------------ erasure -------------------------------
    def erase_range(self, ts_start_ms: int, ts_end_ms: int) -> dict[str, int]:
        """Delete every perception row inside the window. Gap rows inside the
        window are kept only if they extend beyond it (clipped); the caller
        (erasure.py) writes one privacy_excluded gap spanning the erasure.
        Also returns `capture_ids` (list) so LanceDB ocr_blocks can be swept."""
        counts: dict = {}
        with self._lock:
            cap_rows = self._conn.execute(
                "SELECT capture_id, frame_sha256, thumb_sha256 FROM captures "
                "WHERE ts_utc >= ? AND ts_utc < ?",
                (ts_start_ms, ts_end_ms)).fetchall()
            cap_ids = [r["capture_id"] for r in cap_rows]
            frame_shas = [r["frame_sha256"] for r in cap_rows if r["frame_sha256"]]
            thumb_shas = [r["thumb_sha256"] for r in cap_rows if r["thumb_sha256"]]
            counts["capture_ids"] = list(cap_ids)
            counts["frame_shas"] = list(frame_shas)
            counts["thumb_shas"] = list(thumb_shas)
            marks = ",".join("?" for _ in cap_ids)
            if cap_ids:
                if self.fts_ok:
                    # FTS content rows must go before their ocr_lines rows.
                    self._conn.execute(
                        f"""DELETE FROM ocr_fts WHERE rowid IN (
                              SELECT ol.rowid FROM ocr_lines ol
                              WHERE ol.first_capture_id IN ({marks}))""",
                        cap_ids)
                counts["ocr_lines"] = self._conn.execute(
                    f"DELETE FROM ocr_lines WHERE first_capture_id IN ({marks})",
                    cap_ids).rowcount
                counts["frame_line_map"] = self._conn.execute(
                    f"DELETE FROM frame_line_map WHERE capture_id IN ({marks})",
                    cap_ids).rowcount
                counts["extractions"] = self._conn.execute(
                    f"DELETE FROM extractions WHERE capture_id IN ({marks})",
                    cap_ids).rowcount
            for table in ("meta_events", "captures", "salience_scores"):
                counts[table] = self._conn.execute(
                    f"DELETE FROM {table} WHERE ts_utc >= ? AND ts_utc < ?",
                    (ts_start_ms, ts_end_ms)).rowcount
            counts["activity_blocks"] = self._conn.execute(
                "DELETE FROM activity_blocks WHERE ts_start >= ? AND "
                "ts_end < ?", (ts_start_ms, ts_end_ms)).rowcount
            self._conn.commit()
        return counts

    # ------------------------------ L3 writers ----------------------------
    def upsert_activity_block(self, block) -> str:
        """Idempotent on (ts_start, dominant_app): reuse block_id / replace."""
        import json as _json
        from app.perception.schemas import ActivityBlock
        if not isinstance(block, ActivityBlock):
            block = ActivityBlock(**block) if isinstance(block, dict) else block
        caps = block.capture_ids
        if isinstance(caps, str):
            caps_json = caps
        else:
            caps_json = _json.dumps(list(caps or []))
        with self._lock:
            existing = self._conn.execute(
                "SELECT block_id, ts_end, capture_ids, summary FROM "
                "activity_blocks WHERE ts_start=? AND dominant_app=? "
                "LIMIT 1",
                (block.ts_start, block.dominant_app or "")).fetchone()
            if existing:
                # Skip write when identical; otherwise replace in place.
                if (int(existing["ts_end"]) == int(block.ts_end)
                        and (existing["capture_ids"] or "") == caps_json
                        and (existing["summary"] or "") == (block.summary or "")):
                    return existing["block_id"]
                bid = existing["block_id"]
                self._conn.execute(
                    """UPDATE activity_blocks SET ts_end=?, dominant_domain=?,
                       dominant_doc=?, input_intensity=?, capture_ids=?,
                       summary=?, schema_version=? WHERE block_id=?""",
                    (block.ts_end, block.dominant_domain, block.dominant_doc,
                     block.input_intensity, caps_json, block.summary,
                     block.schema_version, bid))
                self._conn.commit()
                return bid
            self._conn.execute(
                """INSERT INTO activity_blocks
                   (block_id, ts_start, ts_end, dominant_app, dominant_domain,
                    dominant_doc, input_intensity, capture_ids, summary,
                    schema_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (block.block_id, block.ts_start, block.ts_end,
                 block.dominant_app, block.dominant_domain, block.dominant_doc,
                 block.input_intensity, caps_json, block.summary,
                 block.schema_version))
            self._conn.commit()
        return block.block_id

    def list_activity_blocks(self, since_ms: int | None = None,
                             limit: int = 200) -> list[dict]:
        q = "SELECT * FROM activity_blocks"
        args: list = []
        if since_ms is not None:
            q += " WHERE ts_end >= ?"
            args.append(since_ms)
        q += " ORDER BY ts_start DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def insert_extraction(self, ext) -> bool:
        """INSERT OR IGNORE on (type, norm_span_key, capture_id). True if new."""
        from app.perception.schemas import Extraction, now_ms
        if not isinstance(ext, Extraction):
            ext = Extraction(**ext) if isinstance(ext, dict) else ext
        ts = ext.ts_utc or now_ms()
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO extractions
                   (extraction_id, block_id, capture_id, type, payload_json,
                    confidence, source_span, norm_span_key, model,
                    model_version, egress, ts_utc, schema_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ext.extraction_id, ext.block_id, ext.capture_id, ext.type,
                 ext.payload_json, ext.confidence, ext.source_span,
                 ext.norm_span_key, ext.model, ext.model_version, ext.egress,
                 ts, ext.schema_version))
            self._conn.commit()
            return cur.rowcount > 0

    def has_extractions(self, capture_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM extractions WHERE capture_id=? LIMIT 1",
                (capture_id,)).fetchone()
            return row is not None

    def list_extractions(self, capture_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM extractions WHERE capture_id=?",
                (capture_id,)).fetchall()]

    def captures_needing_extract(self, limit: int = 50) -> list[dict]:
        """Captures that have OCR map rows and no extractions yet."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT c.* FROM captures c
                   WHERE c.kind IN ('full','scroll_delta')
                     AND EXISTS (SELECT 1 FROM frame_line_map flm
                                 WHERE flm.capture_id = c.capture_id)
                     AND NOT EXISTS (SELECT 1 FROM extractions e
                                    WHERE e.capture_id = c.capture_id)
                   ORDER BY c.ts_utc ASC LIMIT ?""", (int(limit),)).fetchall()
            return [dict(r) for r in rows]

    def recent_meta_ordered(self, since_ms: int, limit: int = 5000) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM meta_events WHERE ts_utc >= ? "
                "ORDER BY ts_utc ASC LIMIT ?", (since_ms, limit)).fetchall()]

    def get_export_watermark(self, table_name: str) -> tuple[int, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_ts_utc, last_rowid FROM export_watermarks "
                "WHERE table_name=?", (table_name,)).fetchone()
            if not row:
                return 0, 0
            return int(row["last_ts_utc"]), int(row["last_rowid"])

    def set_export_watermark(self, table_name: str, last_ts_utc: int,
                             last_rowid: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO export_watermarks (table_name, last_ts_utc, last_rowid)
                   VALUES (?,?,?)
                   ON CONFLICT(table_name) DO UPDATE SET
                     last_ts_utc=excluded.last_ts_utc,
                     last_rowid=excluded.last_rowid""",
                (table_name, int(last_ts_utc), int(last_rowid)))
            self._conn.commit()

    def export_rows_since(self, table: str, since_ts: int,
                          limit: int = 5000) -> list[dict]:
        """Fetch rows newer than watermark for Parquet export."""
        # Tables with ts_utc vs ts_start.
        ts_col = {
            "meta_events": "ts_utc", "captures": "ts_utc",
            "extractions": "ts_utc", "supervision_events": "ts_utc",
            "salience_scores": "ts_utc", "gaps": "ts_start",
            "activity_blocks": "ts_start", "ocr_lines": None,
        }.get(table)
        with self._lock:
            if table == "ocr_lines":
                # No ts — export by rowid watermark stored in last_rowid.
                rows = self._conn.execute(
                    "SELECT rowid AS _rowid, * FROM ocr_lines "
                    "WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
                    (since_ts, limit)).fetchall()
            elif ts_col:
                rows = self._conn.execute(
                    f"SELECT rowid AS _rowid, * FROM {table} "
                    f"WHERE {ts_col} > ? ORDER BY {ts_col} ASC LIMIT ?",
                    (since_ts, limit)).fetchall()
            else:
                return []
            return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        out = {}
        with self._lock:
            for t in ("meta_events", "gaps", "captures", "ocr_lines",
                      "extractions", "activity_blocks", "supervision_events"):
                out[t] = int(self._conn.execute(
                    f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_pstore: PerceptionStore | None = None
_pstore_lock = threading.Lock()


def get_pstore() -> PerceptionStore:
    global _pstore
    if _pstore is None:
        with _pstore_lock:
            if _pstore is None:
                _pstore = PerceptionStore()
    return _pstore
