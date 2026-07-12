from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .database import Database
from .domain import (
    SQLITE_REAL_LIMIT,
    ValidationError,
    canonical_timezone,
    normalize_food_name,
    scale_per_100,
)


UTC = timezone.utc


@dataclass(frozen=True)
class MigrationReport:
    users: int
    entries: int
    favorites: int
    skipped_entries: int
    skipped_favorites: int


def migrate_legacy_database(
    source_path: Path | str, target_path: Path | str
) -> MigrationReport:
    source = Path(source_path).resolve()
    target = Path(target_path).resolve()
    if source == target:
        raise ValueError("Source and target database paths must be different.")
    if not source.is_file():
        raise FileNotFoundError(source)

    with closing(sqlite3.connect(source)) as source_connection:
        source_connection.row_factory = sqlite3.Row
        _validate_legacy_schema(source_connection)
        users = source_connection.execute(
            "SELECT user_id, timezone FROM users ORDER BY user_id"
        ).fetchall()
        entries = source_connection.execute(
            """
            SELECT entry_id, user_id, entry_date, name, calories, grams, protein, fat, carbs
            FROM food_entries ORDER BY entry_id
            """
        ).fetchall()
        favorites = source_connection.execute(
            """
            SELECT favorite_id, user_id, name, calories, protein, fat, carbs
            FROM favorite_foods ORDER BY favorite_id
            """
        ).fetchall()

    database = Database(target)
    database.initialize()
    now = int(time.time())
    imported_users = {
        int(row["user_id"]): _legacy_timezone(row["timezone"]) for row in users
    }
    imported_users.update(
        (int(row["user_id"]), None)
        for row in entries
        if row["user_id"] not in imported_users
    )
    imported_users.update(
        (int(row["user_id"]), None)
        for row in favorites
        if row["user_id"] not in imported_users
    )

    valid_entries = []
    skipped_entries = 0
    for row in entries:
        try:
            valid_entries.append(_validate_legacy_entry(row))
        except (TypeError, ValueError, ValidationError):
            skipped_entries += 1

    valid_favorites = []
    skipped_favorites = 0
    for row in favorites:
        try:
            valid_favorites.append(_validate_legacy_favorite(row))
        except (TypeError, ValueError, ValidationError):
            skipped_favorites += 1

    connection = database._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = sum(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("users", "food_entries", "favorite_foods")
        )
        if existing:
            raise RuntimeError("The target database must be empty.")
        connection.executemany(
            """
            INSERT INTO users(user_id, timezone, created_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?)
            """,
            [
                (user_id, timezone_name, now, now)
                for user_id, timezone_name in imported_users.items()
            ],
        )
        connection.executemany(
            """
            INSERT INTO food_entries(
                entry_id, user_id, eaten_at_utc, name, grams, calories, protein, fat, carbs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            valid_entries,
        )
        connection.executemany(
            """
            INSERT INTO favorite_foods(
                favorite_id, user_id, name, name_key, calories_per_100g,
                protein_per_100g, fat_per_100g, carbs_per_100g,
                created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [values + (now, now) for values in valid_favorites],
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return MigrationReport(
        users=len(imported_users),
        entries=len(valid_entries),
        favorites=len(valid_favorites),
        skipped_entries=skipped_entries,
        skipped_favorites=skipped_favorites,
    )


def _validate_legacy_schema(connection: sqlite3.Connection) -> None:
    required = {
        "users": {"user_id", "timezone"},
        "food_entries": {
            "entry_id",
            "user_id",
            "entry_date",
            "name",
            "calories",
            "grams",
            "protein",
            "fat",
            "carbs",
        },
        "favorite_foods": {
            "favorite_id",
            "user_id",
            "name",
            "calories",
            "protein",
            "fat",
            "carbs",
        },
    }
    for table, columns in required.items():
        actual = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not columns.issubset(actual):
            raise RuntimeError(f"{table} is not a recognized Go database table.")


def _legacy_timezone(value: object) -> Optional[str]:
    if value is None:
        return None
    try:
        return canonical_timezone(str(value))
    except ValidationError:
        return None


def _legacy_timestamp(value: object) -> int:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp())


def _stored_number(value: object, *, positive: bool = False) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed >= SQLITE_REAL_LIMIT:
        raise ValidationError("Invalid stored numeric value")
    if positive and parsed <= 0:
        raise ValidationError("Value must be positive")
    if not positive and parsed < 0:
        raise ValidationError("Value cannot be negative")
    return parsed


def _optional_stored_number(value: object) -> Optional[float]:
    return None if value is None else _stored_number(value)


def _validate_legacy_entry(row: sqlite3.Row) -> tuple[object, ...]:
    name = None if row["name"] is None else normalize_food_name(str(row["name"]))
    return (
        int(row["entry_id"]),
        int(row["user_id"]),
        _legacy_timestamp(row["entry_date"]),
        name,
        _stored_number(row["grams"], positive=True),
        _stored_number(row["calories"]),
        _optional_stored_number(row["protein"]),
        _optional_stored_number(row["fat"]),
        _optional_stored_number(row["carbs"]),
    )


def _validate_legacy_favorite(row: sqlite3.Row) -> tuple[object, ...]:
    name = normalize_food_name(str(row["name"]))
    calories = _stored_number(row["calories"])
    protein = _optional_stored_number(row["protein"])
    fat = _optional_stored_number(row["fat"])
    carbs = _optional_stored_number(row["carbs"])
    scale_per_100(calories, 100.0, protein, fat, carbs)
    return (
        int(row["favorite_id"]),
        int(row["user_id"]),
        name,
        name.casefold(),
        calories,
        protein,
        fat,
        carbs,
    )
