"""WS-B — backup, takeout, restore.

The claims under test are the ones a tester's data depends on:
a hot database survives the copy, secrets never enter a zip, the takeout is
readable without Mnemos, and a backup round-trips through the restore script
to the same rows.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import export
from app.storage import Store
from app.version import __version__

import importlib.util
_SPEC = importlib.util.spec_from_file_location(
    "restore_backup", Path(__file__).resolve().parent.parent
    / "scripts" / "restore_backup.py")
rb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rb)


def seed(store: Store, n: int = 25) -> None:
    for i in range(n):
        store.insert(Event(time=1_756_000_000.0 + i, modality=Modality.AUDIO,
                           raw=f"utterance number {i}",
                           summary=f"summary {i}", source="test"))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="quill_exp_"))
        self.data = self.root / "data"
        self.data.mkdir()
        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": str(self.data)},
                              clear=False)
        self.env.start()
        self.store = Store(db_path=self.data / "quill.db",
                           audio_dir=self.data / "audio")
        seed(self.store)
        # A realistic data directory: media, sidecars, and secrets.
        (self.data / "audio" / "1756000000.wav").write_bytes(b"RIFF" + b"\0" * 512)
        (self.data / "frames").mkdir()
        (self.data / "frames" / "f1.jpg").write_bytes(b"\xff\xd8" + b"\0" * 256)
        (self.data / "lance").mkdir()
        (self.data / "lance" / "data.lance").write_bytes(b"lance" * 100)
        (self.data / "capture_consent.json").write_text('{"consented": true}')
        (self.data / ".api_token").write_text("LAN-TOKEN-SECRET-0001")
        (self.data / ".mcp_token").write_text("MCP-TOKEN-SECRET-0001")
        (self.root / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-secret\n")

    def tearDown(self) -> None:
        self.env.stop()

    def backup(self, **kw) -> Path:
        out = self.root / "backup.zip"
        export.write_backup(out, self.data, store=self.store, **kw)
        return out


class ManifestTests(_Base):
    def test_manifest_carries_versions_and_counts(self) -> None:
        with zipfile.ZipFile(self.backup()) as zf:
            man = json.loads(zf.read("manifest.json"))
        self.assertEqual(man["kind"], "mnemos.backup")
        self.assertEqual(man["schema"], export.BACKUP_SCHEMA)
        self.assertEqual(man["app_version"], __version__)
        self.assertEqual(man["counts"]["events"], 25)
        self.assertIn("facts", man["counts"])
        self.assertIn("people", man["counts"])
        self.assertIn("tables", man["schema_versions"])
        self.assertIn("events", man["schema_versions"]["tables"])
        self.assertTrue(man["install_id"])
        self.assertIsInstance(man["created_at"], float)


    def test_counts_describe_the_snapshot_not_the_live_store(self) -> None:
        """A backup taken mid-capture must not report pre-snapshot counts.

        The manifest is the zip's first entry, so it is tempting to fill it
        from the live store before copying anything — and then a busy install
        ships a manifest saying 307 events beside a database holding 657.
        """
        import threading
        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                self.store.insert(Event(time=time.time(), modality=Modality.AUDIO,
                                        raw=f"live {i}", summary=f"l{i}",
                                        source="test"))
                i += 1

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            path = self.backup()
        finally:
            stop.set()
            t.join(timeout=5)

        target = self.root / "counted"
        rb.restore(path, target)
        with zipfile.ZipFile(path) as zf:
            man = json.loads(zf.read("manifest.json"))
        restored = Store(db_path=target / "quill.db", audio_dir=target / "audio")
        self.assertEqual(man["counts"]["events"], restored.count())
        self.assertEqual(man["counts"]["facts"], restored.fact_count())
        # And it really was a moving target, or this test proves nothing.
        self.assertGreater(self.store.count(), 25)


class SecretExclusionTests(_Base):
    def _names(self, path: Path) -> list[str]:
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()

    def test_tokens_and_env_never_appear_in_a_backup(self) -> None:
        names = self._names(self.backup())
        for secret in (".env", ".credentials.env", ".api_token", ".mcp_token"):
            for name in names:
                self.assertNotIn(secret, name, f"{secret} leaked as {name}")

    def test_no_secret_bytes_appear_anywhere_in_the_zip(self) -> None:
        """Walk the contents, not just the names."""
        path = self.backup()
        blob = path.read_bytes()
        self.assertNotIn(b"LAN-TOKEN-SECRET-0001", blob)
        self.assertNotIn(b"MCP-TOKEN-SECRET-0001", blob)
        self.assertNotIn(b"sk-ant-secret", blob)

    def test_wal_sidecars_are_excluded(self) -> None:
        """The DB is exported via VACUUM INTO; a stale -wal beside it would
        restore as yesterday's database."""
        (self.data / "quill.db-wal").write_bytes(b"stale wal")
        (self.data / "quill.db-shm").write_bytes(b"stale shm")
        names = self._names(self.backup())
        self.assertIn("data/quill.db", names)
        self.assertFalse([n for n in names if n.endswith(("-wal", "-shm"))])

    def test_takeout_excludes_secrets_too(self) -> None:
        out = self.root / "t.zip"
        export.write_takeout(out, self.data, store=self.store)
        blob = out.read_bytes()
        self.assertNotIn(b"LAN-TOKEN-SECRET-0001", blob)
        self.assertNotIn(b"sk-ant-secret", blob)


class HotDatabaseTests(_Base):
    def test_backup_of_a_live_database_is_consistent(self) -> None:
        """Write throughout the backup; the copy must open and be coherent."""
        stop = threading.Event()
        errors: list[Exception] = []

        def writer():
            i = 0
            while not stop.is_set():
                try:
                    self.store.insert(Event(
                        time=time.time(), modality=Modality.AUDIO,
                        raw=f"concurrent {i}", summary=f"c{i}", source="test"))
                    i += 1
                except Exception as exc:      # pragma: no cover
                    errors.append(exc)
                    return

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            path = self.backup()
        finally:
            stop.set()
            t.join(timeout=5)
        self.assertEqual(errors, [])

        extracted = self.root / "check"
        with zipfile.ZipFile(path) as zf:
            zf.extract("data/quill.db", extracted)
        db = extracted / "data" / "quill.db"
        conn = sqlite3.connect(str(db))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0],
                             "ok")
            n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            # A point-in-time snapshot: at least the seed, at most what exists
            # now — never a torn read.
            self.assertGreaterEqual(n, 25)
            self.assertLessEqual(n, self.store.count())
        finally:
            conn.close()

    def test_snapshot_is_not_a_file_copy(self) -> None:
        """VACUUM INTO produces a checkpointed single file with no sidecars."""
        dest = self.root / "snap.db"
        export.snapshot_sqlite(self.data / "quill.db", dest)
        self.assertTrue(dest.is_file())
        self.assertFalse(dest.with_name(dest.name + "-wal").exists())
        conn = sqlite3.connect(str(dest))
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 25)
        finally:
            conn.close()


class SizeGuardTests(_Base):
    def test_guard_refuses_when_the_projection_exceeds_the_budget(self) -> None:
        import shutil as _sh
        from collections import namedtuple
        Usage = namedtuple("Usage", "total used free")
        with patch.object(_sh, "disk_usage", return_value=Usage(1, 1, 1000)):
            guard = export.size_guard(self.data)
        self.assertFalse(guard["ok"])
        self.assertIn("free", guard["detail"])
        with self.assertRaises(export.ExportError):
            with patch.object(_sh, "disk_usage", return_value=Usage(1, 1, 1000)):
                next(export.backup_stream(self.data, store=self.store))

    def test_guard_passes_with_room(self) -> None:
        self.assertTrue(export.size_guard(self.data)["ok"])

    def test_guard_can_be_skipped_for_an_offline_caller(self) -> None:
        self.assertTrue(self.backup(check_size=False).is_file())

    def test_status_reports_the_last_backup(self) -> None:
        self.assertIsNone(export.status()["last_backup_at"])
        self.backup()
        st = export.status()
        self.assertIsNotNone(st["last_backup_at"])
        self.assertIsNotNone(st["last_backup_human"])
        self.assertTrue(st["backup_possible"])


class StreamingTests(_Base):
    def test_the_zip_arrives_in_pieces_not_one_blob(self) -> None:
        """Streaming is the whole point — a 107 GB data dir cannot be buffered."""
        chunks = [c for c in export.backup_stream(self.data, store=self.store)
                  if c]
        self.assertGreater(len(chunks), 3)
        # And no single chunk holds the whole archive.
        total = sum(len(c) for c in chunks)
        self.assertLess(max(len(c) for c in chunks), total)

    def test_streamed_bytes_are_a_valid_zip(self) -> None:
        import io
        blob = b"".join(export.backup_stream(self.data, store=self.store))
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            self.assertIsNone(zf.testzip())
            self.assertIn("manifest.json", zf.namelist())


class TakeoutTests(_Base):
    def _takeout(self, redact: bool = False) -> zipfile.ZipFile:
        out = self.root / f"takeout-{redact}.zip"
        export.write_takeout(out, self.data, store=self.store, redact=redact)
        return zipfile.ZipFile(out)

    def test_jsonl_parses_and_counts_match_the_store(self) -> None:
        with self._takeout() as zf:
            names = zf.namelist()
            for f in ("events.jsonl", "facts.jsonl", "people.jsonl",
                      "relations.jsonl", "README.txt", "manifest.json"):
                self.assertIn(f, names)
            events = [json.loads(line) for line in
                      zf.read("events.jsonl").decode("utf-8").splitlines()]
            man = json.loads(zf.read("manifest.json"))
        self.assertEqual(len(events), self.store.count())
        self.assertEqual(man["counts"]["events"], self.store.count())
        self.assertEqual(man["kind"], "mnemos.takeout")
        self.assertEqual(events[0]["summary"], "summary 0")

    def test_readme_explains_the_layout(self) -> None:
        with self._takeout() as zf:
            readme = zf.read("README.txt").decode("utf-8")
        for f in ("events.jsonl", "facts.jsonl", "people.jsonl",
                  "relations.jsonl", "media/"):
            self.assertIn(f, readme)
        # It must say plainly that this is not the restore path.
        self.assertIn("not a backup", readme)

    def test_redacted_variant_scrubs_keys_and_personal_lines(self) -> None:
        self.store.insert(Event(
            time=1_756_100_000.0, modality=Modality.AUDIO,
            raw="my key is sk-ant-api03-SECRETKEYVALUEHERE0001",
            summary="spouse therapy appointment on Tuesday", source="test"))
        with self._takeout(redact=True) as zf:
            blob = zf.read("events.jsonl").decode("utf-8")
            man = json.loads(zf.read("manifest.json"))
        self.assertTrue(man["redacted"])
        self.assertNotIn("SECRETKEYVALUEHERE0001", blob)
        self.assertIn("[REDACTED_KEY]", blob)
        self.assertNotIn("therapy", blob.lower())

    def test_unredacted_variant_keeps_the_user_their_own_data(self) -> None:
        self.store.insert(Event(time=1_756_100_000.0, modality=Modality.AUDIO,
                                raw="spouse therapy appointment",
                                summary="spouse therapy appointment",
                                source="test"))
        with self._takeout(redact=False) as zf:
            blob = zf.read("events.jsonl").decode("utf-8")
        self.assertIn("therapy", blob)

    def test_referenced_media_travels_with_the_export(self) -> None:
        wav = self.data / "audio" / "1756000000.wav"
        self.store.insert(Event(
            time=1_756_200_000.0, modality=Modality.AUDIO, raw="with audio",
            summary="with audio", source="test",
            meta={"audio_path": str(wav)}))
        with self._takeout() as zf:
            names = zf.namelist()
        self.assertIn("media/audio/1756000000.wav", names)


class RestoreRoundTripTests(_Base):
    def test_backup_restores_to_an_equivalent_database(self) -> None:
        path = self.backup()
        before = {
            "events": self.store.count(),
            "facts": self.store.fact_count(),
            "rows": [(e.time, e.summary) for _, e in self.store.all_with_ids()],
        }
        target = self.root / "restored"
        out = rb.restore(path, target)
        self.assertTrue(out["ok"])

        restored = Store(db_path=target / "quill.db", audio_dir=target / "audio")
        self.assertEqual(restored.count(), before["events"])
        self.assertEqual(restored.fact_count(), before["facts"])
        self.assertEqual([(e.time, e.summary)
                          for _, e in restored.all_with_ids()], before["rows"])
        # Media and sidecar files came back too.
        self.assertTrue((target / "audio" / "1756000000.wav").is_file())
        self.assertTrue((target / "lance" / "data.lance").is_file())
        self.assertTrue((target / "RESTORED_FROM.json").is_file())

    def test_restore_swaps_atomically_and_can_keep_the_old_dir(self) -> None:
        path = self.backup()
        target = self.root / "existing"
        target.mkdir()
        (target / "old-marker.txt").write_text("previous install")
        out = rb.restore(path, target, keep_old=True)
        self.assertFalse((target / "old-marker.txt").exists())
        kept = Path(out["previous_kept_at"])
        self.assertTrue((kept / "old-marker.txt").is_file())

    def test_restore_refuses_a_takeout_zip(self) -> None:
        t = self.root / "t.zip"
        export.write_takeout(t, self.data, store=self.store)
        with self.assertRaises(ValueError) as ctx:
            rb.restore(t, self.root / "nope")
        # A takeout carries a manifest too — the refusal is on `kind`, and the
        # message has to tell the user which zip they actually want.
        self.assertIn("not a Mnemos backup", str(ctx.exception))
        self.assertIn("mnemos.takeout", str(ctx.exception))

    def test_restore_refuses_a_newer_backup_unless_forced(self) -> None:
        path = self.backup()
        problems = rb.validate({"kind": "mnemos.backup", "schema": 1,
                                "app_version": "99.0.0"},
                               current_version=__version__)
        self.assertTrue(problems)
        self.assertIn("newer than this build", problems[0])
        self.assertEqual(
            rb.validate({"kind": "mnemos.backup", "schema": 1,
                         "app_version": "99.0.0"},
                        current_version=__version__, strict=False), [])

    def test_restore_refuses_an_unsupported_schema(self) -> None:
        problems = rb.validate({"kind": "mnemos.backup", "schema": 99,
                                "app_version": __version__},
                               current_version=__version__)
        self.assertTrue(any("schema" in p for p in problems))

    def test_restore_refuses_path_traversal(self) -> None:
        evil = self.root / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("manifest.json", json.dumps(
                {"kind": "mnemos.backup", "schema": 1,
                 "app_version": __version__}))
            zf.writestr("data/../../escaped.txt", "pwned")
        with self.assertRaises(ValueError) as ctx:
            rb.restore(evil, self.root / "target")
        self.assertIn("unsafe path", str(ctx.exception))

    def test_restore_refuses_while_the_server_is_up(self) -> None:
        path = self.backup()
        with patch.object(rb, "server_is_up", return_value=True):
            rc = rb.main([str(path), str(self.root / "t2")])
        self.assertEqual(rc, 1)
        self.assertFalse((self.root / "t2").exists())
        with patch.object(rb, "server_is_up", return_value=False):
            rc = rb.main([str(path), str(self.root / "t3")])
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "t3" / "quill.db").is_file())

    def test_probe_treats_an_unreachable_port_as_down(self) -> None:
        self.assertFalse(rb.server_is_up("127.0.0.1", 59_999, timeout=0.3))

    def test_cli_reports_a_corrupt_zip_rather_than_half_restoring(self) -> None:
        bad = self.root / "bad.zip"
        bad.write_bytes(b"not a zip at all")
        with patch.object(rb, "server_is_up", return_value=False):
            self.assertEqual(rb.main([str(bad), str(self.root / "t4")]), 1)
        self.assertFalse((self.root / "t4").exists())


class RouteTests(_Base):
    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app)

    def test_export_endpoints_stream_zips(self) -> None:
        store = self.store
        with patch("app.storage.get_store", lambda: store), \
                patch("app.services.export.data_dir", lambda: self.data):
            client = self._client()
            st = client.get("/export/status").json()
            self.assertTrue(st["ok"])

            r = client.get("/export/backup")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers["content-type"], "application/zip")
            self.assertIn("mnemos-backup-", r.headers["content-disposition"])
            import io
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                self.assertIn("manifest.json", zf.namelist())

            r = client.get("/export/takeout?redact=true")
            self.assertEqual(r.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                self.assertTrue(json.loads(zf.read("manifest.json"))["redacted"])

    def test_size_guard_surfaces_as_a_clean_error_not_a_truncated_zip(self) -> None:
        import shutil as _sh
        from collections import namedtuple
        Usage = namedtuple("Usage", "total used free")
        store = self.store
        with patch("app.storage.get_store", lambda: store), \
                patch("app.services.export.data_dir", lambda: self.data), \
                patch.object(_sh, "disk_usage", return_value=Usage(1, 1, 1000)):
            r = self._client().get("/export/backup")
        self.assertEqual(r.status_code, 400)
        self.assertIn("free", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
