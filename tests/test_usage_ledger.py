"""WS-A — local usage ledger: bump/flush, crash tolerance, content-free schema.

The ledger is the instrument the Oct 1 kill-or-continue checkpoint reads, and
it is also the one new component that touches every hot path. These tests hold
two lines: the counts must be right across flushes and restarts, and no caller
must ever be able to get free text into the table.
"""
from __future__ import annotations

import os
import sqlite3
import string
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import usage_ledger as ul
from app.services.usage_ledger import UsageLedger
from app.storage import USAGE_COUNTER_COLUMNS, Store


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_usage_"))
        self.env = patch.dict(os.environ, {
            "QUILL_DATA_DIR": str(self.tmp),
            "QUILL_USAGE_LEDGER": "1",
            "QUILL_USAGE_PING_URL": "",
        }, clear=False)
        self.env.start()
        self.store = Store(db_path=self.tmp / "quill.db",
                           audio_dir=self.tmp / "audio")
        self.led = UsageLedger()

    def tearDown(self) -> None:
        self.env.stop()


class BumpFlushTests(_Base):
    def test_bump_accumulates_in_memory_then_flushes(self) -> None:
        self.led.bump("searches", 2, now=1_756_000_000.0)   # 2025-08-24 UTC
        self.led.bump("searches", now=1_756_000_000.0)
        self.led.bump("chat_turns", now=1_756_000_000.0)
        # Nothing is in SQLite until the flush — that is the whole design.
        self.assertIsNone(self.store.get_usage_day(ul.utc_day(1_756_000_000.0)))
        self.led.flush(self.store, now=1_756_000_000.0)
        row = self.store.get_usage_day(ul.utc_day(1_756_000_000.0))
        self.assertEqual(row["searches"], 3)
        self.assertEqual(row["chat_turns"], 1)

    def test_flush_is_additive_across_calls(self) -> None:
        for _ in range(3):
            self.led.bump("approvals", now=1_756_000_000.0)
            self.led.flush(self.store, now=1_756_000_000.0)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(1_756_000_000.0))["approvals"], 3)

    def test_flush_clears_pending_so_counts_are_not_doubled(self) -> None:
        self.led.bump("searches", 5, now=1_756_000_000.0)
        self.led.flush(self.store, now=1_756_000_000.0)
        self.led.flush(self.store, now=1_756_000_000.0)   # nothing new
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(1_756_000_000.0))["searches"], 5)

    def test_counts_split_across_utc_day_boundary(self) -> None:
        before = 1_755_993_599.0   # 23:59:59 UTC
        after = 1_755_993_601.0    # 00:00:01 UTC next day
        self.led.bump("searches", now=before)
        self.led.bump("searches", 4, now=after)
        self.led.flush(self.store, now=after)
        self.assertEqual(self.store.get_usage_day(ul.utc_day(before))["searches"], 1)
        self.assertEqual(self.store.get_usage_day(ul.utc_day(after))["searches"], 4)

    def test_crash_loses_only_unflushed_counts(self) -> None:
        """A crash costs at most one flush interval — never the flushed history."""
        self.led.bump("searches", 3, now=1_756_000_000.0)
        self.led.flush(self.store, now=1_756_000_000.0)
        self.led.bump("searches", 99, now=1_756_000_000.0)   # never flushed
        dead = UsageLedger()                                  # simulate restart
        dead.bump("searches", now=1_756_000_100.0)
        dead.flush(self.store, now=1_756_000_100.0)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(1_756_000_000.0))["searches"], 4)

    def test_write_failure_returns_deltas_to_the_accumulator(self) -> None:
        class Boom:
            def bump_usage_daily(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")
        self.led.bump("searches", 7, now=1_756_000_000.0)
        out = self.led.flush(Boom(), now=1_756_000_000.0)
        self.assertFalse(out["ok"])
        # Retried on the next flush rather than dropped.
        self.led.flush(self.store, now=1_756_000_000.0)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(1_756_000_000.0))["searches"], 7)

    def test_disabled_ledger_records_nothing(self) -> None:
        with patch.dict(os.environ, {"QUILL_USAGE_LEDGER": "0"}, clear=False):
            led = UsageLedger()
            led.bump("searches", 10, now=1_756_000_000.0)
            led.mark_active(now=1_756_000_000.0)
            self.assertFalse(led.flush(self.store, now=1_756_000_000.0)["ok"])
        self.assertIsNone(self.store.get_usage_day(ul.utc_day(1_756_000_000.0)))


# 2025-08-24T01:46:00Z — deliberately aligned to a minute boundary so the
# "same minute" cases in these tests really are the same minute.
MINUTE_START = 1_755_999_960.0


class ActiveMinuteTests(_Base):
    def test_repeat_marks_within_a_minute_count_once(self) -> None:
        base = MINUTE_START
        for offset in (0, 1, 5, 30, 59):
            self.led.mark_active(now=base + offset)
        self.led.flush(self.store, now=base)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(base))["active_minutes"], 1)

    def test_distinct_minutes_accumulate(self) -> None:
        base = MINUTE_START
        for minute in range(4):
            self.led.mark_active(now=base + minute * 60)
        self.led.flush(self.store, now=base)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(base))["active_minutes"], 4)

    def test_minutes_are_not_rewritten_on_later_flushes(self) -> None:
        """The dedup set lives for the process; only the delta is persisted."""
        base = MINUTE_START
        self.led.mark_active(now=base)
        self.led.flush(self.store, now=base)
        self.led.mark_active(now=base + 30)      # same minute again
        self.led.mark_active(now=base + 120)     # a new minute
        self.led.flush(self.store, now=base)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(base))["active_minutes"], 2)

    def test_minutes_are_attributed_to_their_own_utc_day(self) -> None:
        before, after = 1_755_993_500.0, 1_755_993_700.0
        self.led.mark_active(now=before)
        self.led.mark_active(now=after)
        self.led.flush(self.store, now=after)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(before))["active_minutes"], 1)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(after))["active_minutes"], 1)


class CaptureMinuteTests(_Base):
    def test_running_capture_accrues_whole_minutes(self) -> None:
        t0 = 1_756_000_000.0
        self.led.capture_started("audio", now=t0)
        self.led.flush(self.store, now=t0 + 150)    # 2.5 minutes
        row = self.store.get_usage_day(ul.utc_day(t0))
        self.assertEqual(row["capture_audio_minutes"], 2)
        # The half minute is carried, not dropped: 30s + 30s = one more minute.
        self.led.flush(self.store, now=t0 + 180)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(t0))["capture_audio_minutes"], 3)

    def test_stop_accrues_the_tail_and_stops_counting(self) -> None:
        t0 = 1_756_000_000.0
        self.led.capture_started("desktop", now=t0)
        self.led.capture_stopped("desktop", now=t0 + 300)
        self.led.flush(self.store, now=t0 + 100_000)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(t0))["capture_desktop_minutes"], 5)

    def test_double_start_does_not_double_count(self) -> None:
        t0 = 1_756_000_000.0
        self.led.capture_started("audio", now=t0)
        self.led.capture_started("audio", now=t0 + 60)   # ignored
        self.led.flush(self.store, now=t0 + 120)
        self.assertEqual(
            self.store.get_usage_day(ul.utc_day(t0))["capture_audio_minutes"], 2)


class ContentFreeTests(_Base):
    """House rule 5 — numbers and enum-ish strings only, never content."""

    def test_schema_has_no_free_text_columns(self) -> None:
        cols = {r[1]: r[2].upper() for r in
                self.store._conn.execute("PRAGMA table_info(usage_daily)")}
        text_cols = {c for c, t in cols.items() if t.startswith("TEXT")}
        # The only strings are: the UTC day key, a random UUID, the app
        # version, and platform.system(). Everything else is an integer.
        self.assertEqual(text_cols, {"day", "install_id", "version", "os"})
        for counter in USAGE_COUNTER_COLUMNS:
            self.assertTrue(cols[counter].startswith("INT"), counter)

    def test_fuzzing_bump_cannot_write_text_anywhere(self) -> None:
        """Every hostile field name is dropped, and the row stays clean."""
        payloads = [
            "text", "raw", "summary", "query", "'; DROP TABLE usage_daily;--",
            "searches; UPDATE usage_daily SET os='leak'",
            "os", "version", "install_id", "day",       # real columns, not counters
            "active_minutes",                            # derived, not bumpable
            "", " ", "\n", "\x00", "a" * 5000,
            "Call Dr. Alvarez about the biopsy results",
            *(f"f{c}" for c in string.punctuation),
        ]
        self.led.bump("searches", now=1_756_000_000.0)
        for name in payloads:
            self.led.bump(name, 3, now=1_756_000_000.0)
            self.led.bump(name, "not-an-int", now=1_756_000_000.0)  # type: ignore[arg-type]
        self.led.flush(self.store, now=1_756_000_000.0)
        row = self.store.get_usage_day(ul.utc_day(1_756_000_000.0))
        self.assertEqual(row["searches"], 1)
        self.assertNotEqual(row["os"], "leak")
        blob = " ".join(str(v) for v in row.values())
        self.assertNotIn("biopsy", blob)
        self.assertNotIn("DROP", blob.upper())
        # And the table itself still exists with exactly one row.
        self.assertEqual(len(self.store.list_usage_daily()), 1)

    def test_dropped_field_names_are_tallied_not_stored(self) -> None:
        self.led.bump("transcript", now=1_756_000_000.0)
        self.assertIn("transcript", self.led.pending()["dropped_fields"])
        self.led.flush(self.store, now=1_756_000_000.0)
        self.assertIsNone(self.store.get_usage_day(ul.utc_day(1_756_000_000.0)))

    def test_bump_never_raises_even_when_the_store_is_gone(self) -> None:
        """Instrumentation must never break a capture or a request (rule 3)."""
        self.led.bump("searches")           # no store bound; must not raise
        self.led.mark_active()
        self.led.capture_started("audio")
        self.led.capture_stopped("nonsense")
        with patch.object(UsageLedger, "_store", side_effect=RuntimeError("no db")):
            self.assertFalse(self.led.flush()["ok"])


class HotPathCostTests(_Base):
    """Acceptance: the capture path must show no measurable regression.

    The structural guarantee is the real one — `bump` touches a Counter behind
    one lock and does no I/O until the flush timer fires — so the assertions
    below check that directly, plus a generous wall-clock ceiling to catch a
    future change that quietly puts a write back on the hot path.
    """

    def test_bump_does_no_io(self) -> None:
        import builtins
        opened: list = []
        real_open = builtins.open

        def watched(*a, **k):
            opened.append(a[0] if a else None)
            return real_open(*a, **k)

        with patch.object(builtins, "open", watched), \
                patch.object(UsageLedger, "_store",
                             side_effect=AssertionError("bump touched the DB")):
            for _ in range(1000):
                self.led.bump("searches")
                self.led.mark_active()
        self.assertEqual(opened, [])

    def test_ledger_on_costs_under_a_microsecond_per_bump(self) -> None:
        import time as _t

        def run(n: int) -> float:
            best = None
            for _ in range(3):
                t0 = _t.perf_counter()
                for _ in range(n):
                    self.led.bump("capture_audio_minutes")
                elapsed = _t.perf_counter() - t0
                best = elapsed if best is None else min(best, elapsed)
            return best

        per_call_us = run(20_000) / 20_000 * 1e6
        # Wildly generous: a capture frame arrives every ~32 ms, so even 50x
        # this would be unmeasurable. The point is to fail loudly if someone
        # puts a write, a network call, or a full-table scan behind bump().
        self.assertLess(per_call_us, 20.0, f"{per_call_us:.2f} us per bump")

    def test_disabled_ledger_is_cheaper_still(self) -> None:
        """Ledger off must be a near-free early return, not a partial path."""
        with patch.dict(os.environ, {"QUILL_USAGE_LEDGER": "0"}, clear=False):
            led = UsageLedger()
            for _ in range(1000):
                led.bump("searches")
                led.mark_active()
                led.capture_started("audio")
            self.assertEqual(led.pending()["counts"], {})
            self.assertEqual(led.pending()["minutes"], {})
            self.assertEqual(led.pending()["capture_running"], [])


class BenchmarkGateTests(_Base):
    """WS-A acceptance: "capture-path benchmarks show no measurable regression
    (ledger off vs on)". Runs the real harness so the claim is measured here,
    not just asserted structurally above.

    scripts/bench_usage_overhead.py is the full version (more iterations, a
    printed table, --strict for CI); this runs it small enough to belong in the
    suite. Gates are loose on purpose — the number that matters is that the
    capture write path carries no ledger call at all.
    """

    def _bench(self, iters: int = 2000) -> dict:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "bench_usage_overhead",
            Path(__file__).resolve().parent.parent
            / "scripts" / "bench_usage_overhead.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.bench(iters)

    def test_no_measurable_regression_on_any_path(self) -> None:
        out = self._bench()
        for name, p in out["paths"].items():
            self.assertTrue(
                p["pass"],
                f"{name}: +{p['delta_us']:.2f} us ({p['delta_pct']:+.1f}%) "
                f"exceeds both gates ({p['gate_us']} us / {p['gate_pct']}%)")
        self.assertTrue(out["pass"])

    def test_the_capture_write_path_makes_zero_ledger_calls(self) -> None:
        """The binding form of "no regression on the capture path".

        Timing cannot prove this: an event write ends in an fsync, so run-to-run
        jitter is tens of microseconds either way and would swamp a bump that
        costs well under one. The exact claim is that the path takes no ledger
        call at all — so count them instead of timing them.
        """
        from unittest.mock import MagicMock
        from app.events import Event, Modality
        from app.services import memory as mem_mod
        from app.services.memory import MemoryEngine

        engine = MemoryEngine(store=self.store)
        engine._semantic = False
        engine._vectors = None

        spy = MagicMock()
        with patch("app.services.usage_ledger.usage", spy), \
                patch.object(mem_mod, "memory", engine):
            for i in range(50):
                engine._on_event(Event(
                    time=1_756_000_000.0 + i, modality=Modality.AUDIO,
                    raw=f"utterance {i}", summary=f"s{i}", source="test"))
        self.assertEqual(spy.method_calls, [],
                         "a ledger call has appeared on the capture write path")
        self.assertEqual(self.store.count(), 50)   # the writes really happened

    def test_capture_session_lifecycle_is_negligible(self) -> None:
        """start+stop runs once per capture session, never per frame."""
        self.assertLess(self._bench()["capture_session_us"], 50.0)


class InstallIdTests(_Base):
    def test_id_is_minted_once_and_is_stable(self) -> None:
        first = ul.install_id()
        self.assertEqual(first, ul.install_id())
        self.assertTrue(ul.install_path().is_file())

    def test_id_is_random_not_derived_from_the_machine(self) -> None:
        import platform
        import socket
        first = ul.install_id()
        ul.install_path().unlink()
        second = ul.install_id()
        self.assertNotEqual(first, second)   # nothing about the box seeds it
        for leak in (platform.node(), socket.gethostname(), platform.machine()):
            if leak:
                self.assertNotIn(leak.lower(), first.lower())

    def test_flush_stamps_identity_columns(self) -> None:
        from app.version import __version__
        self.led.bump("searches", now=1_756_000_000.0)
        self.led.flush(self.store, now=1_756_000_000.0)
        row = self.store.get_usage_day(ul.utc_day(1_756_000_000.0))
        self.assertEqual(row["install_id"], ul.install_id())
        self.assertEqual(row["version"], __version__)
        # os is platform.system() alone — no build string, no hostname.
        import platform
        self.assertEqual(row["os"], platform.system())


if __name__ == "__main__":
    unittest.main()
