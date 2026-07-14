from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from functools import lru_cache
from typing import Generic, Optional, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


UTC = timezone.utc
SQLITE_REAL_LIMIT = 1e308
MAX_CALORIES_PER_100G = 10_000.0
MAX_SERVING_GRAMS = 100_000.0
MAX_DAILY_GOAL_KCAL = 50_000.0
MAX_ENTRY_AGE_SECONDS = 366 * 24 * 60 * 60
ENTRY_TIME_GRACE_SECONDS = 120


class ValidationError(ValueError):
    """Raised when user-provided domain data is invalid."""


class StateConflict(RuntimeError):
    """Raised when a persisted workflow changed before an operation completed."""


class NotFound(LookupError):
    """Raised for records that are absent or not owned by the acting user."""


class SessionState(str, Enum):
    WAIT_TIMEZONE = "wait_timezone"
    WAIT_FOOD_NAME = "wait_food_name"
    WAIT_CALORIES = "wait_calories"
    WAIT_GRAMS = "wait_grams"
    WAIT_PROTEIN = "wait_protein"
    WAIT_FAT = "wait_fat"
    WAIT_CARBS = "wait_carbs"
    WAIT_SAVE_FAVORITE = "wait_save_favorite"
    WAIT_FAVORITE_SEARCH = "wait_favorite_search"
    WAIT_FAVORITE_GRAMS = "wait_favorite_grams"
    WAIT_FAVORITE_AMENDMENT = "wait_favorite_amendment"
    WAIT_GOAL = "wait_goal"
    WAIT_RECENT_GRAMS = "wait_recent_grams"
    WAIT_ENTRY_GRAMS = "wait_entry_grams"
    WAIT_ENTRY_TIME = "wait_entry_time"


class Period(str, Enum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    WEEK = "week"
    MONTH = "month"


@dataclass(frozen=True)
class Session:
    user_id: int
    chat_id: int
    state: SessionState
    draft_name: Optional[str] = None
    calories_per_100g: Optional[float] = None
    serving_grams: Optional[float] = None
    protein_per_100g: Optional[float] = None
    fat_per_100g: Optional[float] = None
    carbs_per_100g: Optional[float] = None
    selected_favorite_id: Optional[int] = None
    selected_nutrient: Optional[str] = None
    selected_entry_id: Optional[int] = None
    prompt_pending: bool = False
    last_message_id: Optional[int] = None
    revision: int = 0
    updated_at_utc: int = 0


@dataclass(frozen=True)
class NutritionTotals:
    calories: float
    grams: float
    protein: Optional[float]
    fat: Optional[float]
    carbs: Optional[float]


@dataclass(frozen=True)
class FoodEntry:
    entry_id: int
    user_id: int
    eaten_at_utc: int
    name: Optional[str]
    nutrition: NutritionTotals


@dataclass(frozen=True)
class FavoriteFood:
    favorite_id: int
    user_id: int
    name: str
    calories_per_100g: float
    protein_per_100g: Optional[float]
    fat_per_100g: Optional[float]
    carbs_per_100g: Optional[float]


@dataclass(frozen=True)
class Stats:
    entry_count: int
    calories: float
    protein: Optional[float]
    fat: Optional[float]
    carbs: Optional[float]
    coverage_total: int = 0
    protein_coverage: int = 0
    fat_coverage: int = 0
    carbs_coverage: int = 0


@dataclass(frozen=True)
class DayStats:
    """Nutrition totals for one local calendar day that has entries."""

    day: date
    entry_count: int
    calories: float
    protein: Optional[float]
    fat: Optional[float]
    carbs: Optional[float]
    protein_coverage: int = 0
    fat_coverage: int = 0
    carbs_coverage: int = 0


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    offset: int
    has_previous: bool
    has_next: bool


@dataclass(frozen=True)
class PeriodBounds:
    start_utc: int
    end_utc: int
    start_local_date: date
    end_local_date: date


def _parse_finite(text: str, label: str) -> float:
    normalized = text.strip()
    if "," in normalized and "." not in normalized:
        normalized = normalized.replace(",", ".")
    try:
        value = float(normalized)
    except ValueError as exc:
        raise ValidationError(f"{label} must be a number.") from exc
    if not math.isfinite(value):
        raise ValidationError(f"{label} must be finite.")
    return value


def check_calories_per_100g(value: float) -> float:
    if not math.isfinite(value):
        raise ValidationError("Calories must be finite.")
    if value < 0:
        raise ValidationError("Calories cannot be negative.")
    if value >= SQLITE_REAL_LIMIT:
        raise ValidationError("Calories are too large to store.")
    if value > MAX_CALORIES_PER_100G:
        raise ValidationError(
            f"Calories must be no more than {MAX_CALORIES_PER_100G:.0f} per 100g."
        )
    return value


def check_serving_grams(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValidationError("Grams must be greater than zero.")
    if value >= SQLITE_REAL_LIMIT:
        raise ValidationError("Grams are too large to store.")
    if value > MAX_SERVING_GRAMS:
        raise ValidationError(
            f"Serving weight must be no more than {MAX_SERVING_GRAMS:.0f} grams."
        )
    return value


def check_macro(value: float, label: str) -> float:
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValidationError(f"{label} must be between 0 and 100 grams per 100g.")
    return value


def check_daily_goal(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValidationError("Daily goal must be greater than zero.")
    if value > MAX_DAILY_GOAL_KCAL:
        raise ValidationError(
            f"Daily goal must be no more than {MAX_DAILY_GOAL_KCAL:.0f} kcal."
        )
    return value


def parse_calories(text: str) -> float:
    return check_calories_per_100g(_parse_finite(text, "Calories"))


def parse_grams(text: str) -> float:
    return check_serving_grams(_parse_finite(text, "Grams"))


def parse_macro(text: str, label: str) -> float:
    return check_macro(_parse_finite(text, label), label)


def parse_daily_goal(text: str) -> float:
    return check_daily_goal(_parse_finite(text, "Daily goal"))


def normalize_food_name(text: str) -> str:
    name = " ".join(text.split())
    if not name:
        raise ValidationError("Food name cannot be empty.")
    if len(name) > 200:
        raise ValidationError("Food name must be 200 characters or fewer.")
    return name


def normalize_search_query(text: str) -> str:
    query = " ".join(text.split())
    if not query:
        raise ValidationError("Search text cannot be empty.")
    if len(query) > 200:
        raise ValidationError("Search text must be 200 characters or fewer.")
    return query


def validate_macro_sum(
    protein: Optional[float], fat: Optional[float], carbs: Optional[float]
) -> None:
    values = (protein, fat, carbs)
    for value in values:
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 100):
            raise ValidationError(
                "Macronutrients must be finite and between 0 and 100."
            )
    if sum(value or 0.0 for value in values) > 100.000001:
        raise ValidationError(
            "Protein, fat, and carbs cannot add up to more than 100g."
        )


def scale_per_100(
    calories_per_100g: float,
    grams: float,
    protein_per_100g: Optional[float],
    fat_per_100g: Optional[float],
    carbs_per_100g: Optional[float],
) -> NutritionTotals:
    if (
        not math.isfinite(calories_per_100g)
        or calories_per_100g < 0
        or calories_per_100g >= SQLITE_REAL_LIMIT
    ):
        raise ValidationError("Calories cannot be negative or non-finite.")
    if not math.isfinite(grams) or grams <= 0 or grams >= SQLITE_REAL_LIMIT:
        raise ValidationError("Grams must be finite and greater than zero.")
    for label, value in (
        ("Protein", protein_per_100g),
        ("Fat", fat_per_100g),
        ("Carbs", carbs_per_100g),
    ):
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 100):
            raise ValidationError(f"{label} must be finite and between 0 and 100.")
    validate_macro_sum(protein_per_100g, fat_per_100g, carbs_per_100g)
    factor = grams / 100.0
    calories = calories_per_100g * factor
    protein = None if protein_per_100g is None else protein_per_100g * factor
    fat = None if fat_per_100g is None else fat_per_100g * factor
    carbs = None if carbs_per_100g is None else carbs_per_100g * factor
    for value in (calories, protein, fat, carbs):
        if value is not None and (
            not math.isfinite(value) or value >= SQLITE_REAL_LIMIT
        ):
            raise ValidationError("The scaled nutrition value is too large to store.")
    return NutritionTotals(
        calories=calories,
        grams=grams,
        protein=protein,
        fat=fat,
        carbs=carbs,
    )


def per_100_from_totals(
    totals: NutritionTotals,
) -> tuple[float, Optional[float], Optional[float], Optional[float]]:
    """Recover per-100g values from a stored entry so it can be re-scaled.

    Macros are clamped to the valid 0..100 range because floating-point
    round-trips can push a boundary value like 100.0 slightly past it.
    """
    if not math.isfinite(totals.grams) or totals.grams <= 0:
        raise ValidationError("The stored entry has an invalid serving weight.")
    factor = 100.0 / totals.grams

    def scaled_macro(value: Optional[float]) -> Optional[float]:
        return None if value is None else min(100.0, max(0.0, value * factor))

    return (
        totals.calories * factor,
        scaled_macro(totals.protein),
        scaled_macro(totals.fat),
        scaled_macro(totals.carbs),
    )


@dataclass(frozen=True)
class QuickAdd:
    name: str
    calories_per_100g: float
    serving_grams: float
    protein_per_100g: Optional[float] = None
    fat_per_100g: Optional[float] = None
    carbs_per_100g: Optional[float] = None


_QUICK_ADD_CALORIE_UNITS = frozenset({"kcal", "cal", "ккал", "кал"})
_QUICK_ADD_GRAM_UNITS = frozenset(
    {"g", "gr", "gram", "grams", "г", "гр", "грамм", "граммов"}
)
_QUICK_ADD_MACRO_PREFIXES = {
    "p": "protein",
    "f": "fat",
    "c": "carbs",
    "б": "protein",
    "ж": "fat",
    "у": "carbs",
}
_QUICK_ADD_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)([a-zа-яё]*)")
_QUICK_ADD_MACRO = re.compile(r"([pfcбжу])(\d+(?:[.,]\d+)?)")


def _quick_add_float(raw: str) -> float:
    return float(raw.replace(",", "."))


def parse_quick_add(text: str) -> Optional[QuickAdd]:
    """Parse a one-message food entry like ``bread 250 kcal 150 g p8 f3 c47``.

    The expected shape is a food name followed by calories per 100g and the
    serving weight in grams. Units (``kcal``/``g`` and their Russian forms) are
    optional and may fix the value order; without units the first number is
    calories and the second is grams. Optional macro tokens ``p``/``f``/``c``
    (or ``б``/``ж``/``у``) give protein, fat, and carbs per 100g.

    Returns None when the text does not look like a quick-add entry, and
    raises ValidationError when it does but the values are invalid.
    """
    tokens = text.split()
    values: list[tuple[float, Optional[str]]] = []
    macros: dict[str, float] = {}
    pending_unit: Optional[str] = None
    index = len(tokens) - 1
    while index >= 0:
        token = tokens[index].lower().rstrip(".")
        if token in _QUICK_ADD_CALORIE_UNITS or token in _QUICK_ADD_GRAM_UNITS:
            if pending_unit is not None:
                break
            pending_unit = "kcal" if token in _QUICK_ADD_CALORIE_UNITS else "g"
            index -= 1
            continue
        macro_match = _QUICK_ADD_MACRO.fullmatch(token)
        if macro_match is not None and pending_unit is None:
            nutrient = _QUICK_ADD_MACRO_PREFIXES[macro_match.group(1)]
            if nutrient in macros:
                break
            macros[nutrient] = _quick_add_float(macro_match.group(2))
            index -= 1
            continue
        number_match = _QUICK_ADD_NUMBER.fullmatch(token)
        if number_match is None:
            break
        suffix = number_match.group(2)
        if suffix == "":
            kind = pending_unit
        elif pending_unit is not None:
            break
        elif suffix in _QUICK_ADD_CALORIE_UNITS:
            kind = "kcal"
        elif suffix in _QUICK_ADD_GRAM_UNITS:
            kind = "g"
        else:
            break
        values.append((_quick_add_float(number_match.group(1)), kind))
        pending_unit = None
        index -= 1
    if pending_unit is not None or len(values) != 2 or index < 0:
        return None
    values.reverse()

    kinds = [kind for _, kind in values]
    if kinds.count("kcal") > 1 or kinds.count("g") > 1:
        raise ValidationError(
            "Specify calories once and grams once, for example: bread 250 kcal 150 g."
        )
    calories: Optional[float] = None
    grams: Optional[float] = None
    unassigned: list[float] = []
    for value, kind in values:
        if kind == "kcal":
            calories = value
        elif kind == "g":
            grams = value
        else:
            unassigned.append(value)
    if calories is None and unassigned:
        calories = unassigned.pop(0)
    if grams is None and unassigned:
        grams = unassigned.pop(0)
    if calories is None or grams is None:
        return None

    name = normalize_food_name(" ".join(tokens[: index + 1]))
    calories = check_calories_per_100g(calories)
    grams = check_serving_grams(grams)
    protein = macros.get("protein")
    fat = macros.get("fat")
    carbs = macros.get("carbs")
    for label, value in (("Protein", protein), ("Fat", fat), ("Carbs", carbs)):
        if value is not None:
            check_macro(value, label)
    validate_macro_sum(protein, fat, carbs)
    return QuickAdd(name, calories, grams, protein, fat, carbs)


def parse_entry_time(text: str, timezone_name: str, now_utc: int) -> int:
    """Parse ``HH:MM`` (today in the user's timezone) or a full local datetime."""
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("The saved timezone is no longer available.") from exc
    raw = " ".join(text.split())
    parsed: Optional[datetime] = None
    for pattern in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            parsed = datetime.strptime(raw, pattern)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            clock = datetime.strptime(raw, "%H:%M")
        except ValueError as exc:
            raise ValidationError(
                "Enter the time as HH:MM for today, or YYYY-MM-DD HH:MM."
            ) from exc
        local_today = datetime.fromtimestamp(now_utc, UTC).astimezone(zone).date()
        parsed = datetime.combine(local_today, clock.time())
    epoch = int(parsed.replace(tzinfo=zone).astimezone(UTC).timestamp())
    if epoch > now_utc + ENTRY_TIME_GRACE_SECONDS:
        raise ValidationError("The entry time cannot be in the future.")
    if epoch < now_utc - MAX_ENTRY_AGE_SECONDS:
        raise ValidationError("The entry time cannot be more than a year in the past.")
    return epoch


@lru_cache(maxsize=1)
def _timezone_index() -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    full: dict[str, str] = {}
    suffixes: dict[str, list[str]] = {}
    for key in available_timezones():
        full[key.casefold()] = key
        suffix = key.rsplit("/", 1)[-1].casefold()
        suffixes.setdefault(suffix, []).append(key)
    return full, {key: tuple(sorted(values)) for key, values in suffixes.items()}


def canonical_timezone(value: str) -> str:
    candidate = value.strip().replace(" ", "_")
    if not candidate or len(candidate) > 128:
        raise ValidationError("Enter a valid IANA timezone, such as Europe/Moscow.")

    full, suffixes = _timezone_index()
    canonical = full.get(candidate.casefold())
    if canonical is None and "/" not in candidate:
        matches = suffixes.get(candidate.casefold(), ())
        if len(matches) == 1:
            canonical = matches[0]
        elif len(matches) > 1:
            raise ValidationError(
                "That city is ambiguous. Enter its full IANA timezone, such as Europe/London."
            )
    if canonical is None:
        raise ValidationError(
            "Unknown timezone. Try an IANA name such as Europe/Moscow."
        )
    try:
        return ZoneInfo(canonical).key
    except ZoneInfoNotFoundError as exc:
        raise ValidationError(
            "Timezone data is unavailable for that location."
        ) from exc


def period_bounds(
    period: Period, timezone_name: str, now_utc: Optional[datetime] = None
) -> PeriodBounds:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("The saved timezone is no longer available.") from exc

    current = now_utc or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    today = current.astimezone(zone).date()

    if period == Period.TODAY:
        start_date, end_date = today, today + timedelta(days=1)
    elif period == Period.YESTERDAY:
        start_date, end_date = today - timedelta(days=1), today
    elif period == Period.WEEK:
        start_date, end_date = today - timedelta(days=6), today + timedelta(days=1)
    elif period == Period.MONTH:
        start_date = today.replace(day=1)
        end_date = today + timedelta(days=1)
    else:
        raise ValueError(f"Unsupported period: {period}")

    return _bounds_from_dates(zone, start_date, end_date)


def month_bounds(year: int, month: int, timezone_name: str) -> PeriodBounds:
    """Bounds of one local calendar month, for browsing past statistics."""
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("The saved timezone is no longer available.") from exc
    if not 2000 <= year <= 2100 or not 1 <= month <= 12:
        raise ValidationError("Unsupported statistics month.")
    start_date = date(year, month, 1)
    end_date = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return _bounds_from_dates(zone, start_date, end_date)


def _bounds_from_dates(
    zone: ZoneInfo, start_date: date, end_date: date
) -> PeriodBounds:
    start_local = datetime.combine(start_date, time.min, tzinfo=zone)
    end_local = datetime.combine(end_date, time.min, tzinfo=zone)
    return PeriodBounds(
        start_utc=int(start_local.astimezone(UTC).timestamp()),
        end_utc=int(end_local.astimezone(UTC).timestamp()),
        start_local_date=start_date,
        end_local_date=end_date,
    )


def local_date(timestamp_utc: int, timezone_name: str) -> date:
    return (
        datetime.fromtimestamp(timestamp_utc, UTC)
        .astimezone(ZoneInfo(timezone_name))
        .date()
    )
