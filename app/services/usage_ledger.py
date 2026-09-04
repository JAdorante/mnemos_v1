"""Local usage ledger — the pilot's WAU / retention instrument (WS-A).

Local-first, content-free, shared only by an explicit act.

*Local-first*: every count lands in `usage_daily` in the tester's own SQLite
store, one row per **UTC** day. Nothing here opens a socket by itself. The
weekly ping (:func:`maybe_ping`) needs *both* a configured URL and a consent
flag the user stored through the Privacy controls, and it defaults to neither.

*Content-free*: :func:`UsageLedger.bump` accepts only the counter names in
``storage.USAGE_COUNTER_COLUMNS`` and only integers. A query, a fact, a name,
a window title — none of them can reach this table, because there is no column
they could land in and no code path that passes caller text into SQL. The only
strings stored are the install id (a random UUID), the app version, and
``platform.system()``.

*Never blocks*: :func:`UsageLedger.bump` swallows its own exceptions, so a call
site is one line with no ``try`` around it and instrumentation can never fail a
capture or a request. Writes are batched in memory and flushed by a timer
thread every ``QUILL_USAGE_FLUSH_S`` seconds (and at shutdown); a crash loses at
most one flush interval of counting, which is the intended trade.

**Timezone: everything is UTC.** Day keys, week windows, the install day, and
every metric below. A tester in UTC-7 who works at 6 pm is counted on the next
UTC day — deliberate, because the Oct 1 cohort table must add up across
timezones, and a per-machine local day makes "week 2" mean different things on
different machines.
"""
from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.atomic_json import write_json
from app.config import settings
from app.storage import USAGE_COUNTER_COLUMNS
from app.version import __version__

# Counters callers may bump. `active_minutes` is derived from minute-stamps at
# flush time (see mark_active) rather than bumped directly, so it is excluded
# from the public bump surface — bumping it by hand would double-count.
BUMPABLE: frozenset[str] = frozenset(USAGE_COUNTER_COLUMNS) - {"active_minutes"}

_SECONDS_PER_DAY = 86400.0


# --------------------------------------------------------------------------
# install identity
# --------------------------------------------------------------------------
def _data_dir() -> Path:
    return Path(os.environ.get("QUILL_DATA_DIR") or settings.storage.data_dir)


def install_path() -> Path:
    return _data_dir() / "install.json"


_install_lock = threading.Lock()


def install_info() -> dict[str, Any]:
    """`{install_id, created_at}` — minted on first read, then stable.

    The id is ``uuid4``: random, never derived from hardware, MAC, hostname or
    anything else that could identify the machine outside this install. Losing
    ``data/`` means a new install id, which is the correct semantics — the
    cohort counts installs, not people.
    """
    p = install_path()
    with _install_lock:
        try:
            if p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8"))
                iid = str(raw.get("install_id") or "").strip()
                if iid:
                    created = raw.get("created_at")
                    return {"install_id": iid,
                            "created_at": float(created) if created else None}
        except Exception as exc:
            print(f"[usage] install id read skipped ({exc}).")
        info = {"install_id": str(uuid.uuid4()), "created_at": time.time()}
        try:
            write_json(p, info)
        except Exception as exc:
            print(f"[usage] install id write skipped ({exc}).")
        return info


def install_id() -> str:
    return str(install_info()["install_id"])


# --------------------------------------------------------------------------
# consent for the weekly ping (persisted state, deliberately NOT an env var)
# --------------------------------------------------------------------------
def consent_path() -> Path:
    return _data_dir() / "usage_consent.json"


def ping_consent() -> dict[str, Any]:
    """`{consented, decided_at, last_ping_at, last_attempt_at, last_error}`."""
    out: dict[str, Any] = {"consented": False, "decided_at": None,
                           "last_ping_at": None, "last_attempt_at": None,
                           "last_error": None}
    try:
        p = consent_path()
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                out["consented"] = bool(raw.get("consented"))
                for k in ("decided_at", "last_ping_at", "last_attempt_at"):
                    v = raw.get(k)
                    out[k] = float(v) if v is not None else None
                out["last_error"] = raw.get("last_error")
    except Exception as exc:
        print(f"[usage] consent read skipped ({exc}).")
    return out


def _save_consent(**fields: Any) -> dict[str, Any]:
    state = ping_consent()
    state.update(fields)
    try:
        write_json(consent_path(), state)
    except Exception as exc:
        print(f"[usage] consent write skipped ({exc}).")
    return state


def set_ping_consent(consented: bool, *, now: float | None = None) -> dict[str, Any]:
    """Record the user's standing decision on the weekly stats ping."""
    return _save_consent(consented=bool(consented),
                         decided_at=float(now if now is not None else time.time()))


# --------------------------------------------------------------------------
# date math (UTC; pure functions so the Oct 1 numbers are testable)
# --------------------------------------------------------------------------
# One entry per distinct UTC day seen. The day key is an exact function of
# floor(ts / 86400) — POSIX time has no leap seconds, so every timestamp in a
# day maps to the same bucket — which makes this a memo, not an approximation.
# It matters because utc_day() is the hottest line in the ledger: every bump
# and every marked request calls it, and datetime+strftime dominated both.
_DAY_CACHE: dict[int, str] = {}


def utc_day(ts: float | None = None) -> str:
    """UTC calendar day key, ``YYYY-MM-DD``."""
    seconds = float(ts if ts is not None else time.time())
    bucket = int(seconds // _SECONDS_PER_DAY)
    day = _DAY_CACHE.get(bucket)
    if day is None:
        day = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
        if len(_DAY_CACHE) > 512:      # a long-lived install, or a test sweeping years
            _DAY_CACHE.clear()
        _DAY_CACHE[bucket] = day
    return day


def _parse_day(day: str) -> datetime:
    return datetime.strptime(str(day), "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _day_offset(day: str, base: str) -> int:
    """Whole UTC days from `base` to `day` (0 = same day, negative = before)."""
    return (_parse_day(day) - _parse_day(base)).days


def day_is_active(row: dict[str, Any]) -> bool:
    """An *active day* is a day the human actually used Sparrow.

    Defined as at least one active minute — a minute in which a request hit
    chat, search, the Console or an approval. A bare app start does not count:
    an install that boots on login every morning and is never opened is not a
    weekly-active user, and counting it would inflate the Oct 1 decision.
    """
    try:
        return int(row.get("active_minutes") or 0) > 0
    except (TypeError, ValueError):
        return False


def metrics_from_rows(rows: list[dict[str, Any]], *, install_day: str,
                      now: float | None = None) -> dict[str, Any]:
    """Derived pilot metrics from `usage_daily` rows. Pure — no I/O, no clock
    beyond `now`, so the checkpoint math is unit-testable to the day.

    Windows (all UTC, all inclusive):

    * ``active_days_last_7`` — active days in the 7-day window ending today
      (today and the 6 days before it).
    * ``is_wau`` — that count is >= 1.
    * ``week_index_since_install`` — 1 on the install day through day 7, 2 for
      days 8-14, and so on. Days before the install day (a clock that went
      backwards) clamp to 1.
    * ``wk1_active_days`` / ``wk2_active_days`` — active days in days 1-7 and
      days 8-14 counting the install day as day 1.
    * ``retained_wk2`` — at least one active day in days 8-14.
    """
    now = float(now if now is not None else time.time())
    today = utc_day(now)
    active_days = sorted({str(r.get("day")) for r in rows
                          if r.get("day") and day_is_active(r)})

    last7 = [d for d in active_days if -6 <= _day_offset(d, today) <= 0]
    since_install = _day_offset(today, install_day)
    wk1 = [d for d in active_days if 0 <= _day_offset(d, install_day) <= 6]
    wk2 = [d for d in active_days if 7 <= _day_offset(d, install_day) <= 13]

    totals = {c: 0 for c in USAGE_COUNTER_COLUMNS}
    for r in rows:
        for c in USAGE_COUNTER_COLUMNS:
            try:
                totals[c] += int(r.get(c) or 0)
            except (TypeError, ValueError):
                pass

    return {
        "today": today,
        "install_day": install_day,
        "days_since_install": since_install,
        "week_index_since_install": max(1, since_install // 7 + 1),
        "active_days_last_7": len(last7),
        "is_wau": bool(last7),
        "wk1_active_days": len(wk1),
        "wk2_active_days": len(wk2),
        # Week 2 has not happened yet before day 8 — report False, and let
        # `wk2_complete` say whether that False is a verdict or just "too soon".
        "retained_wk2": bool(wk2),
        "wk2_complete": since_install >= 13,
        "total_active_days": len(active_days),
        "first_active_day": active_days[0] if active_days else None,
        "last_active_day": active_days[-1] if active_days else None,
        "days_recorded": len(rows),
        "totals": totals,
        "timezone": "UTC",
    }


# --------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------
class UsageLedger:
    """In-memory accumulator + timer flush. One lock, never held over I/O."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (utc_day, counter) -> delta not yet written
        self._counts: Counter[tuple[str, str]] = Counter()
        # utc_day -> set of minute-stamps seen this process
        self._minutes: dict[str, set[int]] = {}
        # utc_day -> how many of those minutes are already in SQLite
        self._minutes_written: dict[str, int] = {}
        # capture kind -> monotonic start; whole minutes accrue at flush
        self._capture_since: dict[str, float] = {}
        self._capture_carry: dict[str, float] = {}
        self._timer: threading.Timer | None = None
        self._running = False
        self._dropped: Counter[str] = Counter()

    # ---- configuration (env-first at call time so tests can toggle) -------
    def enabled(self) -> bool:
        raw = os.environ.get("QUILL_USAGE_LEDGER")
        if raw is not None:
            return raw not in ("0", "false", "False")
        return bool(settings.usage.enabled)

    def _flush_s(self) -> float:
        try:
            return max(1.0, float(os.environ.get(
                "QUILL_USAGE_FLUSH_S", settings.usage.flush_s)))
        except (TypeError, ValueError):
            return 60.0

    # ---- the hot path ----------------------------------------------------
    def bump(self, field: str, n: int = 1, *, now: float | None = None) -> None:
        """Add `n` to a counter for the current UTC day. Never raises.

        Call sites are one bare line on purpose: the try/except lives here, so
        instrumentation can never break capture, search or a request. Unknown
        or non-integer fields are dropped (and tallied for /usage/stats) rather
        than stored — there is no column they could land in.
        """
        try:
            if not self.enabled():
                return
            name = str(field)
            if name not in BUMPABLE:
                self._dropped[name[:40]] += 1
                return
            delta = int(n)
            if delta == 0:
                return
            day = utc_day(now)
            with self._lock:
                self._counts[(day, name)] += delta
        except Exception as exc:  # pragma: no cover - defensive by design
            print(f"[usage] bump skipped ({exc}).")

    def mark_active(self, now: float | None = None) -> None:
        """Mark the current UTC minute as active. Idempotent within a minute.

        Deduping on the minute-stamp is what makes `active_minutes` a measure of
        time spent rather than of requests made: a chat page polling four times
        a second still contributes exactly one minute.
        """
        try:
            if not self.enabled():
                return
            ts = float(now if now is not None else time.time())
            day = utc_day(ts)
            minute = int(ts // 60)
            with self._lock:
                self._minutes.setdefault(day, set()).add(minute)
        except Exception as exc:  # pragma: no cover - defensive by design
            print(f"[usage] mark_active skipped ({exc}).")

    # ---- capture heartbeat ----------------------------------------------
    def capture_started(self, kind: str, *, now: float | None = None) -> None:
        """Begin accruing capture minutes for 'audio' or 'desktop'."""
        try:
            if not self.enabled() or kind not in ("audio", "desktop"):
                return
            with self._lock:
                self._capture_since.setdefault(
                    kind, float(now if now is not None else time.time()))
        except Exception as exc:  # pragma: no cover - defensive by design
            print(f"[usage] capture_started skipped ({exc}).")

    def capture_stopped(self, kind: str, *, now: float | None = None) -> None:
        try:
            if kind not in ("audio", "desktop"):
                return
            self._accrue_capture(now=now, keep_running=False, only=kind)
        except Exception as exc:  # pragma: no cover - defensive by design
            print(f"[usage] capture_stopped skipped ({exc}).")

    def _accrue_capture(self, *, now: float | None = None,
                        keep_running: bool = True,
                        only: str | None = None) -> None:
        """Fold elapsed capture time into whole minutes; carry the remainder.

        This is the "per-minute tick while running" without a per-frame hook:
        the pipelines only report start and stop, and the flush timer converts
        wall time into minutes. Sub-minute remainders carry forward so an hour
        of 40-second clips is not rounded away to zero.
        """
        ts = float(now if now is not None else time.time())
        with self._lock:
            kinds = [only] if only else list(self._capture_since)
            for kind in kinds:
                started = self._capture_since.get(kind)
                if started is None:
                    continue
                elapsed = max(0.0, ts - started) + self._capture_carry.get(kind, 0.0)
                whole = int(elapsed // 60)
                self._capture_carry[kind] = elapsed - whole * 60
                if whole:
                    col = f"capture_{kind}_minutes"
                    self._counts[(utc_day(ts), col)] += whole
                if keep_running:
                    self._capture_since[kind] = ts
                else:
                    self._capture_since.pop(kind, None)
                    self._capture_carry.pop(kind, None)

    # ---- flush -----------------------------------------------------------
    def _store(self, store=None):
        if store is not None:
            return store
        from app.storage import get_store
        return get_store()

    def flush(self, store=None, *, now: float | None = None) -> dict[str, Any]:
        """Write pending deltas to SQLite. Best-effort; returns what it wrote.

        On a write failure the deltas are put back so the next flush retries
        them — a transient locked DB costs latency, not counts.
        """
        out: dict[str, Any] = {"ok": True, "days": 0, "rows": {}}
        if not self.enabled():
            return {**out, "ok": False, "reason": "disabled"}
        try:
            self._accrue_capture(now=now)
            with self._lock:
                pending = self._counts
                self._counts = Counter()
                minute_deltas: dict[str, int] = {}
                for day, seen in self._minutes.items():
                    written = self._minutes_written.get(day, 0)
                    delta = len(seen) - written
                    if delta > 0:
                        minute_deltas[day] = delta
                        self._minutes_written[day] = len(seen)
            by_day: dict[str, dict[str, int]] = {}
            for (day, name), delta in pending.items():
                by_day.setdefault(day, {})[name] = delta
            for day, delta in minute_deltas.items():
                by_day.setdefault(day, {})["active_minutes"] = delta
            if not by_day:
                return out
            st = self._store(store)
            iid = install_id()
            osname = platform.system()
            written_days = []
            try:
                for day, deltas in sorted(by_day.items()):
                    st.bump_usage_daily(day, deltas, install_id=iid,
                                        version=__version__, os_name=osname)
                    written_days.append(day)
                    out["rows"][day] = dict(deltas)
            except Exception:
                # Put back everything not yet written so nothing is lost.
                with self._lock:
                    for day, deltas in by_day.items():
                        if day in written_days:
                            continue
                        for name, delta in deltas.items():
                            if name == "active_minutes":
                                self._minutes_written[day] = max(
                                    0, self._minutes_written.get(day, 0) - delta)
                            else:
                                self._counts[(day, name)] += delta
                raise
            out["days"] = len(written_days)
            return out
        except Exception as exc:
            print(f"[usage] flush skipped ({exc}).")
            return {**out, "ok": False, "reason": str(exc)}

    # ---- lifecycle -------------------------------------------------------
    def start(self, *, app_start: bool = True) -> None:
        """Begin the flush timer (idempotent) and count one app start."""
        if not self.enabled():
            return
        try:
            install_info()  # mint the id on first run, before anything reads it
            if app_start:
                self.bump("app_starts")
            with self._lock:
                if self._running:
                    return
                self._running = True
            self._schedule()
        except Exception as exc:
            print(f"[usage] start skipped ({exc}).")

    def _schedule(self) -> None:
        with self._lock:
            if not self._running:
                return
            timer = threading.Timer(self._flush_s(), self._tick)
            timer.daemon = True
            self._timer = timer
        timer.start()

    def _tick(self) -> None:
        try:
            self.flush()
            maybe_ping()
        except Exception as exc:  # pragma: no cover - defensive by design
            print(f"[usage] tick skipped ({exc}).")
        finally:
            self._schedule()

    def stop(self) -> None:
        """Cancel the timer and flush what is pending (shutdown hook)."""
        with self._lock:
            self._running = False
            timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        self._accrue_capture(keep_running=False)
        self.flush()

    # ---- introspection ---------------------------------------------------
    def pending(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counts": {f"{d}:{n}": v for (d, n), v in self._counts.items()},
                "minutes": {d: len(s) for d, s in self._minutes.items()},
                "capture_running": sorted(self._capture_since),
                "dropped_fields": dict(self._dropped),
            }


usage = UsageLedger()


# --------------------------------------------------------------------------
# report + metrics surface
# --------------------------------------------------------------------------
def metrics(now: float | None = None, store=None) -> dict[str, Any]:
    """Derived metrics for this install (see :func:`metrics_from_rows`)."""
    info = install_info()
    created = info.get("created_at")
    rows: list[dict[str, Any]] = []
    try:
        st = usage._store(store)
        rows = st.list_usage_daily(limit=1000)
    except Exception as exc:
        print(f"[usage] metrics rows skipped ({exc}).")
    # The install day is the id file's birthday; if that is somehow missing,
    # fall back to the earliest recorded day so the windows still line up.
    if created:
        install_day = utc_day(float(created))
    elif rows:
        install_day = str(rows[0]["day"])
    else:
        install_day = utc_day(now)
    out = metrics_from_rows(rows, install_day=install_day, now=now)
    out["install_id"] = info["install_id"]
    return out


def report_payload(now: float | None = None, store=None) -> dict[str, Any]:
    """The exact JSON a tester shares — rows plus derived metrics.

    Content-free by construction: `days` is whatever is in `usage_daily` (all
    integers plus install id / version / os), and `metrics` is arithmetic over
    it. This same shape is what the Privacy controls display *before* asking
    for weekly-ping consent — there is no second, richer payload.
    """
    info = install_info()
    try:
        rows = usage._store(store).list_usage_daily(limit=1000)
    except Exception as exc:
        print(f"[usage] report rows skipped ({exc}).")
        rows = []
    return {
        "schema": "mnemos.usage/1",
        "install_id": info["install_id"],
        "installed_at_day": utc_day(info["created_at"]) if info.get("created_at") else None,
        "version": __version__,
        "os": platform.system(),
        "generated_at_day": utc_day(now),
        "timezone": "UTC",
        "days": rows,
        "metrics": metrics(now=now, store=store),
    }


def redacted_report_json(now: float | None = None, store=None) -> str:
    """Report JSON put through the crash-report redactor before it can leave.

    Defense in depth, not a fix: on a compliant payload this is a byte-for-byte
    no-op (asserted in test_usage_report). It exists so that if a counter ever
    regresses into storing content, the leak is scrubbed rather than shipped.
    """
    from app.services import crash_report
    return crash_report._redact(
        json.dumps(report_payload(now=now, store=store), indent=2, sort_keys=True))


def write_report(now: float | None = None, store=None) -> dict[str, Any]:
    """Write ``data/logs/usage-<install_id>-<day>.json`` for manual sending.

    Same interaction shape as the crash-report zip: the file lands on the
    tester's disk and they decide whether to send it.
    """
    from app.services import crash_report
    payload_text = redacted_report_json(now=now, store=store)
    iid = install_id()
    day = utc_day(now)
    out = crash_report.logs_dir() / f"usage-{iid}-{day}.json"
    out.write_text(payload_text, encoding="utf-8")
    return {"ok": True, "path": str(out), "install_id": iid, "day": day,
            "bytes": len(payload_text)}


# --------------------------------------------------------------------------
# opt-in weekly ping (default off; needs URL *and* stored consent)
# --------------------------------------------------------------------------
def _ping_url() -> str:
    raw = os.environ.get("QUILL_USAGE_PING_URL")
    if raw is None:
        raw = settings.usage.ping_url
    return (raw or "").strip()


def ping_status(now: float | None = None) -> dict[str, Any]:
    state = ping_consent()
    url = _ping_url()
    return {
        "url_configured": bool(url),
        "consented": bool(state["consented"]),
        # Both, or nothing happens. This is the whole gate.
        "will_ping": bool(url and state["consented"]),
        "decided_at": state["decided_at"],
        "last_ping_at": state["last_ping_at"],
        "last_attempt_at": state["last_attempt_at"],
        "last_error": state["last_error"],
        "every_days": float(settings.usage.ping_every_days),
    }


def _due(state: dict[str, Any], now: float) -> bool:
    every = float(settings.usage.ping_every_days) * _SECONDS_PER_DAY
    retry = float(settings.usage.ping_retry_days) * _SECONDS_PER_DAY
    last_attempt = state.get("last_attempt_at")
    if last_attempt is not None and now - float(last_attempt) < retry:
        return False  # at most one attempt per day, success or failure
    last_ok = state.get("last_ping_at")
    return last_ok is None or (now - float(last_ok)) >= every


def maybe_ping(now: float | None = None, store=None, *,
               transport=None, force: bool = False) -> dict[str, Any]:
    """POST the report if (and only if) URL + consent + cadence all say yes.

    Never raises, never retried more than once a day, never blocks anything:
    the caller is the flush timer thread, and a failure is recorded in the
    consent file and logged. `transport` is injectable so tests can assert that
    a non-consented install makes exactly zero requests.
    """
    now = float(now if now is not None else time.time())
    url = _ping_url()
    state = ping_consent()
    if not url:
        return {"ok": False, "sent": False, "reason": "no_url"}
    if not state["consented"]:
        return {"ok": False, "sent": False, "reason": "no_consent"}
    if not force and not _due(state, now):
        return {"ok": False, "sent": False, "reason": "not_due"}
    body = redacted_report_json(now=now, store=store).encode("utf-8")
    _save_consent(last_attempt_at=now)
    try:
        if transport is not None:
            transport(url, body)
        else:
            _post(url, body)
    except Exception as exc:
        _save_consent(last_error=f"{type(exc).__name__}: {exc}")
        print(f"[usage] weekly ping failed ({exc}); will retry tomorrow.")
        return {"ok": False, "sent": False, "reason": "error", "error": str(exc)}
    _save_consent(last_ping_at=now, last_error=None)
    return {"ok": True, "sent": True, "bytes": len(body)}


def _post(url: str, body: bytes) -> None:
    from urllib.request import Request, urlopen
    req = Request(url, data=body, method="POST",
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=float(settings.usage.ping_timeout_s)) as resp:
        code = int(getattr(resp, "status", 0) or 0)
        if code and code >= 400:
            raise RuntimeError(f"HTTP {code}")
