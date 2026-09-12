from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Callable, Optional, TypeVar
from urllib.parse import urljoin

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .callbacks import (
    CallbackAction,
    PAGE_SIZE,
    STATS_PAGE_SIZE,
    confirmation_expired,
    parse_callback,
)
from .config import Settings
from .database import Database
from .health import HealthStore
from .domain import (
    EARLIEST_DIARY_DATE,
    FoodEntry,
    WeightRecord,
    UNIT_100G,
    UNIT_SERVING,
    NotFound,
    Period,
    Session,
    SessionState,
    StateConflict,
    ValidationError,
    canonical_timezone,
    day_bounds,
    local_date,
    month_bounds,
    normalize_food_name,
    normalize_search_query,
    parse_calories,
    parse_calories_per_serving,
    parse_daily_goal,
    parse_entry_time,
    parse_grams,
    parse_macro,
    parse_macro_per_serving,
    parse_nutrient_value,
    parse_quick_add,
    parse_servings,
    parse_weight,
    period_bounds,
    validate_macro_sum,
)
from .render import (
    CANCEL_KEYBOARD,
    ENTRY_GRAMS_PROMPT,
    ENTRY_NAME_PROMPT,
    ENTRY_SERVINGS_PROMPT,
    ENTRY_TIME_PROMPT,
    FAVORITE_DECISION_KEYBOARD,
    FAVORITE_MATCH_GRAMS_PROMPT,
    FAVORITE_SEARCH_PROMPT,
    FAVORITE_SERVINGS_MANUAL_KEYBOARD,
    FAVORITE_SERVINGS_PROMPT,
    FAVORITE_TO_SERVING_PROMPT,
    FOOD_NAME_PROMPT,
    GOAL_KEYBOARD,
    GOAL_PROMPT,
    MAIN_KEYBOARD,
    MANUAL_ENTRY_KEYBOARD,
    Markup,
    QUICK_ADD_USAGE,
    REPEAT_KEYBOARD,
    SERVINGS_KEYBOARD,
    SKIP_KEYBOARD,
    STATS_KEYBOARD,
    TIMEZONE_CHANGE_PROMPT,
    TIMEZONE_ONBOARDING_PROMPT,
    TIMEZONE_REQUIRED_MARKUP,
    TIMEZONE_CHANGE_KEYBOARD,
    SETTINGS_KEYBOARD,
    TIMEZONE_SETUP_REQUIRED_PROMPT,
    WEIGHT_PROMPT,
    amount_text,
    contextual,
    entry_action_rows,
    favorite_action_rows,
    macro_text,
    nutrient_edit_prompt,
    day_navigation_row,
    day_stats_block,
    entry_button_text,
    entry_details,
    favorite_button_text,
    favorite_details,
    month_navigation_row,
    navigation_row,
    session_prompt,
    stats_day_rows,
    stats_totals_text,
)


__all__ = [
    "CallbackAction",
    "parse_callback",
    "PAGE_SIZE",
    "MAIN_KEYBOARD",
    "STATS_KEYBOARD",
    "CANCEL_KEYBOARD",
    "SKIP_KEYBOARD",
    "FAVORITE_DECISION_KEYBOARD",
    "TIMEZONE_REQUIRED_MARKUP",
    "start",
    "cancel",
    "update_timezone",
    "add_command",
    "weight_command",
    "handle_text",
    "handle_callback",
    "unknown_command",
    "handle_non_text",
    "handle_error",
    "build_application",
]

logger = logging.getLogger(__name__)
T = TypeVar("T")
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


@dataclass
class Turn:
    """Everything the shared handler preamble established for one update."""

    database: Database
    user_id: int
    chat_id: int
    message: Any
    session: Optional[Session]
    session_expired: bool


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


async def _call(function: Callable[..., T], *args: object, **kwargs: object) -> T:
    return await asyncio.to_thread(function, *args, **kwargs)


def _identity(update: Update) -> tuple[int, int]:
    if update.effective_user is None or update.effective_chat is None:
        raise RuntimeError("Update has no user or chat identity")
    return update.effective_user.id, update.effective_chat.id


async def _require_private(update: Update) -> bool:
    chat = update.effective_chat
    if chat is not None and chat.type == ChatType.PRIVATE:
        return True
    if chat is None:
        if update.callback_query is not None:
            await update.callback_query.answer("This action is unavailable.")
        return False
    if update.callback_query is not None:
        await update.callback_query.answer(
            "Open the bot in a private chat to protect your food data.", show_alert=True
        )
    elif update.effective_message is not None:
        await update.effective_message.reply_text(
            "Please use this bot in a private chat to protect your food data."
        )
    return False


async def _active_session(
    database: Database, user_id: int, chat_id: int
) -> tuple[Optional[Session], bool]:
    """Fetch the current session, clearing and flagging it when expired."""
    session = await _call(database.get_session, user_id, chat_id)
    if session is None or not _session_expired(session, database.now_epoch()):
        return session, False
    await _call(database.clear_session, user_id, chat_id)
    return None, True


async def _begin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    register_user: bool = False,
) -> Optional[Turn]:
    """Shared message-handler preamble; returns None when the update is ignored."""
    if not await _require_private(update) or update.effective_message is None:
        return None
    user_id, chat_id = _identity(update)
    database = _db(context)
    if register_user:
        await _call(database.ensure_user, user_id)
    session, expired = await _active_session(database, user_id, chat_id)
    return Turn(database, user_id, chat_id, update.effective_message, session, expired)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    turn = await _begin(update, context, register_user=True)
    if turn is None:
        return
    database = turn.database
    timezone_name = await _call(database.get_timezone, turn.user_id)
    session = turn.session
    if session is not None:
        if timezone_name is None and session.state != SessionState.WAIT_TIMEZONE:
            session = await _call(
                database.start_session,
                turn.user_id,
                turn.chat_id,
                SessionState.WAIT_TIMEZONE,
                prompt_pending=True,
            )
        else:
            refreshed = replace(session, updated_at_utc=database.now_epoch())
            session = await _call(database.update_session, session, refreshed)
        prompt, markup = session_prompt(session, timezone_name is not None)
        await turn.message.reply_text(prompt, reply_markup=markup)
        if session.prompt_pending:
            await _mark_prompt_delivered(database, session)
        return
    if timezone_name is None:
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_TIMEZONE,
            turn.message,
            TIMEZONE_ONBOARDING_PROMPT,
            TIMEZONE_REQUIRED_MARKUP,
        )
        return
    await turn.message.reply_text(
        "Welcome to the Calorie Calculator Bot.", reply_markup=MAIN_KEYBOARD
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    turn = await _begin(update, context)
    if turn is None:
        return
    database = turn.database
    timezone_name = await _call(database.get_timezone, turn.user_id)
    if timezone_name is None:
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_TIMEZONE,
            turn.message,
            TIMEZONE_SETUP_REQUIRED_PROMPT,
            TIMEZONE_REQUIRED_MARKUP,
        )
        return
    await _call(database.clear_session, turn.user_id, turn.chat_id)
    text = (
        "Favorite was not saved. The food entry remains in your diary."
        if turn.session is not None
        and turn.session.state == SessionState.WAIT_SAVE_FAVORITE
        else "Cancelled."
    )
    await turn.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    if turn.session is not None:
        await _return_to_item(turn, turn.session)


async def update_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    turn = await _begin(update, context, register_user=True)
    if turn is None:
        return
    database = turn.database
    active = turn.session
    if active is not None and active.state != SessionState.WAIT_TIMEZONE:
        prompt, markup = session_prompt(
            active, await _call(database.get_timezone, turn.user_id) is not None
        )
        await turn.message.reply_text(
            "Finish or cancel the current input before changing timezone.\n\n" + prompt,
            reply_markup=markup,
        )
        await _mark_prompt_delivered(database, active)
        return
    await _start_with_prompt(
        database,
        turn.user_id,
        turn.chat_id,
        SessionState.WAIT_TIMEZONE,
        turn.message,
        TIMEZONE_CHANGE_PROMPT,
        TIMEZONE_CHANGE_KEYBOARD,
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    turn = await _begin(update, context, register_user=True)
    if turn is None or turn.message.text is None:
        return
    database = turn.database
    if turn.session is not None:
        prompt, markup = session_prompt(
            turn.session, await _call(database.get_timezone, turn.user_id) is not None
        )
        await turn.message.reply_text(
            "Finish or cancel the current input before adding food.\n\n" + prompt,
            reply_markup=markup,
        )
        await _mark_prompt_delivered(database, turn.session)
        return
    if await _call(database.get_timezone, turn.user_id) is None:
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_TIMEZONE,
            turn.message,
            TIMEZONE_ONBOARDING_PROMPT,
            TIMEZONE_REQUIRED_MARKUP,
        )
        return
    parts = turn.message.text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if not payload or not await _log_quick_add(turn, payload):
        await turn.message.reply_text(QUICK_ADD_USAGE, reply_markup=MAIN_KEYBOARD)


async def _start_weight_prompt(turn: Turn) -> None:
    """Show the weight summary and ask for a new measurement."""
    summary = await _weight_summary(turn.database, turn.user_id)
    prompt = WEIGHT_PROMPT if summary is None else f"{summary}\n\n{WEIGHT_PROMPT}"
    await _start_with_prompt(
        turn.database,
        turn.user_id,
        turn.chat_id,
        SessionState.WAIT_WEIGHT,
        turn.message,
        prompt,
        CANCEL_KEYBOARD,
    )


async def weight_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    turn = await _begin(update, context, register_user=True)
    if turn is None or turn.message.text is None:
        return
    database = turn.database
    if turn.session is not None:
        prompt, markup = session_prompt(
            turn.session, await _call(database.get_timezone, turn.user_id) is not None
        )
        await turn.message.reply_text(
            "Finish or cancel the current input before logging weight.\n\n" + prompt,
            reply_markup=markup,
        )
        await _mark_prompt_delivered(database, turn.session)
        return
    if await _call(database.get_timezone, turn.user_id) is None:
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_TIMEZONE,
            turn.message,
            TIMEZONE_ONBOARDING_PROMPT,
            TIMEZONE_REQUIRED_MARKUP,
        )
        return
    parts = turn.message.text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if not payload:
        await _start_weight_prompt(turn)
        return
    try:
        weight = parse_weight(payload)
    except ValidationError as exc:
        await turn.message.reply_text(str(exc), reply_markup=MAIN_KEYBOARD)
        return
    message_date = getattr(turn.message, "date", None)
    measured_at = (
        int(message_date.timestamp())
        if message_date is not None
        else database.now_epoch()
    )
    record = await _call(database.add_weight, turn.user_id, measured_at, weight)
    await _send_weight_receipt(database, turn.user_id, turn.message, record)


async def _log_quick_add(turn: Turn, text: str) -> bool:
    """Try to log the text as a one-message food entry; True when handled."""
    try:
        quick = parse_quick_add(text)
    except ValidationError as exc:
        await turn.message.reply_text(str(exc), reply_markup=MAIN_KEYBOARD)
        return True
    if quick is None:
        return False
    message_date = getattr(turn.message, "date", None)
    event_epoch = (
        int(message_date.timestamp())
        if message_date is not None
        else turn.database.now_epoch()
    )
    entry = await _call(
        turn.database.add_entry,
        turn.user_id,
        event_epoch,
        quick.name,
        quick.calories_per_100g,
        quick.serving_grams,
        quick.protein_per_100g,
        quick.fat_per_100g,
        quick.carbs_per_100g,
    )
    await _send_receipt(turn.database, turn.user_id, turn.message, entry)
    return True


async def _today_progress(database: Database, user_id: int) -> str:
    """One-line calorie total for the user's local today, with goal if set."""
    timezone_name = await _call(database.get_timezone, user_id)
    if timezone_name is None:
        return ""
    bounds = period_bounds(Period.TODAY, timezone_name)
    stats = await _call(database.stats, user_id, bounds.start_utc, bounds.end_utc)
    goal = await _call(database.get_daily_goal, user_id)
    total = stats.calories
    if goal is None:
        return f"Today: {total:.0f} kcal."
    remaining = goal - total
    if remaining >= 0:
        return f"Today: {total:.0f} / {goal:.0f} kcal ({remaining:.0f} left)."
    return f"Today: {total:.0f} / {goal:.0f} kcal ({-remaining:.0f} over)."


async def _send_receipt(
    database: Database,
    user_id: int,
    message: Any,
    entry: Any,
    restore_keyboard: bool = False,
) -> None:
    """Confirm a saved entry with its details and Undo/Edit inline actions."""
    progress = await _today_progress(database, user_id)
    text = (
        f"Added {entry.name or 'food'} · {amount_text(entry.nutrition)}\n"
        f"{entry.nutrition.calories:.0f} kcal\n{progress}\n"
        f"{macro_text(entry.nutrition.protein, entry.nutrition.fat, entry.nutrition.carbs)}"
    )
    rows = [
        [
            InlineKeyboardButton(
                "Undo",
                callback_data=(f"entry:undo:{entry.entry_id}:{database.now_epoch()}"),
            ),
            InlineKeyboardButton(
                "Edit", callback_data=f"entry:view:{entry.entry_id}:0"
            ),
        ]
    ]
    if entry.name is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    "Save as favorite",
                    callback_data=f"entry:fav:{entry.entry_id}",
                )
            ]
        )
    if restore_keyboard:
        await message.reply_text("Saved to your diary.", reply_markup=MAIN_KEYBOARD)
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def _send_weight_receipt(
    database: Database,
    user_id: int,
    message: Any,
    record: WeightRecord,
    restore_keyboard: bool = False,
    edited: bool = False,
    viewing: bool = False,
) -> None:
    if restore_keyboard:
        await message.reply_text(
            "Weight updated." if edited else "Weight recorded.",
            reply_markup=STATS_KEYBOARD,
        )
    timezone_name = await _call(database.get_timezone, user_id)
    measured_on = local_date(record.measured_at_utc, timezone_name or "UTC")
    summary = await _weight_summary(database, user_id)
    label = "Measurement:" if viewing else "Updated" if edited else "Recorded"
    text = f"{label} {record.weight_kg:.1f} kg · {measured_on:%d %b %Y}"
    if summary:
        text += f"\n\n{summary}"
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Edit measurement",
                        callback_data=f"weight:edit:{record.weight_id}",
                    ),
                    InlineKeyboardButton(
                        "Remove measurement" if edited or viewing else "Undo",
                        callback_data=f"weight:undo:{record.weight_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "Back to progress", callback_data="menu:progress"
                    )
                ],
            ]
        ),
    )


async def _entry_card(
    database: Database,
    user_id: int,
    entry: FoodEntry,
    offset: int = 0,
    day: Optional[str] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    timezone_name = await _call(database.get_timezone, user_id)
    if day is None and timezone_name is not None:
        day = local_date(entry.eaten_at_utc, timezone_name).isoformat()
    return entry_details(entry, timezone_name), InlineKeyboardMarkup(
        entry_action_rows(entry, offset, day)
    )


async def _return_to_item(turn: Turn, session: Session) -> None:
    if (
        session.state
        in {
            SessionState.WAIT_ENTRY_GRAMS,
            SessionState.WAIT_ENTRY_NAME,
            SessionState.WAIT_ENTRY_TIME,
            SessionState.WAIT_ENTRY_AMENDMENT,
        }
        and session.selected_entry_id is not None
    ):
        entry = await _call(
            turn.database.get_entry, turn.user_id, session.selected_entry_id
        )
        if entry is not None:
            text, keyboard = await _entry_card(
                turn.database,
                turn.user_id,
                entry,
                session.return_offset,
                session.return_day,
            )
            await turn.message.reply_text(text, reply_markup=keyboard)
    elif session.state in {
        SessionState.WAIT_FAVORITE_AMENDMENT,
        SessionState.WAIT_FAVORITE_TO_SERVING,
    }:
        favorite = await _call(
            turn.database.get_favorite, turn.user_id, session.selected_favorite_id
        )
        if favorite is not None:
            await turn.message.reply_text(
                favorite_details(favorite),
                reply_markup=InlineKeyboardMarkup(
                    favorite_action_rows(favorite, session.return_offset)
                ),
            )
    elif (
        session.state == SessionState.WAIT_WEIGHT_EDIT
        and session.selected_weight_id is not None
    ):
        record = await _call(
            turn.database.get_weight, turn.user_id, session.selected_weight_id
        )
        if record is not None:
            await _send_weight_receipt(
                turn.database, turn.user_id, turn.message, record, viewing=True
            )


async def _weight_summary(database: Database, user_id: int) -> Optional[str]:
    """Latest weight plus the floating 7-day average and week-over-week delta."""
    latest = await _call(database.latest_weight, user_id)
    if latest is None:
        return None
    timezone_name = await _call(database.get_timezone, user_id)
    if timezone_name is None:
        return f"Latest weight: {latest.weight_kg:.1f} kg."
    today = local_date(database.now_epoch(), timezone_name)
    week_start = day_bounds(today - timedelta(days=6), timezone_name).start_utc
    week_end = day_bounds(today, timezone_name).end_utc
    previous_start = day_bounds(today - timedelta(days=13), timezone_name).start_utc
    current = await _call(database.average_weight, user_id, week_start, week_end)
    previous = await _call(database.average_weight, user_id, previous_start, week_start)
    measured_on = local_date(latest.measured_at_utc, timezone_name)
    lines = [f"Latest weight: {latest.weight_kg:.1f} kg ({measured_on.isoformat()})."]
    if current is not None:
        line = f"7-day average: {current:.1f} kg"
        if previous is not None:
            line += f" ({current - previous:+.1f} kg vs previous week)"
        lines.append(line + ".")
    return "\n".join(lines)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    turn = await _begin(update, context, register_user=True)
    if turn is None or turn.message.text is None:
        return
    text = turn.message.text.strip()
    database = turn.database

    if text == "Cancel":
        if (
            turn.session is not None
            and turn.session.state == SessionState.WAIT_SAVE_FAVORITE
        ):
            await _call(database.clear_session, turn.user_id, turn.chat_id)
            await turn.message.reply_text(
                "Favorite was not saved. The food entry remains in your diary.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        await cancel(update, context)
        return

    session = turn.session
    timezone_name = await _call(database.get_timezone, turn.user_id)
    if turn.session_expired:
        if timezone_name is None:
            await _start_with_prompt(
                database,
                turn.user_id,
                turn.chat_id,
                SessionState.WAIT_TIMEZONE,
                turn.message,
                "The previous input expired. Enter your timezone to continue:",
                TIMEZONE_REQUIRED_MARKUP,
            )
        else:
            await turn.message.reply_text(
                "The previous input expired. Choose an option again.",
                reply_markup=MAIN_KEYBOARD,
            )
        return
    message_id = getattr(turn.message, "message_id", None)
    if session is not None and (
        session.prompt_pending
        or (message_id is not None and message_id == session.last_message_id)
    ):
        prompt, markup = session_prompt(session, timezone_name is not None)
        prefix = (
            "The previous prompt was not confirmed."
            if session.prompt_pending
            else "That message was already processed."
        )
        await turn.message.reply_text(f"{prefix}\n\n{prompt}", reply_markup=markup)
        if session.prompt_pending:
            await _mark_prompt_delivered(database, session)
        return
    if timezone_name is None and (
        session is None or session.state != SessionState.WAIT_TIMEZONE
    ):
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_TIMEZONE,
            turn.message,
            TIMEZONE_ONBOARDING_PROMPT,
            TIMEZONE_REQUIRED_MARKUP,
        )
        return

    if session is not None:
        await _handle_session_text(update, context, session, text)
        return

    if text.casefold() == "add food":
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_FOOD_NAME,
            turn.message,
            FOOD_NAME_PROMPT,
            SKIP_KEYBOARD,
        )
    elif text in {"Food Today", "Diary"}:
        await _show_entries(update, context, 0)
    elif text in {"Statistics", "Progress"}:
        await turn.message.reply_text(
            "Progress — view your food statistics or record your weight:",
            reply_markup=STATS_KEYBOARD,
        )
    elif text == "Settings":
        await turn.message.reply_text(
            "Settings — daily goal, timezone and Apple Health:",
            reply_markup=SETTINGS_KEYBOARD,
        )
    elif text == "Apple Health":
        await _health_action(update, context, [])
    elif text == "Week Stats":
        await _show_daily_stats(update, context, Period.WEEK, 0)
    elif text == "Month Stats":
        await _show_daily_stats(update, context, Period.MONTH, 0)
    elif text.casefold() == "search favorites":
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_FAVORITE_SEARCH,
            turn.message,
            FAVORITE_SEARCH_PROMPT,
            CANCEL_KEYBOARD,
        )
    elif text in {"My Favorites", "Favorites"}:
        await _show_favorites(update, context, 0)
    elif text.casefold() == "recent foods":
        await _show_recent(update, context)
    elif text.casefold() == "daily goal":
        goal = await _call(database.get_daily_goal, turn.user_id)
        prompt = (
            f"Your daily goal is {goal:.0f} kcal. {GOAL_PROMPT}"
            if goal is not None
            else GOAL_PROMPT
        )
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_GOAL,
            turn.message,
            prompt,
            GOAL_KEYBOARD,
        )
    elif text == "Weight":
        await _start_weight_prompt(turn)
    elif text in {"Update Timezone", "Timezone"}:
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_TIMEZONE,
            turn.message,
            TIMEZONE_CHANGE_PROMPT,
            TIMEZONE_CHANGE_KEYBOARD,
        )
    elif text in {"Back", "Main menu"}:
        await turn.message.reply_text("Select an option:", reply_markup=MAIN_KEYBOARD)
    elif not await _log_quick_add(turn, text):
        await turn.message.reply_text(
            "Choose an option from the keyboard, or log a food in one message, "
            "for example: oatmeal 370 60.",
            reply_markup=MAIN_KEYBOARD,
        )


async def _handle_session_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: Session,
    text: str,
) -> None:
    message = update.effective_message
    if message is None:
        return
    database = _db(context)
    now = database.now_epoch()
    message_date = getattr(message, "date", None)
    event_epoch = int(message_date.timestamp()) if message_date is not None else now
    message_id = getattr(message, "message_id", None)

    try:
        if session.state == SessionState.WAIT_TIMEZONE:
            timezone_name = canonical_timezone(text)
            await _call(database.complete_timezone_session, session, timezone_name, now)
            await message.reply_text(
                f"Timezone set to {timezone_name}. Meals will appear on your local calendar day.\n\n"
                "Tap Add food to log your first meal, or send a quick entry such as oatmeal 370 60 "
                "(370 kcal per 100 g, 60 g eaten).",
                reply_markup=MAIN_KEYBOARD,
            )
        elif session.state == SessionState.WAIT_FOOD_NAME:
            name = None if text == "Skip" else normalize_food_name(text)
            favorite = (
                None
                if name is None
                else await _call(database.find_favorite_by_name, session.user_id, name)
            )
            if favorite is None:
                await _advance_with_prompt(
                    database,
                    session,
                    SessionState.WAIT_CALORIES,
                    message,
                    message_id,
                    draft_name=name,
                )
            elif favorite.unit == UNIT_SERVING:
                advanced = await _transition(
                    database,
                    session,
                    SessionState.WAIT_FAVORITE_SERVINGS,
                    message_id,
                    draft_name=name,
                    selected_favorite_id=favorite.favorite_id,
                )
                await message.reply_text(
                    f"Found favorite {favorite.name} "
                    f"({favorite.calories_per_100g:.0f} kcal/serving). "
                    f"{FAVORITE_SERVINGS_PROMPT}",
                    reply_markup=FAVORITE_SERVINGS_MANUAL_KEYBOARD,
                )
                await _mark_prompt_delivered(database, advanced)
            else:
                advanced = await _transition(
                    database,
                    session,
                    SessionState.WAIT_FAVORITE_GRAMS,
                    message_id,
                    draft_name=name,
                    selected_favorite_id=favorite.favorite_id,
                )
                await message.reply_text(
                    f"Found favorite {favorite.name} "
                    f"({favorite.calories_per_100g:.0f} kcal/100 g). "
                    f"{FAVORITE_MATCH_GRAMS_PROMPT}",
                    reply_markup=MANUAL_ENTRY_KEYBOARD,
                )
                await _mark_prompt_delivered(database, advanced)
        elif session.state == SessionState.WAIT_CALORIES:
            if text == "Per Serving" and session.draft_unit == UNIT_100G:
                await _advance_with_prompt(
                    database,
                    session,
                    SessionState.WAIT_CALORIES,
                    message,
                    message_id,
                    draft_unit=UNIT_SERVING,
                )
            else:
                serving_mode = session.draft_unit == UNIT_SERVING
                calories = (
                    parse_calories_per_serving(text)
                    if serving_mode
                    else parse_calories(text)
                )
                await _advance_with_prompt(
                    database,
                    session,
                    SessionState.WAIT_GRAMS,
                    message,
                    message_id,
                    calories_per_100g=calories,
                )
        elif session.state == SessionState.WAIT_GRAMS:
            if session.draft_unit == UNIT_SERVING:
                servings = parse_servings(text)
                await _advance_with_prompt(
                    database,
                    session,
                    SessionState.WAIT_PROTEIN,
                    message,
                    message_id,
                    draft_servings=servings,
                )
            else:
                grams = parse_grams(text)
                await _advance_with_prompt(
                    database,
                    session,
                    SessionState.WAIT_PROTEIN,
                    message,
                    message_id,
                    serving_grams=grams,
                )
        elif session.state == SessionState.WAIT_PROTEIN:
            protein = (
                None if text == "Skip" else _parse_draft_macro(session, text, "Protein")
            )
            if session.draft_unit == UNIT_100G:
                validate_macro_sum(
                    protein, session.fat_per_100g, session.carbs_per_100g
                )
            await _advance_with_prompt(
                database,
                session,
                SessionState.WAIT_FAT,
                message,
                message_id,
                protein_per_100g=protein,
            )
        elif session.state == SessionState.WAIT_FAT:
            fat = None if text == "Skip" else _parse_draft_macro(session, text, "Fat")
            if session.draft_unit == UNIT_100G:
                validate_macro_sum(
                    session.protein_per_100g, fat, session.carbs_per_100g
                )
            await _advance_with_prompt(
                database,
                session,
                SessionState.WAIT_CARBS,
                message,
                message_id,
                fat_per_100g=fat,
            )
        elif session.state == SessionState.WAIT_CARBS:
            carbs = (
                None if text == "Skip" else _parse_draft_macro(session, text, "Carbs")
            )
            if session.draft_unit == UNIT_100G:
                validate_macro_sum(
                    session.protein_per_100g, session.fat_per_100g, carbs
                )
            completed = replace(
                session, carbs_per_100g=carbs, last_message_id=message_id
            )
            entry = await _call(database.complete_food_draft, completed, event_epoch)
            await _send_receipt(
                database, session.user_id, message, entry, restore_keyboard=True
            )
        elif session.state == SessionState.WAIT_SAVE_FAVORITE:
            if text == "Yes":
                await _call(
                    database.save_session_as_favorite,
                    session.user_id,
                    session.chat_id,
                    now,
                )
                await message.reply_text(
                    "Product saved as a favorite.", reply_markup=MAIN_KEYBOARD
                )
            elif text == "No":
                await _call(database.clear_session, session.user_id, session.chat_id)
                await message.reply_text("Done.", reply_markup=MAIN_KEYBOARD)
            else:
                await message.reply_text(
                    "Choose Yes or No.",
                    reply_markup=FAVORITE_DECISION_KEYBOARD,
                )
        elif session.state == SessionState.WAIT_FAVORITE_SEARCH:
            query = normalize_search_query(text)
            matches = await _call(database.search_favorites, session.user_id, query, 21)
            favorites = matches[:20]
            if not favorites:
                await message.reply_text(
                    "No matching favorite foods found. Try another name or add a new food.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Search again", callback_data="menu:search"
                                ),
                                InlineKeyboardButton(
                                    "Browse favorites", callback_data="fav:list:0"
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "Add food", callback_data="menu:add"
                                )
                            ],
                        ]
                    ),
                )
            else:
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                favorite_button_text(favorite),
                                callback_data=f"fav:use:{favorite.favorite_id}",
                            )
                        ]
                        for favorite in favorites
                    ]
                    + [
                        [
                            InlineKeyboardButton(
                                "Search again", callback_data="menu:search"
                            ),
                            InlineKeyboardButton(
                                "Browse favorites", callback_data="fav:list:0"
                            ),
                        ]
                    ]
                )
                label = (
                    "Select a favorite product (first 20 matches):"
                    if len(matches) > 20
                    else "Select a favorite product:"
                )
                await message.reply_text(label, reply_markup=keyboard)
            await message.reply_text(
                "Favorites — search or choose a food above.", reply_markup=MAIN_KEYBOARD
            )
            await _call(database.clear_session, session.user_id, session.chat_id)
        elif session.state in {
            SessionState.WAIT_FAVORITE_GRAMS,
            SessionState.WAIT_FAVORITE_SERVINGS,
        }:
            if text == "Enter Manually" and session.draft_name is not None:
                await _advance_with_prompt(
                    database,
                    session,
                    SessionState.WAIT_CALORIES,
                    message,
                    message_id,
                    selected_favorite_id=None,
                )
            else:
                amount = (
                    parse_servings(text)
                    if session.state == SessionState.WAIT_FAVORITE_SERVINGS
                    else parse_grams(text)
                )
                entry = await _call(
                    database.use_selected_favorite,
                    session.user_id,
                    session.chat_id,
                    amount,
                    event_epoch,
                )
                await _send_receipt(
                    database, session.user_id, message, entry, restore_keyboard=True
                )
        elif session.state == SessionState.WAIT_RECENT_GRAMS:
            amount = (
                None
                if text == "Same as last time"
                else _parse_entry_amount(session, text)
            )
            entry = await _call(
                database.use_selected_entry,
                session.user_id,
                session.chat_id,
                amount,
                event_epoch,
            )
            await _send_receipt(
                database, session.user_id, message, entry, restore_keyboard=True
            )
        elif session.state == SessionState.WAIT_GOAL:
            goal = None if text == "Remove" else parse_daily_goal(text)
            await _call(database.complete_goal_session, session, goal, now)
            await message.reply_text(
                "Daily goal removed."
                if goal is None
                else f"Daily goal set to {goal:.0f} kcal.",
                reply_markup=MAIN_KEYBOARD,
            )
        elif session.state == SessionState.WAIT_ENTRY_GRAMS:
            amount = _parse_entry_amount(session, text)
            entry = await _call(
                database.update_entry_amount,
                session.user_id,
                session.chat_id,
                amount,
                now,
            )
            await message.reply_text(
                f"Serving updated to {amount_text(entry.nutrition)} "
                f"({entry.nutrition.calories:.0f} kcal).",
                reply_markup=MAIN_KEYBOARD,
            )
        elif session.state == SessionState.WAIT_ENTRY_NAME:
            entry = await _call(
                database.update_entry_name,
                session.user_id,
                session.chat_id,
                text,
                now,
            )
            await message.reply_text(
                f"Food entry renamed to {entry.name}.", reply_markup=MAIN_KEYBOARD
            )
        elif session.state == SessionState.WAIT_ENTRY_AMENDMENT:
            nutrient = session.selected_nutrient or "value"
            value = parse_nutrient_value(text, nutrient.title())
            entry = await _call(
                database.update_entry_field,
                session.user_id,
                session.chat_id,
                value,
                now,
            )
            await message.reply_text(
                "Food entry updated.",
                reply_markup=MAIN_KEYBOARD,
            )
        elif session.state == SessionState.WAIT_FAVORITE_TO_SERVING:
            grams = parse_grams(text)
            favorite = await _call(
                database.convert_favorite_to_serving,
                session.user_id,
                session.chat_id,
                grams,
                now,
            )
            await message.reply_text(
                "Favorite converted to a serving.",
                reply_markup=MAIN_KEYBOARD,
            )
        elif session.state == SessionState.WAIT_WEIGHT:
            weight = parse_weight(text)
            record = await _call(
                database.complete_weight_session, session, weight, event_epoch
            )
            await _send_weight_receipt(
                database, session.user_id, message, record, restore_keyboard=True
            )
        elif session.state == SessionState.WAIT_WEIGHT_EDIT:
            record = await _call(
                database.complete_weight_edit, session, parse_weight(text)
            )
            await _send_weight_receipt(
                database,
                session.user_id,
                message,
                record,
                restore_keyboard=True,
                edited=True,
            )
        elif session.state == SessionState.WAIT_ENTRY_TIME:
            timezone_name = await _call(database.get_timezone, session.user_id)
            if timezone_name is None:
                raise StateConflict("Timezone is required to edit entry time")
            eaten_at = parse_entry_time(text, timezone_name, now)
            await _call(
                database.update_entry_time,
                session.user_id,
                session.chat_id,
                eaten_at,
                now,
            )
            await message.reply_text("Entry time updated.", reply_markup=MAIN_KEYBOARD)
        elif session.state == SessionState.WAIT_FAVORITE_AMENDMENT:
            if session.selected_nutrient not in {
                "calories",
                "protein",
                "fat",
                "carbs",
            }:
                raise StateConflict("Favorite amendment context is incomplete")
            # Unit-specific limits are enforced by the database against the
            # favorite's own unit (per 100g or per serving).
            value = parse_nutrient_value(text, session.selected_nutrient.title())
            await _call(
                database.complete_favorite_amendment,
                session.user_id,
                session.chat_id,
                value,
                now,
            )
            await message.reply_text(
                "Favorite product updated.", reply_markup=MAIN_KEYBOARD
            )
        else:
            raise StateConflict("Unknown workflow state")
        if session.state != SessionState.WAIT_WEIGHT_EDIT:
            await _return_to_item(
                Turn(database, session.user_id, session.chat_id, message, None, False),
                session,
            )
    except ValidationError as exc:
        current = await _call(database.get_session, session.user_id, session.chat_id)
        if current is not None and current.revision == session.revision:
            refreshed = replace(current, updated_at_utc=database.now_epoch())
            await _call(database.update_session, current, refreshed)
        await message.reply_text(str(exc))
    except (StateConflict, NotFound):
        await _call(database.clear_session, session.user_id, session.chat_id)
        await message.reply_text(
            "That workflow is no longer available. Please start again.",
            reply_markup=MAIN_KEYBOARD,
        )


def _parse_draft_macro(session: Session, text: str, label: str) -> float:
    if session.draft_unit == UNIT_SERVING:
        return parse_macro_per_serving(text, label)
    return parse_macro(text, label)


def _parse_entry_amount(session: Session, text: str) -> float:
    """Parse an amount in the selected entry's own unit.

    The unit is carried on the session so the prompt, the validation limits,
    and the error message all agree on grams versus servings.
    """
    if session.draft_unit == UNIT_SERVING:
        return parse_servings(text)
    return parse_grams(text)


async def _transition(
    database: Database,
    session: Session,
    state: SessionState,
    message_id: Optional[int],
    **changes: object,
) -> Session:
    updated = replace(
        session,
        state=state,
        prompt_pending=True,
        last_message_id=message_id,
        updated_at_utc=database.now_epoch(),
        **changes,
    )
    return await _call(database.update_session, session, updated)


async def _mark_prompt_delivered(database: Database, session: Session) -> Session:
    ready = replace(
        session,
        prompt_pending=False,
        updated_at_utc=database.now_epoch(),
    )
    return await _call(database.update_session, session, ready)


async def _advance_with_prompt(
    database: Database,
    session: Session,
    state: SessionState,
    message: Any,
    message_id: Optional[int],
    **changes: object,
) -> Session:
    advanced = await _transition(
        database, session, state, message_id=message_id, **changes
    )
    prompt, markup = session_prompt(advanced, True)
    await message.reply_text(prompt, reply_markup=markup)
    return await _mark_prompt_delivered(database, advanced)


async def _start_with_prompt(
    database: Database,
    user_id: int,
    chat_id: int,
    state: SessionState,
    message: Any,
    prompt: str,
    markup: Markup,
    **values: object,
) -> Session:
    values.setdefault("last_message_id", getattr(message, "message_id", None))
    started = await _call(
        database.start_session,
        user_id,
        chat_id,
        state,
        prompt_pending=True,
        **values,
    )
    await message.reply_text(prompt, reply_markup=markup)
    return await _mark_prompt_delivered(database, started)


def _session_expired(session: Session, now_utc: int) -> bool:
    return session.updated_at_utc < now_utc - SESSION_TTL_SECONDS


async def _reprompt(turn: Turn, prefix: str) -> None:
    """Repeat the active session's prompt below a short explanation."""
    session = turn.session
    if session is None:
        return
    has_timezone = await _call(turn.database.get_timezone, turn.user_id) is not None
    prompt, markup = session_prompt(session, has_timezone)
    await turn.message.reply_text(f"{prefix}\n\n{prompt}", reply_markup=markup)
    await _mark_prompt_delivered(turn.database, session)


async def _day_totals_text(
    database: Database,
    user_id: int,
    timezone_name: str,
    day: date,
    today: date,
) -> Optional[str]:
    """Totals block for one local day; None when the day has no entries."""
    bounds = day_bounds(day, timezone_name)
    stats = await _call(database.stats, user_id, bounds.start_utc, bounds.end_utc)
    if stats.entry_count == 0:
        return None
    if day == today:
        title = "Today's totals"
    elif day == today - timedelta(days=1):
        title = "Yesterday's totals"
    else:
        title = f"Totals for {day.isoformat()}"
    goal = await _call(database.get_daily_goal, user_id) if day == today else None
    return stats_totals_text(title, stats, goal)


async def _show_daily_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    period: Period,
    offset: int,
    edit: bool = False,
    month: Optional[tuple[int, int]] = None,
) -> None:
    user_id, _ = _identity(update)
    database = _db(context)
    timezone_name = await _call(database.get_timezone, user_id)
    if timezone_name is None:
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                "Set your timezone first with /updatetimezone."
            )
        return
    today = local_date(database.now_epoch(), timezone_name)
    if period == Period.MONTH:
        year, month_number = month or (today.year, today.month)
        bounds = month_bounds(year, month_number, timezone_name)
        shown_month = date(year, month_number, 1)
        prefix = f"stats:month:{shown_month:%Y-%m}"
        title = f"{shown_month:%B %Y}, logged days"
        empty_label = f"{shown_month:%B %Y}"
    else:
        bounds = period_bounds(period, timezone_name)
        shown_month = None
        prefix = "stats:week"
        title = "Last 7 days, logged days"
        empty_label = "week"
    page = await _call(
        database.daily_breakdown,
        user_id,
        bounds.start_utc,
        bounds.end_utc,
        timezone_name,
        offset,
        STATS_PAGE_SIZE,
    )
    if not page.items and offset > 0:
        page = await _call(
            database.daily_breakdown,
            user_id,
            bounds.start_utc,
            bounds.end_utc,
            timezone_name,
            0,
            STATS_PAGE_SIZE,
        )
    rows: list[list[InlineKeyboardButton]] = []
    if not page.items:
        text = f"No food entries found for {empty_label}."
    else:
        blocks = "\n\n".join(day_stats_block(day) for day in page.items)
        text = f"{title}:\n\nTap a day below to open its diary.\n\n{blocks}"
        rows.extend(stats_day_rows(page.items))
        navigation = navigation_row(page, prefix, STATS_PAGE_SIZE)
        if navigation:
            rows.append(navigation)
    if shown_month is not None:
        months = month_navigation_row(shown_month.year, shown_month.month, today)
        if months:
            rows.append(months)
    keyboard = InlineKeyboardMarkup(rows) if rows else None
    if edit and update.callback_query is not None:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _show_entries(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    offset: int,
    edit: bool = False,
    day: Optional[date] = None,
) -> None:
    """Diary page for one local day (today when ``day`` is None)."""
    user_id, _ = _identity(update)
    database = _db(context)
    timezone_name = await _call(database.get_timezone, user_id)
    if timezone_name is None:
        if update.effective_message is not None:
            await update.effective_message.reply_text(
                "Set your timezone first with /updatetimezone."
            )
        return
    today = local_date(database.now_epoch(), timezone_name)
    oldest = min(EARLIEST_DIARY_DATE, today)
    shown_day = today if day is None else min(max(day, oldest), today)
    is_today = shown_day == today
    bounds = day_bounds(shown_day, timezone_name)
    page = await _call(
        database.page_entries,
        user_id,
        bounds.start_utc,
        bounds.end_utc,
        offset,
        PAGE_SIZE,
    )
    if not page.items and offset > 0:
        page = await _call(
            database.page_entries,
            user_id,
            bounds.start_utc,
            bounds.end_utc,
            0,
            PAGE_SIZE,
        )
    list_prefix = "entry:list" if is_today else f"entry:list:{shown_day.isoformat()}"
    view_suffix = "" if is_today else f":{shown_day.isoformat()}"
    day_label = "today" if is_today else shown_day.isoformat()
    rows: list[list[InlineKeyboardButton]] = []
    if not page.items:
        text = f"No food entries found for {day_label}. Tap Add food to log a meal for today."
        rows.append([InlineKeyboardButton("Add food", callback_data="menu:add")])
    else:
        totals = await _day_totals_text(
            database, user_id, timezone_name, shown_day, today
        )
        title = "Today's food entries" if is_today else f"Entries for {day_label}"
        text = f"{totals}\n\n{title}:"
        rows = [
            [
                InlineKeyboardButton(
                    entry_button_text(entry, timezone_name),
                    callback_data=(
                        f"entry:view:{entry.entry_id}:{page.offset}{view_suffix}"
                    ),
                )
            ]
            for entry in page.items
        ]
        navigation = navigation_row(page, list_prefix)
        if navigation:
            rows.append(navigation)
    day_navigation = day_navigation_row(shown_day, today, oldest)
    if day_navigation:
        rows.append(day_navigation)
    keyboard = InlineKeyboardMarkup(rows)
    if edit and update.callback_query is not None:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _show_favorites(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    offset: int,
    edit: bool = False,
) -> None:
    user_id, _ = _identity(update)
    database = _db(context)
    page = await _call(database.page_favorites, user_id, offset, PAGE_SIZE)
    if not page.items and offset > 0:
        page = await _call(database.page_favorites, user_id, 0, PAGE_SIZE)
    if not page.items:
        text = "No favorite foods yet. Log a food, then tap Save as favorite on its receipt."
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Add food", callback_data="menu:add")],
                [InlineKeyboardButton("Search favorites", callback_data="menu:search")],
            ]
        )
    else:
        rows = [
            [
                InlineKeyboardButton(
                    favorite_button_text(favorite),
                    callback_data=f"fav:view:{favorite.favorite_id}:{page.offset}",
                )
            ]
            for favorite in page.items
        ]
        navigation = navigation_row(page, "fav:list")
        if navigation:
            rows.append(navigation)
        rows.append(
            [InlineKeyboardButton("Search favorites", callback_data="menu:search")]
        )
        text, keyboard = "Your favorite foods:", InlineKeyboardMarkup(rows)
    if edit and update.callback_query is not None:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _show_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    user_id, _ = _identity(update)
    database = _db(context)
    templates = await _call(database.recent_entry_templates, user_id, 10)
    if not templates:
        await message.reply_text(
            "No recent foods yet. Log a food first.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Add food", callback_data="menu:add")]]
            ),
        )
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    entry_button_text(entry),
                    callback_data=f"recent:use:{entry.entry_id}",
                )
            ]
            for entry in templates
        ]
    )
    await message.reply_text(
        "Select a recent food to log again:", reply_markup=keyboard
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not await _require_private(update):
        return
    await query.answer()
    if query.message is None or not query.message.is_accessible:
        return

    action = parse_callback(query.data or "")
    if action is None:
        await query.edit_message_text("This action is invalid or has expired.")
        return

    user_id, chat_id = _identity(update)
    database = _db(context)
    active_session, _ = await _active_session(database, user_id, chat_id)
    if active_session is not None and action.kind not in {"cancel", "dismiss"}:
        has_timezone = await _call(database.get_timezone, user_id) is not None
        prompt, markup = session_prompt(active_session, has_timezone)
        await query.message.reply_text(
            f"Finish the current input first, or choose Cancel.\n\n{prompt}",
            reply_markup=markup,
        )
        await _mark_prompt_delivered(database, active_session)
        return
    try:
        if (
            action.kind.startswith("entry_")
            and action.record_id is not None
            and action.day is None
        ):
            selected = await _call(database.get_entry, user_id, action.record_id)
            timezone_name = await _call(database.get_timezone, user_id)
            if selected is not None and timezone_name is not None:
                action = replace(
                    action, day=local_date(selected.eaten_at_utc, timezone_name)
                )
        if action.kind == "cancel":
            timezone_name = await _call(database.get_timezone, user_id)
            if timezone_name is None:
                await query.edit_message_text("Timezone setup is required.")
                await _start_with_prompt(
                    database,
                    user_id,
                    chat_id,
                    SessionState.WAIT_TIMEZONE,
                    query.message,
                    TIMEZONE_ONBOARDING_PROMPT,
                    TIMEZONE_REQUIRED_MARKUP,
                )
            elif active_session is None or active_session.state not in {
                SessionState.WAIT_FAVORITE_SEARCH,
                SessionState.WAIT_FAVORITE_GRAMS,
                SessionState.WAIT_FAVORITE_SERVINGS,
                SessionState.WAIT_FAVORITE_AMENDMENT,
                SessionState.WAIT_FAVORITE_TO_SERVING,
                SessionState.WAIT_RECENT_GRAMS,
                SessionState.WAIT_ENTRY_GRAMS,
                SessionState.WAIT_ENTRY_TIME,
                SessionState.WAIT_ENTRY_NAME,
                SessionState.WAIT_ENTRY_AMENDMENT,
                SessionState.WAIT_GOAL,
                SessionState.WAIT_WEIGHT,
                SessionState.WAIT_WEIGHT_EDIT,
            }:
                await query.edit_message_text("This prompt has expired.")
            else:
                await _call(database.clear_session, user_id, chat_id)
                await query.edit_message_text("Cancelled.")
                await query.message.reply_text(
                    "Select an option:", reply_markup=MAIN_KEYBOARD
                )
                await _return_to_item(
                    Turn(database, user_id, chat_id, query.message, None, False),
                    active_session,
                )
        elif action.kind == "dismiss":
            await query.edit_message_text("No changes made.")
        elif action.kind in {"menu_add", "menu_search"}:
            has_timezone = await _call(database.get_timezone, user_id) is not None
            state = (
                SessionState.WAIT_FOOD_NAME
                if action.kind == "menu_add"
                else SessionState.WAIT_FAVORITE_SEARCH
            )
            prompt = (
                FOOD_NAME_PROMPT
                if action.kind == "menu_add"
                else FAVORITE_SEARCH_PROMPT
            )
            markup = SKIP_KEYBOARD if action.kind == "menu_add" else CANCEL_KEYBOARD
            if not has_timezone:
                state, prompt, markup = (
                    SessionState.WAIT_TIMEZONE,
                    TIMEZONE_ONBOARDING_PROMPT,
                    TIMEZONE_REQUIRED_MARKUP,
                )
            await _start_with_prompt(
                database, user_id, chat_id, state, query.message, prompt, markup
            )
        elif action.kind in {"menu_progress", "menu_settings"}:
            await query.message.reply_text(
                "Progress — food statistics and weight:"
                if action.kind == "menu_progress"
                else "Settings — daily goal, timezone and Apple Health:",
                reply_markup=STATS_KEYBOARD
                if action.kind == "menu_progress"
                else SETTINGS_KEYBOARD,
            )
        elif (
            action.kind in {"weight_edit", "weight_undo", "weight_view"}
            and action.record_id is not None
        ):
            record = await _call(database.get_weight, user_id, action.record_id)
            if record is None:
                await query.edit_message_text(
                    "This measurement is no longer available.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Back to progress", callback_data="menu:progress"
                                )
                            ]
                        ]
                    ),
                )
            elif action.kind == "weight_undo":
                await _call(database.delete_weight, user_id, record.weight_id)
                summary = await _weight_summary(database, user_id)
                await query.edit_message_text(
                    "Weight measurement removed.\n\n"
                    + (summary or "No weight measurements yet."),
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Back to progress", callback_data="menu:progress"
                                )
                            ]
                        ]
                    ),
                )
            elif action.kind == "weight_view":
                await _send_weight_receipt(
                    database, user_id, query.message, record, viewing=True
                )
            else:
                timezone_name = await _call(database.get_timezone, user_id)
                measured_on = local_date(record.measured_at_utc, timezone_name or "UTC")
                prompt = f"Correct the measurement from {measured_on:%d %b %Y}.\nCurrently {record.weight_kg:.1f} kg.\n\nEnter the corrected weight in kg:"
                await _start_with_prompt(
                    database,
                    user_id,
                    chat_id,
                    SessionState.WAIT_WEIGHT_EDIT,
                    query.message,
                    prompt,
                    CANCEL_KEYBOARD,
                    selected_weight_id=record.weight_id,
                    prompt_text=prompt,
                )
        elif action.kind == "entry_list":
            await _show_entries(
                update, context, action.offset, edit=True, day=action.day
            )
        elif action.kind == "stats_page" and action.period is not None:
            month = None
            if action.month is not None:
                year_text, month_text = action.month.split("-")
                month = (int(year_text), int(month_text))
            await _show_daily_stats(
                update,
                context,
                Period(action.period),
                action.offset,
                edit=True,
                month=month,
            )
        elif action.kind == "stats_day":
            timezone_name = await _call(database.get_timezone, user_id)
            if timezone_name is None:
                await query.edit_message_text(
                    "Set your timezone first with /updatetimezone."
                )
            else:
                yesterday = local_date(database.now_epoch(), timezone_name) - timedelta(
                    days=1
                )
                await _show_entries(update, context, 0, edit=True, day=yesterday)
        elif action.kind == "favorite_list":
            await _show_favorites(update, context, action.offset, edit=True)
        elif action.kind == "entry_view" and action.record_id is not None:
            entry = await _call(database.get_entry, user_id, action.record_id)
            if entry is None:
                raise NotFound("Food entry not found")
            text, keyboard = await _entry_card(
                database,
                user_id,
                entry,
                action.offset,
                action.day.isoformat() if action.day else None,
            )
            await query.edit_message_text(text, reply_markup=keyboard)
        elif action.kind == "entry_grams" and action.record_id is not None:
            entry = await _call(database.get_entry, user_id, action.record_id)
            if entry is None:
                raise NotFound("Food entry not found")
            serving_based = entry.nutrition.servings is not None
            started = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_ENTRY_GRAMS,
                selected_entry_id=entry.entry_id,
                return_day=action.day.isoformat() if action.day else None,
                return_offset=action.offset,
                draft_unit=UNIT_SERVING if serving_based else UNIT_100G,
                prompt_pending=True,
            )
            await query.edit_message_text(
                f"Editing the amount for {entry.name or 'Unnamed food'} "
                f"(currently {amount_text(entry.nutrition)})."
            )
            await query.message.reply_text(
                ENTRY_SERVINGS_PROMPT if serving_based else ENTRY_GRAMS_PROMPT,
                reply_markup=CANCEL_KEYBOARD,
            )
            await _mark_prompt_delivered(database, started)
        elif action.kind == "entry_name" and action.record_id is not None:
            entry = await _call(database.get_entry, user_id, action.record_id)
            if entry is None:
                raise NotFound("Food entry not found")
            started = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_ENTRY_NAME,
                selected_entry_id=entry.entry_id,
                return_day=action.day.isoformat() if action.day else None,
                return_offset=action.offset,
                prompt_pending=True,
            )
            await query.edit_message_text(f"Renaming {entry.name or 'Unnamed food'}.")
            await query.message.reply_text(
                ENTRY_NAME_PROMPT, reply_markup=CANCEL_KEYBOARD
            )
            await _mark_prompt_delivered(database, started)
        elif (
            action.kind == "entry_field"
            and action.record_id is not None
            and action.nutrient is not None
        ):
            entry = await _call(database.get_entry, user_id, action.record_id)
            if entry is None:
                raise NotFound("Food entry not found")
            serving_based = entry.nutrition.servings is not None
            started = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_ENTRY_AMENDMENT,
                selected_entry_id=entry.entry_id,
                return_day=action.day.isoformat() if action.day else None,
                return_offset=action.offset,
                selected_nutrient=action.nutrient,
                prompt_text=nutrient_edit_prompt(entry, action.nutrient),
                draft_unit=UNIT_SERVING if serving_based else UNIT_100G,
                prompt_pending=True,
            )
            await query.edit_message_text(
                f"Editing {action.nutrient} for {entry.name or 'Unnamed food'}."
            )
            await query.message.reply_text(
                started.prompt_text,
                reply_markup=CANCEL_KEYBOARD,
            )
            await _mark_prompt_delivered(database, started)
        elif action.kind == "entry_favorite" and action.record_id is not None:
            favorite, created = await _call(
                database.add_favorite_from_entry, user_id, action.record_id
            )
            await query.message.reply_text(
                (
                    f"{favorite.name} saved as a favorite."
                    if created
                    else f"Favorite {favorite.name} updated with these values."
                ),
                reply_markup=MAIN_KEYBOARD,
            )
        elif action.kind == "entry_time" and action.record_id is not None:
            entry = await _call(database.get_entry, user_id, action.record_id)
            if entry is None:
                raise NotFound("Food entry not found")
            started = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_ENTRY_TIME,
                selected_entry_id=entry.entry_id,
                return_day=action.day.isoformat() if action.day else None,
                return_offset=action.offset,
                prompt_pending=True,
            )
            await query.edit_message_text(
                f"Editing time for {entry.name or 'Unnamed food'}."
            )
            await query.message.reply_text(
                ENTRY_TIME_PROMPT, reply_markup=CANCEL_KEYBOARD
            )
            await _mark_prompt_delivered(database, started)
        elif action.kind == "recent_use" and action.record_id is not None:
            entry = await _call(database.get_entry, user_id, action.record_id)
            if entry is None:
                raise NotFound("Food entry not found")
            serving_based = entry.nutrition.servings is not None
            started = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_RECENT_GRAMS,
                selected_entry_id=entry.entry_id,
                return_day=action.day.isoformat() if action.day else None,
                return_offset=action.offset,
                draft_unit=UNIT_SERVING if serving_based else UNIT_100G,
                prompt_pending=True,
            )
            name = entry.name or "Unnamed food"
            await query.edit_message_text(f"Selected: {name}")
            amount = (
                "the number of servings"
                if serving_based
                else "the serving weight in grams"
            )
            await query.message.reply_text(
                f"Enter {amount} for {name}, or choose "
                f"Same as last time ({amount_text(entry.nutrition)}):",
                reply_markup=REPEAT_KEYBOARD,
            )
            await _mark_prompt_delivered(database, started)
        elif action.kind == "entry_delete" and action.record_id is not None:
            entry = await _call(database.get_entry, user_id, action.record_id)
            if entry is None:
                raise NotFound("Food entry not found")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Delete",
                            callback_data=contextual(
                                f"entry:dc:{entry.entry_id}:{database.now_epoch()}",
                                action.offset,
                                action.day.isoformat() if action.day else None,
                            ),
                        ),
                        InlineKeyboardButton(
                            "Keep",
                            callback_data=contextual(
                                f"entry:view:{entry.entry_id}",
                                action.offset,
                                action.day.isoformat() if action.day else None,
                            ),
                        ),
                    ]
                ]
            )
            await query.edit_message_text(
                f"Delete {entry.name or 'this food'} · {amount_text(entry.nutrition)}?",
                reply_markup=keyboard,
            )
        elif (
            action.kind in {"entry_delete_confirm", "entry_undo"}
            and action.record_id is not None
        ):
            if confirmation_expired(action, database.now_epoch()):
                await query.edit_message_text(
                    "Undo is no longer available. Open the entry to edit or delete it."
                    if action.kind == "entry_undo"
                    else "This delete confirmation has expired. Open the entry to try again.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Open entry",
                                    callback_data=contextual(
                                        f"entry:view:{action.record_id}",
                                        action.offset,
                                        action.day.isoformat() if action.day else None,
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            await _call(database.delete_entry, user_id, action.record_id)
            await query.edit_message_text(
                "Food entry deleted.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Back to diary",
                                callback_data=(
                                    f"entry:list:{action.day.isoformat()}:{action.offset}"
                                    if action.day
                                    else f"entry:list:{action.offset}"
                                ),
                            )
                        ]
                    ]
                ),
            )
        elif action.kind == "favorite_view" and action.record_id is not None:
            favorite = await _call(database.get_favorite, user_id, action.record_id)
            if favorite is None:
                raise NotFound("Favorite not found")
            await query.edit_message_text(
                favorite_details(favorite),
                reply_markup=InlineKeyboardMarkup(
                    favorite_action_rows(favorite, action.offset)
                ),
            )
        elif action.kind == "favorite_use" and action.record_id is not None:
            favorite = await _call(database.get_favorite, user_id, action.record_id)
            if favorite is None:
                raise NotFound("Favorite not found")
            serving_based = favorite.unit == UNIT_SERVING
            started = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_FAVORITE_SERVINGS
                if serving_based
                else SessionState.WAIT_FAVORITE_GRAMS,
                selected_favorite_id=favorite.favorite_id,
                return_offset=action.offset,
                prompt_pending=True,
            )
            await query.edit_message_text(f"Selected favorite: {favorite.name}")
            if serving_based:
                await query.message.reply_text(
                    f"Enter the number of servings of {favorite.name}, "
                    "for example 1 or 0.5:",
                    reply_markup=SERVINGS_KEYBOARD,
                )
            else:
                await query.message.reply_text(
                    f"Enter the serving weight in grams for {favorite.name}:",
                    reply_markup=CANCEL_KEYBOARD,
                )
            await _mark_prompt_delivered(database, started)
        elif action.kind == "favorite_to_serving" and action.record_id is not None:
            favorite = await _call(database.get_favorite, user_id, action.record_id)
            if favorite is None:
                raise NotFound("Favorite not found")
            if favorite.unit == UNIT_SERVING:
                await query.edit_message_text("This favorite is already serving-based.")
            else:
                started = await _call(
                    database.start_session,
                    user_id,
                    chat_id,
                    SessionState.WAIT_FAVORITE_TO_SERVING,
                    selected_favorite_id=favorite.favorite_id,
                    return_offset=action.offset,
                    prompt_pending=True,
                )
                await query.edit_message_text(
                    f"Converting {favorite.name} to a serving."
                )
                await query.message.reply_text(
                    FAVORITE_TO_SERVING_PROMPT, reply_markup=CANCEL_KEYBOARD
                )
                await _mark_prompt_delivered(database, started)
        elif action.kind == "favorite_edit" and action.record_id is not None:
            favorite = await _call(database.get_favorite, user_id, action.record_id)
            if favorite is None:
                raise NotFound("Favorite not found")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Calories",
                            callback_data=contextual(
                                f"fav:field:{favorite.favorite_id}:calories",
                                action.offset,
                            ),
                        ),
                        InlineKeyboardButton(
                            "Protein",
                            callback_data=contextual(
                                f"fav:field:{favorite.favorite_id}:protein",
                                action.offset,
                            ),
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "Fat",
                            callback_data=contextual(
                                f"fav:field:{favorite.favorite_id}:fat", action.offset
                            ),
                        ),
                        InlineKeyboardButton(
                            "Carbs",
                            callback_data=contextual(
                                f"fav:field:{favorite.favorite_id}:carbs", action.offset
                            ),
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "Back to favorite",
                            callback_data=contextual(
                                f"fav:view:{favorite.favorite_id}", action.offset
                            ),
                        )
                    ],
                ]
            )
            await query.edit_message_text(
                favorite_details(favorite) + "\n\nChoose a nutrient to edit:",
                reply_markup=keyboard,
            )
        elif (
            action.kind == "favorite_field"
            and action.record_id is not None
            and action.nutrient is not None
        ):
            favorite = await _call(database.get_favorite, user_id, action.record_id)
            if favorite is None:
                raise NotFound("Favorite not found")
            started = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_FAVORITE_AMENDMENT,
                selected_favorite_id=favorite.favorite_id,
                return_offset=action.offset,
                selected_nutrient=action.nutrient,
                prompt_text=nutrient_edit_prompt(favorite, action.nutrient),
                prompt_pending=True,
            )
            await query.edit_message_text(
                f"Editing {action.nutrient} for {favorite.name}."
            )
            await query.message.reply_text(
                started.prompt_text,
                reply_markup=CANCEL_KEYBOARD,
            )
            await _mark_prompt_delivered(database, started)
        elif action.kind == "favorite_delete" and action.record_id is not None:
            favorite = await _call(database.get_favorite, user_id, action.record_id)
            if favorite is None:
                raise NotFound("Favorite not found")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Delete",
                            callback_data=contextual(
                                f"fav:delete-confirm:{favorite.favorite_id}:{database.now_epoch()}",
                                action.offset,
                            ),
                        ),
                        InlineKeyboardButton(
                            "Keep",
                            callback_data=contextual(
                                f"fav:view:{favorite.favorite_id}", action.offset
                            ),
                        ),
                    ]
                ]
            )
            await query.edit_message_text(
                f"Delete favorite {favorite.name}?", reply_markup=keyboard
            )
        elif action.kind == "favorite_delete_confirm" and action.record_id is not None:
            if confirmation_expired(action, database.now_epoch()):
                await query.edit_message_text(
                    "This delete confirmation has expired. Open the favorite to try again.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Open favorite",
                                    callback_data=contextual(
                                        f"fav:view:{action.record_id}", action.offset
                                    ),
                                )
                            ]
                        ]
                    ),
                )
                return
            await _call(database.delete_favorite, user_id, action.record_id)
            await query.edit_message_text(
                "Favorite product deleted.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Back to favorites",
                                callback_data=f"fav:list:{action.offset}",
                            )
                        ]
                    ]
                ),
            )
        else:
            await query.edit_message_text("This action is invalid or has expired.")
    except (NotFound, StateConflict):
        await query.edit_message_text(
            "This item is unavailable or belongs to another user."
        )
    except ValidationError as exc:
        await query.edit_message_text(str(exc))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    turn = await _begin(update, context)
    if turn is None:
        return
    if turn.session is None:
        await turn.message.reply_text(
            "Unknown command. Use /start or choose an option from the keyboard.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    await _reprompt(turn, "Unknown command.")


async def handle_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    turn = await _begin(update, context)
    if turn is None:
        return
    if turn.session is None:
        await turn.message.reply_text(
            "Choose a text command from the keyboard.", reply_markup=MAIN_KEYBOARD
        )
        return
    await _reprompt(turn, "Please send a text value.")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = None
    chat_id = None
    update_id = None
    if isinstance(update, Update):
        update_id = update.update_id
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
    error = context.error
    exc_info = None
    if error is not None:
        exc_info = (type(error), error, error.__traceback__)
    logger.error(
        "Unhandled update error update_id=%s user_id=%s chat_id=%s",
        update_id,
        user_id,
        chat_id,
        exc_info=exc_info,
    )
    if (
        isinstance(update, Update)
        and update.effective_message is not None
        and update.effective_message.is_accessible
    ):
        try:
            await update.effective_message.reply_text(
                "The response could not be completed. Send /start to show the current "
                "step or return to the menu before entering more data."
            )
        except TelegramError:
            logger.warning(
                "Failed to send error response update_id=%s user_id=%s chat_id=%s",
                update_id,
                user_id,
                chat_id,
            )


def _health_sample_text(sample: dict | None) -> str:
    if sample is None:
        return "удалено / значение не указано"
    labels = {
        "calories": "Калории",
        "protein": "Белки",
        "fat": "Жиры",
        "carbs": "Углеводы",
        "weight": "Вес",
    }
    return f"{labels[sample['type']]}: {sample['value']:g} {sample['unit']}, {sample['date']}"


def _health_keyboard(url: str | None, connected: bool) -> InlineKeyboardMarkup:
    rows = []
    if url:
        if not connected:
            rows.append(
                [
                    InlineKeyboardButton(
                        "Подключить Apple Health", callback_data="health:connect"
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "Как настроить шорткат на iPhone",
                    url=urljoin(url, "/static/apple-health.html"),
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("Проверить подключение", callback_data="health:status")]
    )
    return InlineKeyboardMarkup(rows)


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _health_action(update, context, context.args or [])


async def health_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_private(update):
        return
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if query.message is None or not query.message.is_accessible:
        return
    args = []
    if query.data == "health:connect":
        user_id, _ = _identity(update)
        status = await _call(HealthStore(_db(context).path).status, user_id)
        # An old button or a double tap must not invalidate an installed key.
        if not status["connected"]:
            args = ["connect"]
    await _health_action(update, context, args)


async def _health_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]
) -> None:
    if not await _require_private(update) or update.effective_message is None:
        return
    user_id, _ = _identity(update)
    store = HealthStore(_db(context).path)
    url = context.application.bot_data.get("miniapp_url")
    guide = urljoin(url, "/static/apple-health.html") if url else None
    try:
        if args and args[0] == "connect" and len(args) <= 2:
            if not url:
                await update.effective_message.reply_text(
                    "Сначала настройте HTTPS-сервер Mini App и MINIAPP_URL по docs/miniapp.md."
                )
                return
            token = await _call(
                store.connect, user_id, args[1] if len(args) == 2 else None
            )
            await update.effective_message.reply_text(
                "Подключение Apple Health для одного iPhone.\n"
                "При первом подключении переносятся записи с начала сегодняшнего дня. "
                "Для истории: /health connect ГГГГ-ММ-ДД. Повторное подключение сохраняет "
                "прогресс и прежнюю дату, если новая дата не указана. Старый ключ отключён.\n\n"
                f"Адрес сервера:\n{urljoin(url, '/health/v1/')}\n\n"
                f"Персональный ключ (скопируйте только следующую строку):\n{token}\n\n"
                "Добавьте его в команду на iPhone; не публикуйте команду вместе с ключом.\n"
                f"Пошаговая настройка:\n{guide}\n\n"
                "Отключить доступ: /health disconnect",
                reply_markup=_health_keyboard(url, True),
                disable_web_page_preview=True,
            )
            return
        if args == ["disconnect"]:
            await _call(store.disconnect, user_id)
            text = "Доступ Apple Health отключён. История переноса сохранена; записи в «Здоровье» остаются."
        elif len(args) == 2 and args[0] in {"saved", "retry"}:
            await _call(store.recover, user_id, args[0], args[1])
            text = "Состояние переноса обновлено. Можно запустить команду на iPhone."
        elif len(args) == 3 and args[0] == "corrected":
            await _call(store.corrected, user_id, args[1], args[2])
            text = "Ручное исправление отмечено. Отправьте /health для проверки остальных записей."
        elif not args:
            status = await _call(store.status, user_id)
            text = (
                "Apple Health: "
                + ("подключено" if status["connected"] else "не подключено")
                + "\n\nПереносите калории, БЖУ и вес из бота в «Здоровье». "
                "Для этого один раз настройте шорткат в приложении «Команды» на iPhone, "
                "а затем запускайте его для переноса новых записей.\n\n"
                + (
                    "Откройте пошаговую инструкцию кнопкой ниже.\n"
                    if url and status["connected"]
                    else "Начните с кнопки «Подключить Apple Health», затем откройте инструкцию ниже.\n"
                    if url
                    else "Подключение пока недоступно: администратору нужно настроить сервер экспорта.\n"
                )
                + "\nПодключить или заменить ключ: /health connect\n"
                "Перенести историю: /health connect ГГГГ-ММ-ДД\nОтключить: /health disconnect"
            )
            if "confirmed" in status:
                text += f"\n\nПодтверждено показателей: {status['confirmed']}. Новых: {status['remaining']}."
            if status.get("status") == "review":
                receipt = status["receipt"]
                text += (
                    "\n\nНезавершённый перенос:\n"
                    + _health_sample_text(status["sample"])
                    + "\nОстановите команду на iPhone и проверьте этот показатель в «Здоровье». "
                    "Если запись уже есть, отправьте:\n"
                    + f"/health saved {receipt}"
                    + "\nТолько если записи нет:\n"
                    + f"/health retry {receipt}"
                )
            for issue in status.get("issues", [])[:3]:
                text += (
                    f"\n\nИзменение {issue['id']}:\nБыло: "
                    + _health_sample_text(issue["previous"])
                    + "\nСтало: "
                    + _health_sample_text(issue["current"])
                    + "\nИсправьте или удалите старую запись в «Здоровье», затем подтвердите:\n"
                    + f"/health corrected {issue['id']} {issue['revision']}"
                )
            if status.get("issue_count", 0) > 3:
                text += f"\nВсего изменений: {status['issue_count']}. После исправления отправьте /health снова."
            if guide:
                text += f"\n\nИнструкция: {guide}"
        else:
            text = "Неизвестная команда. Отправьте /health для инструкции."
    except (ValidationError, StateConflict) as exc:
        text = str(exc)
    status = await _call(store.status, user_id)
    await update.effective_message.reply_text(
        text,
        reply_markup=_health_keyboard(url, status["connected"]),
        disable_web_page_preview=True,
    )


async def miniapp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_private(update):
        return
    url = context.application.bot_data.get("miniapp_url")
    if not url:
        await update.effective_message.reply_text("Mini App пока не настроен.")
        return
    await update.effective_message.reply_text(
        "Ваш дневник питания",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Открыть дневник", web_app=WebAppInfo(url=url))]]
        ),
    )


def build_application(
    settings: Settings, database: Optional[Database] = None
) -> Application:
    store = database or Database(settings.database_path)
    store.initialize()
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .connect_timeout(10.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(10.0)
        .concurrent_updates(False)
        .build()
    )
    application.bot_data["database"] = store
    application.bot_data["miniapp_url"] = settings.miniapp_url
    new_messages = filters.UpdateType.MESSAGE
    application.add_handler(
        CommandHandler("health", health_command, filters=new_messages)
    )
    application.add_handler(
        CommandHandler("app", miniapp_command, filters=new_messages)
    )
    application.add_handler(CommandHandler("start", start, filters=new_messages))
    application.add_handler(CommandHandler("cancel", cancel, filters=new_messages))
    application.add_handler(
        CommandHandler("updatetimezone", update_timezone, filters=new_messages)
    )
    application.add_handler(CommandHandler("add", add_command, filters=new_messages))
    application.add_handler(
        CommandHandler("weight", weight_command, filters=new_messages)
    )
    application.add_handler(
        CallbackQueryHandler(health_callback, pattern=r"^health:(connect|status)$")
    )
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(
        MessageHandler(new_messages & filters.TEXT & ~filters.COMMAND, handle_text)
    )
    application.add_handler(
        MessageHandler(new_messages & filters.COMMAND, unknown_command)
    )
    application.add_handler(
        MessageHandler(new_messages & ~filters.TEXT, handle_non_text)
    )
    application.add_error_handler(handle_error)
    return application
