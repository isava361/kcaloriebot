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
    EARLIEST_DIARY_DATE,
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
    per_unit_from_totals,
)


Markup = Union[ReplyKeyboardMarkup, ReplyKeyboardRemove]

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Add food"), KeyboardButton("Diary")],
        [KeyboardButton("Recent foods"), KeyboardButton("Favorites")],
        [KeyboardButton("Progress"), KeyboardButton("Settings")],
    ],
    resize_keyboard=True,
)

STATS_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Week Stats"), KeyboardButton("Month Stats")],
        [KeyboardButton("Weight"), KeyboardButton("Main menu")],
    ],
    resize_keyboard=True,
)

SETTINGS_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Daily goal"), KeyboardButton("Timezone")],
        [KeyboardButton("Apple Health")],
        [KeyboardButton("Main menu")],
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

TIMEZONE_CHOICES = [
    [KeyboardButton("Europe/Moscow"), KeyboardButton("Europe/London")],
    [KeyboardButton("America/New_York"), KeyboardButton("Asia/Dubai")],
]
TIMEZONE_REQUIRED_MARKUP = ReplyKeyboardMarkup(TIMEZONE_CHOICES, resize_keyboard=True)
TIMEZONE_CHANGE_KEYBOARD = ReplyKeyboardMarkup(
    TIMEZONE_CHOICES + [[KeyboardButton("Cancel")]],
    resize_keyboard=True,
)


TIMEZONE_ONBOARDING_PROMPT = (
    "Track meals and see your daily totals. First, choose your timezone so meals "
    "appear on the right day.\n\nTap a timezone below, or type your region/city "
    "in this format: Europe/Paris."
)
TIMEZONE_SETUP_REQUIRED_PROMPT = (
    "Choose your timezone so meals appear on the right day. Tap a choice below, "
    "or type a timezone name such as Europe/Paris:"
)
TIMEZONE_CHANGE_PROMPT = (
    "Choose your new timezone below, or type a timezone name such as Europe/Paris. "
    "Meals near midnight may move to the previous or next day in your diary:"
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
RECENT_SERVINGS_PROMPT = "Enter the number of servings, or choose Same as last time:"
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
    if session.prompt_text is not None:
        return session.prompt_text, CANCEL_KEYBOARD
    serving_mode = session.draft_unit == UNIT_SERVING
    prompts: dict[SessionState, tuple[str, Markup]] = {
        SessionState.WAIT_TIMEZONE: (
            TIMEZONE_CHANGE_PROMPT if has_timezone else TIMEZONE_ONBOARDING_PROMPT,
            TIMEZONE_CHANGE_KEYBOARD if has_timezone else TIMEZONE_REQUIRED_MARKUP,
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
        SessionState.WAIT_RECENT_GRAMS: (
            RECENT_SERVINGS_PROMPT if serving_mode else RECENT_GRAMS_PROMPT,
            REPEAT_KEYBOARD,
        ),
        SessionState.WAIT_ENTRY_GRAMS: (
            ENTRY_SERVINGS_PROMPT if serving_mode else ENTRY_GRAMS_PROMPT,
            CANCEL_KEYBOARD,
        ),
        SessionState.WAIT_ENTRY_TIME: (ENTRY_TIME_PROMPT, CANCEL_KEYBOARD),
        SessionState.WAIT_ENTRY_NAME: (ENTRY_NAME_PROMPT, CANCEL_KEYBOARD),
        SessionState.WAIT_ENTRY_AMENDMENT: (
            f"Enter the new {session.selected_nutrient or 'nutrient'} value "
            f"{'per serving' if serving_mode else 'per 100g'}:",
            CANCEL_KEYBOARD,
        ),
        SessionState.WAIT_WEIGHT: (WEIGHT_PROMPT, CANCEL_KEYBOARD),
        SessionState.WAIT_WEIGHT_EDIT: (
            "Enter the corrected weight in kg:",
            CANCEL_KEYBOARD,
        ),
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


def number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def optional_grams(value: Optional[float]) -> str:
    return "not recorded" if value is None else f"{number(value)} g"


def macro_text(
    protein: Optional[float], fat: Optional[float], carbs: Optional[float]
) -> str:
    if protein is None and fat is None and carbs is None:
        return "Macros not recorded"
    return " · ".join(
        f"{label}: {optional_grams(value)}"
        for label, value in (("Protein", protein), ("Fat", fat), ("Carbs", carbs))
    )


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
    calories_line = f"Calories: {stats.calories:.0f} kcal"
    if goal is not None:
        remaining = goal - stats.calories
        calories_line += (
            f" / {goal:.0f} goal ({remaining:.0f} left)"
            if remaining >= 0
            else f" / {goal:.0f} goal ({-remaining:.0f} over)"
        )
    if stats.protein is None and stats.fat is None and stats.carbs is None:
        return f"{title}:\n{calories_line}\nMacros not recorded"
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
    if previous >= EARLIEST_DIARY_DATE:
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
    if stats.protein is None and stats.fat is None and stats.carbs is None:
        macros = "Macros not recorded"
    return f"{stats.day.isoformat()} — {stats.calories:.0f} kcal\n{macros}"


def amount_text(nutrition: NutritionTotals) -> str:
    """Human-readable amount: grams, servings, or servings with known weight."""
    if nutrition.servings is not None:
        label = "serving" if nutrition.servings == 1 else "servings"
        rendered = f"{nutrition.servings:g} {label}"
        if nutrition.grams is not None:
            rendered += f" ({number(nutrition.grams)} g)"
        return rendered
    assert nutrition.grams is not None
    return f"{number(nutrition.grams)} g"


def entry_button_text(entry: FoodEntry, timezone_name: Optional[str] = None) -> str:
    name = short(entry.name or "Unnamed food", 22)
    prefix = ""
    if timezone_name is not None:
        prefix = f"{local_datetime(entry.eaten_at_utc, timezone_name):%H:%M} "
    return f"{prefix}{name} · {entry.nutrition.calories:.0f} kcal"


def favorite_button_text(favorite: FavoriteFood) -> str:
    unit = "serving" if favorite.unit == UNIT_SERVING else "100g"
    return f"{short(favorite.name, 22)} · {favorite.calories_per_100g:.0f} kcal/{unit}"


def entry_details(entry: FoodEntry, timezone_name: Optional[str] = None) -> str:
    time_line = ""
    if timezone_name is not None:
        eaten_at = local_datetime(entry.eaten_at_utc, timezone_name)
        time_line = f"Time: {eaten_at:%Y-%m-%d %H:%M}\n"
    return (
        f"{entry.name or 'Unnamed food'} · {amount_text(entry.nutrition)}\n"
        f"{time_line}"
        f"{entry.nutrition.calories:.0f} kcal\n"
        f"{macro_text(entry.nutrition.protein, entry.nutrition.fat, entry.nutrition.carbs)}"
    )


def favorite_details(favorite: FavoriteFood) -> str:
    if favorite.unit == UNIT_SERVING:
        header = f"{favorite.name} per serving"
        if favorite.serving_grams is not None:
            header += f" ({number(favorite.serving_grams)} g)"
    else:
        header = f"{favorite.name} per 100g"
    return (
        f"{header}\n"
        f"{favorite.calories_per_100g:.0f} kcal\n"
        f"{macro_text(favorite.protein_per_100g, favorite.fat_per_100g, favorite.carbs_per_100g)}"
    )


def nutrient_edit_prompt(item: Union[FoodEntry, FavoriteFood], nutrient: str) -> str:
    """Explain the editable label value separately from the amount consumed."""
    suffix = "kcal" if nutrient == "calories" else "g"
    if isinstance(item, FoodEntry):
        unit, *values = per_unit_from_totals(item.nutrition)
        value = dict(zip(("calories", "protein", "fat", "carbs"), values))[nutrient]
        total = getattr(item.nutrition, nutrient)
        header = f"{item.name or 'Unnamed food'} · {amount_text(item.nutrition)}"
        logged = "not recorded" if total is None else f"{number(total)} {suffix} logged"
    else:
        unit = item.unit
        value = getattr(item, f"{nutrient}_per_100g")
        header, logged = item.name, ""
    basis = "per serving" if unit == UNIT_SERVING else "per 100 g"
    current = "not recorded" if value is None else f"{number(value)} {suffix} {basis}"
    explanation = f"Current {nutrient}: {current}"
    if logged:
        explanation += f" → {logged}"
    return f"{header}\n{explanation}\n\nEnter the new {nutrient} in {suffix} {basis}:"


def contextual(data: str, offset: int = 0, day: Optional[str] = None) -> str:
    return f"{data}|{offset}|{day or ''}" if offset or day else data


def entry_action_rows(
    entry: FoodEntry, offset: int, day: Optional[str]
) -> list[list[InlineKeyboardButton]]:
    def button(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label, callback_data=contextual(action, offset, day)
        )

    prefix = f"entry:field:{entry.entry_id}"
    back = f"entry:list:{day}:{offset}" if day else f"entry:list:{offset}"
    return [
        [
            button("Edit amount", f"entry:grams:{entry.entry_id}"),
            button("Edit time", f"entry:time:{entry.entry_id}"),
            button("Edit name", f"entry:name:{entry.entry_id}"),
        ],
        [
            button("Edit calories", f"{prefix}:calories"),
            button("Edit protein", f"{prefix}:protein"),
        ],
        [button("Edit fat", f"{prefix}:fat"), button("Edit carbs", f"{prefix}:carbs")],
        [
            button("Delete", f"entry:delete:{entry.entry_id}"),
            InlineKeyboardButton("Back to diary", callback_data=back),
        ],
    ]


def favorite_action_rows(
    favorite: FavoriteFood, offset: int
) -> list[list[InlineKeyboardButton]]:
    def button(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=contextual(action, offset))

    rows = [
        [
            InlineKeyboardButton(
                "Log food", callback_data=f"fav:use:{favorite.favorite_id}"
            ),
            button("Edit nutrition", f"fav:edit:{favorite.favorite_id}"),
        ],
        [
            button("Delete", f"fav:delete:{favorite.favorite_id}"),
            InlineKeyboardButton(
                "Back to favorites", callback_data=f"fav:list:{offset}"
            ),
        ],
    ]
    if favorite.unit != UNIT_SERVING:
        rows.insert(
            1, [button("Set serving size", f"fav:serving:{favorite.favorite_id}")]
        )
    return rows


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
