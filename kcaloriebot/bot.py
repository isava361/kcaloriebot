from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, TypeVar

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
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

from .config import Settings
from .database import Database
from .domain import (
    FavoriteFood,
    FoodEntry,
    NotFound,
    Page,
    Period,
    Session,
    SessionState,
    StateConflict,
    ValidationError,
    canonical_timezone,
    normalize_food_name,
    normalize_search_query,
    parse_calories,
    parse_grams,
    parse_macro,
    period_bounds,
    validate_macro_sum,
)


logger = logging.getLogger(__name__)
T = TypeVar("T")
PAGE_SIZE = 5
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Add Food"), KeyboardButton("Food Today")],
        [
            KeyboardButton("Statistics"),
            KeyboardButton("Search Favorites"),
            KeyboardButton("My Favorites"),
        ],
        [KeyboardButton("Update Timezone")],
    ],
    resize_keyboard=True,
)

STATS_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Today Stats"), KeyboardButton("Yesterday Stats")],
        [KeyboardButton("Week Stats"), KeyboardButton("Month Stats")],
        [KeyboardButton("Back")],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Cancel")]], resize_keyboard=True
)

SKIP_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Skip"), KeyboardButton("Cancel")]], resize_keyboard=True
)

FAVORITE_DECISION_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Yes"), KeyboardButton("No")]],
    resize_keyboard=True,
)

TIMEZONE_REQUIRED_MARKUP = ReplyKeyboardRemove()


@dataclass(frozen=True)
class CallbackAction:
    kind: str
    record_id: Optional[int] = None
    offset: int = 0
    nutrient: Optional[str] = None
    issued_at: Optional[int] = None


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_private(update) or update.effective_message is None:
        return
    user_id, chat_id = _identity(update)
    database = _db(context)
    await _call(database.ensure_user, user_id)
    session = await _call(database.get_session, user_id, chat_id)
    if session is not None and _session_expired(session, database.now_epoch()):
        await _call(database.clear_session, user_id, chat_id)
        session = None
    timezone_name = await _call(database.get_timezone, user_id)
    if session is not None:
        if timezone_name is None and session.state != SessionState.WAIT_TIMEZONE:
            session = await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_TIMEZONE,
                prompt_pending=True,
            )
        else:
            refreshed = replace(session, updated_at_utc=database.now_epoch())
            session = await _call(database.update_session, session, refreshed)
        prompt, markup = _session_prompt(session, timezone_name is not None)
        await update.effective_message.reply_text(prompt, reply_markup=markup)
        if session.prompt_pending:
            await _mark_prompt_delivered(database, session)
        return
    if timezone_name is None:
        await _start_with_prompt(
            database,
            user_id,
            chat_id,
            SessionState.WAIT_TIMEZONE,
            update.effective_message,
            "Enter your IANA timezone or city, for example Europe/Moscow or New York:",
            TIMEZONE_REQUIRED_MARKUP,
        )
        return
    await update.effective_message.reply_text(
        "Welcome to the Calorie Calculator Bot.", reply_markup=MAIN_KEYBOARD
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_private(update) or update.effective_message is None:
        return
    user_id, chat_id = _identity(update)
    database = _db(context)
    timezone_name = await _call(database.get_timezone, user_id)
    if timezone_name is None:
        await _start_with_prompt(
            database,
            user_id,
            chat_id,
            SessionState.WAIT_TIMEZONE,
            update.effective_message,
            "Timezone setup is required. Enter an IANA timezone such as Europe/Moscow.",
            TIMEZONE_REQUIRED_MARKUP,
        )
        return
    active = await _call(database.get_session, user_id, chat_id)
    await _call(database.clear_session, user_id, chat_id)
    text = (
        "Favorite was not saved. The food entry remains in your diary."
        if active is not None and active.state == SessionState.WAIT_SAVE_FAVORITE
        else "Cancelled."
    )
    await update.effective_message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def update_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_private(update) or update.effective_message is None:
        return
    user_id, chat_id = _identity(update)
    database = _db(context)
    await _call(database.ensure_user, user_id)
    active = await _call(database.get_session, user_id, chat_id)
    if active is not None and active.state != SessionState.WAIT_TIMEZONE:
        prompt, markup = _session_prompt(
            active, await _call(database.get_timezone, user_id) is not None
        )
        await update.effective_message.reply_text(
            "Finish or cancel the current input before changing timezone.\n\n" + prompt,
            reply_markup=markup,
        )
        await _mark_prompt_delivered(database, active)
        return
    await _start_with_prompt(
        database,
        user_id,
        chat_id,
        SessionState.WAIT_TIMEZONE,
        update.effective_message,
        "Enter your new IANA timezone or city. Changing it also changes how existing "
        "entries near midnight are grouped into calendar days:",
        CANCEL_KEYBOARD,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_private(update) or update.effective_message is None:
        return
    message = update.effective_message
    if message.text is None:
        return
    text = message.text.strip()
    user_id, chat_id = _identity(update)
    database = _db(context)
    await _call(database.ensure_user, user_id)

    if text == "Cancel":
        active = await _call(database.get_session, user_id, chat_id)
        if active is not None and active.state == SessionState.WAIT_SAVE_FAVORITE:
            await _call(database.clear_session, user_id, chat_id)
            await message.reply_text(
                "Favorite was not saved. The food entry remains in your diary.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        await cancel(update, context)
        return

    session = await _call(database.get_session, user_id, chat_id)
    timezone_name = await _call(database.get_timezone, user_id)
    if session is not None and _session_expired(session, database.now_epoch()):
        await _call(database.clear_session, user_id, chat_id)
        if timezone_name is None:
            await _start_with_prompt(
                database,
                user_id,
                chat_id,
                SessionState.WAIT_TIMEZONE,
                message,
                "The previous input expired. Enter your timezone to continue:",
                TIMEZONE_REQUIRED_MARKUP,
            )
        else:
            await message.reply_text(
                "The previous input expired. Choose an option again.",
                reply_markup=MAIN_KEYBOARD,
            )
        return
    message_id = getattr(message, "message_id", None)
    if session is not None and (
        session.prompt_pending
        or (message_id is not None and message_id == session.last_message_id)
    ):
        prompt, markup = _session_prompt(session, timezone_name is not None)
        prefix = (
            "The previous prompt was not confirmed."
            if session.prompt_pending
            else "That message was already processed."
        )
        await message.reply_text(f"{prefix}\n\n{prompt}", reply_markup=markup)
        if session.prompt_pending:
            await _mark_prompt_delivered(database, session)
        return
    if timezone_name is None and (
        session is None or session.state != SessionState.WAIT_TIMEZONE
    ):
        await _start_with_prompt(
            database,
            user_id,
            chat_id,
            SessionState.WAIT_TIMEZONE,
            message,
            "Enter your IANA timezone or city, for example Europe/Moscow or New York:",
            TIMEZONE_REQUIRED_MARKUP,
        )
        return

    if session is not None:
        await _handle_session_text(update, context, session, text)
        return

    if text == "Add Food":
        await _start_with_prompt(
            database,
            user_id,
            chat_id,
            SessionState.WAIT_FOOD_NAME,
            message,
            "Enter the food name, or choose Skip:",
            SKIP_KEYBOARD,
        )
    elif text == "Food Today":
        await _show_entries(update, context, 0)
    elif text == "Statistics":
        await message.reply_text(
            "Select a statistics period:", reply_markup=STATS_KEYBOARD
        )
    elif text == "Today Stats":
        await _show_stats(update, context, Period.TODAY)
    elif text == "Yesterday Stats":
        await _show_stats(update, context, Period.YESTERDAY)
    elif text == "Week Stats":
        await _show_stats(update, context, Period.WEEK)
    elif text == "Month Stats":
        await _show_stats(update, context, Period.MONTH)
    elif text == "Search Favorites":
        await _start_with_prompt(
            database,
            user_id,
            chat_id,
            SessionState.WAIT_FAVORITE_SEARCH,
            message,
            "Enter a favorite food name to search:",
            CANCEL_KEYBOARD,
        )
    elif text == "My Favorites":
        await _show_favorites(update, context, 0)
    elif text == "Update Timezone":
        await _start_with_prompt(
            database,
            user_id,
            chat_id,
            SessionState.WAIT_TIMEZONE,
            message,
            "Enter your new IANA timezone or city. Changing it also changes how existing "
            "entries near midnight are grouped into calendar days:",
            CANCEL_KEYBOARD,
        )
    elif text == "Back":
        await message.reply_text("Select an option:", reply_markup=MAIN_KEYBOARD)
    else:
        await message.reply_text(
            "Choose an option from the keyboard.", reply_markup=MAIN_KEYBOARD
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
            await _advance_with_prompt(
                database,
                session,
                SessionState.WAIT_CALORIES,
                message,
                "Enter calories per 100g:",
                CANCEL_KEYBOARD,
                message_id,
                draft_name=name,
            )
        elif session.state == SessionState.WAIT_CALORIES:
            calories = parse_calories(text)
            await _advance_with_prompt(
                database,
                session,
                SessionState.WAIT_GRAMS,
                message,
                "Enter the serving weight in grams:",
                CANCEL_KEYBOARD,
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
                "Enter protein per 100g, or choose Skip:",
                SKIP_KEYBOARD,
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
                "Enter fat per 100g, or choose Skip:",
                SKIP_KEYBOARD,
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
                "Enter carbs per 100g, or choose Skip:",
                SKIP_KEYBOARD,
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
            if session.draft_name is None:
                await message.reply_text(
                    "Food entry added.", reply_markup=MAIN_KEYBOARD
                )
            else:
                await message.reply_text(
                    "Food entry added. Save this product as a favorite?",
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
                                _favorite_button_text(favorite),
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
            grams = parse_grams(text)
            await _call(
                database.use_selected_favorite,
                session.user_id,
                session.chat_id,
                grams,
                event_epoch,
            )
            await message.reply_text("Food entry added.", reply_markup=MAIN_KEYBOARD)
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
    prompt: str,
    markup: ReplyKeyboardMarkup | ReplyKeyboardRemove,
    message_id: Optional[int],
    **changes: object,
) -> Session:
    advanced = await _transition(
        database, session, state, message_id=message_id, **changes
    )
    await message.reply_text(prompt, reply_markup=markup)
    return await _mark_prompt_delivered(database, advanced)


async def _start_with_prompt(
    database: Database,
    user_id: int,
    chat_id: int,
    state: SessionState,
    message: Any,
    prompt: str,
    markup: ReplyKeyboardMarkup | ReplyKeyboardRemove,
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


def _session_prompt(
    session: Session, has_timezone: bool
) -> tuple[str, ReplyKeyboardMarkup | ReplyKeyboardRemove]:
    prompts: dict[
        SessionState, tuple[str, ReplyKeyboardMarkup | ReplyKeyboardRemove]
    ] = {
        SessionState.WAIT_TIMEZONE: (
            "Enter your new IANA timezone or city:"
            if has_timezone
            else "Enter your IANA timezone or city, for example Europe/Moscow:",
            CANCEL_KEYBOARD if has_timezone else TIMEZONE_REQUIRED_MARKUP,
        ),
        SessionState.WAIT_FOOD_NAME: (
            "Enter the food name, or choose Skip:",
            SKIP_KEYBOARD,
        ),
        SessionState.WAIT_CALORIES: (
            "Enter calories per 100g:",
            CANCEL_KEYBOARD,
        ),
        SessionState.WAIT_GRAMS: (
            "Enter the serving weight in grams:",
            CANCEL_KEYBOARD,
        ),
        SessionState.WAIT_PROTEIN: (
            "Enter protein per 100g, or choose Skip:",
            SKIP_KEYBOARD,
        ),
        SessionState.WAIT_FAT: (
            "Enter fat per 100g, or choose Skip:",
            SKIP_KEYBOARD,
        ),
        SessionState.WAIT_CARBS: (
            "Enter carbs per 100g, or choose Skip:",
            SKIP_KEYBOARD,
        ),
        SessionState.WAIT_SAVE_FAVORITE: (
            "The food entry is already saved. Save this product as a favorite?",
            FAVORITE_DECISION_KEYBOARD,
        ),
        SessionState.WAIT_FAVORITE_SEARCH: (
            "Enter a favorite food name to search:",
            CANCEL_KEYBOARD,
        ),
        SessionState.WAIT_FAVORITE_GRAMS: (
            "Enter the serving weight in grams for the selected favorite:",
            CANCEL_KEYBOARD,
        ),
        SessionState.WAIT_FAVORITE_AMENDMENT: (
            f"Enter the new {session.selected_nutrient or 'nutrient'} value per 100g:",
            CANCEL_KEYBOARD,
        ),
    }
    return prompts[session.state]


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
    average = period in {Period.WEEK, Period.MONTH}
    stats = await _call(
        database.stats,
        user_id,
        bounds.start_utc,
        bounds.end_utc,
        timezone_name,
        average,
    )
    if stats.entry_count == 0:
        await message.reply_text(f"No food entries found for {period.value}.")
        return
    title = {
        Period.TODAY: "Today's totals",
        Period.YESTERDAY: "Yesterday's totals",
        Period.WEEK: "7-day logged-day average",
        Period.MONTH: "Month-to-date logged-day average",
    }[period]
    coverage_unit = "days" if average else "entries"
    await message.reply_text(
        f"{title}:\n"
        f"Calories: {stats.calories:.2f}\n"
        f"{_stat_macro_line('Protein', stats.protein, stats.protein_coverage, stats.coverage_total, coverage_unit)}\n"
        f"{_stat_macro_line('Fat', stats.fat, stats.fat_coverage, stats.coverage_total, coverage_unit)}\n"
        f"{_stat_macro_line('Carbs', stats.carbs, stats.carbs_coverage, stats.coverage_total, coverage_unit)}"
    )


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
                    _entry_button_text(entry),
                    callback_data=f"entry:view:{entry.entry_id}:{page.offset}",
                )
            ]
            for entry in page.items
        ]
        navigation = _navigation_row(page, "entry:list")
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
                    _favorite_button_text(favorite),
                    callback_data=f"fav:view:{favorite.favorite_id}:{page.offset}",
                )
            ]
            for favorite in page.items
        ]
        navigation = _navigation_row(page, "fav:list")
        if navigation:
            rows.append(navigation)
        text, keyboard = "Your favorite foods:", InlineKeyboardMarkup(rows)
    if edit and update.callback_query is not None:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=keyboard)


def _navigation_row(page: Page[Any], prefix: str) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if page.has_previous:
        row.append(
            InlineKeyboardButton(
                "Previous", callback_data=f"{prefix}:{max(0, page.offset - PAGE_SIZE)}"
            )
        )
    if page.has_next:
        row.append(
            InlineKeyboardButton(
                "Next", callback_data=f"{prefix}:{page.offset + PAGE_SIZE}"
            )
        )
    return row


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
    active_session = await _call(database.get_session, user_id, chat_id)
    if active_session is not None and _session_expired(
        active_session, database.now_epoch()
    ):
        await _call(database.clear_session, user_id, chat_id)
        active_session = None
    if active_session is not None and action.kind not in {"cancel", "dismiss"}:
        has_timezone = await _call(database.get_timezone, user_id) is not None
        prompt, markup = _session_prompt(active_session, has_timezone)
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
                    "Enter your IANA timezone or city:",
                    TIMEZONE_REQUIRED_MARKUP,
                )
            elif active_session is None or active_session.state not in {
                SessionState.WAIT_FAVORITE_SEARCH,
                SessionState.WAIT_FAVORITE_GRAMS,
                SessionState.WAIT_FAVORITE_AMENDMENT,
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
                            "Delete", callback_data=f"entry:delete:{entry.entry_id}"
                        ),
                        InlineKeyboardButton(
                            "Back", callback_data=f"entry:list:{action.offset}"
                        ),
                    ]
                ]
            )
            await query.edit_message_text(_entry_details(entry), reply_markup=keyboard)
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
            if _confirmation_expired(action, database.now_epoch()):
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
                _favorite_details(favorite), reply_markup=keyboard
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
            if _confirmation_expired(action, database.now_epoch()):
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


def parse_callback(data: str) -> Optional[CallbackAction]:
    if data in {"cancel", "cancel_all"}:
        return CallbackAction("cancel")
    if data == "dismiss":
        return CallbackAction("dismiss")

    parts = data.split(":")
    try:
        if len(parts) == 3 and parts[:2] == ["entry", "list"]:
            return CallbackAction("entry_list", offset=_parse_offset(parts[2]))
        if len(parts) == 3 and parts[:2] == ["fav", "list"]:
            return CallbackAction("favorite_list", offset=_parse_offset(parts[2]))
        if len(parts) == 3 and parts[0] == "entry":
            kinds = {
                "view": "entry_view",
                "delete": "entry_delete",
                "delete-confirm": "entry_delete_confirm",
            }
            if parts[1] in kinds:
                return CallbackAction(kinds[parts[1]], record_id=_parse_id(parts[2]))
        if len(parts) == 4 and parts[:2] == ["entry", "view"]:
            return CallbackAction(
                "entry_view",
                record_id=_parse_id(parts[2]),
                offset=_parse_offset(parts[3]),
            )
        if len(parts) == 4 and parts[:2] == ["entry", "delete-confirm"]:
            return CallbackAction(
                "entry_delete_confirm",
                record_id=_parse_id(parts[2]),
                issued_at=_parse_timestamp(parts[3]),
            )
        if len(parts) == 3 and parts[0] == "fav":
            kinds = {
                "view": "favorite_view",
                "use": "favorite_use",
                "edit": "favorite_edit",
                "delete": "favorite_delete",
                "delete-confirm": "favorite_delete_confirm",
            }
            if parts[1] in kinds:
                return CallbackAction(kinds[parts[1]], record_id=_parse_id(parts[2]))
        if len(parts) == 4 and parts[:2] == ["fav", "view"]:
            return CallbackAction(
                "favorite_view",
                record_id=_parse_id(parts[2]),
                offset=_parse_offset(parts[3]),
            )
        if len(parts) == 4 and parts[:2] == ["fav", "delete-confirm"]:
            return CallbackAction(
                "favorite_delete_confirm",
                record_id=_parse_id(parts[2]),
                issued_at=_parse_timestamp(parts[3]),
            )
        if len(parts) == 4 and parts[:2] == ["fav", "field"]:
            if parts[3] not in {"calories", "protein", "fat", "carbs"}:
                return None
            return CallbackAction(
                "favorite_field", record_id=_parse_id(parts[2]), nutrient=parts[3]
            )

        legacy_offset_prefixes = {
            "previous:": "entry_list",
            "next:": "entry_list",
            "previous_fav:": "favorite_list",
            "next_fav:": "favorite_list",
        }
        for prefix, kind in legacy_offset_prefixes.items():
            if data.startswith(prefix):
                return CallbackAction(kind, offset=_parse_offset(data[len(prefix) :]))

        legacy_id_prefixes = (
            ("entry_confirm_delete_", "entry_delete_confirm", None),
            ("entry_cancel_delete_", "dismiss", None),
            ("entry_delete_", "entry_delete", None),
            ("entry_choose_", "entry_view", None),
            ("fave_confirm_delete_", "favorite_delete_confirm", None),
            ("fave_cancel_delete_", "dismiss", None),
            ("favedelete_", "favorite_delete", None),
            ("fave_amend_", "favorite_edit", None),
            ("choose_favorite_", "favorite_view", None),
            ("calories_amend_", "favorite_field", "calories"),
            ("protein_amend_", "favorite_field", "protein"),
            ("fat_amend_", "favorite_field", "fat"),
            ("carbs_amend_", "favorite_field", "carbs"),
            ("favorite_", "favorite_use", None),
        )
        for prefix, kind, nutrient in legacy_id_prefixes:
            if data.startswith(prefix):
                return CallbackAction(
                    kind, record_id=_parse_id(data[len(prefix) :]), nutrient=nutrient
                )
    except ValueError:
        return None
    return None


def _parse_id(value: str) -> int:
    record_id = int(value)
    if record_id <= 0:
        raise ValueError("record ID must be positive")
    return record_id


def _parse_offset(value: str) -> int:
    offset = int(value)
    if offset < 0 or offset > 10_000 or offset % PAGE_SIZE != 0:
        raise ValueError("invalid page offset")
    return offset


def _parse_timestamp(value: str) -> int:
    timestamp = int(value)
    if timestamp <= 0:
        raise ValueError("invalid timestamp")
    return timestamp


def _confirmation_expired(action: CallbackAction, now_utc: int) -> bool:
    return action.issued_at is None or abs(now_utc - action.issued_at) > 15 * 60


def _short(value: str, limit: int = 42) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _optional_grams(value: Optional[float]) -> str:
    return "not set" if value is None else f"{value:.2f}g"


def _stat_macro_line(
    label: str,
    value: Optional[float],
    coverage: int,
    coverage_total: int,
    coverage_unit: str,
) -> str:
    rendered = f"{label}: {_optional_grams(value)}"
    if coverage < coverage_total:
        rendered += f" (partial: {coverage}/{coverage_total} {coverage_unit})"
    return rendered


def _entry_button_text(entry: FoodEntry) -> str:
    name = _short(entry.name or "Unnamed food", 30)
    return f"{name} - {entry.nutrition.calories:.2f} kcal, {entry.nutrition.grams:.2f}g"


def _favorite_button_text(favorite: FavoriteFood) -> str:
    return f"{_short(favorite.name, 30)} - {favorite.calories_per_100g:.2f} kcal/100g"


def _entry_details(entry: FoodEntry) -> str:
    return (
        f"{entry.name or 'Unnamed food'}\n"
        f"Calories: {entry.nutrition.calories:.2f}\n"
        f"Serving: {entry.nutrition.grams:.2f}g\n"
        f"Protein: {_optional_grams(entry.nutrition.protein)}\n"
        f"Fat: {_optional_grams(entry.nutrition.fat)}\n"
        f"Carbs: {_optional_grams(entry.nutrition.carbs)}"
    )


def _favorite_details(favorite: FavoriteFood) -> str:
    return (
        f"{favorite.name} per 100g\n"
        f"Calories: {favorite.calories_per_100g:.2f}\n"
        f"Protein: {_optional_grams(favorite.protein_per_100g)}\n"
        f"Fat: {_optional_grams(favorite.fat_per_100g)}\n"
        f"Carbs: {_optional_grams(favorite.carbs_per_100g)}"
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_private(update) or update.effective_message is None:
        return
    user_id, chat_id = _identity(update)
    database = _db(context)
    session = await _call(database.get_session, user_id, chat_id)
    if session is not None and _session_expired(session, database.now_epoch()):
        await _call(database.clear_session, user_id, chat_id)
        session = None
    if session is None:
        await update.effective_message.reply_text(
            "Unknown command. Use /start or choose an option from the keyboard.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    prompt, markup = _session_prompt(
        session, await _call(database.get_timezone, user_id) is not None
    )
    await update.effective_message.reply_text(
        f"Unknown command.\n\n{prompt}", reply_markup=markup
    )
    refreshed = replace(
        session,
        prompt_pending=False,
        updated_at_utc=database.now_epoch(),
    )
    await _call(database.update_session, session, refreshed)


async def handle_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_private(update) or update.effective_message is None:
        return
    user_id, chat_id = _identity(update)
    database = _db(context)
    session = await _call(database.get_session, user_id, chat_id)
    if session is not None and _session_expired(session, database.now_epoch()):
        await _call(database.clear_session, user_id, chat_id)
        session = None
    if session is not None:
        prompt, markup = _session_prompt(
            session, await _call(database.get_timezone, user_id) is not None
        )
        await update.effective_message.reply_text(
            f"Please send a text value.\n\n{prompt}", reply_markup=markup
        )
        refreshed = replace(
            session,
            prompt_pending=False,
            updated_at_utc=database.now_epoch(),
        )
        await _call(database.update_session, session, refreshed)
    else:
        await update.effective_message.reply_text(
            "Choose a text command from the keyboard.", reply_markup=MAIN_KEYBOARD
        )


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
