from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Optional

from .domain import (
    UNIT_100G,
    UNIT_SERVING,
    DayStats,
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
    WeightRecord,
    canonical_timezone,
    check_calories_per_serving,
    check_daily_goal,
    check_macro,
    check_macro_per_serving,
    check_calories_per_100g,
    check_serving_grams,
    check_weight_kg,
    local_date,
    normalize_food_name,
    per_unit_from_totals,
    scale_per_100,
    scale_per_serving,
    validate_macro_sum,
)


SCHEMA_VERSION = 5

# The favorite_foods and sessions nutrition columns keep their historical
# *_per_100g names; for unit = 'serving' rows they hold per-serving values.
_FAVORITES_DDL = """
CREATE TABLE {name} (
    favorite_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
    name_key TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT '100g' CHECK (unit IN ('100g', 'serving')),
    serving_grams REAL NULL
        CHECK (serving_grams IS NULL OR (serving_grams > 0 AND serving_grams < 1e308)),
    calories_per_100g REAL NOT NULL
        CHECK (calories_per_100g >= 0 AND calories_per_100g < 1e308),
    protein_per_100g REAL NULL
        CHECK (protein_per_100g IS NULL OR (protein_per_100g >= 0 AND protein_per_100g < 1e308)),
    fat_per_100g REAL NULL
        CHECK (fat_per_100g IS NULL OR (fat_per_100g >= 0 AND fat_per_100g < 1e308)),
    carbs_per_100g REAL NULL
        CHECK (carbs_per_100g IS NULL OR (carbs_per_100g >= 0 AND carbs_per_100g < 1e308)),
    created_at_utc INTEGER NOT NULL,
    updated_at_utc INTEGER NOT NULL,
    CHECK (
        unit = 'serving' OR (
            coalesce(protein_per_100g, 0) <= 100 AND
            coalesce(fat_per_100g, 0) <= 100 AND
            coalesce(carbs_per_100g, 0) <= 100 AND
            coalesce(protein_per_100g, 0) +
            coalesce(fat_per_100g, 0) +
            coalesce(carbs_per_100g, 0) <= 100.000001
        )
    ),
    UNIQUE (user_id, favorite_id)
);
"""

_ENTRIES_DDL = """
CREATE TABLE {name} (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    eaten_at_utc INTEGER NOT NULL CHECK (typeof(eaten_at_utc) = 'integer'),
    name TEXT NULL CHECK (name IS NULL OR length(trim(name)) BETWEEN 1 AND 200),
    grams REAL NULL CHECK (grams IS NULL OR (grams > 0 AND grams < 1e308)),
    servings REAL NULL CHECK (servings IS NULL OR (servings > 0 AND servings < 1e308)),
    calories REAL NOT NULL CHECK (calories >= 0 AND calories < 1e308),
    protein REAL NULL CHECK (protein IS NULL OR (protein >= 0 AND protein < 1e308)),
    fat REAL NULL CHECK (fat IS NULL OR (fat >= 0 AND fat < 1e308)),
    carbs REAL NULL CHECK (carbs IS NULL OR (carbs >= 0 AND carbs < 1e308)),
    CHECK (grams IS NOT NULL OR servings IS NOT NULL)
);
"""

_SESSIONS_DDL = """
CREATE TABLE {name} (
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    draft_name TEXT NULL CHECK (draft_name IS NULL OR length(trim(draft_name)) BETWEEN 1 AND 200),
    draft_unit TEXT NOT NULL DEFAULT '100g' CHECK (draft_unit IN ('100g', 'serving')),
    draft_servings REAL NULL
        CHECK (draft_servings IS NULL OR (draft_servings > 0 AND draft_servings < 1e308)),
    calories_per_100g REAL NULL
        CHECK (calories_per_100g IS NULL OR (calories_per_100g >= 0 AND calories_per_100g < 1e308)),
    serving_grams REAL NULL
        CHECK (serving_grams IS NULL OR (serving_grams > 0 AND serving_grams < 1e308)),
    protein_per_100g REAL NULL
        CHECK (protein_per_100g IS NULL OR (protein_per_100g >= 0 AND protein_per_100g < 1e308)),
    fat_per_100g REAL NULL
        CHECK (fat_per_100g IS NULL OR (fat_per_100g >= 0 AND fat_per_100g < 1e308)),
    carbs_per_100g REAL NULL
        CHECK (carbs_per_100g IS NULL OR (carbs_per_100g >= 0 AND carbs_per_100g < 1e308)),
    selected_favorite_id INTEGER NULL,
    selected_nutrient TEXT NULL
        CHECK (selected_nutrient IS NULL OR selected_nutrient IN ('calories', 'protein', 'fat', 'carbs')),
    selected_entry_id INTEGER NULL,
    prompt_pending INTEGER NOT NULL DEFAULT 0 CHECK (prompt_pending IN (0, 1)),
    last_message_id INTEGER NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at_utc INTEGER NOT NULL,
    PRIMARY KEY (user_id, chat_id),
    FOREIGN KEY (user_id, selected_favorite_id)
        REFERENCES favorite_foods(user_id, favorite_id) ON DELETE CASCADE
);
"""

_WEIGHTS_DDL = """
CREATE TABLE {name} (
    weight_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    measured_at_utc INTEGER NOT NULL CHECK (typeof(measured_at_utc) = 'integer'),
    weight_kg REAL NOT NULL CHECK (weight_kg > 0 AND weight_kg <= 1000)
);
"""

_INDEXES_SQL = """
CREATE INDEX food_entries_user_time_idx
    ON food_entries(user_id, eaten_at_utc DESC, entry_id DESC);
CREATE INDEX favorites_user_id_idx
    ON favorite_foods(user_id, favorite_id DESC);
CREATE INDEX favorites_user_name_idx
    ON favorite_foods(user_id, name_key, favorite_id DESC);
CREATE INDEX sessions_updated_idx ON sessions(updated_at_utc);
CREATE INDEX weights_user_time_idx
    ON weights(user_id, measured_at_utc DESC, weight_id DESC);
"""

SCHEMA = (
    """
BEGIN;

CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    timezone TEXT NULL CHECK (timezone IS NULL OR length(timezone) BETWEEN 1 AND 128),
    daily_calorie_goal REAL NULL CHECK (daily_calorie_goal IS NULL OR (daily_calorie_goal > 0 AND daily_calorie_goal <= 50000)),
    created_at_utc INTEGER NOT NULL,
    updated_at_utc INTEGER NOT NULL
);
"""
    + _FAVORITES_DDL.format(name="favorite_foods")
    + _ENTRIES_DDL.format(name="food_entries")
    + _SESSIONS_DDL.format(name="sessions")
    + _WEIGHTS_DDL.format(name="weights")
    + _INDEXES_SQL
    + """
PRAGMA user_version = 4;
COMMIT;
"""
)

# v3 -> v4 rebuilds tables whose CHECK constraints changed. It runs with
# foreign keys off; new tables are created under a suffix, filled, and swapped
# in so foreign-key clauses in other tables keep referencing the right names.
_MIGRATE_V3_TO_V4 = (
    "BEGIN;"
    + _ENTRIES_DDL.format(name="food_entries_v4")
    + """
INSERT INTO food_entries_v4(
    entry_id, user_id, eaten_at_utc, name, grams, servings,
    calories, protein, fat, carbs
)
SELECT entry_id, user_id, eaten_at_utc, name, grams, NULL,
       calories, protein, fat, carbs
FROM food_entries;
DROP TABLE food_entries;
ALTER TABLE food_entries_v4 RENAME TO food_entries;
"""
    + _FAVORITES_DDL.format(name="favorite_foods_v4")
    + """
INSERT INTO favorite_foods_v4(
    favorite_id, user_id, name, name_key, unit, serving_grams,
    calories_per_100g, protein_per_100g, fat_per_100g, carbs_per_100g,
    created_at_utc, updated_at_utc
)
SELECT favorite_id, user_id, name, name_key, '100g', NULL,
       calories_per_100g, protein_per_100g, fat_per_100g, carbs_per_100g,
       created_at_utc, updated_at_utc
FROM favorite_foods;
DROP TABLE favorite_foods;
ALTER TABLE favorite_foods_v4 RENAME TO favorite_foods;
"""
    + _SESSIONS_DDL.format(name="sessions_v4")
    + """
INSERT INTO sessions_v4(
    user_id, chat_id, state, draft_name, draft_unit, draft_servings,
    calories_per_100g, serving_grams, protein_per_100g, fat_per_100g,
    carbs_per_100g, selected_favorite_id, selected_nutrient, selected_entry_id,
    prompt_pending, last_message_id, revision, updated_at_utc
)
SELECT user_id, chat_id, state, draft_name, '100g', NULL,
       calories_per_100g, serving_grams, protein_per_100g, fat_per_100g,
       carbs_per_100g, selected_favorite_id, selected_nutrient, selected_entry_id,
       prompt_pending, last_message_id, revision, updated_at_utc
FROM sessions;
DROP TABLE sessions;
ALTER TABLE sessions_v4 RENAME TO sessions;
"""
    + _WEIGHTS_DDL.format(name="weights")
    + _INDEXES_SQL
    + """
PRAGMA user_version = 4;
COMMIT;
"""
)


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
                version = 4
            if version in (1, 2):
                self._upgrade_v1_v2_to_v3(connection)
                version = 3
            if version == 3:
                self._upgrade_v3_to_v4(connection)
                version = 4
            if version == 4:
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE sessions ADD COLUMN return_day TEXT NULL;
                    ALTER TABLE sessions ADD COLUMN return_offset INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE sessions ADD COLUMN selected_weight_id INTEGER NULL;
                    ALTER TABLE sessions ADD COLUMN prompt_text TEXT NULL;
                    PRAGMA user_version = 5;
                    COMMIT;
                """)
                version = 5
            if version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported database schema version {version}; expected {SCHEMA_VERSION}."
                )

    @staticmethod
    def _upgrade_v1_v2_to_v3(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            session_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "prompt_pending" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN prompt_pending INTEGER "
                    "NOT NULL DEFAULT 0 CHECK (prompt_pending IN (0, 1))"
                )
            if "last_message_id" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN last_message_id INTEGER NULL"
                )
            if "selected_entry_id" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN selected_entry_id INTEGER NULL"
                )
            user_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(users)")
            }
            if "daily_calorie_goal" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN daily_calorie_goal REAL "
                    "NULL CHECK (daily_calorie_goal IS NULL OR "
                    "(daily_calorie_goal > 0 AND daily_calorie_goal <= 50000))"
                )
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _upgrade_v3_to_v4(connection: sqlite3.Connection) -> None:
        # Table rebuilds need foreign keys off, and legacy rename semantics so
        # swapping the new tables in does not rewrite other tables' foreign-key
        # clauses. Both pragmas must be set outside a transaction.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA legacy_alter_table = ON")
        try:
            connection.executescript(_MIGRATE_V3_TO_V4)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "Schema migration produced foreign key violations; "
                    "restore the pre-update backup."
                )
        finally:
            connection.execute("PRAGMA legacy_alter_table = OFF")
            connection.execute("PRAGMA foreign_keys = ON")

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

    def get_daily_goal(self, user_id: int) -> Optional[float]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT daily_calorie_goal FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return None if row is None else row["daily_calorie_goal"]

    def set_daily_goal(
        self, user_id: int, goal: Optional[float], now_utc: Optional[int] = None
    ) -> None:
        if goal is not None:
            check_daily_goal(goal)
        now = self.now_epoch() if now_utc is None else now_utc
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET daily_calorie_goal = ?, updated_at_utc = ? "
                "WHERE user_id = ?",
                (goal, now, user_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("User not found")

    def complete_goal_session(
        self,
        session: Session,
        goal: Optional[float],
        now_utc: Optional[int] = None,
    ) -> None:
        if session.state != SessionState.WAIT_GOAL:
            raise StateConflict("Goal workflow is not active")
        if goal is not None:
            check_daily_goal(goal)
        now = self.now_epoch() if now_utc is None else now_utc
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_session(connection, session)
            cursor = connection.execute(
                "UPDATE users SET daily_calorie_goal = ?, updated_at_utc = ? "
                "WHERE user_id = ?",
                (goal, now, session.user_id),
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
                        user_id, chat_id, state, draft_name, draft_unit,
                        draft_servings, calories_per_100g, serving_grams,
                        protein_per_100g, fat_per_100g, carbs_per_100g,
                        selected_favorite_id, selected_nutrient, selected_entry_id,
                        prompt_pending, last_message_id, revision, updated_at_utc,
                        return_day, return_offset, selected_weight_id, prompt_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, chat_id) DO UPDATE SET
                        state = excluded.state,
                        draft_name = excluded.draft_name,
                        draft_unit = excluded.draft_unit,
                        draft_servings = excluded.draft_servings,
                        calories_per_100g = excluded.calories_per_100g,
                        serving_grams = excluded.serving_grams,
                        protein_per_100g = excluded.protein_per_100g,
                        fat_per_100g = excluded.fat_per_100g,
                        carbs_per_100g = excluded.carbs_per_100g,
                        selected_favorite_id = excluded.selected_favorite_id,
                        selected_nutrient = excluded.selected_nutrient,
                        selected_entry_id = excluded.selected_entry_id,
                        prompt_pending = excluded.prompt_pending,
                        last_message_id = excluded.last_message_id,
                        revision = sessions.revision + 1,
                        updated_at_utc = excluded.updated_at_utc,
                        return_day = excluded.return_day,
                        return_offset = excluded.return_offset,
                        selected_weight_id = excluded.selected_weight_id,
                        prompt_text = excluded.prompt_text
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
            updated.draft_unit,
            updated.draft_servings,
            updated.calories_per_100g,
            updated.serving_grams,
            updated.protein_per_100g,
            updated.fat_per_100g,
            updated.carbs_per_100g,
            updated.selected_favorite_id,
            updated.selected_nutrient,
            updated.selected_entry_id,
            int(updated.prompt_pending),
            updated.last_message_id,
            updated.return_day,
            updated.return_offset,
            updated.selected_weight_id,
            updated.prompt_text,
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
                    state = ?, draft_name = ?, draft_unit = ?, draft_servings = ?,
                    calories_per_100g = ?, serving_grams = ?,
                    protein_per_100g = ?, fat_per_100g = ?, carbs_per_100g = ?,
                    selected_favorite_id = ?, selected_nutrient = ?,
                    selected_entry_id = ?, prompt_pending = ?, last_message_id = ?,
                    return_day = ?, return_offset = ?, selected_weight_id = ?, prompt_text = ?,
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
            if persisted.calories_per_100g is None:
                raise StateConflict("Food draft is incomplete")
            if persisted.draft_unit == UNIT_SERVING:
                if persisted.draft_servings is None:
                    raise StateConflict("Food draft is incomplete")
                totals = scale_per_serving(
                    persisted.calories_per_100g,
                    persisted.draft_servings,
                    persisted.protein_per_100g,
                    persisted.fat_per_100g,
                    persisted.carbs_per_100g,
                )
            else:
                if persisted.serving_grams is None:
                    raise StateConflict("Food draft is incomplete")
                totals = scale_per_100(
                    persisted.calories_per_100g,
                    persisted.serving_grams,
                    persisted.protein_per_100g,
                    persisted.fat_per_100g,
                    persisted.carbs_per_100g,
                )
            entry_id = self._insert_entry(
                connection,
                persisted.user_id,
                eaten_at_utc,
                persisted.draft_name,
                totals,
            )
            self._delete_exact_session(connection, persisted)
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
        self, user_id: int, chat_id: int, amount: float, eaten_at_utc: int
    ) -> FoodEntry:
        """Log the session's selected favorite.

        ``amount`` is a weight in grams for per-100g favorites and a serving
        count (0.5, 1, 2, ...) for per-serving favorites.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            ).fetchone()
            expected_states = {
                SessionState.WAIT_FAVORITE_GRAMS.value,
                SessionState.WAIT_FAVORITE_SERVINGS.value,
            }
            if row is None or row["state"] not in expected_states:
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
            if favorite.unit == UNIT_SERVING:
                totals = scale_per_serving(
                    favorite.calories_per_100g,
                    amount,
                    favorite.protein_per_100g,
                    favorite.fat_per_100g,
                    favorite.carbs_per_100g,
                    serving_grams=favorite.serving_grams,
                )
            else:
                totals = scale_per_100(
                    favorite.calories_per_100g,
                    amount,
                    favorite.protein_per_100g,
                    favorite.fat_per_100g,
                    favorite.carbs_per_100g,
                )
            entry_id = self._insert_entry(
                connection, user_id, eaten_at_utc, favorite.name, totals
            )
            self._delete_exact_session(connection, session)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return FoodEntry(entry_id, user_id, eaten_at_utc, favorite.name, totals)

    def use_selected_entry(
        self,
        user_id: int,
        chat_id: int,
        amount: Optional[float],
        eaten_at_utc: int,
    ) -> FoodEntry:
        """Log the session's selected entry again; None repeats the last amount.

        ``amount`` is a weight in grams for gram-based entries and a serving
        count for serving-based entries.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sessions WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            ).fetchone()
            if row is None or row["state"] != SessionState.WAIT_RECENT_GRAMS.value:
                raise StateConflict("Selected food is no longer available")
            session = self._row_to_session(row)
            if session.selected_entry_id is None:
                raise StateConflict("Selected food is missing")
            source_row = connection.execute(
                "SELECT * FROM food_entries WHERE user_id = ? AND entry_id = ?",
                (user_id, session.selected_entry_id),
            ).fetchone()
            if source_row is None:
                raise NotFound("Food entry not found")
            source = self._row_to_entry(source_row)
            totals = self._rescaled_totals(source.nutrition, amount)
            entry_id = self._insert_entry(
                connection, user_id, eaten_at_utc, source.name, totals
            )
            self._delete_exact_session(connection, session)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return FoodEntry(entry_id, user_id, eaten_at_utc, source.name, totals)

    @staticmethod
    def _rescaled_totals(
        source: NutritionTotals, amount: Optional[float]
    ) -> NutritionTotals:
        """Scale a stored entry's nutrition to a new amount in its own unit."""
        unit, calories, protein, fat, carbs = per_unit_from_totals(source)
        if unit == UNIT_SERVING:
            assert source.servings is not None
            servings = source.servings if amount is None else amount
            serving_grams = (
                None if source.grams is None else source.grams / source.servings
            )
            return scale_per_serving(
                calories, servings, protein, fat, carbs, serving_grams=serving_grams
            )
        grams = source.grams if amount is None else amount
        assert grams is not None
        return scale_per_100(calories, grams, protein, fat, carbs)

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
            self._validate_favorite_values(
                favorite.unit,
                candidate_calories,
                candidate["protein"],
                candidate["fat"],
                candidate["carbs"],
                favorite.serving_grams,
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

    def find_favorite_by_name(self, user_id: int, name: str) -> Optional[FavoriteFood]:
        """Return the newest favorite whose name matches, ignoring case."""
        key = normalize_food_name(name).casefold()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM favorite_foods
                WHERE user_id = ? AND name_key = ?
                ORDER BY favorite_id DESC
                LIMIT 1
                """,
                (user_id, key),
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

    def add_entry(
        self,
        user_id: int,
        eaten_at_utc: int,
        name: Optional[str],
        calories_per_100g: float,
        grams: float,
        protein_per_100g: Optional[float] = None,
        fat_per_100g: Optional[float] = None,
        carbs_per_100g: Optional[float] = None,
    ) -> FoodEntry:
        clean_name = None if name is None else normalize_food_name(name)
        totals = scale_per_100(
            calories_per_100g, grams, protein_per_100g, fat_per_100g, carbs_per_100g
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO food_entries(
                    user_id, eaten_at_utc, name, grams, calories, protein, fat, carbs
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    eaten_at_utc,
                    clean_name,
                    totals.grams,
                    totals.calories,
                    totals.protein,
                    totals.fat,
                    totals.carbs,
                ),
            )
            entry_id = int(cursor.lastrowid)
        return FoodEntry(entry_id, user_id, eaten_at_utc, clean_name, totals)

    def log_favorite(
        self, user_id: int, favorite_id: int, amount: float, eaten_at_utc: int
    ) -> FoodEntry:
        """Log an owned favorite without modifying an active chat workflow."""
        favorite = self.get_favorite(user_id, favorite_id)
        if favorite is None:
            raise NotFound("Favorite not found")
        values = (
            favorite.calories_per_100g,
            amount,
            favorite.protein_per_100g,
            favorite.fat_per_100g,
            favorite.carbs_per_100g,
        )
        totals = (
            scale_per_serving(*values, serving_grams=favorite.serving_grams)
            if favorite.unit == UNIT_SERVING
            else scale_per_100(*values)
        )
        with self._connect() as connection:
            entry_id = self._insert_entry(
                connection, user_id, eaten_at_utc, favorite.name, totals
            )
        return FoodEntry(entry_id, user_id, eaten_at_utc, favorite.name, totals)

    def add_favorite_from_entry(
        self, user_id: int, entry_id: int, now_utc: Optional[int] = None
    ) -> tuple[FavoriteFood, bool]:
        """Save an entry's food as a favorite; returns (favorite, created).

        A favorite whose name matches an existing one (ignoring case) is
        updated in place instead of duplicated.
        """
        now = self.now_epoch() if now_utc is None else now_utc
        entry = self.get_entry(user_id, entry_id)
        if entry is None:
            raise NotFound("Food entry not found")
        if entry.name is None:
            raise ValidationError("Name the entry first so it can become a favorite.")
        unit, calories, protein, fat, carbs = per_unit_from_totals(entry.nutrition)
        serving_grams = None
        if unit == UNIT_SERVING and entry.nutrition.grams is not None:
            assert entry.nutrition.servings is not None
            serving_grams = entry.nutrition.grams / entry.nutrition.servings
        self._validate_favorite_values(
            unit, calories, protein, fat, carbs, serving_grams
        )
        existing = self.find_favorite_by_name(user_id, entry.name)
        with self._connect() as connection:
            if existing is None:
                favorite_id = self._insert_favorite(
                    connection,
                    user_id,
                    entry.name,
                    calories,
                    protein,
                    fat,
                    carbs,
                    now,
                    unit=unit,
                    serving_grams=serving_grams,
                )
            else:
                favorite_id = existing.favorite_id
                cursor = connection.execute(
                    """
                    UPDATE favorite_foods
                    SET unit = ?, serving_grams = ?, calories_per_100g = ?,
                        protein_per_100g = ?, fat_per_100g = ?, carbs_per_100g = ?,
                        updated_at_utc = ?
                    WHERE user_id = ? AND favorite_id = ?
                    """,
                    (
                        unit,
                        serving_grams,
                        calories,
                        protein,
                        fat,
                        carbs,
                        now,
                        user_id,
                        favorite_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise NotFound("Favorite not found")
        favorite = self.get_favorite(user_id, favorite_id)
        if favorite is None:
            raise StateConflict("Favorite was not saved")
        return favorite, existing is None

    def convert_favorite_to_serving(
        self,
        user_id: int,
        chat_id: int,
        serving_grams: float,
        now_utc: Optional[int] = None,
    ) -> FavoriteFood:
        """Convert the session's selected per-100g favorite to per-serving."""
        check_serving_grams(serving_grams)
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
                or row["state"] != SessionState.WAIT_FAVORITE_TO_SERVING.value
            ):
                raise StateConflict("Favorite conversion is no longer active")
            session = self._row_to_session(row)
            if session.selected_favorite_id is None:
                raise StateConflict("Favorite conversion context is incomplete")
            favorite_row = connection.execute(
                "SELECT * FROM favorite_foods WHERE user_id = ? AND favorite_id = ?",
                (user_id, session.selected_favorite_id),
            ).fetchone()
            if favorite_row is None:
                raise NotFound("Favorite not found")
            favorite = self._row_to_favorite(favorite_row)
            if favorite.unit != UNIT_100G:
                raise StateConflict("The favorite is already serving-based")
            factor = serving_grams / 100.0

            def converted(value: Optional[float]) -> Optional[float]:
                return None if value is None else value * factor

            calories = favorite.calories_per_100g * factor
            protein = converted(favorite.protein_per_100g)
            fat = converted(favorite.fat_per_100g)
            carbs = converted(favorite.carbs_per_100g)
            self._validate_favorite_values(
                UNIT_SERVING, calories, protein, fat, carbs, serving_grams
            )
            cursor = connection.execute(
                """
                UPDATE favorite_foods
                SET unit = ?, serving_grams = ?, calories_per_100g = ?,
                    protein_per_100g = ?, fat_per_100g = ?, carbs_per_100g = ?,
                    updated_at_utc = ?
                WHERE user_id = ? AND favorite_id = ?
                """,
                (
                    UNIT_SERVING,
                    serving_grams,
                    calories,
                    protein,
                    fat,
                    carbs,
                    now,
                    user_id,
                    session.selected_favorite_id,
                ),
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

    def complete_weight_session(
        self,
        session: Session,
        weight_kg: float,
        measured_at_utc: int,
    ) -> WeightRecord:
        if session.state != SessionState.WAIT_WEIGHT:
            raise StateConflict("Weight workflow is not active")
        check_weight_kg(weight_kg)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_session(connection, session)
            cursor = connection.execute(
                """
                INSERT INTO weights(user_id, measured_at_utc, weight_kg)
                VALUES (?, ?, ?)
                """,
                (session.user_id, measured_at_utc, weight_kg),
            )
            weight_id = int(cursor.lastrowid)
            self._delete_exact_session(connection, session)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return WeightRecord(weight_id, session.user_id, measured_at_utc, weight_kg)

    def add_weight(
        self, user_id: int, measured_at_utc: int, weight_kg: float
    ) -> WeightRecord:
        check_weight_kg(weight_kg)
        self.ensure_user(user_id)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO weights(user_id, measured_at_utc, weight_kg)
                VALUES (?, ?, ?)
                """,
                (user_id, measured_at_utc, weight_kg),
            )
            weight_id = int(cursor.lastrowid)
        return WeightRecord(weight_id, user_id, measured_at_utc, weight_kg)

    def get_weight(self, user_id: int, weight_id: int) -> Optional[WeightRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM weights WHERE user_id = ? AND weight_id = ?",
                (user_id, weight_id),
            ).fetchone()
        return None if row is None else self._row_to_weight(row)

    def delete_weight(self, user_id: int, weight_id: int) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM weights WHERE user_id = ? AND weight_id = ?",
                (user_id, weight_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("Weight measurement not found")

    def complete_weight_edit(self, session: Session, weight_kg: float) -> WeightRecord:
        if (
            session.state != SessionState.WAIT_WEIGHT_EDIT
            or session.selected_weight_id is None
        ):
            raise StateConflict("Weight editing is no longer active")
        check_weight_kg(weight_kg)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_session(connection, session)
            row = connection.execute(
                "SELECT * FROM weights WHERE user_id = ? AND weight_id = ?",
                (session.user_id, session.selected_weight_id),
            ).fetchone()
            if row is None:
                raise NotFound("Weight measurement not found")
            connection.execute(
                "UPDATE weights SET weight_kg = ? WHERE user_id = ? AND weight_id = ?",
                (weight_kg, session.user_id, session.selected_weight_id),
            )
            self._delete_exact_session(connection, session)
            connection.commit()
            return replace(self._row_to_weight(row), weight_kg=weight_kg)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def latest_weight(self, user_id: int) -> Optional[WeightRecord]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM weights
                WHERE user_id = ?
                ORDER BY measured_at_utc DESC, weight_id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return None if row is None else self._row_to_weight(row)

    def average_weight(
        self, user_id: int, start_utc: int, end_utc: int
    ) -> Optional[float]:
        """Mean of all measurements in [start_utc, end_utc), or None."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT AVG(weight_kg) AS average FROM weights
                WHERE user_id = ? AND measured_at_utc >= ? AND measured_at_utc < ?
                """,
                (user_id, start_utc, end_utc),
            ).fetchone()
        return row["average"]

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

    def update_entry_amount(
        self, user_id: int, chat_id: int, amount: float, now_utc: Optional[int] = None
    ) -> FoodEntry:
        """Re-scale the session's selected entry to a new amount in its unit.

        ``amount`` is a weight in grams for gram-based entries and a serving
        count for serving-based entries.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            session, entry = self._selected_entry_session(
                connection, user_id, chat_id, SessionState.WAIT_ENTRY_GRAMS
            )
            totals = self._rescaled_totals(entry.nutrition, amount)
            self._write_entry_totals(connection, user_id, entry.entry_id, totals)
            self._delete_exact_session(connection, session)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return FoodEntry(
            entry.entry_id, user_id, entry.eaten_at_utc, entry.name, totals
        )

    def update_entry_name(
        self, user_id: int, chat_id: int, name: str, now_utc: Optional[int] = None
    ) -> FoodEntry:
        """Rename the session's selected entry."""
        clean_name = normalize_food_name(name)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            session, entry = self._selected_entry_session(
                connection, user_id, chat_id, SessionState.WAIT_ENTRY_NAME
            )
            cursor = connection.execute(
                "UPDATE food_entries SET name = ? WHERE user_id = ? AND entry_id = ?",
                (clean_name, user_id, entry.entry_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("Food entry not found")
            self._delete_exact_session(connection, session)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return FoodEntry(
            entry.entry_id, user_id, entry.eaten_at_utc, clean_name, entry.nutrition
        )

    def update_entry_field(
        self, user_id: int, chat_id: int, value: float, now_utc: Optional[int] = None
    ) -> FoodEntry:
        """Set one nutrient of the session's selected entry.

        ``value`` is per 100g for gram-based entries and per one serving for
        serving-based entries; the stored totals are re-scaled accordingly.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            session, entry = self._selected_entry_session(
                connection, user_id, chat_id, SessionState.WAIT_ENTRY_AMENDMENT
            )
            nutrient = session.selected_nutrient
            if nutrient not in {"calories", "protein", "fat", "carbs"}:
                raise StateConflict("Entry amendment context is incomplete")
            unit, calories, protein, fat, carbs = per_unit_from_totals(entry.nutrition)
            per_unit = {
                "calories": calories,
                "protein": protein,
                "fat": fat,
                "carbs": carbs,
            }
            per_unit[nutrient] = value
            if nutrient == "calories":
                if unit == UNIT_SERVING:
                    check_calories_per_serving(value)
                else:
                    check_calories_per_100g(value)
            elif unit == UNIT_SERVING:
                check_macro_per_serving(value, nutrient.title())
            else:
                check_macro(value, nutrient.title())
            if unit == UNIT_SERVING:
                assert entry.nutrition.servings is not None
                serving_grams = (
                    None
                    if entry.nutrition.grams is None
                    else entry.nutrition.grams / entry.nutrition.servings
                )
                totals = scale_per_serving(
                    per_unit["calories"],
                    entry.nutrition.servings,
                    per_unit["protein"],
                    per_unit["fat"],
                    per_unit["carbs"],
                    serving_grams=serving_grams,
                )
            else:
                assert entry.nutrition.grams is not None
                totals = scale_per_100(
                    per_unit["calories"],
                    entry.nutrition.grams,
                    per_unit["protein"],
                    per_unit["fat"],
                    per_unit["carbs"],
                )
            self._write_entry_totals(connection, user_id, entry.entry_id, totals)
            self._delete_exact_session(connection, session)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return FoodEntry(
            entry.entry_id, user_id, entry.eaten_at_utc, entry.name, totals
        )

    @staticmethod
    def _write_entry_totals(
        connection: sqlite3.Connection,
        user_id: int,
        entry_id: int,
        totals: NutritionTotals,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE food_entries
            SET grams = ?, servings = ?, calories = ?, protein = ?, fat = ?, carbs = ?
            WHERE user_id = ? AND entry_id = ?
            """,
            (
                totals.grams,
                totals.servings,
                totals.calories,
                totals.protein,
                totals.fat,
                totals.carbs,
                user_id,
                entry_id,
            ),
        )
        if cursor.rowcount != 1:
            raise NotFound("Food entry not found")

    def update_entry_time(
        self,
        user_id: int,
        chat_id: int,
        eaten_at_utc: int,
        now_utc: Optional[int] = None,
    ) -> FoodEntry:
        """Move the session's selected entry to a different timestamp."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            session, entry = self._selected_entry_session(
                connection, user_id, chat_id, SessionState.WAIT_ENTRY_TIME
            )
            cursor = connection.execute(
                """
                UPDATE food_entries SET eaten_at_utc = ?
                WHERE user_id = ? AND entry_id = ?
                """,
                (eaten_at_utc, user_id, entry.entry_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("Food entry not found")
            self._delete_exact_session(connection, session)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return FoodEntry(
            entry.entry_id, user_id, eaten_at_utc, entry.name, entry.nutrition
        )

    def _selected_entry_session(
        self,
        connection: sqlite3.Connection,
        user_id: int,
        chat_id: int,
        expected_state: SessionState,
    ) -> tuple[Session, FoodEntry]:
        row = connection.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        ).fetchone()
        if row is None or row["state"] != expected_state.value:
            raise StateConflict("Entry amendment is no longer active")
        session = self._row_to_session(row)
        if session.selected_entry_id is None:
            raise StateConflict("Entry amendment context is incomplete")
        entry_row = connection.execute(
            "SELECT * FROM food_entries WHERE user_id = ? AND entry_id = ?",
            (user_id, session.selected_entry_id),
        ).fetchone()
        if entry_row is None:
            raise NotFound("Food entry not found")
        return session, self._row_to_entry(entry_row)

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

    def recent_entry_templates(
        self, user_id: int, limit: int = 10
    ) -> tuple[FoodEntry, ...]:
        """Return the newest named entries, one per distinct casefolded name."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM food_entries
                WHERE user_id = ? AND name IS NOT NULL
                ORDER BY entry_id DESC
                LIMIT 200
                """,
                (user_id,),
            ).fetchall()
        templates: list[FoodEntry] = []
        seen: set[str] = set()
        for row in rows:
            entry = self._row_to_entry(row)
            key = (entry.name or "").casefold()
            if key in seen:
                continue
            seen.add(key)
            templates.append(entry)
            if len(templates) == limit:
                break
        return tuple(templates)

    def stats(self, user_id: int, start_utc: int, end_utc: int) -> Stats:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS entry_count,
                       COALESCE(SUM(calories), 0.0) AS calories,
                       SUM(protein) AS protein, COUNT(protein) AS protein_coverage,
                       SUM(fat) AS fat, COUNT(fat) AS fat_coverage,
                       SUM(carbs) AS carbs, COUNT(carbs) AS carbs_coverage
                FROM food_entries
                WHERE user_id = ? AND eaten_at_utc >= ? AND eaten_at_utc < ?
                """,
                (user_id, start_utc, end_utc),
            ).fetchone()
        return Stats(
            entry_count=row["entry_count"],
            calories=row["calories"],
            protein=row["protein"],
            fat=row["fat"],
            carbs=row["carbs"],
            coverage_total=row["entry_count"],
            protein_coverage=row["protein_coverage"],
            fat_coverage=row["fat_coverage"],
            carbs_coverage=row["carbs_coverage"],
        )

    def daily_breakdown(
        self,
        user_id: int,
        start_utc: int,
        end_utc: int,
        timezone_name: str,
        offset: int = 0,
        limit: int = 5,
    ) -> Page[DayStats]:
        """Per-day totals for local days that have entries, newest day first."""
        self._validate_page(offset, limit)
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
        macro_columns = ("protein", "fat", "carbs")
        daily: dict[date, dict[str, float]] = {}
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
            for column in macro_columns:
                if row[column] is not None:
                    bucket[column] += row[column]
                    bucket[f"{column}_known"] += 1
        days = sorted(daily, reverse=True)
        items = tuple(
            DayStats(
                day=day,
                entry_count=int(daily[day]["entries"]),
                calories=daily[day]["calories"],
                protein=daily[day]["protein"] if daily[day]["protein_known"] else None,
                fat=daily[day]["fat"] if daily[day]["fat_known"] else None,
                carbs=daily[day]["carbs"] if daily[day]["carbs_known"] else None,
                protein_coverage=int(daily[day]["protein_known"]),
                fat_coverage=int(daily[day]["fat_known"]),
                carbs_coverage=int(daily[day]["carbs_known"]),
            )
            for day in days[offset : offset + limit]
        )
        return Page(items, offset, offset > 0, len(days) > offset + limit)

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
            session.draft_unit,
            session.draft_servings,
            session.calories_per_100g,
            session.serving_grams,
            session.protein_per_100g,
            session.fat_per_100g,
            session.carbs_per_100g,
            session.selected_favorite_id,
            session.selected_nutrient,
            session.selected_entry_id,
            int(session.prompt_pending),
            session.last_message_id,
            session.updated_at_utc,
            session.return_day,
            session.return_offset,
            session.selected_weight_id,
            session.prompt_text,
        )

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            state=SessionState(row["state"]),
            draft_name=row["draft_name"],
            draft_unit=row["draft_unit"],
            draft_servings=row["draft_servings"],
            calories_per_100g=row["calories_per_100g"],
            serving_grams=row["serving_grams"],
            protein_per_100g=row["protein_per_100g"],
            fat_per_100g=row["fat_per_100g"],
            carbs_per_100g=row["carbs_per_100g"],
            selected_favorite_id=row["selected_favorite_id"],
            selected_nutrient=row["selected_nutrient"],
            selected_entry_id=row["selected_entry_id"],
            prompt_pending=bool(row["prompt_pending"]),
            last_message_id=row["last_message_id"],
            revision=row["revision"],
            updated_at_utc=row["updated_at_utc"],
            return_day=row["return_day"],
            return_offset=row["return_offset"],
            selected_weight_id=row["selected_weight_id"],
            prompt_text=row["prompt_text"],
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
            unit=row["unit"],
            serving_grams=row["serving_grams"],
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
                servings=row["servings"],
            ),
        )

    @staticmethod
    def _row_to_weight(row: sqlite3.Row) -> WeightRecord:
        return WeightRecord(
            weight_id=row["weight_id"],
            user_id=row["user_id"],
            measured_at_utc=row["measured_at_utc"],
            weight_kg=row["weight_kg"],
        )

    @staticmethod
    def _insert_entry(
        connection: sqlite3.Connection,
        user_id: int,
        eaten_at_utc: int,
        name: Optional[str],
        totals: NutritionTotals,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO food_entries(
                user_id, eaten_at_utc, name, grams, servings,
                calories, protein, fat, carbs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                eaten_at_utc,
                name,
                totals.grams,
                totals.servings,
                totals.calories,
                totals.protein,
                totals.fat,
                totals.carbs,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _validate_favorite_values(
        unit: str,
        calories: float,
        protein: Optional[float],
        fat: Optional[float],
        carbs: Optional[float],
        serving_grams: Optional[float],
    ) -> None:
        if unit == UNIT_SERVING:
            check_calories_per_serving(calories)
            for label, value in (("Protein", protein), ("Fat", fat), ("Carbs", carbs)):
                if value is not None:
                    check_macro_per_serving(value, label)
            if serving_grams is not None:
                check_serving_grams(serving_grams)
        else:
            scale_per_100(calories, 100.0, protein, fat, carbs)

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
        unit: str = UNIT_100G,
        serving_grams: Optional[float] = None,
    ) -> int:
        name = normalize_food_name(name)
        Database._validate_favorite_values(
            unit,
            calories_per_100g,
            protein_per_100g,
            fat_per_100g,
            carbs_per_100g,
            serving_grams,
        )
        cursor = connection.execute(
            """
            INSERT INTO favorite_foods(
                user_id, name, name_key, unit, serving_grams, calories_per_100g,
                protein_per_100g, fat_per_100g, carbs_per_100g,
                created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                name,
                name.casefold(),
                unit,
                serving_grams,
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
