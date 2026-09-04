"""A real Sparrow server that writes continuously, meant to be SIGKILLed.

Used by tests/test_backup_crash.py to reproduce the WS-B acceptance case
literally: "kill the server mid-write, back up, restore to a fresh dir, boot".

Run as a subprocess with QUILL_DATA_DIR pointing at a scratch directory:

    python tests/fixtures/crash_server.py <port> <committed-log-path>

It serves `app.main:app` on <port> and, on a background thread, writes events
through the same `MemoryEngine` the audio capture thread uses in production —
capture writes via the engine, not over HTTP, so this is the faithful shape.
Every event id is appended to <committed-log-path> and fsynced *after* its
SQLite commit returns, so a killer knows exactly which rows must survive.

The process never shuts down cleanly. That is the point: no `stop_all`, no
worker drain, no SQLite checkpoint — it leaves a hot, uncheckpointed WAL on
disk, which is what makes a naive file-copy backup restore stale or corrupt.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _writer(log_path: Path, stop: threading.Event) -> None:
    from app.events import Event, Modality
    from app.services.memory import memory

    store = memory._ensure_store()
    log = log_path.open("a", buffering=1)
    i = 0
    while not stop.is_set():
        eid = store.insert(Event(
            time=time.time(), modality=Modality.AUDIO,
            raw=f"utterance {i} about the capital-connect renewal",
            summary=f"summary {i}", source="crash-fixture"))
        # Only recorded once the commit has returned: everything in this file
        # is a row the database has promised to keep.
        log.write(f"{eid}\n")
        log.flush()
        os.fsync(log.fileno())
        i += 1


def main() -> None:
    port = int(sys.argv[1])
    log_path = Path(sys.argv[2])

    import uvicorn
    from app.main import app

    stop = threading.Event()
    t = threading.Thread(target=_writer, args=(log_path, stop), daemon=True)
    t.start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


if __name__ == "__main__":
    main()
