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


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Add Food"), KeyboardButton("Food Today")],
        [
            KeyboardButton("Statistics"),
            KeyboardButton("Search Favorites"),
            KeyboardButton("My Favorites"),
        ],
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
    [[KeyboardButton("Yes"), KeyboardButton("No"), KeyboardButton("Cancel")]],
    resize_keyboard=True,
)


@dataclass(frozen=True)
class CallbackAction:
    kind: str
    record_id: Optional[int] = None
    offset: int = 0
    nutrient: Optional[str] = None


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
    if chat is None or chat.type == ChatType.PRIVATE:
        return True
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
    await _call(database.clear_session, user_id, chat_id)
    timezone_name = await _call(database.get_timezone, user_id)
    if timezone_name is None:
        await _call(
            database.start_session, user_id, chat_id, SessionState.WAIT_TIMEZONE
        )
        await update.effective_message.reply_text(
            "Enter your IANA timezone or city, for example Europe/Moscow or New York:",
            reply_markup=CANCEL_KEYBOARD,
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
        await _call(
            database.start_session, user_id, chat_id, SessionState.WAIT_TIMEZONE
        )
        await update.effective_message.reply_text(
            "A timezone is required before food can be assigned to a local day.",
            reply_markup=CANCEL_KEYBOARD,
        )
        return
    await _call(database.clear_session, user_id, chat_id)
    await update.effective_message.reply_text("Cancelled.", reply_markup=MAIN_KEYBOARD)


async def update_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_private(update) or update.effective_message is None:
        return
    user_id, chat_id = _identity(update)
    database = _db(context)
    await _call(database.ensure_user, user_id)
    await _call(database.start_session, user_id, chat_id, SessionState.WAIT_TIMEZONE)
    await update.effective_message.reply_text(
        "Enter your new IANA timezone or city:", reply_markup=CANCEL_KEYBOARD
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
        await cancel(update, context)
        return

    session = await _call(database.get_session, user_id, chat_id)
    timezone_name = await _call(database.get_timezone, user_id)
    if timezone_name is None and (
        session is None or session.state != SessionState.WAIT_TIMEZONE
    ):
        await _call(
            database.start_session, user_id, chat_id, SessionState.WAIT_TIMEZONE
        )
        await message.reply_text(
            "Enter your IANA timezone or city, for example Europe/Moscow or New York:",
            reply_markup=CANCEL_KEYBOARD,
        )
        return

    if session is not None:
        await _handle_session_text(update, context, session, text)
        return

    if text == "Add Food":
        await _call(
            database.start_session, user_id, chat_id, SessionState.WAIT_FOOD_NAME
        )
        await message.reply_text(
            "Enter the food name, or choose Skip:", reply_markup=SKIP_KEYBOARD
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
        await _call(
            database.start_session,
            user_id,
            chat_id,
            SessionState.WAIT_FAVORITE_SEARCH,
        )
        await message.reply_text(
            "Enter a favorite food name to search:", reply_markup=CANCEL_KEYBOARD
        )
    elif text == "My Favorites":
        await _show_favorites(update, context, 0)
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
            await _transition(
                database, session, SessionState.WAIT_CALORIES, draft_name=name
            )
            await message.reply_text(
                "Enter calories per 100g:", reply_markup=CANCEL_KEYBOARD
            )
        elif session.state == SessionState.WAIT_CALORIES:
            calories = parse_calories(text)
            await _transition(
                database, session, SessionState.WAIT_GRAMS, calories_per_100g=calories
            )
            await message.reply_text(
                "Enter the serving weight in grams:", reply_markup=CANCEL_KEYBOARD
            )
        elif session.state == SessionState.WAIT_GRAMS:
            grams = parse_grams(text)
            await _transition(
                database, session, SessionState.WAIT_PROTEIN, serving_grams=grams
            )
            await message.reply_text(
                "Enter protein per 100g, or choose Skip:", reply_markup=SKIP_KEYBOARD
            )
        elif session.state == SessionState.WAIT_PROTEIN:
            protein = None if text == "Skip" else parse_macro(text, "Protein")
            validate_macro_sum(protein, session.fat_per_100g, session.carbs_per_100g)
            await _transition(
                database,
                session,
                SessionState.WAIT_FAT,
                protein_per_100g=protein,
            )
            await message.reply_text(
                "Enter fat per 100g, or choose Skip:", reply_markup=SKIP_KEYBOARD
            )
        elif session.state == SessionState.WAIT_FAT:
            fat = None if text == "Skip" else parse_macro(text, "Fat")
            validate_macro_sum(session.protein_per_100g, fat, session.carbs_per_100g)
            await _transition(
                database, session, SessionState.WAIT_CARBS, fat_per_100g=fat
            )
            await message.reply_text(
                "Enter carbs per 100g, or choose Skip:", reply_markup=SKIP_KEYBOARD
            )
        elif session.state == SessionState.WAIT_CARBS:
            carbs = None if text == "Skip" else parse_macro(text, "Carbs")
            validate_macro_sum(session.protein_per_100g, session.fat_per_100g, carbs)
            completed = replace(session, carbs_per_100g=carbs)
            await _call(database.complete_food_draft, completed, now)
            if session.draft_name is None:
                await message.reply_text(
                    "Food entry added.", reply_markup=MAIN_KEYBOARD
                )
            else:
                await message.reply_text(
                    "Food entry added. Save this product as a favorite?",
                    reply_markup=FAVORITE_DECISION_KEYBOARD,
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
                    "Choose Yes, No, or Cancel.",
                    reply_markup=FAVORITE_DECISION_KEYBOARD,
                )
        elif session.state == SessionState.WAIT_FAVORITE_SEARCH:
            query = normalize_search_query(text)
            favorites = await _call(database.search_favorites, session.user_id, query)
            await _call(database.clear_session, session.user_id, session.chat_id)
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
                await message.reply_text(
                    "Select a favorite product:", reply_markup=keyboard
                )
                await message.reply_text(
                    "Select an option:", reply_markup=MAIN_KEYBOARD
                )
        elif session.state == SessionState.WAIT_FAVORITE_GRAMS:
            grams = parse_grams(text)
            await _call(
                database.use_selected_favorite,
                session.user_id,
                session.chat_id,
                grams,
                now,
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
    **changes: object,
) -> Session:
    updated = replace(
        session,
        state=state,
        updated_at_utc=database.now_epoch(),
        **changes,
    )
    return await _call(database.update_session, session, updated)


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
    await message.reply_text(
        f"{title}:\n"
        f"Calories: {stats.calories:.2f}\n"
        f"Protein: {_optional_grams(stats.protein)}\n"
        f"Fat: {_optional_grams(stats.fat)}\n"
        f"Carbs: {_optional_grams(stats.carbs)}"
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
                    callback_data=f"entry:view:{entry.entry_id}",
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
                    callback_data=f"fav:view:{favorite.favorite_id}",
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
    try:
        if action.kind == "cancel":
            timezone_name = await _call(database.get_timezone, user_id)
            if timezone_name is None:
                await _call(
                    database.start_session,
                    user_id,
                    chat_id,
                    SessionState.WAIT_TIMEZONE,
                )
                await query.edit_message_text("Timezone setup is required.")
                await query.message.reply_text(
                    "Enter your IANA timezone or city:", reply_markup=CANCEL_KEYBOARD
                )
            else:
                await _call(database.clear_session, user_id, chat_id)
                await query.edit_message_text("Cancelled.")
                await query.message.reply_text(
                    "Select an option:", reply_markup=MAIN_KEYBOARD
                )
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
                        )
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
                            callback_data=f"entry:delete-confirm:{entry.entry_id}",
                        ),
                        InlineKeyboardButton("Keep", callback_data="cancel"),
                    ]
                ]
            )
            await query.edit_message_text(
                "Delete this food entry?", reply_markup=keyboard
            )
        elif action.kind == "entry_delete_confirm" and action.record_id is not None:
            await _call(database.delete_entry, user_id, action.record_id)
            await query.edit_message_text("Food entry deleted.")
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
            await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_FAVORITE_GRAMS,
                selected_favorite_id=favorite.favorite_id,
            )
            try:
                await query.edit_message_text(
                    f"Enter the serving weight in grams for {favorite.name}:",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Cancel", callback_data="cancel")]]
                    ),
                )
            except TelegramError:
                await _call(database.clear_session, user_id, chat_id)
                raise
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
                    [InlineKeyboardButton("Cancel", callback_data="cancel")],
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
            await _call(
                database.start_session,
                user_id,
                chat_id,
                SessionState.WAIT_FAVORITE_AMENDMENT,
                selected_favorite_id=favorite.favorite_id,
                selected_nutrient=action.nutrient,
            )
            await query.edit_message_text(
                f"Enter the new {action.nutrient} value per 100g:",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Cancel", callback_data="cancel")]]
                ),
            )
        elif action.kind == "favorite_delete" and action.record_id is not None:
            favorite = await _call(database.get_favorite, user_id, action.record_id)
            if favorite is None:
                raise NotFound("Favorite not found")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Delete",
                            callback_data=f"fav:delete-confirm:{favorite.favorite_id}",
                        ),
                        InlineKeyboardButton("Keep", callback_data="cancel"),
                    ]
                ]
            )
            await query.edit_message_text(
                f"Delete favorite {favorite.name}?", reply_markup=keyboard
            )
        elif action.kind == "favorite_delete_confirm" and action.record_id is not None:
            await _call(database.delete_favorite, user_id, action.record_id)
            await query.edit_message_text("Favorite product deleted.")
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
            ("entry_cancel_delete_", "cancel", None),
            ("entry_delete_", "entry_delete", None),
            ("entry_choose_", "entry_view", None),
            ("fave_confirm_delete_", "favorite_delete_confirm", None),
            ("fave_cancel_delete_", "cancel", None),
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


def _short(value: str, limit: int = 42) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _optional_grams(value: Optional[float]) -> str:
    return "not set" if value is None else f"{value:.2f}g"


def _entry_button_text(entry: FoodEntry) -> str:
    name = _short(entry.name or "Unnamed food", 30)
    return f"{name} - {entry.nutrition.calories:.2f} kcal, {entry.nutrition.grams:.2f}g"


def _favorite_button_text(favorite: FavoriteFood) -> str:
    return f"{_short(favorite.name, 34)} - {favorite.calories_per_100g:.2f} kcal"


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
    await update.effective_message.reply_text(
        "Unknown command. Use /start or choose an option from the keyboard."
    )


async def handle_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_private(update) or update.effective_message is None:
        return
    user_id, chat_id = _identity(update)
    session = await _call(_db(context).get_session, user_id, chat_id)
    if session is not None:
        await update.effective_message.reply_text(
            "Please send a text value or choose Cancel."
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
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "The request could not be completed. Please try again."
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
