import asyncio
import tempfile
import unittest
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from telegram.error import Forbidden, NetworkError, RetryAfter, TimedOut

from kcaloriebot.database import Database
from kcaloriebot.domain import DayStats, Goals, WeightRecord
from kcaloriebot.bot import build_application
from kcaloriebot.config import Settings
from kcaloriebot.notifications import (
    Notifier,
    start_notifier,
    stop_notifier,
    weekly_summary_text,
)

UTC = timezone.utc
MOSCOW = "Europe/Moscow"  # UTC+3 all year
NINE_PM = 21 * 60


def at(year, month, day, hour=0, minute=0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.failures: dict[int, list[Exception]] = {}

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None):
        queue = self.failures.get(chat_id)
        if queue:
            raise queue.pop(0)
        self.sent.append((chat_id, text))


def retry_after() -> RetryAfter:
    with warnings.catch_warnings():
        # PTB warns that retry_after will become a timedelta; irrelevant here.
        warnings.simplefilter("ignore")
        return RetryAfter(timedelta(seconds=5))


def network_error(cause: Exception) -> NetworkError:
    error = NetworkError(f"httpx.{type(cause).__name__}")
    error.__cause__ = cause
    return error


class NotifierTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.directory.name) / "bot.db")
        self.database.initialize()
        self.bot = FakeBot()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def user(self, user_id: int, zone: str = MOSCOW) -> None:
        self.database.set_timezone(user_id, zone, now_utc=1)

    async def tick(self, now: int) -> None:
        # A new Notifier per tick: nothing may depend on process memory.
        await Notifier(self.database, self.bot).tick(now)

    def sent_to(self, user_id: int) -> list[str]:
        return [text for chat, text in self.bot.sent if chat == user_id]


class ReminderDeliveryTests(NotifierTestCase):
    async def test_off_by_default(self) -> None:
        self.user(1)
        await self.tick(at(2026, 11, 10, 20))
        self.assertEqual([], self.bot.sent)

    async def test_sent_once_per_local_day_across_restarts(self) -> None:
        self.user(1)
        self.database.set_reminder(1, NINE_PM, at(2026, 11, 10, 9))
        await self.tick(at(2026, 11, 10, 17, 59))
        self.assertEqual([], self.bot.sent)
        await self.tick(at(2026, 11, 10, 18, 0))
        await self.tick(at(2026, 11, 10, 18, 1))
        await self.tick(at(2026, 11, 10, 20, 30))
        self.assertEqual(1, len(self.sent_to(1)))
        self.assertIn("Nothing logged today", self.sent_to(1)[0])
        self.assertEqual(
            date(2026, 11, 10),
            self.database.get_notification_settings(1).reminder_sent_day,
        )
        await self.tick(at(2026, 11, 11, 18, 0))
        self.assertEqual(2, len(self.sent_to(1)))

    async def test_restart_after_the_reminder_time_still_sends(self) -> None:
        self.user(1)
        self.database.set_reminder(1, NINE_PM, at(2026, 11, 10, 9))
        # The bot was down at 21:00 and comes back at 23:30 local time.
        await self.tick(at(2026, 11, 10, 20, 30))
        self.assertEqual(1, len(self.sent_to(1)))

    async def test_not_sent_when_food_was_logged_that_day(self) -> None:
        self.user(1)
        self.database.set_reminder(1, NINE_PM, at(2026, 11, 10, 9))
        self.database.add_entry(1, at(2026, 11, 10, 9), "Oatmeal", 370, 60)
        await self.tick(at(2026, 11, 10, 18))
        self.assertEqual([], self.bot.sent)
        # The day is handled; deleting the entry later does not trigger it.
        self.assertEqual(
            date(2026, 11, 10),
            self.database.get_notification_settings(1).reminder_sent_day,
        )

    async def test_food_logged_yesterday_late_does_not_count_for_today(self) -> None:
        self.user(1)
        self.database.set_reminder(1, NINE_PM, at(2026, 11, 10, 9))
        # 23:30 local on 9 Nov.
        self.database.add_entry(1, at(2026, 11, 9, 20, 30), "Snack", 400, 50)
        await self.tick(at(2026, 11, 10, 18))
        self.assertEqual(1, len(self.sent_to(1)))

    async def test_setting_a_time_that_already_passed_starts_tomorrow(self) -> None:
        self.user(1)
        settings = self.database.set_reminder(1, NINE_PM, at(2026, 11, 10, 19))
        self.assertEqual(date(2026, 11, 10), settings.reminder_sent_day)
        await self.tick(at(2026, 11, 10, 19, 1))
        self.assertEqual([], self.bot.sent)
        await self.tick(at(2026, 11, 11, 18))
        self.assertEqual(1, len(self.sent_to(1)))

    async def test_crash_between_claim_and_send_does_not_resend(self) -> None:
        self.user(1)
        self.database.set_reminder(1, NINE_PM, at(2026, 11, 10, 9))
        settings = self.database.get_notification_settings(1)
        now = at(2026, 11, 10, 18)
        self.assertTrue(self.database.claim_reminder(settings, date(2026, 11, 10), now))
        # The process died before sending; after the restart nothing repeats.
        await self.tick(now + 60)
        self.assertEqual([], self.bot.sent)

    async def test_timezone_change_after_sending_does_not_repeat_the_day(self) -> None:
        self.user(1, "Asia/Tokyo")
        self.database.set_reminder(1, NINE_PM, at(2026, 11, 10, 0))
        await self.tick(at(2026, 11, 10, 12))  # 21:00 in Tokyo
        self.assertEqual(1, len(self.sent_to(1)))
        self.database.set_timezone(1, "Europe/London", now_utc=at(2026, 11, 10, 13))
        await self.tick(at(2026, 11, 10, 21))  # 21:00 in London, same date
        self.assertEqual(1, len(self.sent_to(1)))
        await self.tick(at(2026, 11, 11, 21))
        self.assertEqual(2, len(self.sent_to(1)))

    async def test_turned_off_reminder_is_not_sent(self) -> None:
        self.user(1)
        self.database.set_reminder(1, NINE_PM, at(2026, 11, 10, 9))
        self.database.set_reminder(1, None, at(2026, 11, 10, 10))
        await self.tick(at(2026, 11, 10, 18))
        self.assertEqual([], self.bot.sent)

    async def test_blocked_user_does_not_break_the_loop_for_others(self) -> None:
        for user_id in (1, 2, 3):
            self.user(user_id)
            self.database.set_reminder(user_id, NINE_PM, at(2026, 11, 10, 9))
        self.bot.failures[1] = [Forbidden("bot was blocked by the user")]
        self.bot.failures[2] = [RuntimeError("unexpected")]
        with self.assertLogs("kcaloriebot.notifications", "INFO") as logs:
            await self.tick(at(2026, 11, 10, 18))
        self.assertIn("bot blocked user_id=1", "\n".join(logs.output))
        self.assertIn("Reminder failed user_id=2", "\n".join(logs.output))
        self.assertEqual([3], [chat for chat, _ in self.bot.sent])
        # Neither failure is retried every minute.
        await self.tick(at(2026, 11, 10, 18, 1))
        self.assertEqual([3], [chat for chat, _ in self.bot.sent])

    async def test_undelivered_messages_are_retried_on_the_next_tick(self) -> None:
        for error in (
            retry_after(),
            network_error(httpx.ConnectError("refused")),
            network_error(httpx.ConnectTimeout("slow")),
        ):
            with self.subTest(error=type(error.__cause__ or error).__name__):
                self.bot.sent.clear()
                self.database.set_timezone(1, MOSCOW, now_utc=1)
                self.database.set_reminder(1, None, 1)
                self.database.set_reminder(1, NINE_PM, at(2026, 11, 10, 9))
                with self.database._connect() as connection:
                    connection.execute("UPDATE users SET reminder_sent_day = NULL")
                self.bot.failures[1] = [error]
                with self.assertLogs("kcaloriebot.notifications", "WARNING"):
                    await self.tick(at(2026, 11, 10, 18))
                self.assertEqual([], self.bot.sent)
                self.assertIsNone(
                    self.database.get_notification_settings(1).reminder_sent_day
                )
                await self.tick(at(2026, 11, 10, 18, 1))
                self.assertEqual(1, len(self.sent_to(1)))

    async def test_ambiguous_timeout_is_not_retried(self) -> None:
        self.user(1)
        self.database.set_reminder(1, NINE_PM, at(2026, 11, 10, 9))
        self.bot.failures[1] = [TimedOut()]
        with self.assertLogs("kcaloriebot.notifications", "WARNING"):
            await self.tick(at(2026, 11, 10, 18))
        await self.tick(at(2026, 11, 10, 18, 1))
        self.assertEqual([], self.bot.sent)


class WeeklySummaryDeliveryTests(NotifierTestCase):
    # 2026-11-09 is a Monday; the summary is released at 09:00 Moscow time.

    def log_week(self, user_id: int) -> None:
        add = self.database.add_entry
        add(user_id, at(2026, 11, 3, 9), "Oatmeal", 400, 250, 10, 5, 60)  # Tue 1000
        add(user_id, at(2026, 11, 5, 9), "Pasta", 350, 500, 12, 2, 70)  # Thu 1750
        add(user_id, at(2026, 11, 5, 15), "Cake", 400, 100)  # Thu +400
        add(user_id, at(2026, 11, 8, 9), "Rice", 130, 1000, 3, 1, 28)  # Sun 1300
        add(user_id, at(2026, 11, 9, 7), "Next week", 100, 100)  # Mon, excluded
        self.database.add_weight(user_id, at(2026, 11, 2, 5), 82.4)
        self.database.add_weight(user_id, at(2026, 11, 8, 5), 81.9)

    async def test_enabling_waits_for_next_monday_then_sends_once(self) -> None:
        self.user(1)
        self.log_week(1)
        settings = self.database.set_weekly_summary(1, True, at(2026, 11, 4, 12))
        self.assertEqual(date(2026, 10, 26), settings.summary_sent_week)
        await self.tick(at(2026, 11, 4, 12, 1))
        self.assertEqual([], self.bot.sent)
        await self.tick(at(2026, 11, 9, 5, 59))
        self.assertEqual([], self.bot.sent)
        await self.tick(at(2026, 11, 9, 6, 0))
        await self.tick(at(2026, 11, 9, 6, 1))
        await self.tick(at(2026, 11, 12, 12))
        texts = self.sent_to(1)
        self.assertEqual(1, len(texts))
        text = texts[0]
        self.assertIn("Weekly summary · 2–8 Nov", text)
        self.assertIn("Logged 3 of 7 days.", text)
        self.assertIn("Average: 1483 kcal per logged day.", text)
        self.assertIn("Highest: Thu 5 Nov, 2150 kcal.", text)
        self.assertIn("Lowest: Tue 3 Nov, 1000 kcal.", text)
        self.assertIn("Weight: 82.4 → 81.9 kg (-0.5 kg).", text)

    async def test_goals_are_compared_and_partial_macros_flagged(self) -> None:
        self.user(1)
        self.log_week(1)
        self.database.set_goals(1, Goals(1500.0, 50.0, None, None))
        self.database.set_weekly_summary(1, True, at(2026, 11, 4, 12))
        await self.tick(at(2026, 11, 9, 6))
        text = self.sent_to(1)[0]
        self.assertIn("(goal 1500: 17 under)", text)
        # Thursday's cake has no protein, so the average is a lower bound.
        self.assertIn("Protein: at least 38 g per logged day (goal 50 g).", text)
        self.assertNotIn("Fat:", text)

    async def test_empty_week_sends_nothing_but_is_handled(self) -> None:
        self.user(1)
        self.database.set_weekly_summary(1, True, at(2026, 11, 4, 12))
        await self.tick(at(2026, 11, 9, 6))
        self.assertEqual([], self.bot.sent)
        self.assertEqual(
            date(2026, 11, 2),
            self.database.get_notification_settings(1).summary_sent_week,
        )

    async def test_turned_off_summary_is_not_sent(self) -> None:
        self.user(1)
        self.log_week(1)
        self.database.set_weekly_summary(1, True, at(2026, 11, 4, 12))
        self.database.set_weekly_summary(1, False, at(2026, 11, 5, 12))
        await self.tick(at(2026, 11, 9, 6))
        self.assertEqual([], self.bot.sent)

    async def test_blocked_user_is_not_retried(self) -> None:
        self.user(1)
        self.log_week(1)
        self.database.set_weekly_summary(1, True, at(2026, 11, 4, 12))
        self.bot.failures[1] = [Forbidden("blocked")]
        with self.assertLogs("kcaloriebot.notifications", "INFO"):
            await self.tick(at(2026, 11, 9, 6))
        await self.tick(at(2026, 11, 9, 6, 1))
        self.assertEqual([], self.bot.sent)


class WeeklySummaryTextTests(unittest.TestCase):
    def day(self, day: int, calories: float, **values) -> DayStats:
        values = {"protein": None, "fat": None, "carbs": None, **values}
        return DayStats(date(2026, 11, day), 2, calories, **values)

    def test_nothing_to_report(self) -> None:
        self.assertIsNone(weekly_summary_text(date(2026, 11, 2), (), Goals()))

    def test_weight_only_week(self) -> None:
        weight = WeightRecord(1, 1, 0, 80.0)
        text = weekly_summary_text(date(2026, 10, 26), (), Goals(), weight, weight)
        self.assertEqual(
            "Weekly summary · 26 Oct – 1 Nov\nNo food logged.\n"
            "Weight: 80.0 kg (one measurement).",
            text,
        )

    def test_single_day_has_no_highest_and_lowest(self) -> None:
        text = weekly_summary_text(
            date(2026, 11, 2), (self.day(3, 1800.0),), Goals(calories=2000.0)
        )
        self.assertIn("Average: 1800 kcal per logged day (goal 2000: 200 under).", text)
        self.assertNotIn("Highest", text)

    def test_macro_without_any_data_is_not_reported_as_zero(self) -> None:
        text = weekly_summary_text(
            date(2026, 11, 2),
            (self.day(3, 1800.0), self.day(4, 1900.0)),
            Goals(protein=120.0),
        )
        self.assertIn("Protein: not recorded (goal 120 g).", text)

    def test_complete_macro_average(self) -> None:
        text = weekly_summary_text(
            date(2026, 11, 2),
            (
                self.day(3, 1800.0, protein=100.0, protein_coverage=2),
                self.day(4, 1900.0, protein=140.0, protein_coverage=2),
            ),
            Goals(protein=120.0),
        )
        self.assertIn("Protein: 120 g per logged day (goal 120 g).", text)


class LifecycleTests(NotifierTestCase):
    async def test_loop_starts_on_init_ticks_and_stops_cleanly(self) -> None:
        self.user(1)
        self.database.set_reminder(1, 0, 1)  # due at local midnight every day
        application = SimpleNamespace(
            bot_data={"database": self.database}, bot=self.bot
        )
        await start_notifier(application)
        task = application.bot_data["notifier_task"]
        for _ in range(100):
            if self.bot.sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(1, len(self.sent_to(1)))
        await stop_notifier(application)
        self.assertTrue(task.cancelled())
        self.assertNotIn("notifier_task", application.bot_data)
        await stop_notifier(application)  # stopping twice is harmless

    async def test_a_failing_tick_does_not_end_the_loop(self) -> None:
        calls = 0

        class Flaky(Notifier):
            async def tick(self, now_utc=None) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("database locked")

        task = asyncio.create_task(Flaky(self.database, self.bot).run(interval=0))
        with self.assertLogs("kcaloriebot.notifications", "ERROR"):
            for _ in range(100):
                if calls >= 2:
                    break
                await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertGreaterEqual(calls, 2)

    def test_application_registers_the_loop_hooks(self) -> None:
        settings = Settings(
            bot_token="123:test", database_path=self.database.path, log_level=20
        )
        application = build_application(settings, self.database)
        self.assertIs(start_notifier, application.post_init)
        self.assertIs(stop_notifier, application.post_stop)


if __name__ == "__main__":
    unittest.main()
