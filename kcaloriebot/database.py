from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Optional

from .domain import (
    FavoriteFood,
    FoodEntry,
    NotFound,
    NutritionTotals,
    Page,
    Session,
    SessionState,
    StateConflict,
    Stats,
    ValidationError,
    canonical_timezone,
    local_date,
    normalize_food_name,
    scale_per_100,
    validate_macro_sum,
)


SCHEMA_VERSION = 2

SCHEMA = """
BEGIN;

CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    timezone TEXT NULL CHECK (timezone IS NULL OR length(timezone) BETWEEN 1 AND 128),
    created_at_utc INTEGER NOT NULL,
    updated_at_utc INTEGER NOT NULL
);

CREATE TABLE favorite_foods (
    favorite_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
    name_key TEXT NOT NULL,
    calories_per_100g REAL NOT NULL
        CHECK (calories_per_100g >= 0 AND calories_per_100g < 1e308),
    protein_per_100g REAL NULL
        CHECK (protein_per_100g IS NULL OR protein_per_100g BETWEEN 0 AND 100),
    fat_per_100g REAL NULL
        CHECK (fat_per_100g IS NULL OR fat_per_100g BETWEEN 0 AND 100),
    carbs_per_100g REAL NULL
        CHECK (carbs_per_100g IS NULL OR carbs_per_100g BETWEEN 0 AND 100),
    created_at_utc INTEGER NOT NULL,
    updated_at_utc INTEGER NOT NULL,
    CHECK (
        coalesce(protein_per_100g, 0) +
        coalesce(fat_per_100g, 0) +
        coalesce(carbs_per_100g, 0) <= 100.000001
    ),
    UNIQUE (user_id, favorite_id)
);

CREATE TABLE food_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    eaten_at_utc INTEGER NOT NULL CHECK (typeof(eaten_at_utc) = 'integer'),
    name TEXT NULL CHECK (name IS NULL OR length(trim(name)) BETWEEN 1 AND 200),
    grams REAL NOT NULL CHECK (grams > 0 AND grams < 1e308),
    calories REAL NOT NULL CHECK (calories >= 0 AND calories < 1e308),
    protein REAL NULL CHECK (protein IS NULL OR (protein >= 0 AND protein < 1e308)),
    fat REAL NULL CHECK (fat IS NULL OR (fat >= 0 AND fat < 1e308)),
    carbs REAL NULL CHECK (carbs IS NULL OR (carbs >= 0 AND carbs < 1e308))
);

CREATE TABLE sessions (
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    draft_name TEXT NULL CHECK (draft_name IS NULL OR length(trim(draft_name)) BETWEEN 1 AND 200),
    calories_per_100g REAL NULL
        CHECK (calories_per_100g IS NULL OR (calories_per_100g >= 0 AND calories_per_100g < 1e308)),
    serving_grams REAL NULL
        CHECK (serving_grams IS NULL OR (serving_grams > 0 AND serving_grams < 1e308)),
    protein_per_100g REAL NULL
        CHECK (protein_per_100g IS NULL OR protein_per_100g BETWEEN 0 AND 100),
    fat_per_100g REAL NULL
        CHECK (fat_per_100g IS NULL OR fat_per_100g BETWEEN 0 AND 100),
    carbs_per_100g REAL NULL
        CHECK (carbs_per_100g IS NULL OR carbs_per_100g BETWEEN 0 AND 100),
    selected_favorite_id INTEGER NULL,
    selected_nutrient TEXT NULL
        CHECK (selected_nutrient IS NULL OR selected_nutrient IN ('calories', 'protein', 'fat', 'carbs')),
    prompt_pending INTEGER NOT NULL DEFAULT 0 CHECK (prompt_pending IN (0, 1)),
    last_message_id INTEGER NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at_utc INTEGER NOT NULL,
    PRIMARY KEY (user_id, chat_id),
    FOREIGN KEY (user_id, selected_favorite_id)
        REFERENCES favorite_foods(user_id, favorite_id) ON DELETE CASCADE,
    CHECK (
        coalesce(protein_per_100g, 0) +
        coalesce(fat_per_100g, 0) +
        coalesce(carbs_per_100g, 0) <= 100.000001
    )
);

CREATE INDEX food_entries_user_time_idx
    ON food_entries(user_id, eaten_at_utc DESC, entry_id DESC);
CREATE INDEX favorites_user_id_idx
    ON favorite_foods(user_id, favorite_id DESC);
CREATE INDEX favorites_user_name_idx
    ON favorite_foods(user_id, name_key, favorite_id DESC);
CREATE INDEX sessions_updated_idx ON sessions(updated_at_utc);

PRAGMA user_version = 2;
COMMIT;
"""


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if tables:
                    raise RuntimeError(
                        "The database has an unsupported legacy schema. "
                        "Back it up and use a new DATABASE_PATH for the Python version."
                    )
                connection.executescript(SCHEMA)
            elif version == 1:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(sessions)")
                    }
                    if "prompt_pending" not in columns:
                        connection.execute(
                            "ALTER TABLE sessions ADD COLUMN prompt_pending INTEGER "
                            "NOT NULL DEFAULT 0 CHECK (prompt_pending IN (0, 1))"
                        )
                    if "last_message_id" not in columns:
                        connection.execute(
                            "ALTER TABLE sessions ADD COLUMN last_message_id INTEGER NULL"
                        )
                    connection.execute("PRAGMA user_version = 2")
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported database schema version {version}; expected {SCHEMA_VERSION}."
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @staticmethod
    def now_epoch() -> int:
        return int(time.time())

    def foreign_keys_enabled(self) -> bool:
        with self._connect() as connection:
            return bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])

    def ensure_user(self, user_id: int, now_utc: Optional[int] = None) -> None:
        now = self.now_epoch() if now_utc is None else now_utc
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users(user_id, timezone, created_at_utc, updated_at_utc)
                VALUES (?, NULL, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (user_id, now, now),
            )

    def get_timezone(self, user_id: int) -> Optional[str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT timezone FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return None if row is None else row["timezone"]

    def set_timezone(
        self, user_id: int, timezone_name: str, now_utc: Optional[int] = None
    ) -> None:
        timezone_name = canonical_timezone(timezone_name)
        now = self.now_epoch() if now_utc is None else now_utc
        self.ensure_user(user_id, now)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET timezone = ?, updated_at_utc = ? WHERE user_id = ?",
                (timezone_name, now, user_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("User not found")

    def complete_timezone_session(
        self,
        session: Session,
        timezone_name: str,
        now_utc: Optional[int] = None,
    ) -> None:
        if session.state != SessionState.WAIT_TIMEZONE:
            raise StateConflict("Timezone workflow is not active")
        canonical = canonical_timezone(timezone_name)
        now = self.now_epoch() if now_utc is None else now_utc
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_session(connection, session)
            cursor = connection.execute(
                "UPDATE users SET timezone = ?, updated_at_utc = ? WHERE user_id = ?",
                (canonical, now, session.user_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("User not found")
            self._delete_exact_session(connection, session)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def start_session(
        self,
        user_id: int,
        chat_id: int,
        state: SessionState,
        now_utc: Optional[int] = None,
        **values: object,
    ) -> Session:
        now = self.now_epoch() if now_utc is None else now_utc
        self.ensure_user(user_id, now)
        session = Session(
            user_id=user_id,
            chat_id=chat_id,
            state=state,
            updated_at_utc=now,
            **values,
        )
        columns = self._session_columns(session)
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO sessions(
                        user_id, chat_id, state, draft_name, calories_per_100g,
                        serving_grams, protein_per_100g, fat_per_100g, carbs_per_100g,
                        selected_favorite_id, selected_nutrient, prompt_pending,
                        last_message_id, revision, updated_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(user_id, chat_id) DO UPDATE SET
                        state = excluded.state,
                        draft_name = excluded.draft_name,
                        calories_per_100g = excluded.calories_per_100g,
                        serving_grams = excluded.serving_grams,
                        protein_per_100g = excluded.protein_per_100g,
                        fat_per_100g = excluded.fat_per_100g,
                        carbs_per_100g = excluded.carbs_per_100g,
                        selected_favorite_id = excluded.selected_favorite_id,
                        selected_nutrient = excluded.selected_nutrient,
                        prompt_pending = excluded.prompt_pending,
                        last_message_id = excluded.last_message_id,
                        revision = sessions.revision + 1,
                        updated_at_utc = excluded.updated_at_utc
                    """,
                    columns,
                )
            except sqlite3.IntegrityError as exc:
                if session.selected_favorite_id is not None:
                    owned = connection.execute(
                        """
                        SELECT 1 FROM favorite_foods
                        WHERE user_id = ? AND favorite_id = ?
                        """,
                        (session.user_id, session.selected_favorite_id),
                    ).fetchone()
                    if owned is None:
                        raise NotFound("Favorite not found") from exc
                raise
        stored = self.get_session(user_id, chat_id)
        if stored is None:
            raise StateConflict("Session was not created")
        return stored

    def get_session(self, user_id: int, chat_id: int) -> Optional[Session]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            ).fetchone()
        return None if row is None else self._row_to_session(row)

    def update_session(self, previous: Session, updated: Session) -> Session:
        if (previous.user_id, previous.chat_id) != (updated.user_id, updated.chat_id):
            raise ValueError("Session identity cannot change")
        now = updated.updated_at_utc or self.now_epoch()
        values = (
            updated.state.value,
            updated.draft_name,
            updated.calories_per_100g,
            updated.serving_grams,
            updated.protein_per_100g,
            updated.fat_per_100g,
            updated.carbs_per_100g,
            updated.selected_favorite_id,
            updated.selected_nutrient,
            int(updated.prompt_pending),
            updated.last_message_id,
            now,
            previous.user_id,
            previous.chat_id,
            previous.state.value,
            previous.revision,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions SET
                    state = ?, draft_name = ?, calories_per_100g = ?, serving_grams = ?,
                    protein_per_100g = ?, fat_per_100g = ?, carbs_per_100g = ?,
                    selected_favorite_id = ?, selected_nutrient = ?,
                    prompt_pending = ?, last_message_id = ?,
                    revision = revision + 1, updated_at_utc = ?
                WHERE user_id = ? AND chat_id = ? AND state = ? AND revision = ?
                """,
                values,
            )
            if cursor.rowcount != 1:
                raise StateConflict(
                    "The workflow changed; please use the latest prompt."
                )
        return replace(updated, revision=previous.revision + 1, updated_at_utc=now)

    def clear_session(self, user_id: int, chat_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            )

    def complete_food_draft(self, session: Session, eaten_at_utc: int) -> FoodEntry:
        if session.state != SessionState.WAIT_CARBS:
            raise StateConflict("Food draft is not ready to complete")
        workflow_now = self.now_epoch()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE user_id = ? AND chat_id = ? AND state = ? AND revision = ?
                """,
                (
                    session.user_id,
                    session.chat_id,
                    session.state.value,
                    session.revision,
                ),
            ).fetchone()
            if row is None:
                raise StateConflict(
                    "The workflow changed; please use the latest prompt."
                )
            persisted = replace(
                self._row_to_session(row),
                carbs_per_100g=session.carbs_per_100g,
                last_message_id=session.last_message_id,
            )
            if persisted.calories_per_100g is None or persisted.serving_grams is None:
                raise StateConflict("Food draft is incomplete")
            totals = scale_per_100(
                persisted.calories_per_100g,
                persisted.serving_grams,
                persisted.protein_per_100g,
                persisted.fat_per_100g,
                persisted.carbs_per_100g,
            )
            cursor = connection.execute(
                """
                INSERT INTO food_entries(
                    user_id, eaten_at_utc, name, grams, calories, protein, fat, carbs
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persisted.user_id,
                    eaten_at_utc,
                    persisted.draft_name,
                    totals.grams,
                    totals.calories,
                    totals.protein,
                    totals.fat,
                    totals.carbs,
                ),
            )
            entry_id = int(cursor.lastrowid)
            if persisted.draft_name is None:
                self._delete_exact_session(connection, persisted)
            else:
                cursor = connection.execute(
                    """
                    UPDATE sessions SET state = ?, carbs_per_100g = ?,
                        prompt_pending = 1, last_message_id = ?,
                        revision = revision + 1, updated_at_utc = ?
                    WHERE user_id = ? AND chat_id = ? AND state = ? AND revision = ?
                    """,
                    (
                        SessionState.WAIT_SAVE_FAVORITE.value,
                        persisted.carbs_per_100g,
                        persisted.last_message_id,
                        workflow_now,
                        persisted.user_id,
                        persisted.chat_id,
                        persisted.state.value,
                        persisted.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StateConflict("Food draft changed before completion")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return FoodEntry(
            entry_id=entry_id,
            user_id=persisted.user_id,
            eaten_at_utc=eaten_at_utc,
            name=persisted.draft_name,
            nutrition=totals,
        )

    def save_session_as_favorite(
        self, user_id: int, chat_id: int, now_utc: Optional[int] = None
    ) -> FavoriteFood:
        now = self.now_epoch() if now_utc is None else now_utc
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            ).fetchone()
            if row is None or row["state"] != SessionState.WAIT_SAVE_FAVORITE.value:
                raise StateConflict("Favorite draft is not available")
            session = self._row_to_session(row)
            if session.draft_name is None or session.calories_per_100g is None:
                raise StateConflict("Favorite draft is incomplete")
            validate_macro_sum(
                session.protein_per_100g, session.fat_per_100g, session.carbs_per_100g
            )
            favorite_id = self._insert_favorite(
                connection,
                user_id,
                session.draft_name,
                session.calories_per_100g,
                session.protein_per_100g,
                session.fat_per_100g,
                session.carbs_per_100g,
                now,
            )
            self._delete_exact_session(connection, session)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        favorite = self.get_favorite(user_id, favorite_id)
        if favorite is None:
            raise StateConflict("Favorite was not saved")
        return favorite

    def use_selected_favorite(
        self, user_id: int, chat_id: int, grams: float, eaten_at_utc: int
    ) -> FoodEntry:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            ).fetchone()
            if row is None or row["state"] != SessionState.WAIT_FAVORITE_GRAMS.value:
                raise StateConflict("Selected favorite is no longer available")
            session = self._row_to_session(row)
            if session.selected_favorite_id is None:
                raise StateConflict("Selected favorite is missing")
            favorite_row = connection.execute(
                """
                SELECT * FROM favorite_foods
                WHERE user_id = ? AND favorite_id = ?
                """,
                (user_id, session.selected_favorite_id),
            ).fetchone()
            if favorite_row is None:
                raise NotFound("Favorite not found")
            favorite = self._row_to_favorite(favorite_row)
            totals = scale_per_100(
                favorite.calories_per_100g,
                grams,
                favorite.protein_per_100g,
                favorite.fat_per_100g,
                favorite.carbs_per_100g,
            )
            cursor = connection.execute(
                """
                INSERT INTO food_entries(
                    user_id, eaten_at_utc, name, grams, calories, protein, fat, carbs
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    eaten_at_utc,
                    favorite.name,
                    totals.grams,
                    totals.calories,
                    totals.protein,
                    totals.fat,
                    totals.carbs,
                ),
            )
            entry_id = int(cursor.lastrowid)
            self._delete_exact_session(connection, session)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return FoodEntry(entry_id, user_id, eaten_at_utc, favorite.name, totals)

    def complete_favorite_amendment(
        self, user_id: int, chat_id: int, value: float, now_utc: Optional[int] = None
    ) -> FavoriteFood:
        now = self.now_epoch() if now_utc is None else now_utc
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            ).fetchone()
            if (
                row is None
                or row["state"] != SessionState.WAIT_FAVORITE_AMENDMENT.value
            ):
                raise StateConflict("Favorite amendment is no longer active")
            session = self._row_to_session(row)
            if (
                session.selected_favorite_id is None
                or session.selected_nutrient is None
            ):
                raise StateConflict("Favorite amendment context is incomplete")
            favorite_row = connection.execute(
                "SELECT * FROM favorite_foods WHERE user_id = ? AND favorite_id = ?",
                (user_id, session.selected_favorite_id),
            ).fetchone()
            if favorite_row is None:
                raise NotFound("Favorite not found")
            favorite = self._row_to_favorite(favorite_row)
            candidate = {
                "protein": favorite.protein_per_100g,
                "fat": favorite.fat_per_100g,
                "carbs": favorite.carbs_per_100g,
            }
            if session.selected_nutrient in candidate:
                candidate[session.selected_nutrient] = value
            candidate_calories = (
                value
                if session.selected_nutrient == "calories"
                else favorite.calories_per_100g
            )
            scale_per_100(
                candidate_calories,
                100.0,
                candidate["protein"],
                candidate["fat"],
                candidate["carbs"],
            )
            columns = {
                "calories": "calories_per_100g",
                "protein": "protein_per_100g",
                "fat": "fat_per_100g",
                "carbs": "carbs_per_100g",
            }
            column = columns.get(session.selected_nutrient)
            if column is None:
                raise ValidationError("Unsupported nutrient")
            cursor = connection.execute(
                f"""
                UPDATE favorite_foods SET {column} = ?, updated_at_utc = ?
                WHERE user_id = ? AND favorite_id = ?
                """,
                (value, now, user_id, session.selected_favorite_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("Favorite not found")
            self._delete_exact_session(connection, session)
            connection.commit()
            favorite_id = session.selected_favorite_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        updated = self.get_favorite(user_id, favorite_id)
        if updated is None:
            raise NotFound("Favorite not found")
        return updated

    def add_favorite(
        self,
        user_id: int,
        name: str,
        calories_per_100g: float,
        protein_per_100g: Optional[float],
        fat_per_100g: Optional[float],
        carbs_per_100g: Optional[float],
        now_utc: Optional[int] = None,
    ) -> FavoriteFood:
        name = normalize_food_name(name)
        scale_per_100(
            calories_per_100g,
            100.0,
            protein_per_100g,
            fat_per_100g,
            carbs_per_100g,
        )
        now = self.now_epoch() if now_utc is None else now_utc
        validate_macro_sum(protein_per_100g, fat_per_100g, carbs_per_100g)
        with self._connect() as connection:
            favorite_id = self._insert_favorite(
                connection,
                user_id,
                name,
                calories_per_100g,
                protein_per_100g,
                fat_per_100g,
                carbs_per_100g,
                now,
            )
        favorite = self.get_favorite(user_id, favorite_id)
        if favorite is None:
            raise StateConflict("Favorite was not saved")
        return favorite

    def get_favorite(self, user_id: int, favorite_id: int) -> Optional[FavoriteFood]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM favorite_foods WHERE user_id = ? AND favorite_id = ?",
                (user_id, favorite_id),
            ).fetchone()
        return None if row is None else self._row_to_favorite(row)

    def delete_favorite(self, user_id: int, favorite_id: int) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM favorite_foods WHERE user_id = ? AND favorite_id = ?",
                (user_id, favorite_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("Favorite not found")

    def page_favorites(
        self, user_id: int, offset: int = 0, limit: int = 5
    ) -> Page[FavoriteFood]:
        self._validate_page(offset, limit)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM favorite_foods
                WHERE user_id = ?
                ORDER BY favorite_id DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit + 1, offset),
            ).fetchall()
        items = tuple(self._row_to_favorite(row) for row in rows[:limit])
        return Page(items, offset, offset > 0, len(rows) > limit)

    def search_favorites(
        self, user_id: int, query: str, limit: int = 20
    ) -> tuple[FavoriteFood, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        escaped = (
            query.casefold()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM favorite_foods
                WHERE user_id = ? AND name_key LIKE ? ESCAPE '\\'
                ORDER BY name_key, favorite_id DESC
                LIMIT ?
                """,
                (user_id, f"%{escaped}%", limit),
            ).fetchall()
        return tuple(self._row_to_favorite(row) for row in rows)

    def get_entry(self, user_id: int, entry_id: int) -> Optional[FoodEntry]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM food_entries WHERE user_id = ? AND entry_id = ?",
                (user_id, entry_id),
            ).fetchone()
        return None if row is None else self._row_to_entry(row)

    def delete_entry(self, user_id: int, entry_id: int) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM food_entries WHERE user_id = ? AND entry_id = ?",
                (user_id, entry_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("Food entry not found")

    def page_entries(
        self,
        user_id: int,
        start_utc: int,
        end_utc: int,
        offset: int = 0,
        limit: int = 5,
    ) -> Page[FoodEntry]:
        self._validate_page(offset, limit)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM food_entries
                WHERE user_id = ? AND eaten_at_utc >= ? AND eaten_at_utc < ?
                ORDER BY eaten_at_utc DESC, entry_id DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, start_utc, end_utc, limit + 1, offset),
            ).fetchall()
        items = tuple(self._row_to_entry(row) for row in rows[:limit])
        return Page(items, offset, offset > 0, len(rows) > limit)

    def stats(
        self,
        user_id: int,
        start_utc: int,
        end_utc: int,
        timezone_name: str,
        average_by_logged_day: bool = False,
    ) -> Stats:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT eaten_at_utc, calories, protein, fat, carbs
                FROM food_entries
                WHERE user_id = ? AND eaten_at_utc >= ? AND eaten_at_utc < ?
                ORDER BY eaten_at_utc, entry_id
                """,
                (user_id, start_utc, end_utc),
            ).fetchall()
        if not rows:
            return Stats(0, 0.0, None, None, None)

        macro_columns = ("protein", "fat", "carbs")
        totals = {"calories": 0.0, **{column: 0.0 for column in macro_columns}}
        known_macro_entries = {column: 0 for column in macro_columns}
        daily: dict[object, dict[str, float]] = {}
        for row in rows:
            entry_day = local_date(row["eaten_at_utc"], timezone_name)
            bucket = daily.setdefault(
                entry_day,
                {
                    "entries": 0,
                    "calories": 0.0,
                    **{column: 0.0 for column in macro_columns},
                    **{f"{column}_known": 0 for column in macro_columns},
                },
            )
            bucket["entries"] += 1
            bucket["calories"] += row["calories"]
            totals["calories"] += row["calories"]
            for column in macro_columns:
                if row[column] is not None:
                    bucket[column] += row[column]
                    bucket[f"{column}_known"] += 1
                    totals[column] += row[column]
                    known_macro_entries[column] += 1

        macro_values: list[Optional[float]] = []
        macro_coverage: list[int] = []
        if average_by_logged_day:
            for column in macro_columns:
                complete_days = [
                    bucket
                    for bucket in daily.values()
                    if bucket[f"{column}_known"] == bucket["entries"]
                ]
                macro_coverage.append(len(complete_days))
                macro_values.append(
                    None
                    if not complete_days
                    else sum(bucket[column] for bucket in complete_days)
                    / len(complete_days)
                )
            calories = totals["calories"] / len(daily)
        else:
            for column in macro_columns:
                coverage = known_macro_entries[column]
                macro_coverage.append(coverage)
                macro_values.append(None if coverage == 0 else totals[column])
            calories = totals["calories"]

        return Stats(
            entry_count=len(rows),
            calories=calories,
            protein=macro_values[0],
            fat=macro_values[1],
            carbs=macro_values[2],
            logged_days=len(daily),
            coverage_total=len(daily) if average_by_logged_day else len(rows),
            protein_coverage=macro_coverage[0],
            fat_coverage=macro_coverage[1],
            carbs_coverage=macro_coverage[2],
        )

    @staticmethod
    def _validate_page(offset: int, limit: int) -> None:
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

    @staticmethod
    def _session_columns(session: Session) -> tuple[object, ...]:
        return (
            session.user_id,
            session.chat_id,
            session.state.value,
            session.draft_name,
            session.calories_per_100g,
            session.serving_grams,
            session.protein_per_100g,
            session.fat_per_100g,
            session.carbs_per_100g,
            session.selected_favorite_id,
            session.selected_nutrient,
            int(session.prompt_pending),
            session.last_message_id,
            session.updated_at_utc,
        )

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            state=SessionState(row["state"]),
            draft_name=row["draft_name"],
            calories_per_100g=row["calories_per_100g"],
            serving_grams=row["serving_grams"],
            protein_per_100g=row["protein_per_100g"],
            fat_per_100g=row["fat_per_100g"],
            carbs_per_100g=row["carbs_per_100g"],
            selected_favorite_id=row["selected_favorite_id"],
            selected_nutrient=row["selected_nutrient"],
            prompt_pending=bool(row["prompt_pending"]),
            last_message_id=row["last_message_id"],
            revision=row["revision"],
            updated_at_utc=row["updated_at_utc"],
        )

    @staticmethod
    def _row_to_favorite(row: sqlite3.Row) -> FavoriteFood:
        return FavoriteFood(
            favorite_id=row["favorite_id"],
            user_id=row["user_id"],
            name=row["name"],
            calories_per_100g=row["calories_per_100g"],
            protein_per_100g=row["protein_per_100g"],
            fat_per_100g=row["fat_per_100g"],
            carbs_per_100g=row["carbs_per_100g"],
        )

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> FoodEntry:
        return FoodEntry(
            entry_id=row["entry_id"],
            user_id=row["user_id"],
            eaten_at_utc=row["eaten_at_utc"],
            name=row["name"],
            nutrition=NutritionTotals(
                calories=row["calories"],
                grams=row["grams"],
                protein=row["protein"],
                fat=row["fat"],
                carbs=row["carbs"],
            ),
        )

    @staticmethod
    def _insert_favorite(
        connection: sqlite3.Connection,
        user_id: int,
        name: str,
        calories_per_100g: float,
        protein_per_100g: Optional[float],
        fat_per_100g: Optional[float],
        carbs_per_100g: Optional[float],
        now_utc: int,
    ) -> int:
        name = normalize_food_name(name)
        scale_per_100(
            calories_per_100g,
            100.0,
            protein_per_100g,
            fat_per_100g,
            carbs_per_100g,
        )
        cursor = connection.execute(
            """
            INSERT INTO favorite_foods(
                user_id, name, name_key, calories_per_100g, protein_per_100g,
                fat_per_100g, carbs_per_100g, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                name,
                name.casefold(),
                calories_per_100g,
                protein_per_100g,
                fat_per_100g,
                carbs_per_100g,
                now_utc,
                now_utc,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _assert_session(connection: sqlite3.Connection, session: Session) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM sessions
            WHERE user_id = ? AND chat_id = ? AND state = ? AND revision = ?
            """,
            (session.user_id, session.chat_id, session.state.value, session.revision),
        ).fetchone()
        if row is None:
            raise StateConflict("The workflow changed; please use the latest prompt.")

    @staticmethod
    def _delete_exact_session(connection: sqlite3.Connection, session: Session) -> None:
        cursor = connection.execute(
            """
            DELETE FROM sessions
            WHERE user_id = ? AND chat_id = ? AND state = ? AND revision = ?
            """,
            (session.user_id, session.chat_id, session.state.value, session.revision),
        )
        if cursor.rowcount != 1:
            raise StateConflict("The workflow changed; please use the latest prompt.")
