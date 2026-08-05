"""A durable, single-threaded background job runner.

The processing pipeline (consolidate now; extract next) is too slow to run on
the capture callback or an HTTP request, and must survive a crash. So instead of
doing that work inline, producers `enqueue()` a row in the SQLite `jobs` table
and this worker drains it on its own thread: claim -> dispatch -> finish, with
bounded retries (default 5), exponential backoff, dead-letter on exhaustion,
and a wake signal so it reacts promptly instead of only polling.

This is the "one queue, one worker" from the PRD — minus Celery/Redis, which buy
nothing for a single-process laptop prototype. Handlers are a `{kind: fn}`
registry, so the extractor lands as one more `register("extract", ...)` call.

    worker.register("consolidate", lambda payload: consolidation.rebuild())
    worker.start()
    worker.enqueue("consolidate", unique=True)   # coalesces a burst into one
"""
from __future__ import annotations

import json
import threading
from typing import Callable

from app.config import settings
from app.storage import Store, get_store

Handler = Callable[[dict | None], None]


class JobWorker:
    def __init__(self, store: Store | None = None,
                 poll_interval_s: float | None = None,
                 max_attempts: int | None = None) -> None:
        self._store = store
        self._handlers: dict[str, Handler] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll = (poll_interval_s if poll_interval_s is not None
                      else settings.worker.poll_interval_s)
        self._max = (max_attempts if max_attempts is not None
                     else settings.worker.max_attempts)
        self.last_error: str | None = None

    def _s(self) -> Store:
        if self._store is None:
            self._store = get_store()
        return self._store

    # --- registration / producers -----------------------------------------
    def register(self, kind: str, fn: Handler) -> None:
        self._handlers[kind] = fn

    def enqueue(self, kind: str, payload: dict | None = None,
                *, unique: bool = False) -> int:
        jid = self._s().enqueue_job(
            kind, json.dumps(payload) if payload is not None else None,
            unique_pending=unique)
        self._wake.set()   # nudge the loop so it doesn't wait out the poll
        return jid

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # Recover jobs a previous (crashed/killed) process left mid-flight: with a
        # single worker, anything still 'running' now is orphaned. Without this a
        # killed extract job blocks the backlog forever.
        try:
            n = self._s().requeue_stale_jobs()
            if n:
                print(f"[worker] recovered {n} stale job(s) from a prior run.")
        except Exception as exc:
            print(f"[worker] stale-job recovery skipped ({exc}).")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="job-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def stats(self) -> dict:
        return self._s().job_stats()

    # --- the loop ----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._s().claim_job()
            if job is None:
                self._wake.wait(self._poll)   # sleep until nudged or timeout
                self._wake.clear()
                continue
            self._dispatch(job)

    def _dispatch(self, job: dict) -> None:
        fn = self._handlers.get(job["kind"])
        if fn is None:
            self._s().fail_job(job["id"], f"no handler for kind={job['kind']!r}",
                               self._max)
            return
        payload = None
        if job.get("payload"):
            try:
                payload = json.loads(job["payload"])
            except Exception as exc:
                status = self._s().fail_job(
                    job["id"], f"corrupt payload: {exc}", self._max)
                print(f"[worker] job {job['id']} ({job['kind']}) corrupt payload "
                      f"[attempt {job['attempts']}] -> {status}: {exc}")
                return
        try:
            fn(payload)
            self._s().finish_job(job["id"])
        except Exception as exc:
            self.last_error = f"{job['kind']}: {exc}"
            status = self._s().fail_job(job["id"], str(exc), self._max)
            label = ("dead-letter"
                     if status == "dead"
                     else f"retry (backoff, {status})")
            print(f"[worker] job {job['id']} ({job['kind']}) failed "
                  f"[attempt {job['attempts']}/{self._max}] -> {label}: {exc}")


# Process-wide worker. Handlers are registered at startup (see app/main.py).
worker = JobWorker()
