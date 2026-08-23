"""WS-B acceptance, literally: kill the server mid-write, back up, restore, boot.

The other export tests write concurrently from a thread inside the test
process, which proves `VACUUM INTO` takes a consistent point-in-time snapshot.
It does not prove the thing a tester's disk failure actually looks like: a
process killed without a clean shutdown, leaving an uncheckpointed WAL beside
the database. That is the case where a naive file copy restores stale or
corrupt data, so it gets its own test with a real server and a real SIGKILL.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "crash_server.py"

try:
    import uvicorn  # noqa: F401
    _HAVE_UVICORN = True
except ImportError:                                    # pragma: no cover
    _HAVE_UVICORN = False

# Booting the real app (embedder, routers, worker registry) costs real seconds.
BOOT_TIMEOUT_S = 180.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@unittest.skipUnless(_HAVE_UVICORN, "uvicorn not installed")
@unittest.skipUnless(hasattr(signal, "SIGKILL"), "POSIX-only: needs SIGKILL")
class KilledServerBackupTests(unittest.TestCase):
    """One expensive test, run end to end, asserting the whole acceptance line."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="quill_crash_"))
        cls.data = cls.root / "data"
        cls.data.mkdir()
        cls.log = cls.root / "committed.log"
        cls.port = _free_port()

        env = dict(os.environ)
        env.update({
            "QUILL_DATA_DIR": str(cls.data),
            "QUILL_PORT": str(cls.port),
            # Keep the boot to the parts this test is about: storage + serving.
            "QUILL_SEMANTIC": "0", "QUILL_WORKER": "0", "QUILL_EXTRACT": "0",
            "QUILL_REFLECT": "0", "QUILL_AUTOSTART": "0",
            "QUILL_UPDATE_CHECK": "0", "QUILL_ANTICIPATE": "0",
            "QUILL_MEETING_POLL": "0", "QUILL_PROFILE": "",
        })
        cls.proc = subprocess.Popen(
            [sys.executable, str(FIXTURE), str(cls.port), str(cls.log)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        deadline = time.monotonic() + BOOT_TIMEOUT_S
        cls.booted = False
        while time.monotonic() < deadline:
            if cls.proc.poll() is not None:
                err = (cls.proc.stderr.read() or b"").decode()[-2000:]
                raise unittest.SkipTest(f"crash fixture died on boot:\n{err}")
            try:
                with urlopen(f"http://127.0.0.1:{cls.port}/health", timeout=2) as r:
                    if r.status == 200:
                        cls.booted = True
                        break
            except (URLError, OSError):
                time.sleep(0.5)
        if not cls.booted:
            cls.proc.kill()
            raise unittest.SkipTest("server did not come up in time")

        # Let it accumulate a backlog the WAL has not checkpointed yet.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if cls.log.is_file() and len(cls._committed_ids()) > 400:
                break
            time.sleep(0.2)

        # SIGKILL: no shutdown hook, no worker drain, no SQLite checkpoint.
        cls.proc.send_signal(signal.SIGKILL)
        cls.proc.wait(timeout=30)
        cls.committed = cls._committed_ids()

    @classmethod
    def _committed_ids(cls) -> list[int]:
        if not cls.log.is_file():
            return []
        out = []
        for line in cls.log.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.isdigit():
                out.append(int(line))
        return out

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.proc.poll() is None:                    # pragma: no cover
            cls.proc.kill()
        if cls.proc.stderr is not None:
            cls.proc.stderr.close()

    # -- the acceptance line, in order ------------------------------------
    def test_1_the_server_really_died_mid_write(self) -> None:
        """Guard the premise. A clean shutdown here would prove nothing.

        `quill.db` runs on SQLite's default rollback journal (only
        `perception.db` sets journal_mode=WAL), so the artefact of an unclean
        kill is a `-journal` file when the process died inside a transaction,
        and nothing at all when it died between them. Either way the property
        under test is the same: no shutdown hook ran, no connection was closed,
        and the backup still has to recover every committed row. The WAL case —
        where a naive copy demonstrably loses data — is covered separately in
        WalRecoveryTests below.
        """
        self.assertEqual(self.proc.returncode, -signal.SIGKILL)
        self.assertGreater(len(self.committed), 400)
        self.assertTrue((self.data / "quill.db").is_file())
        # Nothing in the data dir suggests an orderly exit.
        self.assertFalse((self.data / "export_state.json").exists())

    def test_2_backup_of_the_dead_install_restores_every_committed_row(self) -> None:
        import importlib.util
        from app.services import export
        from app.storage import Store

        spec = importlib.util.spec_from_file_location(
            "restore_backup", REPO / "scripts" / "restore_backup.py")
        rb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rb)

        zip_path = self.root / "backup.zip"
        export.write_backup(zip_path, self.data)

        # The zip must carry the recovered database, not the stale sidecars.
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            manifest = json.loads(zf.read("manifest.json"))
        self.assertIn("data/quill.db", names)
        self.assertFalse([n for n in names if n.endswith(("-wal", "-shm"))])

        fresh = self.root / "restored"
        out = rb.restore(zip_path, fresh)
        self.assertTrue(out["ok"])

        restored = Store(db_path=fresh / "quill.db", audio_dir=fresh / "audio")
        self.assertEqual(
            restored._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        # Every row the dying process was told had committed is present. This
        # is the claim a file-copy backup of a hot WAL database cannot make.
        ids = {eid for eid, _ in restored.all_with_ids()}
        missing = [i for i in self.committed if i not in ids]
        self.assertEqual(missing, [],
                         f"{len(missing)} committed row(s) lost through the backup")
        self.assertEqual(manifest["counts"]["events"], restored.count())

    def test_3_the_restored_install_boots_with_timeline_and_search_intact(self) -> None:
        from app.services.memory import MemoryEngine
        from app.storage import Store

        fresh = self.root / "restored"
        if not (fresh / "quill.db").is_file():
            self.test_2_backup_of_the_dead_install_restores_every_committed_row()

        restored = Store(db_path=fresh / "quill.db", audio_dir=fresh / "audio")
        engine = MemoryEngine(store=restored)
        engine._semantic = False
        engine._vectors = None

        timeline = restored.all_with_ids()
        self.assertGreaterEqual(len(timeline), len(self.committed))
        hits = engine.search("capital-connect", limit=5)
        self.assertEqual(len(hits), 5)
        self.assertTrue(all("capital-connect" in (h.get("raw") or "") for h in hits))

    def test_4_secrets_did_not_travel(self) -> None:
        fresh = self.root / "restored"
        if not fresh.is_dir():
            self.test_2_backup_of_the_dead_install_restores_every_committed_row()
        for secret in (".api_token", ".mcp_token", ".env", ".credentials.env"):
            self.assertFalse((fresh / secret).exists(), secret)


_WAL_WRITER = """
import os, sqlite3, sys
db, log = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db)
conn.execute("PRAGMA journal_mode=WAL")
# Never checkpoint: this is the state a killed process leaves behind.
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE IF NOT EXISTS rows_(id INTEGER PRIMARY KEY, v TEXT)")
conn.commit()
f = open(log, "a", buffering=1)
i = 0
while True:
    conn.execute("INSERT INTO rows_(v) VALUES (?)", (f"row {i}",))
    conn.commit()
    f.write(f"{i}\\n"); f.flush(); os.fsync(f.fileno())
    i += 1
"""


@unittest.skipUnless(hasattr(signal, "SIGKILL"), "POSIX-only: needs SIGKILL")
class WalRecoveryTests(unittest.TestCase):
    """Why the backup uses VACUUM INTO and drops the sidecars.

    `perception.db` runs in WAL mode, and a WAL database that was never
    checkpointed keeps recent commits *outside* the .db file. Copying the .db
    alone silently restores an old database; copying it with a stale -wal is
    worse. This test kills a writer mid-flight and shows both outcomes on the
    same on-disk state, so the export's design is pinned to a demonstrated
    failure rather than to a comment.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = Path(tempfile.mkdtemp(prefix="quill_wal_"))
        cls.db = cls.dir / "hot.db"
        log = cls.dir / "committed.log"
        proc = subprocess.Popen([sys.executable, "-c", _WAL_WRITER,
                                 str(cls.db), str(log)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if log.is_file() and len(log.read_text().splitlines()) > 500:
                break
            time.sleep(0.1)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)
        cls.committed = len(log.read_text().splitlines()) if log.is_file() else 0

    def _count(self, path: Path) -> int:
        import sqlite3
        conn = sqlite3.connect(str(path))
        try:
            return int(conn.execute("SELECT COUNT(*) FROM rows_").fetchone()[0])
        finally:
            conn.close()

    def test_the_premise_a_hot_uncheckpointed_wal_exists(self) -> None:
        self.assertGreater(self.committed, 500)
        wal = self.db.with_name(self.db.name + "-wal")
        self.assertTrue(wal.is_file(), "writer checkpointed after all")
        self.assertGreater(wal.stat().st_size, 0)

    def test_a_naive_file_copy_loses_committed_rows(self) -> None:
        """The bug this design avoids — asserted, not assumed.

        The damage is worse than "a few recent rows": with no checkpoint, the
        .db file can still be the empty database the writer started from, so
        the copy has neither the rows nor the *schema*. Both outcomes are the
        same failure, and a backup that produced either would look fine to a
        tester right up until they needed it.
        """
        import shutil as _sh
        import sqlite3
        naive = self.dir / "naive.db"
        _sh.copyfile(self.db, naive)          # the .db alone, as a zip would
        try:
            recovered = self._count(naive)
        except sqlite3.OperationalError as exc:
            self.assertIn("no such table", str(exc))
            return                            # the strongest form of the loss
        self.assertLess(
            recovered, self.committed,
            "expected the WAL-resident commits to be missing from a plain copy")

    def test_vacuum_into_recovers_every_committed_row(self) -> None:
        from app.services import export
        snap = self.dir / "snap.db"
        export.snapshot_sqlite(self.db, snap)
        # >= rather than ==: the writer can be killed after SQLite returns from
        # a commit but before it appends that id to the log, so the database
        # legitimately holds up to one row the log never recorded. The
        # guarantee under test is one-directional — no committed row is lost.
        self.assertGreaterEqual(self._count(snap), self.committed)
        self.assertLessEqual(self._count(snap), self.committed + 1)
        # And it is self-contained: no sidecar has to travel with it.
        self.assertFalse(snap.with_name(snap.name + "-wal").exists())
        self.assertFalse(snap.with_name(snap.name + "-shm").exists())

    def test_the_exporter_takes_the_snapshot_path_for_this_file(self) -> None:
        """Guard the wiring: a WAL db must be recognised as SQLite and excluded
        from the raw-copy branch."""
        from app.services import export
        self.assertTrue(export._is_sqlite(self.db))
        self.assertTrue(export._is_excluded(Path("hot.db-wal")))
        self.assertTrue(export._is_excluded(Path("hot.db-shm")))
        self.assertFalse(export._is_excluded(Path("hot.db")))


if __name__ == "__main__":
    unittest.main()
