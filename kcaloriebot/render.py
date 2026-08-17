"""Keyboards, prompts, and message formatting shared by the bot handlers."""

from __future__ import annotations

from typing import Any, Optional, Union

from telegram import (
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from datetime import date, timedelta

from .callbacks import PAGE_SIZE
from .domain import (
    UNIT_SERVING,
    DayStats,
    FavoriteFood,
    FoodEntry,
    NutritionTotals,
    Page,
    Session,
    SessionState,
    Stats,
    local_datetime,
)


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
            KeyboardButton("Weight"),
        ],
        [KeyboardButton("Update Timezone")],
    ],
    resize_keyboard=True,
)

STATS_KEYBOARD = ReplyKeyboardMarkup(
    [
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

PER_SERVING_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("Per Serving"), KeyboardButton("Cancel")]],
    resize_keyboard=True,
)

SERVINGS_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("0.5"), KeyboardButton("1"), KeyboardButton("2")],
        [KeyboardButton("Cancel")],
    ],
    resize_keyboard=True,
)

FAVORITE_SERVINGS_MANUAL_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("0.5"), KeyboardButton("1"), KeyboardButton("2")],
        [KeyboardButton("Enter Manually"), KeyboardButton("Cancel")],
    ],
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
ENTRY_SERVINGS_PROMPT = "Enter the new number of servings, for example 1 or 0.5:"
ENTRY_TIME_PROMPT = "Enter the new entry time as HH:MM for today, or YYYY-MM-DD HH:MM:"
ENTRY_NAME_PROMPT = "Enter the new food name:"
SERVING_CALORIES_PROMPT = "Enter calories per one serving:"
SERVINGS_COUNT_PROMPT = "Enter the number of servings, for example 1 or 0.5:"
SERVING_PROTEIN_PROMPT = "Enter protein per serving in grams, or choose Skip:"
SERVING_FAT_PROMPT = "Enter fat per serving in grams, or choose Skip:"
SERVING_CARBS_PROMPT = "Enter carbs per serving in grams, or choose Skip:"
FAVORITE_SERVINGS_PROMPT = (
    "Enter the number of servings for the selected favorite, for example 1 or 0.5:"
)
FAVORITE_TO_SERVING_PROMPT = (
    "How many grams is one serving? The favorite's per-100g values will be "
    "converted to per-serving values:"
)
WEIGHT_PROMPT = "Enter your weight in kilograms, for example 82.5:"
QUICK_ADD_USAGE = (
    "Log a food in one message: name, calories per 100g, grams. "
    "Example: oatmeal 370 60 or bread 250 kcal 150 g. "
    "Optional macros per 100g: p12 f6 c60."
)


def session_prompt(session: Session, has_timezone: bool) -> tuple[str, Markup]:
    """Return the prompt and keyboard that re-ask the session's current step."""
    serving_mode = session.draft_unit == UNIT_SERVING
    prompts: dict[SessionState, tuple[str, Markup]] = {
        SessionState.WAIT_TIMEZONE: (
            TIMEZONE_CHANGE_PROMPT if has_timezone else TIMEZONE_ONBOARDING_PROMPT,
            CANCEL_KEYBOARD if has_timezone else TIMEZONE_REQUIRED_MARKUP,
        ),
        SessionState.WAIT_FOOD_NAME: (FOOD_NAME_PROMPT, SKIP_KEYBOARD),
        SessionState.WAIT_CALORIES: (
            (SERVING_CALORIES_PROMPT, CANCEL_KEYBOARD)
            if serving_mode
            else (CALORIES_PROMPT, PER_SERVING_KEYBOARD)
        ),
        SessionState.WAIT_GRAMS: (
            (SERVINGS_COUNT_PROMPT, SERVINGS_KEYBOARD)
            if serving_mode
            else (GRAMS_PROMPT, CANCEL_KEYBOARD)
        ),
        SessionState.WAIT_PROTEIN: (
            SERVING_PROTEIN_PROMPT if serving_mode else PROTEIN_PROMPT,
            SKIP_KEYBOARD,
        ),
        SessionState.WAIT_FAT: (
            SERVING_FAT_PROMPT if serving_mode else FAT_PROMPT,
            SKIP_KEYBOARD,
        ),
        SessionState.WAIT_CARBS: (
            SERVING_CARBS_PROMPT if serving_mode else CARBS_PROMPT,
            SKIP_KEYBOARD,
        ),
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
        SessionState.WAIT_FAVORITE_SERVINGS: (
            (FAVORITE_SERVINGS_PROMPT, FAVORITE_SERVINGS_MANUAL_KEYBOARD)
            if session.draft_name is not None
            and session.selected_favorite_id is not None
            else (FAVORITE_SERVINGS_PROMPT, SERVINGS_KEYBOARD)
        ),
        SessionState.WAIT_FAVORITE_TO_SERVING: (
            FAVORITE_TO_SERVING_PROMPT,
            CANCEL_KEYBOARD,
        ),
        SessionState.WAIT_FAVORITE_AMENDMENT: (
            f"Enter the new {session.selected_nutrient or 'nutrient'} value:",
            CANCEL_KEYBOARD,
        ),
        SessionState.WAIT_GOAL: (GOAL_PROMPT, GOAL_KEYBOARD),
        SessionState.WAIT_RECENT_GRAMS: (RECENT_GRAMS_PROMPT, REPEAT_KEYBOARD),
        SessionState.WAIT_ENTRY_GRAMS: (ENTRY_GRAMS_PROMPT, CANCEL_KEYBOARD),
        SessionState.WAIT_ENTRY_TIME: (ENTRY_TIME_PROMPT, CANCEL_KEYBOARD),
        SessionState.WAIT_ENTRY_NAME: (ENTRY_NAME_PROMPT, CANCEL_KEYBOARD),
        SessionState.WAIT_ENTRY_AMENDMENT: (
            f"Enter the new {session.selected_nutrient or 'nutrient'} value:",
            CANCEL_KEYBOARD,
        ),
        SessionState.WAIT_WEIGHT: (WEIGHT_PROMPT, CANCEL_KEYBOARD),
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


def stats_totals_text(title: str, stats: Stats, goal: Optional[float] = None) -> str:
    calories_line = f"Calories: {stats.calories:.2f}"
    if goal is not None:
        remaining = goal - stats.calories
        calories_line += (
            f" / {goal:.0f} goal ({remaining:.0f} left)"
            if remaining >= 0
            else f" / {goal:.0f} goal ({-remaining:.0f} over)"
        )
    return (
        f"{title}:\n"
        f"{calories_line}\n"
        f"{stat_macro_line('Protein', stats.protein, stats.protein_coverage, stats.coverage_total, 'entries')}\n"
        f"{stat_macro_line('Fat', stats.fat, stats.fat_coverage, stats.coverage_total, 'entries')}\n"
        f"{stat_macro_line('Carbs', stats.carbs, stats.carbs_coverage, stats.coverage_total, 'entries')}"
    )


def month_navigation_row(
    year: int, month: int, current: date
) -> list[InlineKeyboardButton]:
    """Buttons that switch the month statistics to an adjacent month."""
    row: list[InlineKeyboardButton] = []
    previous = date(year - 1, 12, 1) if month == 1 else date(year, month - 1, 1)
    if previous.year >= 2020:
        row.append(
            InlineKeyboardButton(
                f"◀ {previous:%b %Y}",
                callback_data=f"stats:month:{previous:%Y-%m}:0",
            )
        )
    upcoming = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    if (upcoming.year, upcoming.month) <= (current.year, current.month):
        row.append(
            InlineKeyboardButton(
                f"{upcoming:%b %Y} ▶",
                callback_data=f"stats:month:{upcoming:%Y-%m}:0",
            )
        )
    return row


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


def amount_text(nutrition: NutritionTotals) -> str:
    """Human-readable amount: grams, servings, or servings with known weight."""
    if nutrition.servings is not None:
        label = "serving" if nutrition.servings == 1 else "servings"
        rendered = f"{nutrition.servings:g} {label}"
        if nutrition.grams is not None:
            rendered += f" ({nutrition.grams:.0f}g)"
        return rendered
    assert nutrition.grams is not None
    return f"{nutrition.grams:.2f}g"


def entry_button_text(entry: FoodEntry, timezone_name: Optional[str] = None) -> str:
    name = short(entry.name or "Unnamed food", 30)
    prefix = ""
    if timezone_name is not None:
        prefix = f"{local_datetime(entry.eaten_at_utc, timezone_name):%H:%M} "
    return (
        f"{prefix}{name} - {entry.nutrition.calories:.2f} kcal, "
        f"{amount_text(entry.nutrition)}"
    )


def favorite_button_text(favorite: FavoriteFood) -> str:
    unit = "serving" if favorite.unit == UNIT_SERVING else "100g"
    return f"{short(favorite.name, 30)} - {favorite.calories_per_100g:.2f} kcal/{unit}"


def entry_details(entry: FoodEntry, timezone_name: Optional[str] = None) -> str:
    time_line = ""
    if timezone_name is not None:
        eaten_at = local_datetime(entry.eaten_at_utc, timezone_name)
        time_line = f"Time: {eaten_at:%Y-%m-%d %H:%M}\n"
    return (
        f"{entry.name or 'Unnamed food'}\n"
        f"{time_line}"
        f"Calories: {entry.nutrition.calories:.2f}\n"
        f"Serving: {amount_text(entry.nutrition)}\n"
        f"Protein: {optional_grams(entry.nutrition.protein)}\n"
        f"Fat: {optional_grams(entry.nutrition.fat)}\n"
        f"Carbs: {optional_grams(entry.nutrition.carbs)}"
    )


def favorite_details(favorite: FavoriteFood) -> str:
    if favorite.unit == UNIT_SERVING:
        header = f"{favorite.name} per serving"
        if favorite.serving_grams is not None:
            header += f" ({favorite.serving_grams:.0f}g)"
    else:
        header = f"{favorite.name} per 100g"
    return (
        f"{header}\n"
        f"Calories: {favorite.calories_per_100g:.2f}\n"
        f"Protein: {optional_grams(favorite.protein_per_100g)}\n"
        f"Fat: {optional_grams(favorite.fat_per_100g)}\n"
        f"Carbs: {optional_grams(favorite.carbs_per_100g)}"
    )


def day_navigation_row(
    day: date, today: date, oldest: date
) -> list[InlineKeyboardButton]:
    """Previous/next-day buttons for the diary, bounded by history and today."""
    row: list[InlineKeyboardButton] = []
    previous = day - timedelta(days=1)
    if previous >= oldest:
        row.append(
            InlineKeyboardButton(
                f"◀ {previous:%d %b}",
                callback_data=f"entry:list:{previous.isoformat()}:0",
            )
        )
    upcoming = day + timedelta(days=1)
    if upcoming <= today:
        row.append(
            InlineKeyboardButton(
                f"{upcoming:%d %b} ▶",
                callback_data=f"entry:list:{upcoming.isoformat()}:0",
            )
        )
    if day != today:
        row.append(InlineKeyboardButton("Today", callback_data="entry:list:0"))
    return row


def stats_day_rows(
    days: tuple[DayStats, ...], per_row: int = 4
) -> list[list[InlineKeyboardButton]]:
    """Buttons that open the diary page of each listed statistics day."""
    buttons = [
        InlineKeyboardButton(
            f"{stats.day:%d %b}",
            callback_data=f"entry:list:{stats.day.isoformat()}:0",
        )
        for stats in days
    ]
    return [
        buttons[start : start + per_row] for start in range(0, len(buttons), per_row)
    ]
