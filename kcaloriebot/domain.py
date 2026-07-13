from __future__ import annotations

import math
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
    logged_days: int = 0
    coverage_total: int = 0
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


def parse_calories(text: str) -> float:
    value = _parse_finite(text, "Calories")
    if value < 0:
        raise ValidationError("Calories cannot be negative.")
    if value >= SQLITE_REAL_LIMIT:
        raise ValidationError("Calories are too large to store.")
    if value > MAX_CALORIES_PER_100G:
        raise ValidationError(
            f"Calories must be no more than {MAX_CALORIES_PER_100G:.0f} per 100g."
        )
    return value


def parse_grams(text: str) -> float:
    value = _parse_finite(text, "Grams")
    if value <= 0:
        raise ValidationError("Grams must be greater than zero.")
    if value >= SQLITE_REAL_LIMIT:
        raise ValidationError("Grams are too large to store.")
    if value > MAX_SERVING_GRAMS:
        raise ValidationError(
            f"Serving weight must be no more than {MAX_SERVING_GRAMS:.0f} grams."
        )
    return value


def parse_macro(text: str, label: str) -> float:
    value = _parse_finite(text, label)
    if not 0 <= value <= 100:
        raise ValidationError(f"{label} must be between 0 and 100 grams per 100g.")
    return value


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
