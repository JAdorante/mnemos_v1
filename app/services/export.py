"""Data export & backup (WS-B) — the "prove I can leave" path.

``data/`` is the only copy of a user's memory. Before this, there was no way
out of it: no backup, no portable export, and one disk failure ends a tester's
participation. Two shapes, deliberately different:

* **Backup** (:func:`backup_stream`) — a faithful, restorable copy of the whole
  data directory. SQLite databases are copied with ``VACUUM INTO`` (or the
  ``sqlite3`` backup API) against the *live* connection, never a file copy: a
  hot WAL database copied with ``shutil`` restores as a corrupt or
  silently-stale database, which is worse than no backup. Restored by
  ``scripts/restore_backup.py``.

* **Takeout** (:func:`takeout_stream`) — portable JSONL a human can read
  without Sparrow installed: events, facts, people, relations, one file each,
  plus a README. ``redact=True`` runs the text fields through the crash-report
  redactor for a share-safe variant.

Both stream. The 107 GB incident is the reason: a data directory can be far
larger than RAM, so the zip is generated chunk-by-chunk into the response and
never buffered, and :func:`size_guard` refuses up front when the projected zip
would not comfortably fit in free space.

Secrets never enter either zip: ``.env``, ``.credentials.env``, the LAN API
token and the MCP token are excluded by name, and the exclusion is asserted by
walking the namelist in `test_export`.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator

from app.atomic_json import write_json
from app.config import settings
from app.version import __version__

# The manifest schema version. Bump when the zip layout changes in a way the
# restore script must reject rather than guess at.
BACKUP_SCHEMA = 1
TAKEOUT_SCHEMA = 1

MANIFEST_NAME = "manifest.json"
_STATE_FILE = "export_state.json"


class ExportError(RuntimeError):
    """Refused before doing any work (size guard, missing data dir)."""


def data_dir() -> Path:
    return Path(os.environ.get("QUILL_DATA_DIR") or settings.storage.data_dir)


def _excluded() -> tuple[str, ...]:
    return tuple(settings.export.excluded)


def _is_excluded(rel: Path) -> bool:
    """Secrets and volatile sidecars, matched on any path component.

    WAL/SHM sidecars are excluded because the DB is exported through
    ``VACUUM INTO`` instead — shipping a stale -wal beside a checkpointed copy
    is precisely how a "backup" restores as yesterday's database.
    """
    names = set(rel.parts) | {rel.name}
    if names & set(_excluded()):
        return True
    return rel.name.endswith(("-wal", "-shm", ".db-journal"))


def _is_sqlite(path: Path) -> bool:
    if path.suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        return False
    try:
        with path.open("rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


# --------------------------------------------------------------------------
# size guard
# --------------------------------------------------------------------------
def dir_size(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file() and not _is_excluded(p.relative_to(root)):
                total += p.stat().st_size
        except OSError:
            continue
    return total


def size_guard(root: Path | None = None, *, dest: Path | None = None,
               fraction: float | None = None) -> dict[str, Any]:
    """Refuse when the projected zip would exceed free disk * fraction.

    Projected size is the raw byte total: compression only ever helps, so this
    is a deliberately pessimistic bound. Returning rather than raising lets the
    caller report the numbers to the user.
    """
    root = Path(root or data_dir())
    frac = float(fraction if fraction is not None
                 else settings.export.free_disk_fraction)
    projected = dir_size(root)
    try:
        free = shutil.disk_usage(str(dest or root)).free
    except OSError:
        free = 0
    budget = free * frac
    return {
        "ok": projected <= budget,
        "projected_bytes": projected,
        "free_bytes": free,
        "budget_bytes": int(budget),
        "fraction": frac,
        "detail": (
            f"Backup needs about {projected / 1e9:.1f} GB but only "
            f"{free / 1e9:.1f} GB is free "
            f"(the limit is {frac:.0%} of free space). Free some space, or "
            f"back up to another drive."
        ) if projected > budget else "",
    }


# --------------------------------------------------------------------------
# consistent SQLite copies
# --------------------------------------------------------------------------
def snapshot_sqlite(src: Path, dest: Path) -> None:
    """Copy a *live* SQLite database consistently.

    ``VACUUM INTO`` first (compact, single file, no sidecars); if the SQLite
    build predates it, fall back to the backup API. Both take a read
    transaction, so a writer mid-flight produces a consistent snapshot at a
    single point in time — which a file copy of a WAL database does not.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    conn = sqlite3.connect(str(src))
    try:
        try:
            conn.execute("VACUUM INTO ?", (str(dest),))
            return
        except sqlite3.OperationalError:
            pass  # SQLite < 3.27: fall through to the backup API
        out = sqlite3.connect(str(dest))
        try:
            conn.backup(out)
        finally:
            out.close()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# manifests
# --------------------------------------------------------------------------
_COUNT_TABLES = ("events", "facts", "people", "relations")


def counts_from_db(path: Path) -> dict[str, Any]:
    """Row counts read from a *snapshot* file, not the live store.

    The manifest has to describe the database the zip actually contains. Read
    from the live store instead and a backup taken while capture is running
    reports the counts from before the snapshot — the restore script then
    prints numbers that do not match what it restored.
    """
    out: dict[str, Any] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in _COUNT_TABLES:
            if table in present:
                out[table] = int(conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error as exc:
        out["error"] = str(exc)
    finally:
        conn.close()
    return out


def _vector_count() -> dict[str, Any]:
    try:
        from app.vectorstore import get_vectorstore
        return {"vectors": len(get_vectorstore().list_ids())}
    except Exception as exc:
        return {"vectors_error": str(exc)}


def _schema_versions(store=None) -> dict[str, Any]:
    """Enough of the schema shape for the restore script to refuse a mismatch."""
    out: dict[str, Any] = {"backup": BACKUP_SCHEMA}
    try:
        if store is None:
            from app.storage import get_store
            store = get_store()
        rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        out["tables"] = [r[0] for r in rows]
        out["user_version"] = int(store._conn.execute(
            "PRAGMA user_version").fetchone()[0])
    except Exception as exc:
        out["error"] = str(exc)
    return out


def backup_manifest(store=None, *, now: float | None = None,
                    counts: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.services.usage_ledger import install_id
    import platform
    return {
        "kind": "mnemos.backup",
        "schema": BACKUP_SCHEMA,
        "app_version": __version__,
        "schema_versions": _schema_versions(store),
        "created_at": float(now if now is not None else time.time()),
        "install_id": install_id(),
        "os": platform.system(),
        # Counted from the snapshot in the zip, not the live store — see
        # counts_from_db.
        "counts": {**(counts or {}), **_vector_count()},
        "excluded": list(_excluded()),
    }


# --------------------------------------------------------------------------
# backup — streamed zip of the data directory
# --------------------------------------------------------------------------
class _Sink:
    """A file-like that hands each written block straight to the generator."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        self._pos += len(data)
        return len(data)

    def flush(self) -> None:
        return None

    def tell(self) -> int:
        return self._pos

    def drain(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


def backup_stream(root: Path | None = None, *, store=None,
                  check_size: bool = True,
                  now: float | None = None) -> Iterator[bytes]:
    """Yield a zip of the data directory, chunk by chunk. Never buffers it.

    Databases go in as ``VACUUM INTO`` snapshots (see :func:`snapshot_sqlite`);
    everything else is streamed from disk in ``chunk_bytes`` blocks. Raises
    :class:`ExportError` *before* yielding anything when the size guard fails,
    so the caller can still turn it into a clean HTTP error.
    """
    root = Path(root or data_dir())
    if not root.is_dir():
        raise ExportError(f"data directory not found: {root}")
    if check_size:
        guard = size_guard(root)
        if not guard["ok"]:
            raise ExportError(guard["detail"])

    chunk = max(4096, int(settings.export.chunk_bytes))
    sink = _Sink()
    tmp = Path(tempfile.mkdtemp(prefix="mnemos_backup_"))
    try:
        # Snapshot every live database FIRST, then describe those snapshots in
        # the manifest. The manifest has to be the zip's first entry (so a
        # reader can validate before extracting gigabytes), and it must report
        # what the zip contains — which means the databases exist before it is
        # written, not after.
        files: list[tuple[Path, Path]] = []          # (source-to-read, arc-rel)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if _is_excluded(rel):
                continue
            if _is_sqlite(path):
                snap = tmp / rel
                try:
                    snapshot_sqlite(path, snap)      # never a file copy
                except Exception as exc:
                    print(f"[export] {rel}: snapshot failed ({exc}); skipped.")
                    continue
                files.append((snap, rel))
            else:
                files.append((path, rel))

        # The main store is whichever snapshot carries the `events` table —
        # found by shape rather than by the frozen settings path, so a data dir
        # relocated at runtime still gets counted.
        counts: dict[str, Any] = {}
        for snap, _rel in files:
            if snap.is_relative_to(tmp):
                found = counts_from_db(snap)
                if "events" in found:
                    counts = found
                    break

        # ZIP_STORED: the payload is mostly already-compressed media and Lance
        # fragments, and deflating tens of GB to save a few percent turns a
        # backup into an hour of CPU.
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED,
                             allowZip64=True) as zf:
            zf.writestr(MANIFEST_NAME, json.dumps(
                backup_manifest(store, now=now, counts=counts), indent=2))
            yield sink.drain()
            for src, rel in files:
                arc = f"data/{rel.as_posix()}"
                try:
                    with src.open("rb") as fh, zf.open(arc, "w") as out:
                        while True:
                            block = fh.read(chunk)
                            if not block:
                                break
                            out.write(block)
                            yield sink.drain()
                except OSError as exc:
                    print(f"[export] {rel}: unreadable ({exc}); skipped.")
                yield sink.drain()
        yield sink.drain()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def write_backup(dest: Path, root: Path | None = None, *, store=None,
                 check_size: bool = True, now: float | None = None) -> dict[str, Any]:
    """Same stream, to a file — used by tests and by any offline caller."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with dest.open("wb") as fh:
        for block in backup_stream(root, store=store, check_size=check_size,
                                   now=now):
            fh.write(block)
            total += len(block)
    record_backup(dest, now=now)
    return {"ok": True, "path": str(dest), "bytes": total}


# --------------------------------------------------------------------------
# takeout — portable JSONL a human can read without Sparrow
# --------------------------------------------------------------------------
_README = """Sparrow takeout
==============

This is your memory, in plain files. Nothing here needs Sparrow to read.

  events.jsonl     one JSON object per line: every captured moment
                   (time is UTC epoch seconds; audio_path points into media/)
  facts.jsonl      what Sparrow extracted: tasks, commitments, claims, questions
  people.jsonl     the people it knows about, with aliases
  relations.jsonl  the graph edges between them
  media/           the referenced audio and frame files, kept at the same
                   relative paths the JSONL lines name
  manifest.json    counts, app version, and when this was made

Open a .jsonl in any text editor, or:

  python -c "import json;[print(json.loads(l)['summary']) for l in open('events.jsonl')]"

REDACTED note: if this export was made with redact=true, API keys and
personal-class lines have been scrubbed from the text fields. That variant is
for sharing; take an unredacted one for yourself.

This is an export, not a backup. To restore a working Sparrow, use the backup
zip (Privacy controls -> "Back up my memory") with scripts/restore_backup.py.
"""


def _jsonl(rows: list[dict], redact: bool) -> bytes:
    from app.services import crash_report
    out = []
    for row in rows:
        if redact:
            row = _redact_row(row, crash_report._redact)
        out.append(json.dumps(row, ensure_ascii=False, default=str))
    return ("\n".join(out) + ("\n" if out else "")).encode("utf-8")


def _redact_row(row: Any, redactor) -> Any:
    """Redact text fields, recursively, leaving numbers and structure alone."""
    if isinstance(row, dict):
        return {k: _redact_row(v, redactor) for k, v in row.items()}
    if isinstance(row, list):
        return [_redact_row(v, redactor) for v in row]
    if isinstance(row, str):
        return redactor(row)
    return row


def takeout_rows(store=None) -> dict[str, list[dict]]:
    if store is None:
        from app.storage import get_store
        store = get_store()
    events = [{"id": eid, **ev.to_dict()} for eid, ev in store.all_with_ids()]
    return {
        "events": events,
        "facts": store.list_facts(limit=1_000_000),
        "people": store.all_people(),
        "relations": store.all_relations(),
    }


def takeout_stream(root: Path | None = None, *, store=None, redact: bool = False,
                   check_size: bool = True,
                   now: float | None = None) -> Iterator[bytes]:
    """Yield a portable, human-readable export as a streamed zip."""
    root = Path(root or data_dir())
    if check_size:
        guard = size_guard(root)
        if not guard["ok"]:
            raise ExportError(guard["detail"])
    chunk = max(4096, int(settings.export.chunk_bytes))
    sink = _Sink()
    rows = takeout_rows(store)
    referenced: set[str] = set()
    for ev in rows["events"]:
        for key in ("audio_path", "frame_path", "image_path"):
            val = (ev.get("meta") or {}).get(key) if isinstance(ev.get("meta"), dict) else None
            val = val or ev.get(key)
            if isinstance(val, str) and val.strip():
                referenced.add(val)

    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        manifest = {
            "kind": "mnemos.takeout",
            "schema": TAKEOUT_SCHEMA,
            "app_version": __version__,
            "created_at": float(now if now is not None else time.time()),
            "redacted": bool(redact),
            "counts": {name: len(items) for name, items in rows.items()},
        }
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
        zf.writestr("README.txt", _README)
        yield sink.drain()
        for name, items in rows.items():
            zf.writestr(f"{name}.jsonl", _jsonl(items, redact))
            yield sink.drain()
        # Media stays at the relative path the JSONL lines name, so a line's
        # audio_path resolves inside the zip without rewriting anything.
        for ref in sorted(referenced):
            src = Path(ref)
            if not src.is_absolute():
                src = root / ref
            try:
                rel = src.resolve().relative_to(root.resolve())
            except (ValueError, OSError):
                continue
            if not src.is_file() or _is_excluded(rel):
                continue
            try:
                with src.open("rb") as fh, zf.open(f"media/{rel.as_posix()}", "w") as out:
                    while True:
                        block = fh.read(chunk)
                        if not block:
                            break
                        out.write(block)
                        yield sink.drain()
            except OSError as exc:
                print(f"[export] takeout media {rel} skipped ({exc}).")
            yield sink.drain()
    yield sink.drain()


def write_takeout(dest: Path, root: Path | None = None, *, store=None,
                  redact: bool = False, check_size: bool = True) -> dict[str, Any]:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with dest.open("wb") as fh:
        for block in takeout_stream(root, store=store, redact=redact,
                                    check_size=check_size):
            fh.write(block)
            total += len(block)
    return {"ok": True, "path": str(dest), "bytes": total}


# --------------------------------------------------------------------------
# "last backup" bookkeeping for the Privacy controls
# --------------------------------------------------------------------------
def state_path() -> Path:
    return data_dir() / _STATE_FILE


def record_backup(path: Path | str, *, now: float | None = None) -> None:
    try:
        write_json(state_path(), {
            "last_backup_at": float(now if now is not None else time.time()),
            "last_backup_name": Path(path).name,
        })
    except Exception as exc:
        print(f"[export] backup stamp skipped ({exc}).")


def status() -> dict[str, Any]:
    state: dict[str, Any] = {}
    try:
        p = state_path()
        if p.is_file():
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
    except Exception as exc:
        print(f"[export] status read skipped ({exc}).")
    last = state.get("last_backup_at")
    human = None
    if last:
        human = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last)))
    guard = size_guard()
    return {
        "ok": True,
        "last_backup_at": last,
        "last_backup_human": human,
        "last_backup_name": state.get("last_backup_name"),
        "data_bytes": guard["projected_bytes"],
        "free_bytes": guard["free_bytes"],
        "backup_possible": guard["ok"],
        "size_detail": guard["detail"],
        "excluded": list(_excluded()),
    }


def suggested_name(kind: str = "backup", *, now: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S",
                          time.localtime(now if now is not None else time.time()))
    return f"mnemos-{kind}-{stamp}.zip"
