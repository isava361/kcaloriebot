import unittest
from datetime import date, datetime, timezone

from kcaloriebot.domain import (
    Period,
    ValidationError,
    canonical_timezone,
    latest_summary_week,
    local_date,
    month_bounds,
    period_bounds,
    reminder_due_day,
    summary_due_week,
    week_bounds,
)


UTC = timezone.utc


def epoch(value: datetime) -> int:
    return int(value.timestamp())


class TimezoneTests(unittest.TestCase):
    def test_canonical_timezone_accepts_case_and_city_shorthand(self) -> None:
        self.assertEqual(canonical_timezone(" europe/moscow "), "Europe/Moscow")
        self.assertEqual(canonical_timezone("New York"), "America/New_York")

    def test_canonical_timezone_rejects_blank_and_unknown_values(self) -> None:
        for value in ("", "   ", "Mars/Olympus_Mons"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    canonical_timezone(value)

    def test_spring_dst_day_uses_a_23_hour_utc_range(self) -> None:
        bounds = period_bounds(
            Period.TODAY,
            "Europe/Berlin",
            datetime(2024, 3, 31, 12, tzinfo=UTC),
        )

        self.assertEqual(bounds.start_utc, epoch(datetime(2024, 3, 30, 23, tzinfo=UTC)))
        self.assertEqual(bounds.end_utc, epoch(datetime(2024, 3, 31, 22, tzinfo=UTC)))
        self.assertEqual(bounds.end_utc - bounds.start_utc, 23 * 60 * 60)

    def test_fall_dst_day_uses_a_25_hour_utc_range(self) -> None:
        bounds = period_bounds(
            Period.TODAY,
            "Europe/Berlin",
            datetime(2024, 10, 27, 12, tzinfo=UTC),
        )

        self.assertEqual(
            bounds.start_utc, epoch(datetime(2024, 10, 26, 22, tzinfo=UTC))
        )
        self.assertEqual(bounds.end_utc, epoch(datetime(2024, 10, 27, 23, tzinfo=UTC)))
        self.assertEqual(bounds.end_utc - bounds.start_utc, 25 * 60 * 60)

    def test_yesterday_uses_adjacent_local_midnights_across_dst(self) -> None:
        bounds = period_bounds(
            Period.YESTERDAY,
            "Europe/Berlin",
            datetime(2024, 4, 1, 12, tzinfo=UTC),
        )

        self.assertEqual(bounds.start_local_date, date(2024, 3, 31))
        self.assertEqual(bounds.end_local_date, date(2024, 4, 1))
        self.assertEqual(bounds.end_utc - bounds.start_utc, 23 * 60 * 60)

    def test_week_is_seven_local_calendar_days_including_today(self) -> None:
        bounds = period_bounds(
            Period.WEEK,
            "Europe/Moscow",
            datetime(2026, 7, 13, 12, tzinfo=UTC),
        )

        self.assertEqual(bounds.start_local_date, date(2026, 7, 7))
        self.assertEqual(bounds.end_local_date, date(2026, 7, 14))

    def test_month_starts_on_first_and_ends_after_current_local_day(self) -> None:
        bounds = period_bounds(
            Period.MONTH,
            "Europe/Moscow",
            datetime(2026, 7, 13, 12, tzinfo=UTC),
        )

        self.assertEqual(bounds.start_local_date, date(2026, 7, 1))
        self.assertEqual(bounds.end_local_date, date(2026, 7, 14))

    def test_month_bounds_cover_one_local_calendar_month(self) -> None:
        bounds = month_bounds(2026, 6, "Europe/Moscow")

        self.assertEqual(bounds.start_local_date, date(2026, 6, 1))
        self.assertEqual(bounds.end_local_date, date(2026, 7, 1))

        december = month_bounds(2026, 12, "UTC")
        self.assertEqual(december.end_local_date, date(2027, 1, 1))

    def test_month_bounds_reject_unsupported_months(self) -> None:
        for year, month in ((2026, 0), (2026, 13), (1999, 5), (2101, 1)):
            with self.subTest(year=year, month=month):
                with self.assertRaises(ValidationError):
                    month_bounds(year, month, "UTC")
        with self.assertRaises(ValidationError):
            month_bounds(2026, 6, "Mars/Olympus_Mons")

    def test_naive_now_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            period_bounds(Period.TODAY, "UTC", datetime(2026, 7, 13, 12))

    def test_unknown_saved_timezone_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            period_bounds(
                Period.TODAY,
                "Mars/Olympus_Mons",
                datetime(2026, 7, 13, 12, tzinfo=UTC),
            )

    def test_local_date_uses_the_requested_timezone(self) -> None:
        timestamp = epoch(datetime(2024, 1, 1, 23, 30, tzinfo=UTC))

        self.assertEqual(local_date(timestamp, "UTC"), date(2024, 1, 1))
        self.assertEqual(local_date(timestamp, "Europe/Moscow"), date(2024, 1, 2))


def at(year, month, day, hour=0, minute=0) -> int:
    return epoch(datetime(year, month, day, hour, minute, tzinfo=UTC))


class ReminderDueTests(unittest.TestCase):
    """reminder_due_day(now, zone, minute, sent_day) decides from stored state."""

    NINE_PM = 21 * 60

    def test_due_from_the_local_time_until_the_end_of_the_local_day(self) -> None:
        # Moscow is UTC+3 all year.
        before = at(2026, 11, 10, 17, 59)
        on_time = at(2026, 11, 10, 18, 0)
        late = at(2026, 11, 10, 20, 59)
        after_midnight = at(2026, 11, 10, 21, 1)
        zone = "Europe/Moscow"
        self.assertIsNone(reminder_due_day(before, zone, self.NINE_PM, None))
        self.assertEqual(
            date(2026, 11, 10), reminder_due_day(on_time, zone, self.NINE_PM, None)
        )
        # A restart late in the evening still sends the missed reminder ...
        self.assertEqual(
            date(2026, 11, 10),
            reminder_due_day(late, zone, self.NINE_PM, date(2026, 11, 9)),
        )
        # ... but not after local midnight, when it belongs to the next day.
        self.assertIsNone(
            reminder_due_day(after_midnight, zone, self.NINE_PM, date(2026, 11, 9))
        )

    def test_already_handled_day_is_not_due_again(self) -> None:
        now = at(2026, 11, 10, 19)
        self.assertIsNone(
            reminder_due_day(now, "Europe/Moscow", self.NINE_PM, date(2026, 11, 10))
        )
        self.assertEqual(
            date(2026, 11, 11),
            reminder_due_day(
                at(2026, 11, 11, 18), "Europe/Moscow", self.NINE_PM, date(2026, 11, 10)
            ),
        )

    def test_disabled_or_unknown_timezone_is_never_due(self) -> None:
        now = at(2026, 11, 10, 23)
        self.assertIsNone(reminder_due_day(now, "Europe/Moscow", None, None))
        self.assertIsNone(reminder_due_day(now, "Mars/Olympus_Mons", 0, None))

    def test_wall_clock_time_follows_dst(self) -> None:
        # New York: 21:00 is 02:00 UTC in winter and 01:00 UTC in summer.
        zone = "America/New_York"
        self.assertIsNone(
            reminder_due_day(at(2024, 3, 9, 1, 59), zone, self.NINE_PM, None)
        )
        self.assertEqual(
            date(2024, 3, 8),
            reminder_due_day(at(2024, 3, 9, 2), zone, self.NINE_PM, None),
        )
        self.assertIsNone(
            reminder_due_day(at(2024, 3, 11, 0, 59), zone, self.NINE_PM, None)
        )
        self.assertEqual(
            date(2024, 3, 10),
            reminder_due_day(at(2024, 3, 11, 1), zone, self.NINE_PM, None),
        )

    def test_time_in_the_spring_forward_gap_fires_after_the_jump(self) -> None:
        # Berlin skips 02:00-03:00 on 2024-03-31; 01:59 CET is 00:59 UTC.
        half_past_two = 2 * 60 + 30
        zone = "Europe/Berlin"
        self.assertIsNone(
            reminder_due_day(at(2024, 3, 31, 0, 59), zone, half_past_two, None)
        )
        self.assertEqual(
            date(2024, 3, 31),
            reminder_due_day(at(2024, 3, 31, 1, 0), zone, half_past_two, None),
        )

    def test_repeated_hour_on_fall_back_day_does_not_fire_twice(self) -> None:
        # Berlin repeats 02:00-03:00 on 2024-10-27: 02:30 CEST then 02:30 CET.
        half_past_two = 2 * 60 + 30
        zone = "Europe/Berlin"
        first = at(2024, 10, 27, 0, 30)
        second = at(2024, 10, 27, 1, 30)
        day = reminder_due_day(first, zone, half_past_two, None)
        self.assertEqual(date(2024, 10, 27), day)
        self.assertIsNone(reminder_due_day(second, zone, half_past_two, day))

    def test_timezone_change_west_does_not_repeat_the_same_day(self) -> None:
        # Sent in Tokyo at 21:00 JST (12:00 UTC); the user moves to London,
        # where the same calendar day reaches 21:00 nine hours later.
        sent = reminder_due_day(at(2026, 11, 10, 12), "Asia/Tokyo", self.NINE_PM, None)
        self.assertEqual(date(2026, 11, 10), sent)
        self.assertIsNone(
            reminder_due_day(at(2026, 11, 10, 21), "Europe/London", self.NINE_PM, sent)
        )
        self.assertEqual(
            date(2026, 11, 11),
            reminder_due_day(at(2026, 11, 11, 21), "Europe/London", self.NINE_PM, sent),
        )

    def test_timezone_change_across_the_date_line_does_not_repeat(self) -> None:
        # Handled for 11 Nov in Kiritimati (UTC+14); in Honolulu it is still
        # 10 Nov, which must not count as a new, unreminded day.
        self.assertIsNone(
            reminder_due_day(
                at(2026, 11, 11, 7),
                "Pacific/Honolulu",
                self.NINE_PM,
                date(2026, 11, 11),
            )
        )

    def test_timezone_change_east_uses_the_new_local_day(self) -> None:
        # Sent in Los Angeles for 10 Nov; in Moscow it is already 11 Nov and
        # the reminder fires again only at 21:00 Moscow time.
        sent = date(2026, 11, 10)
        zone = "Europe/Moscow"
        self.assertIsNone(
            reminder_due_day(at(2026, 11, 11, 5, 30), zone, self.NINE_PM, sent)
        )
        self.assertEqual(
            date(2026, 11, 11),
            reminder_due_day(at(2026, 11, 11, 18), zone, self.NINE_PM, sent),
        )

    def test_impossible_future_sent_day_does_not_block_forever(self) -> None:
        self.assertEqual(
            date(2026, 11, 10),
            reminder_due_day(
                at(2026, 11, 10, 19), "Europe/Moscow", self.NINE_PM, date(2030, 1, 1)
            ),
        )


class WeeklySummaryDueTests(unittest.TestCase):
    # 2026-11-09 is a Monday.

    def test_released_on_monday_morning_for_the_previous_week(self) -> None:
        zone = "Europe/Moscow"
        self.assertEqual(
            date(2026, 10, 26), latest_summary_week(at(2026, 11, 9, 5, 59), zone)
        )
        self.assertEqual(
            date(2026, 11, 2), latest_summary_week(at(2026, 11, 9, 6, 0), zone)
        )
        self.assertEqual(
            date(2026, 11, 2),
            summary_due_week(at(2026, 11, 9, 6), zone, date(2026, 10, 26)),
        )
        self.assertIsNone(
            summary_due_week(at(2026, 11, 9, 5, 59), zone, date(2026, 10, 26))
        )

    def test_sent_week_is_not_due_again_after_restart(self) -> None:
        zone = "Europe/Moscow"
        for now in (at(2026, 11, 9, 6), at(2026, 11, 12, 12), at(2026, 11, 16, 5)):
            with self.subTest(now=now):
                self.assertIsNone(summary_due_week(now, zone, date(2026, 11, 2)))
        self.assertEqual(
            date(2026, 11, 9),
            summary_due_week(at(2026, 11, 16, 6), zone, date(2026, 11, 2)),
        )

    def test_long_outage_sends_only_the_latest_week(self) -> None:
        self.assertEqual(
            date(2026, 11, 2),
            summary_due_week(at(2026, 11, 11, 12), "Europe/Moscow", date(2026, 10, 12)),
        )

    def test_release_follows_the_local_monday(self) -> None:
        # 2026-11-09 00:00 UTC is Monday 09:00 in Tokyo but Sunday in New York.
        now = at(2026, 11, 9, 0, 0)
        self.assertEqual(date(2026, 11, 2), latest_summary_week(now, "Asia/Tokyo"))
        self.assertEqual(
            date(2026, 10, 26), latest_summary_week(now, "America/New_York")
        )

    def test_westward_timezone_change_does_not_resend(self) -> None:
        # Sent Monday 09:00 in Kiritimati (Sunday 19:00 UTC); Honolulu is
        # still on Sunday and its latest released week is the one before.
        now = at(2026, 11, 8, 19, 30)
        self.assertEqual(
            date(2026, 11, 2), latest_summary_week(now, "Pacific/Kiritimati")
        )
        self.assertIsNone(summary_due_week(now, "Pacific/Honolulu", date(2026, 11, 2)))

    def test_impossible_future_sent_week_does_not_block_forever(self) -> None:
        self.assertEqual(
            date(2026, 11, 2),
            summary_due_week(at(2026, 11, 9, 6), "Europe/Moscow", date(2031, 1, 6)),
        )

    def test_week_bounds_cover_seven_local_days_across_dst(self) -> None:
        # Berlin switches to summer time on Sunday 2024-03-31.
        bounds = week_bounds(date(2024, 3, 25), "Europe/Berlin")
        self.assertEqual(bounds.start_utc, at(2024, 3, 24, 23))
        self.assertEqual(bounds.end_utc, at(2024, 3, 31, 22))
        self.assertEqual(bounds.end_utc - bounds.start_utc, (7 * 24 - 1) * 3600)


if __name__ == "__main__":
    unittest.main()
