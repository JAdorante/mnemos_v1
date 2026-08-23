"""WS-A — WAU / week-2 retention date math.

An off-by-one here does not produce a visible bug; it produces a wrong number
in the Oct 1 kill-or-continue decision. So the windows are pinned explicitly:
all UTC, all inclusive, install day = day 1.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.services import usage_ledger as ul
from app.services.usage_ledger import UsageLedger
from app.storage import Store


def ts(day: str, hour: int = 12, minute: int = 0) -> float:
    """UTC epoch seconds for a YYYY-MM-DD day."""
    d = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, minute=minute, tzinfo=timezone.utc)
    return d.timestamp()


def rows(*days: str, minutes: int = 5) -> list[dict]:
    """Rows for active days; `minutes=0` makes a recorded-but-inactive day."""
    return [{"day": d, "active_minutes": minutes} for d in days]


class DayKeyTests(unittest.TestCase):
    def test_day_key_is_utc_not_local(self) -> None:
        # 2025-08-24T23:30Z is still the 24th in UTC even where it is the 25th
        # locally — the ledger deliberately does not use local time.
        self.assertEqual(ul.utc_day(ts("2025-08-24", 23, 30)), "2025-08-24")
        self.assertEqual(ul.utc_day(ts("2025-08-24", 0, 0)), "2025-08-24")
        self.assertEqual(ul.utc_day(ts("2025-08-24", 0, 0) - 1), "2025-08-23")

    def test_day_key_crosses_year_and_leap_day(self) -> None:
        self.assertEqual(ul.utc_day(ts("2024-02-29")), "2024-02-29")
        self.assertEqual(ul.utc_day(ts("2025-01-01", 0, 0) - 1), "2024-12-31")

    def test_active_day_needs_an_active_minute_not_just_a_row(self) -> None:
        # An install that boots on login every morning and is never opened is
        # not a weekly-active user.
        self.assertFalse(ul.day_is_active({"day": "2025-08-24", "app_starts": 9,
                                           "active_minutes": 0}))
        self.assertTrue(ul.day_is_active({"day": "2025-08-24",
                                          "active_minutes": 1}))
        self.assertFalse(ul.day_is_active({"day": "2025-08-24"}))
        self.assertFalse(ul.day_is_active({"active_minutes": "oops"}))


class DayKeyMemoTests(unittest.TestCase):
    """utc_day() memoizes on floor(ts/86400) — it must stay exact.

    The memo exists because utc_day is the ledger's hottest line (every bump
    and every marked request calls it). A cache that is even slightly wrong at
    a day boundary would misattribute counts and corrupt the retention math
    silently, so it is checked against the naive computation directly.
    """

    @staticmethod
    def _naive(t: float) -> str:
        return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%Y-%m-%d")

    def test_memo_matches_the_naive_computation_at_boundaries(self) -> None:
        probes = []
        for base in (0, 86_400, 1_755_993_600, 1_756_080_000, 4_102_444_800):
            probes += [base - 1, base - 0.001, base, base + 0.001, base + 1]
        for t in probes:
            self.assertEqual(ul.utc_day(t), self._naive(t), t)

    def test_memo_is_exact_for_pre_epoch_timestamps(self) -> None:
        """floor() on a negative float must not round toward zero."""
        for t in (-1, -0.5, -86_400, -86_401, -1_000_000_000):
            self.assertEqual(ul.utc_day(t), self._naive(t), t)

    def test_memo_is_exact_across_a_random_sweep(self) -> None:
        import random
        rng = random.Random(20260822)
        for _ in range(5_000):
            t = rng.uniform(-2e9, 4e9)
            self.assertEqual(ul.utc_day(t), self._naive(t), t)

    def test_the_cache_cannot_grow_without_bound(self) -> None:
        ul._DAY_CACHE.clear()
        for day in range(2_000):                       # ~5.5 years of days
            ul.utc_day(day * 86_400.0)
        self.assertLessEqual(len(ul._DAY_CACHE), 513)
        # And it is still correct after the reset.
        self.assertEqual(ul.utc_day(1_755_993_600), "2025-08-24")


class TrailingSevenTests(unittest.TestCase):
    def test_window_is_today_plus_the_six_days_before(self) -> None:
        m = ul.metrics_from_rows(
            rows("2025-08-18", "2025-08-24"), install_day="2025-08-01",
            now=ts("2025-08-24"))
        # 08-18 is exactly 6 days back — inside. 08-17 would be outside.
        self.assertEqual(m["active_days_last_7"], 2)
        m = ul.metrics_from_rows(
            rows("2025-08-17", "2025-08-24"), install_day="2025-08-01",
            now=ts("2025-08-24"))
        self.assertEqual(m["active_days_last_7"], 1)

    def test_is_wau_needs_one_active_day(self) -> None:
        self.assertFalse(ul.metrics_from_rows(
            [], install_day="2025-08-01", now=ts("2025-08-24"))["is_wau"])
        self.assertFalse(ul.metrics_from_rows(
            rows("2025-08-24", minutes=0), install_day="2025-08-01",
            now=ts("2025-08-24"))["is_wau"])
        self.assertTrue(ul.metrics_from_rows(
            rows("2025-08-24"), install_day="2025-08-01",
            now=ts("2025-08-24"))["is_wau"])

    def test_days_far_in_the_past_do_not_count(self) -> None:
        m = ul.metrics_from_rows(
            rows("2025-01-01", "2025-06-15"), install_day="2025-01-01",
            now=ts("2025-08-24"))
        self.assertEqual(m["active_days_last_7"], 0)
        self.assertFalse(m["is_wau"])
        self.assertEqual(m["total_active_days"], 2)

    def test_window_is_evaluated_at_the_utc_instant_given(self) -> None:
        # One second before midnight, "today" is still the 24th; one second
        # after, the window has slid and 08-18 has fallen out.
        args = dict(install_day="2025-08-01")
        late = ul.metrics_from_rows(rows("2025-08-18"),
                                    now=ts("2025-08-25", 0, 0) - 1, **args)
        early = ul.metrics_from_rows(rows("2025-08-18"),
                                     now=ts("2025-08-25", 0, 0), **args)
        self.assertEqual(late["active_days_last_7"], 1)
        self.assertEqual(early["active_days_last_7"], 0)


class WeekWindowTests(unittest.TestCase):
    """Install day is day 1. Week 1 = days 1-7, week 2 = days 8-14."""

    def test_week_index_boundaries(self) -> None:
        cases = {
            "2025-08-01": 1,   # install day itself
            "2025-08-07": 1,   # day 7 — last day of week 1
            "2025-08-08": 2,   # day 8 — first day of week 2
            "2025-08-14": 2,   # day 14 — last day of week 2
            "2025-08-15": 3,
            "2025-08-21": 3,
            "2025-08-22": 4,
        }
        for today, expected in cases.items():
            m = ul.metrics_from_rows([], install_day="2025-08-01",
                                     now=ts(today))
            self.assertEqual(m["week_index_since_install"], expected, today)

    def test_wk1_and_wk2_partition_the_first_fortnight(self) -> None:
        every_day = [f"2025-08-{d:02d}" for d in range(1, 15)]
        m = ul.metrics_from_rows(rows(*every_day), install_day="2025-08-01",
                                 now=ts("2025-08-20"))
        self.assertEqual(m["wk1_active_days"], 7)
        self.assertEqual(m["wk2_active_days"], 7)

    def test_day_7_is_week_1_and_day_8_is_week_2(self) -> None:
        m = ul.metrics_from_rows(rows("2025-08-07"), install_day="2025-08-01",
                                 now=ts("2025-08-20"))
        self.assertEqual((m["wk1_active_days"], m["wk2_active_days"]), (1, 0))
        m = ul.metrics_from_rows(rows("2025-08-08"), install_day="2025-08-01",
                                 now=ts("2025-08-20"))
        self.assertEqual((m["wk1_active_days"], m["wk2_active_days"]), (0, 1))

    def test_install_day_activity_lands_in_week_1(self) -> None:
        m = ul.metrics_from_rows(rows("2025-08-01"), install_day="2025-08-01",
                                 now=ts("2025-08-01"))
        self.assertEqual(m["wk1_active_days"], 1)
        self.assertEqual(m["days_since_install"], 0)
        self.assertEqual(m["week_index_since_install"], 1)

    def test_week_windows_cross_month_and_year_ends(self) -> None:
        m = ul.metrics_from_rows(rows("2025-01-04"), install_day="2024-12-28",
                                 now=ts("2025-01-20"))
        self.assertEqual(m["wk2_active_days"], 1)   # 2024-12-28 + 7 days
        self.assertTrue(m["retained_wk2"])

    def test_days_before_install_clamp_to_week_one(self) -> None:
        """A clock that went backwards must not produce week 0 or negative."""
        m = ul.metrics_from_rows([], install_day="2025-08-10",
                                 now=ts("2025-08-01"))
        self.assertEqual(m["week_index_since_install"], 1)
        self.assertEqual(m["days_since_install"], -9)


class RetentionTests(unittest.TestCase):
    def test_retained_needs_activity_in_days_8_to_14(self) -> None:
        base = dict(install_day="2025-08-01", now=ts("2025-08-20"))
        self.assertFalse(ul.metrics_from_rows(
            rows("2025-08-01", "2025-08-07"), **base)["retained_wk2"])
        self.assertTrue(ul.metrics_from_rows(
            rows("2025-08-01", "2025-08-08"), **base)["retained_wk2"])
        self.assertTrue(ul.metrics_from_rows(
            rows("2025-08-14"), **base)["retained_wk2"])
        self.assertFalse(ul.metrics_from_rows(
            rows("2025-08-15"), **base)["retained_wk2"])   # week 3, not week 2

    def test_inactive_week2_rows_do_not_count_as_retention(self) -> None:
        m = ul.metrics_from_rows(rows("2025-08-10", minutes=0),
                                 install_day="2025-08-01", now=ts("2025-08-20"))
        self.assertFalse(m["retained_wk2"])

    def test_wk2_complete_separates_no_from_too_soon(self) -> None:
        """A day-3 install is not 'not retained' — its week 2 has not happened.

        The cohort denominator on Oct 1 must exclude these, or retention is
        divided by installs that never had the chance.
        """
        early = ul.metrics_from_rows([], install_day="2025-08-01",
                                     now=ts("2025-08-03"))
        self.assertFalse(early["retained_wk2"])
        self.assertFalse(early["wk2_complete"])
        late = ul.metrics_from_rows([], install_day="2025-08-01",
                                    now=ts("2025-08-14"))
        self.assertFalse(late["retained_wk2"])
        self.assertTrue(late["wk2_complete"])   # day 14 — the window has closed

    def test_duplicate_day_rows_count_once(self) -> None:
        m = ul.metrics_from_rows(
            [{"day": "2025-08-08", "active_minutes": 3},
             {"day": "2025-08-08", "active_minutes": 9}],
            install_day="2025-08-01", now=ts("2025-08-20"))
        self.assertEqual(m["wk2_active_days"], 1)


class TotalsTests(unittest.TestCase):
    def test_totals_sum_every_counter_and_survive_junk(self) -> None:
        m = ul.metrics_from_rows(
            [{"day": "2025-08-01", "active_minutes": 5, "searches": 2},
             {"day": "2025-08-02", "active_minutes": 1, "searches": None},
             {"day": "2025-08-03", "active_minutes": 1, "searches": "x"}],
            install_day="2025-08-01", now=ts("2025-08-03"))
        self.assertEqual(m["totals"]["searches"], 2)
        self.assertEqual(m["totals"]["active_minutes"], 7)
        self.assertEqual(m["timezone"], "UTC")


class EndToEndTests(unittest.TestCase):
    """Fresh install -> two simulated days -> metrics() is right (acceptance)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_um_"))
        self.env = patch.dict(os.environ, {
            "QUILL_DATA_DIR": str(self.tmp), "QUILL_USAGE_LEDGER": "1",
        }, clear=False)
        self.env.start()
        self.store = Store(db_path=self.tmp / "quill.db",
                           audio_dir=self.tmp / "audio")

    def tearDown(self) -> None:
        self.env.stop()

    def test_two_simulated_days_of_use(self) -> None:
        day1, day2 = ts("2025-09-08", 9), ts("2025-09-09", 9)
        # Pin the install date rather than "now" so the windows are exact.
        from app.atomic_json import write_json
        write_json(ul.install_path(),
                   {"install_id": "fixed-install-0001", "created_at": day1})

        led = UsageLedger()
        led.bump("app_starts", now=day1)
        for i in range(3):
            led.mark_active(now=day1 + i * 60)
            led.bump("searches", now=day1)
        led.flush(self.store, now=day1)

        led2 = UsageLedger()   # a restart between the two days
        led2.bump("app_starts", now=day2)
        led2.mark_active(now=day2)
        led2.bump("chat_turns", 4, now=day2)
        led2.flush(self.store, now=day2)

        m = ul.metrics(now=day2, store=self.store)
        self.assertEqual(m["install_id"], "fixed-install-0001")
        self.assertEqual(m["install_day"], "2025-09-08")
        self.assertEqual(m["active_days_last_7"], 2)
        self.assertTrue(m["is_wau"])
        self.assertEqual(m["wk1_active_days"], 2)
        self.assertEqual(m["wk2_active_days"], 0)
        self.assertFalse(m["wk2_complete"])       # only day 2 of the pilot
        self.assertEqual(m["totals"]["searches"], 3)
        self.assertEqual(m["totals"]["chat_turns"], 4)
        self.assertEqual(m["totals"]["active_minutes"], 4)

    def test_metrics_on_a_brand_new_install(self) -> None:
        m = ul.metrics(now=ts("2025-09-08"), store=self.store)
        self.assertEqual(m["active_days_last_7"], 0)
        self.assertFalse(m["is_wau"])
        self.assertEqual(m["week_index_since_install"], 1)
        self.assertFalse(m["retained_wk2"])
        self.assertFalse(m["wk2_complete"])


if __name__ == "__main__":
    unittest.main()
