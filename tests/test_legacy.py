from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from kcaloriebot.database import Database
from kcaloriebot.legacy import migrate_legacy_database


LEGACY_SCHEMA = """
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    state INTEGER NOT NULL,
    timezone TEXT
);
CREATE TABLE food_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    entry_date DATE NOT NULL,
    calories REAL,
    grams REAL,
    protein REAL,
    fat REAL,
    carbs REAL,
    name TEXT
);
CREATE TABLE favorite_foods (
    favorite_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    calories REAL,
    protein REAL,
    fat REAL,
    carbs REAL
);
"""


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.source = root / "mydb.db"
        self.target = root / "data" / "kcaloriebot.db"
        with sqlite3.connect(self.source) as connection:
            connection.executescript(LEGACY_SCHEMA)
            connection.execute(
                "INSERT INTO users(user_id, state, timezone) VALUES (1, 6, 'Moscow')"
            )
            connection.execute(
                """
                INSERT INTO food_entries(
                    entry_id, user_id, entry_date, name, calories, grams, protein, fat, carbs
                ) VALUES (7, 1, '2024-01-02 03:04:05', 'Rice', 100, 40, 4, 8, 12)
                """
            )
            connection.execute(
                """
                INSERT INTO food_entries(
                    entry_id, user_id, entry_date, name, calories, grams
                ) VALUES (8, 1, '2024-01-02 03:04:05', 'Invalid', 100, -1)
                """
            )
            connection.execute(
                """
                INSERT INTO favorite_foods(
                    favorite_id, user_id, name, calories, protein, fat, carbs
                ) VALUES (9, 1, 'Rice', 250, 10, 20, 30)
                """
            )
            connection.execute(
                """
                INSERT INTO favorite_foods(
                    favorite_id, user_id, name, calories, protein, fat, carbs
                ) VALUES (10, 1, 'Invalid', 250, 60, 30, 20)
                """
            )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_migration_preserves_valid_records_and_resets_state(self) -> None:
        report = migrate_legacy_database(self.source, self.target)
        database = Database(self.target)

        self.assertEqual(1, report.users)
        self.assertEqual(1, report.entries)
        self.assertEqual(1, report.favorites)
        self.assertEqual(1, report.skipped_entries)
        self.assertEqual(1, report.skipped_favorites)
        self.assertEqual("Europe/Moscow", database.get_timezone(1))
        self.assertEqual("Rice", database.get_entry(1, 7).name)
        self.assertEqual("Rice", database.get_favorite(1, 9).name)
        self.assertIsNone(database.get_session(1, 1))

    def test_migration_refuses_nonempty_target(self) -> None:
        database = Database(self.target)
        database.initialize()
        database.ensure_user(99, 1)

        with self.assertRaisesRegex(RuntimeError, "target database must be empty"):
            migrate_legacy_database(self.source, self.target)


if __name__ == "__main__":
    unittest.main()
