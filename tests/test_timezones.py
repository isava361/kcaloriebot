import unittest
from datetime import date, datetime, timezone

from kcaloriebot.domain import (
    Period,
    ValidationError,
    canonical_timezone,
    local_date,
    period_bounds,
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


if __name__ == "__main__":
    unittest.main()
