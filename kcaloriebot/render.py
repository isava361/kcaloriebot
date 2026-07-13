"""Keyboards, prompts, and message formatting shared by the bot handlers."""

from __future__ import annotations

from typing import Any, Optional, Union

from telegram import (
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .callbacks import PAGE_SIZE
from .domain import DayStats, FavoriteFood, FoodEntry, Page, Session, SessionState


Markup = Union[ReplyKeyboardMarkup, ReplyKeyboardRemove]

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Add Food"), KeyboardButton("Food Today")],
        [
            KeyboardButton("Recent Foods"),
            KeyboardButton("Search Favorites"),
            KeyboardButton("My Favorites"),
        ],
        [
            KeyboardButton("Statistics"),
            KeyboardButton("Daily Goal"),
            KeyboardButton("Update Timezone"),
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
    [[KeyboardButton("Yes"), KeyboardButton("No")]],
    resize_keyboard=True,
)

GOAL_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Remove"), KeyboardButton("Cancel")]],
    resize_keyboard=True,
)

REPEAT_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Same as last time")], [KeyboardButton("Cancel")]],
    resize_keyboard=True,
)

MANUAL_ENTRY_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Enter Manually"), KeyboardButton("Cancel")]],
    resize_keyboard=True,
)

TIMEZONE_REQUIRED_MARKUP = ReplyKeyboardRemove()


TIMEZONE_ONBOARDING_PROMPT = (
    "Enter your IANA timezone or city, for example Europe/Moscow or New York:"
)
TIMEZONE_SETUP_REQUIRED_PROMPT = (
    "Timezone setup is required. Enter an IANA timezone such as Europe/Moscow."
)
TIMEZONE_CHANGE_PROMPT = (
    "Enter your new IANA timezone or city. Changing it also changes how existing "
    "entries near midnight are grouped into calendar days:"
)
FOOD_NAME_PROMPT = "Enter the food name, or choose Skip:"
CALORIES_PROMPT = "Enter calories per 100g:"
GRAMS_PROMPT = "Enter the serving weight in grams:"
PROTEIN_PROMPT = "Enter protein per 100g, or choose Skip:"
FAT_PROMPT = "Enter fat per 100g, or choose Skip:"
CARBS_PROMPT = "Enter carbs per 100g, or choose Skip:"
SAVE_FAVORITE_PROMPT = (
    "The food entry is already saved. Save this product as a favorite?"
)
FAVORITE_SEARCH_PROMPT = "Enter a favorite food name to search:"
FAVORITE_GRAMS_PROMPT = "Enter the serving weight in grams for the selected favorite:"
FAVORITE_MATCH_GRAMS_PROMPT = (
    "Enter the serving weight in grams, or choose Enter Manually to type the "
    "nutrition values yourself:"
)
GOAL_PROMPT = "Enter your daily calorie goal in kcal, or choose Remove to clear it:"
RECENT_GRAMS_PROMPT = "Enter the serving weight in grams, or choose Same as last time:"
ENTRY_GRAMS_PROMPT = "Enter the new serving weight in grams:"
ENTRY_TIME_PROMPT = "Enter the new entry time as HH:MM for today, or YYYY-MM-DD HH:MM:"
QUICK_ADD_USAGE = (
    "Log a food in one message: name, calories per 100g, grams. "
    "Example: oatmeal 370 60 or bread 250 kcal 150 g. "
    "Optional macros per 100g: p12 f6 c60."
)


def session_prompt(session: Session, has_timezone: bool) -> tuple[str, Markup]:
    """Return the prompt and keyboard that re-ask the session's current step."""
    prompts: dict[SessionState, tuple[str, Markup]] = {
        SessionState.WAIT_TIMEZONE: (
            TIMEZONE_CHANGE_PROMPT if has_timezone else TIMEZONE_ONBOARDING_PROMPT,
            CANCEL_KEYBOARD if has_timezone else TIMEZONE_REQUIRED_MARKUP,
        ),
        SessionState.WAIT_FOOD_NAME: (FOOD_NAME_PROMPT, SKIP_KEYBOARD),
        SessionState.WAIT_CALORIES: (CALORIES_PROMPT, CANCEL_KEYBOARD),
        SessionState.WAIT_GRAMS: (GRAMS_PROMPT, CANCEL_KEYBOARD),
        SessionState.WAIT_PROTEIN: (PROTEIN_PROMPT, SKIP_KEYBOARD),
        SessionState.WAIT_FAT: (FAT_PROMPT, SKIP_KEYBOARD),
        SessionState.WAIT_CARBS: (CARBS_PROMPT, SKIP_KEYBOARD),
        SessionState.WAIT_SAVE_FAVORITE: (
            SAVE_FAVORITE_PROMPT,
            FAVORITE_DECISION_KEYBOARD,
        ),
        SessionState.WAIT_FAVORITE_SEARCH: (FAVORITE_SEARCH_PROMPT, CANCEL_KEYBOARD),
        SessionState.WAIT_FAVORITE_GRAMS: (
            (FAVORITE_MATCH_GRAMS_PROMPT, MANUAL_ENTRY_KEYBOARD)
            if session.draft_name is not None
            and session.selected_favorite_id is not None
            else (FAVORITE_GRAMS_PROMPT, CANCEL_KEYBOARD)
        ),
        SessionState.WAIT_FAVORITE_AMENDMENT: (
            f"Enter the new {session.selected_nutrient or 'nutrient'} value per 100g:",
            CANCEL_KEYBOARD,
        ),
        SessionState.WAIT_GOAL: (GOAL_PROMPT, GOAL_KEYBOARD),
        SessionState.WAIT_RECENT_GRAMS: (RECENT_GRAMS_PROMPT, REPEAT_KEYBOARD),
        SessionState.WAIT_ENTRY_GRAMS: (ENTRY_GRAMS_PROMPT, CANCEL_KEYBOARD),
        SessionState.WAIT_ENTRY_TIME: (ENTRY_TIME_PROMPT, CANCEL_KEYBOARD),
    }
    return prompts[session.state]


def navigation_row(
    page: Page[Any], prefix: str, page_size: int = PAGE_SIZE
) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if page.has_previous:
        row.append(
            InlineKeyboardButton(
                "Previous", callback_data=f"{prefix}:{max(0, page.offset - page_size)}"
            )
        )
    if page.has_next:
        row.append(
            InlineKeyboardButton(
                "Next", callback_data=f"{prefix}:{page.offset + page_size}"
            )
        )
    return row


def short(value: str, limit: int = 42) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def optional_grams(value: Optional[float]) -> str:
    return "not set" if value is None else f"{value:.2f}g"


def stat_macro_line(
    label: str,
    value: Optional[float],
    coverage: int,
    coverage_total: int,
    coverage_unit: str,
) -> str:
    rendered = f"{label}: {optional_grams(value)}"
    if coverage < coverage_total:
        rendered += f" (partial: {coverage}/{coverage_total} {coverage_unit})"
    return rendered


def _day_macro(
    label: str, value: Optional[float], coverage: int, entry_count: int
) -> str:
    rendered = f"{label}: {optional_grams(value)}"
    if 0 < coverage < entry_count:
        rendered += f" (partial: {coverage}/{entry_count})"
    return rendered


def day_stats_block(stats: DayStats) -> str:
    macros = " | ".join(
        _day_macro(label, value, coverage, stats.entry_count)
        for label, value, coverage in (
            ("Protein", stats.protein, stats.protein_coverage),
            ("Fat", stats.fat, stats.fat_coverage),
            ("Carbs", stats.carbs, stats.carbs_coverage),
        )
    )
    return f"{stats.day.isoformat()} — {stats.calories:.2f} kcal\n{macros}"


def entry_button_text(entry: FoodEntry) -> str:
    name = short(entry.name or "Unnamed food", 30)
    return f"{name} - {entry.nutrition.calories:.2f} kcal, {entry.nutrition.grams:.2f}g"


def favorite_button_text(favorite: FavoriteFood) -> str:
    return f"{short(favorite.name, 30)} - {favorite.calories_per_100g:.2f} kcal/100g"


def entry_details(entry: FoodEntry) -> str:
    return (
        f"{entry.name or 'Unnamed food'}\n"
        f"Calories: {entry.nutrition.calories:.2f}\n"
        f"Serving: {entry.nutrition.grams:.2f}g\n"
        f"Protein: {optional_grams(entry.nutrition.protein)}\n"
        f"Fat: {optional_grams(entry.nutrition.fat)}\n"
        f"Carbs: {optional_grams(entry.nutrition.carbs)}"
    )


def favorite_details(favorite: FavoriteFood) -> str:
    return (
        f"{favorite.name} per 100g\n"
        f"Calories: {favorite.calories_per_100g:.2f}\n"
        f"Protein: {optional_grams(favorite.protein_per_100g)}\n"
        f"Fat: {optional_grams(favorite.fat_per_100g)}\n"
        f"Carbs: {optional_grams(favorite.carbs_per_100g)}"
    )
