"""Evening reminders and weekly summaries.

One asyncio task started in the application's post_init ticks every minute
and asks the database who is due. Whether something is due, and whether it
was already sent, is decided only from persisted state by the pure functions
``reminder_due_day`` and ``summary_due_week`` in domain.py, so a restart
neither repeats nor loses a notification.

Delivery is claim-then-send: the day or week is atomically marked as handled
in the database before the message goes out. The claim is released only when
Telegram certainly did not receive the message (flood control, or no
connection could be opened), so the next tick retries it. Any other failure
keeps the claim: an ambiguous timeout may have delivered the message and
retrying could send it twice, and a user who blocked the bot (Forbidden)
must not stall the loop or be retried every minute.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta
from typing import Any, Callable, Optional

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, NetworkError, RetryAfter, TelegramError

from .database import Database
from .domain import (
    DayStats,
    Goals,
    NotificationSettings,
    WeightRecord,
    reminder_due_day,
    summary_due_week,
    week_bounds,
)

logger = logging.getLogger(__name__)

TICK_SECONDS = 60

REMINDER_TEXT = (
    "Nothing logged today yet. Log a meal with a quick entry such as "
    "oatmeal 370 60, or tap Add food."
)

# httpx errors raised before the request left this process.
_NOT_SENT_CAUSES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def reminder_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Add food", callback_data="menu:add")],
            [InlineKeyboardButton("Reminder settings", callback_data="menu:reminders")],
        ]
    )


def summary_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Reminder settings", callback_data="menu:reminders")]]
    )


def _week_label(week_start: date) -> str:
    end = week_start + timedelta(days=6)
    if week_start.month == end.month:
        return f"{week_start.day}–{end.day} {end:%b}"
    return f"{week_start.day} {week_start:%b} – {end.day} {end:%b}"


def _day_label(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def weekly_summary_text(
    week_start: date,
    days: tuple[DayStats, ...],
    goals: Goals,
    first_weight: Optional[WeightRecord] = None,
    last_weight: Optional[WeightRecord] = None,
) -> Optional[str]:
    """A short recap of one Monday-Sunday week; None when there is nothing
    to report (no food and no weight logged)."""
    if not days and first_weight is None:
        return None
    lines = [f"Weekly summary · {_week_label(week_start)}"]
    if not days:
        lines.append("No food logged.")
    else:
        lines.append(f"Logged {len(days)} of 7 days.")
        average = sum(day.calories for day in days) / len(days)
        line = f"Average: {average:.0f} kcal per logged day"
        if goals.calories is not None:
            difference = average - goals.calories
            direction = "over" if difference > 0 else "under"
            line += f" (goal {goals.calories:.0f}: {abs(difference):.0f} {direction})"
        lines.append(line + ".")
        for label, key in (("Protein", "protein"), ("Fat", "fat"), ("Carbs", "carbs")):
            goal = getattr(goals, key)
            if goal is None:
                continue
            known = sum(getattr(day, key) or 0.0 for day in days) / len(days)
            partial = any(
                getattr(day, f"{key}_coverage") < day.entry_count for day in days
            )
            if partial and known == 0:
                lines.append(f"{label}: not recorded (goal {goal:.0f} g).")
                continue
            amount = f"at least {known:.0f}" if partial else f"{known:.0f}"
            lines.append(f"{label}: {amount} g per logged day (goal {goal:.0f} g).")
        if len(days) > 1:
            highest = max(days, key=lambda day: (day.calories, day.day))
            lowest = min(days, key=lambda day: (day.calories, day.day))
            lines.append(
                f"Highest: {_day_label(highest.day)}, {highest.calories:.0f} kcal. "
                f"Lowest: {_day_label(lowest.day)}, {lowest.calories:.0f} kcal."
            )
    if first_weight is not None and last_weight is not None:
        if first_weight.weight_id == last_weight.weight_id:
            lines.append(f"Weight: {last_weight.weight_kg:.1f} kg (one measurement).")
        else:
            change = last_weight.weight_kg - first_weight.weight_kg
            lines.append(
                f"Weight: {first_weight.weight_kg:.1f} → "
                f"{last_weight.weight_kg:.1f} kg ({change:+.1f} kg)."
            )
    return "\n".join(lines)


class Notifier:
    """Sends due reminders and summaries; ``tick`` is one pass over users."""

    def __init__(
        self,
        database: Database,
        bot: Any,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.bot = bot
        self.clock = clock

    async def run(self, interval: float = TICK_SECONDS) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Notification tick failed")
            await asyncio.sleep(interval)

    async def tick(self, now_utc: Optional[int] = None) -> None:
        now = int(self.clock()) if now_utc is None else now_utc
        candidates = await asyncio.to_thread(self.database.notification_candidates)
        for settings in candidates:
            # One user's failure must not stop the others.
            try:
                await self._remind(settings, now)
            except Exception:
                logger.exception("Reminder failed user_id=%s", settings.user_id)
            try:
                await self._summarize(settings, now)
            except Exception:
                logger.exception("Weekly summary failed user_id=%s", settings.user_id)

    async def _remind(self, settings: NotificationSettings, now: int) -> None:
        assert settings.timezone is not None
        day = reminder_due_day(
            now,
            settings.timezone,
            settings.reminder_minute,
            settings.reminder_sent_day,
        )
        if day is None:
            return
        send = await asyncio.to_thread(self.database.claim_reminder, settings, day, now)
        if not send:
            return
        if not await self._deliver(settings.user_id, REMINDER_TEXT, reminder_markup()):
            await asyncio.to_thread(
                self.database.release_reminder,
                settings.user_id,
                day,
                settings.reminder_sent_day,
            )

    async def _summarize(self, settings: NotificationSettings, now: int) -> None:
        if not settings.weekly_summary:
            return
        assert settings.timezone is not None
        week = summary_due_week(now, settings.timezone, settings.summary_sent_week)
        if week is None:
            return
        if not await asyncio.to_thread(
            self.database.claim_summary, settings, week, now
        ):
            return
        text = await asyncio.to_thread(
            self._summary_text, settings.user_id, settings.timezone, week
        )
        if text is None:
            return
        if not await self._deliver(settings.user_id, text, summary_markup()):
            await asyncio.to_thread(
                self.database.release_summary,
                settings.user_id,
                week,
                settings.summary_sent_week,
            )

    def _summary_text(
        self, user_id: int, timezone_name: str, week: date
    ) -> Optional[str]:
        bounds = week_bounds(week, timezone_name)
        days = self.database.daily_breakdown(
            user_id, bounds.start_utc, bounds.end_utc, timezone_name, 0, 7
        ).items
        first, last = self.database.weight_range(
            user_id, bounds.start_utc, bounds.end_utc
        )
        return weekly_summary_text(
            week,
            tuple(sorted(days, key=lambda day: day.day)),
            self.database.get_goals(user_id),
            first,
            last,
        )

    async def _deliver(
        self, user_id: int, text: str, markup: InlineKeyboardMarkup
    ) -> bool:
        """Send to the user's private chat; False only when Telegram certainly
        did not get the message, so the claim may be released and retried."""
        try:
            # In a private chat the chat ID equals the user ID.
            await self.bot.send_message(chat_id=user_id, text=text, reply_markup=markup)
        except RetryAfter:
            logger.warning("Notification rate-limited user_id=%s", user_id)
            return False
        except Forbidden:
            logger.info("Notification not delivered, bot blocked user_id=%s", user_id)
        except NetworkError as exc:
            if isinstance(exc.__cause__, _NOT_SENT_CAUSES):
                logger.warning("Notification not sent, will retry user_id=%s", user_id)
                return False
            logger.warning(
                "Notification delivery uncertain, not retried user_id=%s", user_id
            )
        except TelegramError:
            logger.warning("Notification rejected user_id=%s", user_id, exc_info=True)
        return True


async def start_notifier(application: Any) -> None:
    """post_init hook: start the notification loop for this application."""
    notifier = Notifier(application.bot_data["database"], application.bot)
    application.bot_data["notifier_task"] = asyncio.create_task(
        notifier.run(), name="kcaloriebot-notifications"
    )


async def stop_notifier(application: Any) -> None:
    """post_stop hook: cancel the notification loop and wait for it."""
    task = application.bot_data.pop("notifier_task", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
