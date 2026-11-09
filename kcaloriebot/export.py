"""CSV export of a user's diary for analysis in a spreadsheet or pandas."""

from __future__ import annotations

import csv
import io
from typing import Iterable, Optional

from .domain import FoodEntry, WeightRecord, local_datetime

FOOD_COLUMNS = (
    "date",
    "time",
    "name",
    "grams",
    "servings",
    "calories",
    "protein",
    "fat",
    "carbs",
)
WEIGHT_COLUMNS = ("date", "time", "weight_kg")


def _number(value: Optional[float]) -> str:
    """Unknown values stay empty so they are not mistaken for zero."""
    return "" if value is None else format(round(value, 2), "g")


def _csv(columns: tuple[str, ...], rows: Iterable[tuple[str, ...]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows(rows)
    # The byte order mark makes Excel read Cyrillic food names as UTF-8.
    return buffer.getvalue().encode("utf-8-sig")


def _local(timestamp_utc: int, timezone_name: str) -> tuple[str, str]:
    moment = local_datetime(timestamp_utc, timezone_name)
    return moment.date().isoformat(), moment.strftime("%H:%M")


def food_csv(entries: Iterable[FoodEntry], timezone_name: str) -> bytes:
    """One row per entry; nutrition is the entry's total, times are local."""
    return _csv(
        FOOD_COLUMNS,
        (
            (
                *_local(entry.eaten_at_utc, timezone_name),
                entry.name or "",
                _number(entry.nutrition.grams),
                _number(entry.nutrition.servings),
                _number(entry.nutrition.calories),
                _number(entry.nutrition.protein),
                _number(entry.nutrition.fat),
                _number(entry.nutrition.carbs),
            )
            for entry in entries
        ),
    )


def weight_csv(weights: Iterable[WeightRecord], timezone_name: str) -> bytes:
    return _csv(
        WEIGHT_COLUMNS,
        (
            (
                *_local(record.measured_at_utc, timezone_name),
                _number(record.weight_kg),
            )
            for record in weights
        ),
    )
