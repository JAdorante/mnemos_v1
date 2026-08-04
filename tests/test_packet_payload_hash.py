"""Packet payload hash + expiry (plan task 0.3).

Every new action packet mints payload_hash = sha256(canonical fields_json)
and expires_at = created_at + 15m. Columns are additive (migration) so older
DBs keep working; only newly recorded packets are bound.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from app.services.agent_log import (
    Recorder,
    canonicalize_packet_fields,
    hash_packet_payload,
)
from app.storage import Store, _PACKET_TTL_S


FIELDS = {"action": "Send email", "to": "Marc", "subject": "Pricing",
          "body": "Following up."}


class CanonicalHashTests(unittest.TestCase):
    def test_key_order_independent(self):
        a = {"to": "Marc", "action": "Send email", "body": "Hi"}
        b = {"body": "Hi", "action": "Send email", "to": "Marc"}
        self.assertEqual(canonicalize_packet_fields(a),
                         canonicalize_packet_fields(b))
        self.assertEqual(hash_packet_payload(a), hash_packet_payload(b))

    def test_hash_is_sha256_of_canonical_json(self):
        canon = canonicalize_packet_fields(FIELDS)
        expected = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        self.assertEqual(hash_packet_payload(FIELDS), expected)
        self.assertEqual(len(expected), 64)

    def test_empty_fields_hash_stable(self):
        self.assertEqual(hash_packet_payload(None), hash_packet_payload({}))
        self.assertEqual(canonicalize_packet_fields(None), "{}")


class RecordPacketHashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def _row(self, pid: int) -> dict:
        row = self.store._conn.execute(
            "SELECT payload_hash, expires_at, created_at, fields_json, "
            "approved_at, approved_via, executed_hash "
            "FROM action_packets WHERE id = ?", (pid,)).fetchone()
        return dict(row)

    def test_record_mints_hash_and_expiry(self):
        before = time.time()
        pid = self.store.record_action_packet(
            summary="Send email to Marc", fields=FIELDS,
            execution_surface="browser", risk_level="high")
        after = time.time()
        row = self._row(pid)
        self.assertEqual(row["payload_hash"], hash_packet_payload(FIELDS))
        self.assertIsNotNone(row["expires_at"])
        self.assertIsNotNone(row["created_at"])
        # TTL window: expires ≈ created + 900s
        self.assertAlmostEqual(row["expires_at"] - row["created_at"],
                               _PACKET_TTL_S, delta=0.05)
        self.assertGreaterEqual(row["created_at"], before - 0.5)
        self.assertLessEqual(row["created_at"], after + 0.5)
        # Approval/execution columns exist but stay NULL until later gates.
        self.assertIsNone(row["approved_at"])
        self.assertIsNone(row["approved_via"])
        self.assertIsNone(row["executed_hash"])

    def test_persisted_fields_json_matches_canonical_hash(self):
        pid = self.store.record_action_packet(fields=FIELDS)
        row = self._row(pid)
        # Re-hashing the stored JSON bytes must equal payload_hash.
        stored = json.loads(row["fields_json"])
        self.assertEqual(hash_packet_payload(stored), row["payload_hash"])
        self.assertEqual(row["fields_json"], canonicalize_packet_fields(FIELDS))

    def test_recorder_path_also_mints_hash(self):
        rec = Recorder(store=self.store)
        rec.start_run("Draft follow-up", surface="browser")
        pid = rec.record_packet(summary="Send email", fields=FIELDS,
                                execution_surface="browser")
        self.assertIsNotNone(pid)
        row = self._row(pid)
        self.assertEqual(row["payload_hash"], hash_packet_payload(FIELDS))
        self.assertAlmostEqual(row["expires_at"] - row["created_at"],
                               _PACKET_TTL_S, delta=0.05)

    def test_drifted_fields_produce_different_hash(self):
        pid = self.store.record_action_packet(fields=FIELDS)
        row = self._row(pid)
        drifted = dict(FIELDS, to="Eve", body="Different body")
        self.assertNotEqual(hash_packet_payload(drifted), row["payload_hash"])


class PacketColumnMigrationTests(unittest.TestCase):
    def test_pre_fix_db_gains_binding_columns(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "old.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE action_packets (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_run_id      INTEGER,
                goal              TEXT,
                summary           TEXT,
                fields_json       TEXT,
                context_json      TEXT,
                source_fact_ids   TEXT,
                approval_required INTEGER NOT NULL DEFAULT 1,
                risk_level        TEXT,
                suggested_agent   TEXT,
                execution_surface TEXT,
                success_criteria  TEXT,
                fallback          TEXT,
                decision          TEXT,
                created_at        REAL    NOT NULL
            )
            """)
        conn.execute(
            "INSERT INTO action_packets (summary, fields_json, created_at) "
            "VALUES ('legacy', '{}', 1.0)")
        conn.commit()
        conn.close()

        store = Store(db_path=db, audio_dir=tmp / "audio")
        cols = {r["name"] for r in store._conn.execute(
            "PRAGMA table_info(action_packets)").fetchall()}
        for col in ("payload_hash", "expires_at", "approved_at",
                    "approved_via", "executed_hash", "executed_at"):
            self.assertIn(col, cols)
        # Legacy row keeps NULL hash — only new packets are bound.
        legacy = dict(store._conn.execute(
            "SELECT payload_hash, expires_at FROM action_packets "
            "WHERE summary = 'legacy'").fetchone())
        self.assertIsNone(legacy["payload_hash"])
        self.assertIsNone(legacy["expires_at"])
        # Fresh insert on the migrated schema still mints both.
        pid = store.record_action_packet(fields=FIELDS)
        fresh = dict(store._conn.execute(
            "SELECT payload_hash, expires_at FROM action_packets "
            "WHERE id = ?", (pid,)).fetchone())
        self.assertEqual(fresh["payload_hash"], hash_packet_payload(FIELDS))
        self.assertIsNotNone(fresh["expires_at"])


if __name__ == "__main__":
    unittest.main()
