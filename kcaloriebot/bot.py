from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, TypeVar

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from .domain import (
    NotFound,
    Period,
    Session,
    SessionState,
    StateConflict,
    ValidationError,
    canonical_timezone,
    normalize_food_name,
    normalize_search_query,
    parse_calories,
    parse_daily_goal,
    parse_entry_time,
    parse_grams,
    parse_macro,
    parse_quick_add,
    period_bounds,
    validate_macro_sum,
)
from .render import (
    CANCEL_KEYBOARD,
    ENTRY_GRAMS_PROMPT,
    ENTRY_TIME_PROMPT,
    FAVORITE_DECISION_KEYBOARD,
    FAVORITE_MATCH_GRAMS_PROMPT,
    FAVORITE_SEARCH_PROMPT,
    FOOD_NAME_PROMPT,
    GOAL_KEYBOARD,
    GOAL_PROMPT,
    MAIN_KEYBOARD,
    MANUAL_ENTRY_KEYBOARD,
    Markup,
    QUICK_ADD_USAGE,
    REPEAT_KEYBOARD,
    SKIP_KEYBOARD,
    STATS_KEYBOARD,
    TIMEZONE_CHANGE_PROMPT,
    TIMEZONE_ONBOARDING_PROMPT,
    TIMEZONE_REQUIRED_MARKUP,
    TIMEZONE_SETUP_REQUIRED_PROMPT,
    day_stats_block,
    entry_button_text,
    entry_details,
    favorite_button_text,
    favorite_details,
    navigation_row,
    session_prompt,
    stat_macro_line,
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
        CANCEL_KEYBOARD,
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
    progress = await _today_progress(turn.database, turn.user_id)
    await turn.message.reply_text(
        f"Added {entry.name}: {entry.nutrition.calories:.2f} kcal, "
        f"{entry.nutrition.grams:.2f}g. {progress}",
        reply_markup=MAIN_KEYBOARD,
    )
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

    if text == "Add Food":
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_FOOD_NAME,
            turn.message,
            FOOD_NAME_PROMPT,
            SKIP_KEYBOARD,
        )
    elif text == "Food Today":
        await _show_entries(update, context, 0)
    elif text == "Statistics":
        await turn.message.reply_text(
            "Select a statistics period:", reply_markup=STATS_KEYBOARD
        )
    elif text == "Today Stats":
        await _show_stats(update, context, Period.TODAY)
    elif text == "Yesterday Stats":
        await _show_stats(update, context, Period.YESTERDAY)
    elif text == "Week Stats":
        await _show_daily_stats(update, context, Period.WEEK, 0)
    elif text == "Month Stats":
        await _show_daily_stats(update, context, Period.MONTH, 0)
    elif text == "Search Favorites":
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_FAVORITE_SEARCH,
            turn.message,
            FAVORITE_SEARCH_PROMPT,
            CANCEL_KEYBOARD,
        )
    elif text == "My Favorites":
        await _show_favorites(update, context, 0)
    elif text == "Recent Foods":
        await _show_recent(update, context)
    elif text == "Daily Goal":
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
    elif text == "Update Timezone":
        await _start_with_prompt(
            database,
            turn.user_id,
            turn.chat_id,
            SessionState.WAIT_TIMEZONE,
            turn.message,
            TIMEZONE_CHANGE_PROMPT,
            CANCEL_KEYBOARD,
        )
    elif text == "Back":
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
                f"Timezone set to {timezone_name}. Local-day tracking is ready.",
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
                    f"({favorite.calories_per_100g:.2f} kcal/100g). "
                    f"{FAVORITE_MATCH_GRAMS_PROMPT}",
                    reply_markup=MANUAL_ENTRY_KEYBOARD,
                )
                await _mark_prompt_delivered(database, advanced)
        elif session.state == SessionState.WAIT_CALORIES:
            calories = parse_calories(text)
            await _advance_with_prompt(
                database,
                session,
                SessionState.WAIT_GRAMS,
                message,
                message_id,
                calories_per_100g=calories,
            )
        elif session.state == SessionState.WAIT_GRAMS:
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
            protein = None if text == "Skip" else parse_macro(text, "Protein")
            validate_macro_sum(protein, session.fat_per_100g, session.carbs_per_100g)
            await _advance_with_prompt(
                database,
                session,
                SessionState.WAIT_FAT,
                message,
                message_id,
                protein_per_100g=protein,
            )
        elif session.state == SessionState.WAIT_FAT:
            fat = None if text == "Skip" else parse_macro(text, "Fat")
            validate_macro_sum(session.protein_per_100g, fat, session.carbs_per_100g)
            await _advance_with_prompt(
                database,
                session,
                SessionState.WAIT_CARBS,
                message,
                message_id,
                fat_per_100g=fat,
            )
        elif session.state == SessionState.WAIT_CARBS:
            carbs = None if text == "Skip" else parse_macro(text, "Carbs")
            validate_macro_sum(session.protein_per_100g, session.fat_per_100g, carbs)
            completed = replace(
                session, carbs_per_100g=carbs, last_message_id=message_id
            )
            await _call(database.complete_food_draft, completed, event_epoch)
            progress = await _today_progress(database, session.user_id)
            if session.draft_name is None:
                await message.reply_text(
                    f"Food entry added. {progress}", reply_markup=MAIN_KEYBOARD
                )
            else:
                await message.reply_text(
                    f"Food entry added. {progress}\nSave this product as a favorite?",
                    reply_markup=FAVORITE_DECISION_KEYBOARD,
                )
                pending = await _call(
                    database.get_session, session.user_id, session.chat_id
                )
                if pending is not None:
                    await _mark_prompt_delivered(database, pending)
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
                    "No matching favorite foods found.", reply_markup=MAIN_KEYBOARD
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
                )
                label = (
                    "Select a favorite product (first 20 matches):"
                    if len(matches) > 20
                    else "Select a favorite product:"
                )
                await message.reply_text(label, reply_markup=keyboard)
                await message.reply_text(
                    "Select an option:", reply_markup=MAIN_KEYBOARD
                )
            await _call(database.clear_session, session.user_id, session.chat_id)
        elif session.state == SessionState.WAIT_FAVORITE_GRAMS:
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
                grams = parse_grams(text)
                await _call(
                    database.use_selected_favorite,
                    session.user_id,
                    session.chat_id,
                    grams,
                    event_epoch,
                )
                progress = await _today_progress(database, session.user_id)
                await message.reply_text(
                    f"Food entry added. {progress}", reply_markup=MAIN_KEYBOARD
                )
        elif session.state == SessionState.WAIT_RECENT_GRAMS:
            grams = None if text == "Same as last time" else parse_grams(text)
            await _call(
                database.use_selected_entry,
                session.user_id,
                session.chat_id,
                grams,
                event_epoch,
            )
            progress = await _today_progress(database, session.user_id)
            await message.reply_text(
                f"Food entry added. {progress}", reply_markup=MAIN_KEYBOARD
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
            grams = parse_grams(text)
            entry = await _call(
                database.update_entry_grams,
                session.user_id,
                session.chat_id,
                grams,
                now,
            )
            await message.reply_text(
                f"Serving weight updated to {entry.nutrition.grams:.2f}g "
                f"({entry.nutrition.calories:.2f} kcal).",
                reply_markup=MAIN_KEYBOARD,
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
            if session.selected_nutrient == "calories":
                value = parse_calories(text)
            elif session.selected_nutrient in {"protein", "fat", "carbs"}:
                value = parse_macro(text, session.selected_nutrient.title())
            else:
                raise StateConflict("Favorite amendment context is incomplete")
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


async def _show_stats(
    update: Update, context: ContextTypes.DEFAULT_TYPE, period: Period
) -> None:
    message = update.effective_message
    if message is None:
        return
    user_id, _ = _identity(update)
    database = _db(context)
    timezone_name = await _call(database.get_timezone, user_id)
    if timezone_name is None:
        await message.reply_text("Set your timezone first with /updatetimezone.")
        return
    bounds = period_bounds(period, timezone_name)
    stats = await _call(database.stats, user_id, bounds.start_utc, bounds.end_utc)
    if stats.entry_count == 0:
        await message.reply_text(f"No food entries found for {period.value}.")
        return
    title = {
        Period.TODAY: "Today's totals",
        Period.YESTERDAY: "Yesterday's totals",
    }[period]
    goal = (
        await _call(database.get_daily_goal, user_id)
        if period == Period.TODAY
        else None
    )
    calories_line = f"Calories: {stats.calories:.2f}"
    if goal is not None:
        remaining = goal - stats.calories
        calories_line += (
            f" / {goal:.0f} goal ({remaining:.0f} left)"
            if remaining >= 0
            else f" / {goal:.0f} goal ({-remaining:.0f} over)"
        )
    await message.reply_text(
        f"{title}:\n"
        f"{calories_line}\n"
        f"{stat_macro_line('Protein', stats.protein, stats.protein_coverage, stats.coverage_total, 'entries')}\n"
        f"{stat_macro_line('Fat', stats.fat, stats.fat_coverage, stats.coverage_total, 'entries')}\n"
        f"{stat_macro_line('Carbs', stats.carbs, stats.carbs_coverage, stats.coverage_total, 'entries')}"
    )


async def _show_daily_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    period: Period,
    offset: int,
    edit: bool = False,
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
    bounds = period_bounds(period, timezone_name)
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
    if not page.items:
        text, keyboard = f"No food entries found for {period.value}.", None
    else:
        title = {
            Period.WEEK: "Last 7 days, logged days",
            Period.MONTH: "This month, logged days",
        }[period]
        blocks = "\n\n".join(day_stats_block(day) for day in page.items)
        text = f"{title}:\n\n{blocks}"
        navigation = navigation_row(page, f"stats:{period.value}", STATS_PAGE_SIZE)
        keyboard = InlineKeyboardMarkup([navigation]) if navigation else None
    if edit and update.callback_query is not None:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def _show_entries(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    offset: int,
    edit: bool = False,
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
    bounds = period_bounds(Period.TODAY, timezone_name)
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
    if not page.items:
        text, keyboard = "No food entries found for today.", None
    else:
        rows = [
            [
                InlineKeyboardButton(
                    entry_button_text(entry),
                    callback_data=f"entry:view:{entry.entry_id}:{page.offset}",
                )
            ]
            for entry in page.items
        ]
        navigation = navigation_row(page, "entry:list")
        if navigation:
            rows.append(navigation)
        text, keyboard = "Today's food entries:", InlineKeyboardMarkup(rows)
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
        text, keyboard = "No favorite foods found.", None
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
            "No recent foods yet. Log a food first.", reply_markup=MAIN_KEYBOARD
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
                SessionState.WAIT_FAVORITE_AMENDMENT,
                SessionState.WAIT_RECENT_GRAMS,
                SessionState.WAIT_ENTRY_GRAMS,
                SessionState.WAIT_ENTRY_TIME,
                SessionState.WAIT_GOAL,
            }:
                await query.edit_message_text("This prompt has expired.")
            else:
                await _call(database.clear_session, user_id, chat_id)
                await query.edit_message_text("Cancelled.")
                await query.message.reply_text(
                    "Select an option:", reply_markup=MAIN_KEYBOARD
                )
        elif action.kind == "dismiss":
            await query.edit_message_text("No changes made.")
        elif action.kind == "entry_list":
            await _show_entries(update, context, action.offset, edit=True)
        elif action.kind == "stats_page" and action.period is not None:
            await _show_daily_stats(
                update, context, Period(action.period), action.offset, edit=True
            )
        elif action.kind == "favorite_list":
            await _show_favorites(update, context, action.offset, edit=True)
        elif action.kind == "entry_view" and action.record_id is not None:
            entry = await _call(database.get_entry, user_id, action.record_id)
            if entry is None:
                raise NotFound("Food entry not found")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Edit Grams",
                            callback_data=f"entry:grams:{entry.entry_id}",
                        ),
                        InlineKeyboardButton(
                            "Edit Time", callback_data=f"entry:time:{entry.entry_id}"
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "Delete", callback_data=f"entry:delete:{entry.entry_id}"
                        ),
                        InlineKeyboardButton(
                            "Back", callback_data=f"entry:list:{action.offset}"
                        ),
                    ],
                ]
            )
            await query.edit_message_text(entry_details(entry), reply_markup=keyboard)
        elif action.kind == "entry_grams" and action.record_id is not None:
            entry = await _call(database.get_entry, user_id, action.record_id)
            if entry is None:
                raise NotFound("Food entry not found")
            started = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_ENTRY_GRAMS,
                selected_entry_id=entry.entry_id,
                prompt_pending=True,
            )
            await query.edit_message_text(
                f"Editing serving weight for {entry.name or 'Unnamed food'} "
                f"(currently {entry.nutrition.grams:.2f}g)."
            )
            await query.message.reply_text(
                ENTRY_GRAMS_PROMPT, reply_markup=CANCEL_KEYBOARD
            )
            await _mark_prompt_delivered(database, started)
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
            started = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_RECENT_GRAMS,
                selected_entry_id=entry.entry_id,
                prompt_pending=True,
            )
            name = entry.name or "Unnamed food"
            await query.edit_message_text(f"Selected: {name}")
            await query.message.reply_text(
                f"Enter the serving weight in grams for {name}, or choose "
                f"Same as last time ({entry.nutrition.grams:.0f}g):",
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
                            callback_data=(
                                f"entry:delete-confirm:{entry.entry_id}:"
                                f"{database.now_epoch()}"
                            ),
                        ),
                        InlineKeyboardButton("Keep", callback_data="dismiss"),
                    ]
                ]
            )
            await query.edit_message_text(
                "Delete this food entry?", reply_markup=keyboard
            )
        elif action.kind == "entry_delete_confirm" and action.record_id is not None:
            if confirmation_expired(action, database.now_epoch()):
                await query.edit_message_text("This delete confirmation has expired.")
                return
            await _call(database.delete_entry, user_id, action.record_id)
            await query.edit_message_text(
                "Food entry deleted.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Back to list", callback_data="entry:list:0"
                            )
                        ]
                    ]
                ),
            )
        elif action.kind == "favorite_view" and action.record_id is not None:
            favorite = await _call(database.get_favorite, user_id, action.record_id)
            if favorite is None:
                raise NotFound("Favorite not found")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Use", callback_data=f"fav:use:{favorite.favorite_id}"
                        ),
                        InlineKeyboardButton(
                            "Amend", callback_data=f"fav:edit:{favorite.favorite_id}"
                        ),
                        InlineKeyboardButton(
                            "Delete", callback_data=f"fav:delete:{favorite.favorite_id}"
                        ),
                        InlineKeyboardButton(
                            "Back", callback_data=f"fav:list:{action.offset}"
                        ),
                    ]
                ]
            )
            await query.edit_message_text(
                favorite_details(favorite), reply_markup=keyboard
            )
        elif action.kind == "favorite_use" and action.record_id is not None:
            favorite = await _call(database.get_favorite, user_id, action.record_id)
            if favorite is None:
                raise NotFound("Favorite not found")
            started = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_FAVORITE_GRAMS,
                selected_favorite_id=favorite.favorite_id,
                prompt_pending=True,
            )
            await query.edit_message_text(f"Selected favorite: {favorite.name}")
            await query.message.reply_text(
                f"Enter the serving weight in grams for {favorite.name}:",
                reply_markup=CANCEL_KEYBOARD,
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
                            callback_data=f"fav:field:{favorite.favorite_id}:calories",
                        ),
                        InlineKeyboardButton(
                            "Protein",
                            callback_data=f"fav:field:{favorite.favorite_id}:protein",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "Fat", callback_data=f"fav:field:{favorite.favorite_id}:fat"
                        ),
                        InlineKeyboardButton(
                            "Carbs",
                            callback_data=f"fav:field:{favorite.favorite_id}:carbs",
                        ),
                    ],
                    [InlineKeyboardButton("Close", callback_data="dismiss")],
                ]
            )
            await query.edit_message_text(
                "Choose a value to amend:", reply_markup=keyboard
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
                selected_nutrient=action.nutrient,
                prompt_pending=True,
            )
            await query.edit_message_text(
                f"Editing {action.nutrient} for {favorite.name}."
            )
            await query.message.reply_text(
                f"Enter the new {action.nutrient} value per 100g:",
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
                            callback_data=(
                                f"fav:delete-confirm:{favorite.favorite_id}:"
                                f"{database.now_epoch()}"
                            ),
                        ),
                        InlineKeyboardButton("Keep", callback_data="dismiss"),
                    ]
                ]
            )
            await query.edit_message_text(
                f"Delete favorite {favorite.name}?", reply_markup=keyboard
            )
        elif action.kind == "favorite_delete_confirm" and action.record_id is not None:
            if confirmation_expired(action, database.now_epoch()):
                await query.edit_message_text("This delete confirmation has expired.")
                return
            await _call(database.delete_favorite, user_id, action.record_id)
            await query.edit_message_text(
                "Favorite product deleted.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Back to list", callback_data="fav:list:0")]]
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
    new_messages = filters.UpdateType.MESSAGE
    application.add_handler(CommandHandler("start", start, filters=new_messages))
    application.add_handler(CommandHandler("cancel", cancel, filters=new_messages))
    application.add_handler(
        CommandHandler("updatetimezone", update_timezone, filters=new_messages)
    )
    application.add_handler(CommandHandler("add", add_command, filters=new_messages))
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
