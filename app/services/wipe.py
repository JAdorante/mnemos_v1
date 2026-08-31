"""Erasure — the "leave nothing behind" path (pilot blocker: clean uninstall).

A tester who wants out on day 2 must be able to stop capture in one click and
delete everything in another, and we must be able to say in writing that it
happened. Export (WS-B) proved a tester can *take* their memory; this proves
they can *destroy* it.

Four deliberate properties:

* **Capture stops first.** Wiping while the mic thread is still writing races
  the delete and leaves a freshly-created ``quill.db`` behind — precisely the
  outcome the tester was trying to avoid. :func:`stop_capture` revokes the
  durable consent allow-list *and* stops the running pipelines, and
  :func:`wipe` calls it before it deletes a byte. It is also worth calling on
  its own: it is the "stop everything now" control.

* **Open database handles are closed first.** A live SQLite connection makes
  the file undeletable on Windows, so the store singletons are closed and
  dropped before the walk. Without this the wipe half-succeeds on the operator's
  Linux box and fails on every tester's laptop.

* **Every place captured content lands, not just ``data/``.** Browser-agent
  page captures live in ``sessions/`` and the desktop agent's action audit in
  ``desktop_agent/sessions/`` — both outside ``QUILL_DATA_DIR`` and both easy
  to forget. :func:`targets` is the enumerated list and the receipt names each
  one with its own byte count, so "everything" is checkable rather than
  claimed.

* **A receipt that outlives the data.** :func:`wipe` writes a JSON receipt
  *beside* the install, never inside the directory it just emptied, recording
  per-target counts and anything it could not remove. Nothing in it is
  personal: paths, counts, version, install id, timestamp.

Shipped configuration (:data:`KEEP_NAMES`) survives unless ``full=True``. Those
files are byte-identical on every install and hold no personal data, while
deleting them leaves the app running fail-closed (a missing
``source_policies.json`` degrades to the restrictive fallback) rather than
fresh. The receipt states what was kept rather than implying an empty
directory.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from app.version import __version__

# The tester types this to confirm. Deliberately not "yes" or "delete": the
# whole point of the control is that it cannot be reached by reflex.
CONFIRM_PHRASE = "DELETE MY MEMORY"

# Shipped configuration, not memory. Kept unless full=True — see module docs.
KEEP_NAMES = ("model_prices.json", "source_policies.json", "score_config.json")

# Credentials are a separate switch: a tester wiping their memory to start over
# should not have to redeem a second invite code.
CREDENTIAL_NAMES = (".credentials.env", ".env")

RECEIPT_PREFIX = "mnemos-deletion-receipt"


class WipeRefused(RuntimeError):
    """Refused before deleting anything (bad confirmation, unsafe path)."""


def install_root() -> Path:
    """Where the app's files are. In a frozen build that is the bundle, not a
    checkout — ``parents[2]`` would point inside the PyInstaller payload."""
    from app.runtime import bundle_root
    return bundle_root()


def data_dir() -> Path:
    from app.services import export
    return export.data_dir().resolve()


def _browser_sessions_dir() -> Path:
    """Where browser_agent actually writes, not where it usually does.

    ``AGENT_DATA_DIR`` relocates it and the default is CWD-relative, so reading
    the agent's own constant is the only way this cannot drift into deleting
    the wrong directory — or, worse, missing the right one.
    """
    try:
        from browser_agent.config import SESSIONS_ROOT
        return Path(SESSIONS_ROOT).expanduser().resolve()
    except Exception:
        return (install_root() / "sessions").resolve()


def _desktop_sessions_dir() -> Path:
    """Same, for the desktop agent's audit trail (QUILL_DESKTOP_SESSIONS)."""
    try:
        from desktop_agent.config import SESSIONS_ROOT
        return Path(SESSIONS_ROOT).expanduser().resolve()
    except Exception:
        return (install_root() / "desktop_agent" / "sessions").resolve()


def targets() -> list[dict[str, Any]]:
    """Every directory holding captured content, in delete order.

    ``contents_only`` keeps the directory itself so a running server does not
    lose the path out from under it; the tree beneath it goes.
    """
    return [
        {"key": "data", "path": data_dir(),
         "label": "Memory, transcripts, audio, frames and indexes",
         "contents_only": True},
        {"key": "browser_sessions", "path": _browser_sessions_dir(),
         "label": "Browser-agent page captures (screenshots, page trees)",
         "contents_only": True},
        {"key": "desktop_sessions", "path": _desktop_sessions_dir(),
         "label": "Desktop-agent action audit log",
         "contents_only": True},
    ]


def _guard(path: Path) -> None:
    """Refuse a path that is obviously not a Mnemos data directory.

    ``QUILL_DATA_DIR`` is operator-settable, so a typo (``/`` or ``~``) must be
    refused here rather than discovered afterwards.
    """
    p = Path(path).resolve()
    if p == p.parent:                       # filesystem root
        raise WipeRefused(f"refusing to delete the filesystem root ({p})")
    if p in (Path.home().resolve(),):
        raise WipeRefused(f"refusing to delete the home directory ({p})")
    if len(p.parts) < 3:
        raise WipeRefused(f"refusing to delete a top-level directory ({p})")


def _measure(path: Path, *, keep: tuple[str, ...] = ()) -> tuple[int, int]:
    """(files, bytes) beneath ``path``, excluding ``keep`` names at any depth."""
    files = 0
    total = 0
    try:
        if path.is_file():
            return (1, path.stat().st_size)
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                if name in keep:
                    continue
                files += 1
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return (files, total)


def _human_bytes(n: int) -> str:
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step or unit == "TB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= step
    return f"{val:.1f} TB"


def preview() -> dict[str, Any]:
    """What a wipe would remove — shown before the confirmation is accepted."""
    # Sizes reflect the default wipe, which keeps the shipped config.
    keep = KEEP_NAMES
    rows = []
    total_files = 0
    total_bytes = 0
    for t in targets():
        path = Path(t["path"])
        exists = path.exists()
        files, size = _measure(path, keep=keep) if exists else (0, 0)
        total_files += files
        total_bytes += size
        rows.append({"key": t["key"], "label": t["label"], "path": str(path),
                     "exists": exists, "files": files, "bytes": size,
                     "human": _human_bytes(size)})
    creds = [str(install_root() / n) for n in CREDENTIAL_NAMES
             if (install_root() / n).is_file()]
    return {
        "ok": True,
        "confirm_phrase": CONFIRM_PHRASE,
        "targets": rows,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "total_human": _human_bytes(total_bytes),
        "kept_by_default": list(KEEP_NAMES),
        "credentials_present": creds,
        "receipt_dir": str(_receipt_dir()),
    }


def stop_capture() -> dict[str, Any]:
    """Stop everything now: revoke the allow-list and halt running pipelines.

    Both halves matter. Revoking consent alone leaves the already-started mic
    thread recording until restart; stopping the pipelines alone lets the next
    ``start_all`` re-arm every source the tester just turned off.
    """
    out: dict[str, Any] = {"ok": True, "revoked": False, "stopped": False,
                           "errors": []}
    try:
        from app.services import capture_consent
        capture_consent.save(consented=False)
        out["revoked"] = True
    except Exception as exc:
        out["ok"] = False
        out["errors"].append(f"consent: {exc}")
    try:
        from app.api.routes import stop_all
        stop_all()
        out["stopped"] = True
    except Exception as exc:
        out["ok"] = False
        out["errors"].append(f"pipelines: {exc}")
    try:
        from app.services import capture_consent
        out["consent"] = capture_consent.status()
    except Exception:
        pass
    return out


def _close_stores() -> list[str]:
    """Close and drop the store singletons so their files can be deleted.

    An open SQLite connection is a deletable file on POSIX and an undeletable
    one on Windows, which is where every tester runs.
    """
    closed: list[str] = []
    try:
        import app.storage as storage
        if storage._store is not None:
            storage._store.close()
            storage._store = None
            closed.append("store")
    except Exception as exc:
        print(f"[wipe] store close skipped ({exc}).")
    try:
        import app.perception.store as pstore
        if pstore._pstore is not None:
            pstore._pstore.close()
            pstore._pstore = None
            closed.append("perception_store")
    except Exception as exc:
        print(f"[wipe] perception store close skipped ({exc}).")
    try:
        import app.vectorstore as vs
        if vs._vs is not None:
            vs._vs = None
            closed.append("vectorstore")
    except Exception as exc:
        print(f"[wipe] vectorstore drop skipped ({exc}).")
    try:
        # The memory engine caches both of the above; leaving it holding a
        # closed connection turns the next search into an opaque traceback.
        from app.services.memory import memory
        memory._store = None
        memory._vectors = None
        memory._events = []
        closed.append("memory")
    except Exception as exc:
        print(f"[wipe] memory reset skipped ({exc}).")
    try:
        from app.services import capture_consent, first_run
        capture_consent._cached = None
        first_run._cached = None
        closed.append("caches")
    except Exception as exc:
        print(f"[wipe] cache reset skipped ({exc}).")
    return closed


def _delete_tree(path: Path, *, keep: tuple[str, ...],
                 contents_only: bool) -> tuple[int, list[str]]:
    """Delete beneath ``path``. Returns (removed_count, failures).

    Failures are collected rather than raised: one locked file must not leave
    the other 8 GB in place, and the receipt has to name what survived.
    """
    removed = 0
    failures: list[str] = []
    if not path.exists():
        return (0, failures)
    entries = list(path.iterdir()) if contents_only else [path]
    for entry in entries:
        if entry.name in keep:
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except Exception as exc:
            failures.append(f"{entry}: {exc}")
    return (removed, failures)


def _receipt_dir() -> Path:
    """Where the receipt lands: beside the install, or home if that is locked.

    It must not live inside anything being deleted, and a packaged install can
    sit in a directory the user cannot write to.
    """
    from app.runtime import is_frozen, user_data_root
    # A packaged install sits in Program Files: writable to the installer, not
    # to the tester running the app. Their own data root always is.
    if is_frozen():
        root = user_data_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return Path.home()
        return root if os.access(root, os.W_OK) else Path.home()
    root = install_root()
    if os.access(root, os.W_OK):
        return root
    return Path.home()


def wipe(confirm: str, *, full: bool = False, credentials: bool = False,
         now: float | None = None) -> dict[str, Any]:
    """Delete every captured byte and write a receipt.

    ``full`` also removes the shipped configuration in :data:`KEEP_NAMES`;
    ``credentials`` also removes ``.env`` / ``.credentials.env``.
    """
    if (confirm or "").strip().upper() != CONFIRM_PHRASE:
        raise WipeRefused(
            f"type {CONFIRM_PHRASE!r} exactly to confirm — nothing was deleted")
    for t in targets():
        _guard(Path(t["path"]))

    stamp = time.time() if now is None else now
    before = preview()
    # Read the install id *before* the delete: it is the only thing tying this
    # receipt to the cohort row, and it lives in the directory being emptied.
    install_id = ""
    try:
        from app.services import usage_ledger
        install_id = usage_ledger.install_id()
    except Exception:
        pass

    stopped = stop_capture()
    closed = _close_stores()

    keep = () if full else KEEP_NAMES
    results = []
    failures: list[str] = []
    for t in targets():
        path = Path(t["path"])
        removed, failed = _delete_tree(path, keep=keep,
                                       contents_only=bool(t["contents_only"]))
        failures.extend(failed)
        results.append({"key": t["key"], "path": str(path), "label": t["label"],
                        "removed": removed, "failed": len(failed)})

    creds_removed = []
    if credentials:
        for name in CREDENTIAL_NAMES:
            p = install_root() / name
            try:
                if p.is_file():
                    p.unlink()
                    creds_removed.append(name)
            except Exception as exc:
                failures.append(f"{p}: {exc}")

    receipt = {
        "kind": "mnemos.deletion_receipt/1",
        "version": __version__,
        "install_id": install_id,
        "deleted_at": stamp,
        "deleted_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(stamp)),
        "targets": results,
        "files_before": before["total_files"],
        "bytes_before": before["total_bytes"],
        "bytes_before_human": before["total_human"],
        "capture_stopped": stopped,
        "handles_closed": closed,
        "kept": [] if full else list(KEEP_NAMES),
        "credentials_removed": creds_removed,
        "failures": failures,
        "complete": not failures,
        "statement": (
            "Every Mnemos capture directory on this machine was emptied at the "
            "time above. Mnemos holds no copy elsewhere: there is no server, "
            "no sync, and no backup outside this machine."
            if not failures else
            "Deletion ran but some paths could not be removed — see failures. "
            "Close Mnemos and re-run to clear them."),
    }
    path = _receipt_dir() / f"{RECEIPT_PREFIX}-{int(stamp)}.json"
    try:
        from app.atomic_json import write_json
        write_json(path, receipt, sort_keys=True)
        receipt["receipt_path"] = str(path)
    except Exception as exc:
        receipt["receipt_path"] = None
        receipt["receipt_error"] = str(exc)
    receipt["ok"] = not failures
    return receipt
