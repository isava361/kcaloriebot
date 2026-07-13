from __future__ import annotations

import sqlite3
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from kcaloriebot.database import SCHEMA, Database
from kcaloriebot.domain import NotFound, SessionState, StateConflict, ValidationError


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "bot.db"
        self.database = Database(self.path)
        self.database.initialize()
        self.database.ensure_user(1, 1_700_000_000)
        self.database.ensure_user(2, 1_700_000_000)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def ready_food_session(
        self, user_id: int = 1, chat_id: int = 10, name: str | None = "Rice"
    ):
        return self.database.start_session(
            user_id,
            chat_id,
            SessionState.WAIT_CARBS,
            now_utc=1_700_000_000,
            draft_name=name,
            calories_per_100g=250.0,
            serving_grams=40.0,
            protein_per_100g=10.0,
            fat_per_100g=20.0,
            carbs_per_100g=30.0,
        )


class OwnershipTests(DatabaseTestCase):
    def test_entry_operations_require_matching_owner(self) -> None:
        session = self.ready_food_session(name=None)
        entry = self.database.complete_food_draft(session, 1_700_000_100)

        self.assertIsNone(self.database.get_entry(2, entry.entry_id))
        with self.assertRaises(NotFound):
            self.database.delete_entry(2, entry.entry_id)
        self.assertIsNotNone(self.database.get_entry(1, entry.entry_id))

    def test_favorite_operations_require_matching_owner(self) -> None:
        favorite = self.database.add_favorite(
            1, "Rice", 250.0, 10.0, 20.0, 30.0, 1_700_000_100
        )

        self.assertIsNone(self.database.get_favorite(2, favorite.favorite_id))
        with self.assertRaises(NotFound):
            self.database.delete_favorite(2, favorite.favorite_id)
        self.assertIsNotNone(self.database.get_favorite(1, favorite.favorite_id))

    def test_session_cannot_select_another_users_favorite(self) -> None:
        favorite = self.database.add_favorite(
            1, "Rice", 250.0, 10.0, 20.0, 30.0, 1_700_000_100
        )
        with self.assertRaises(NotFound):
            self.database.start_session(
                2,
                20,
                SessionState.WAIT_FAVORITE_GRAMS,
                now_utc=1_700_000_101,
                selected_favorite_id=favorite.favorite_id,
            )


class SessionAndTransactionTests(DatabaseTestCase):
    def test_complete_session_survives_database_reopen(self) -> None:
        original = self.ready_food_session()
        reopened = Database(self.path)
        reopened.initialize()

        restored = reopened.get_session(original.user_id, original.chat_id)

        self.assertEqual(original, restored)

    def test_sessions_are_isolated_by_user_and_chat(self) -> None:
        first = self.ready_food_session(user_id=1, chat_id=10)
        second = self.ready_food_session(user_id=2, chat_id=10)
        third = self.ready_food_session(user_id=1, chat_id=11)
        self.database.update_session(first, replace(first, carbs_per_100g=15.0))
        self.database.update_session(second, replace(second, carbs_per_100g=25.0))
        self.database.update_session(third, replace(third, carbs_per_100g=35.0))

        self.assertEqual(15.0, self.database.get_session(1, 10).carbs_per_100g)
        self.assertEqual(25.0, self.database.get_session(2, 10).carbs_per_100g)
        self.assertEqual(35.0, self.database.get_session(1, 11).carbs_per_100g)

    def test_food_completion_scales_once_and_advances_atomically(self) -> None:
        session = self.ready_food_session()

        entry = self.database.complete_food_draft(session, 1_700_000_100)
        stored_session = self.database.get_session(1, 10)

        self.assertAlmostEqual(100.0, entry.nutrition.calories)
        self.assertAlmostEqual(4.0, entry.nutrition.protein)
        self.assertAlmostEqual(8.0, entry.nutrition.fat)
        self.assertAlmostEqual(12.0, entry.nutrition.carbs)
        self.assertEqual(SessionState.WAIT_SAVE_FAVORITE, stored_session.state)
        self.assertEqual(30.0, stored_session.carbs_per_100g)
        self.assertGreater(stored_session.updated_at_utc, entry.eaten_at_utc)

    def test_food_completion_reloads_persisted_draft_fields(self) -> None:
        session = self.ready_food_session(name="Rice")
        forged = replace(
            session,
            draft_name="Changed",
            calories_per_100g=999.0,
            serving_grams=500.0,
            protein_per_100g=0.0,
            carbs_per_100g=15.0,
        )

        entry = self.database.complete_food_draft(forged, 1_700_000_100)

        self.assertEqual("Rice", entry.name)
        self.assertAlmostEqual(100.0, entry.nutrition.calories)
        self.assertAlmostEqual(4.0, entry.nutrition.protein)
        self.assertAlmostEqual(6.0, entry.nutrition.carbs)

    def test_duplicate_food_completion_creates_only_one_entry(self) -> None:
        session = self.ready_food_session(name=None)
        self.database.complete_food_draft(session, 1_700_000_100)

        with self.assertRaises(StateConflict):
            self.database.complete_food_draft(session, 1_700_000_100)

        page = self.database.page_entries(1, 1_699_999_000, 1_700_001_000)
        self.assertEqual(1, len(page.items))
        self.assertIsNone(self.database.get_session(1, 10))

    def test_saving_favorite_and_clearing_session_is_atomic(self) -> None:
        session = self.ready_food_session()
        self.database.complete_food_draft(session, 1_700_000_100)

        favorite = self.database.save_session_as_favorite(1, 10, 1_700_000_101)

        self.assertEqual("Rice", favorite.name)
        self.assertEqual(30.0, favorite.carbs_per_100g)
        self.assertIsNone(self.database.get_session(1, 10))

    def test_use_favorite_inserts_owned_entry_and_clears_session(self) -> None:
        favorite = self.database.add_favorite(
            1, "Rice", 250.0, 10.0, 20.0, 30.0, 1_700_000_100
        )
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_FAVORITE_GRAMS,
            now_utc=1_700_000_101,
            selected_favorite_id=favorite.favorite_id,
        )

        entry = self.database.use_selected_favorite(1, 10, 40.0, 1_700_000_102)

        self.assertEqual("Rice", entry.name)
        self.assertAlmostEqual(100.0, entry.nutrition.calories)
        self.assertIsNone(self.database.get_session(1, 10))

    def test_invalid_amendment_rolls_back_and_keeps_session(self) -> None:
        favorite = self.database.add_favorite(
            1, "Rice", 250.0, 40.0, 30.0, 20.0, 1_700_000_100
        )
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_FAVORITE_AMENDMENT,
            now_utc=1_700_000_101,
            selected_favorite_id=favorite.favorite_id,
            selected_nutrient="carbs",
        )

        with self.assertRaises(ValidationError):
            self.database.complete_favorite_amendment(1, 10, 50.0, 1_700_000_102)

        self.assertEqual(
            20.0, self.database.get_favorite(1, favorite.favorite_id).carbs_per_100g
        )
        self.assertIsNotNone(self.database.get_session(1, 10))

    def test_nan_amendment_does_not_silently_clear_macro(self) -> None:
        favorite = self.database.add_favorite(
            1, "Rice", 250.0, 40.0, 30.0, 20.0, 1_700_000_100
        )
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_FAVORITE_AMENDMENT,
            now_utc=1_700_000_101,
            selected_favorite_id=favorite.favorite_id,
            selected_nutrient="carbs",
        )

        with self.assertRaises(ValidationError):
            self.database.complete_favorite_amendment(1, 10, math.nan, 1_700_000_102)

        self.assertEqual(
            20.0, self.database.get_favorite(1, favorite.favorite_id).carbs_per_100g
        )
        self.assertIsNotNone(self.database.get_session(1, 10))

    def test_timezone_completion_updates_user_and_clears_session(self) -> None:
        session = self.database.start_session(
            1, 10, SessionState.WAIT_TIMEZONE, now_utc=1_700_000_100
        )

        self.database.complete_timezone_session(session, "Europe/Moscow", 1_700_000_101)

        self.assertEqual("Europe/Moscow", self.database.get_timezone(1))
        self.assertIsNone(self.database.get_session(1, 10))

    def test_invalid_timezone_is_rejected_before_storage(self) -> None:
        with self.assertRaises(ValidationError):
            self.database.set_timezone(1, "Not/AZone", 1_700_000_100)
        self.assertIsNone(self.database.get_timezone(1))


class PaginationAndStatsTests(DatabaseTestCase):
    def test_entry_pages_have_stable_order_and_lookahead(self) -> None:
        ids = []
        for index in range(6):
            session = self.ready_food_session(chat_id=100 + index, name=None)
            entry = self.database.complete_food_draft(session, 1_700_000_100)
            ids.append(entry.entry_id)

        first = self.database.page_entries(1, 1_699_999_000, 1_700_001_000, 0, 5)
        second = self.database.page_entries(1, 1_699_999_000, 1_700_001_000, 5, 5)

        self.assertEqual(
            tuple(reversed(ids[1:])), tuple(item.entry_id for item in first.items)
        )
        self.assertTrue(first.has_next)
        self.assertEqual((ids[0],), tuple(item.entry_id for item in second.items))
        self.assertTrue(second.has_previous)
        self.assertFalse(second.has_next)

    def test_favorite_search_treats_wildcards_literally_and_casefolds_unicode(
        self,
    ) -> None:
        percent = self.database.add_favorite(1, "100% Yogurt", 70, 5, 2, 8, 1)
        underscore = self.database.add_favorite(1, "A_B", 70, 5, 2, 8, 2)
        unicode_name = self.database.add_favorite(1, "ЙОГУРТ", 70, 5, 2, 8, 3)

        self.assertEqual(
            (percent.favorite_id,),
            tuple(x.favorite_id for x in self.database.search_favorites(1, "%")),
        )
        self.assertEqual(
            (underscore.favorite_id,),
            tuple(x.favorite_id for x in self.database.search_favorites(1, "_")),
        )
        self.assertEqual(
            (unicode_name.favorite_id,),
            tuple(x.favorite_id for x in self.database.search_favorites(1, "йогурт")),
        )

    def test_stats_average_only_logged_days(self) -> None:
        first = self.ready_food_session(chat_id=20, name=None)
        second = self.ready_food_session(chat_id=21, name=None)
        self.database.complete_food_draft(first, 1_704_067_200)  # 2024-01-01 UTC
        self.database.complete_food_draft(second, 1_704_240_000)  # 2024-01-03 UTC

        stats = self.database.stats(
            1, 1_704_067_200, 1_704_326_400, "UTC", average_by_logged_day=True
        )

        self.assertEqual(2, stats.logged_days)
        self.assertAlmostEqual(100.0, stats.calories)

    def test_stats_preserve_unknown_macros(self) -> None:
        session = self.database.start_session(
            1,
            30,
            SessionState.WAIT_CARBS,
            now_utc=1,
            draft_name=None,
            calories_per_100g=100.0,
            serving_grams=100.0,
        )
        self.database.complete_food_draft(session, 1_704_067_200)

        stats = self.database.stats(1, 1_704_067_200, 1_704_153_600, "UTC")

        self.assertIsNone(stats.protein)
        self.assertIsNone(stats.fat)
        self.assertIsNone(stats.carbs)

    def test_daily_average_excludes_day_with_partial_macro_coverage(self) -> None:
        first = self.database.start_session(
            1,
            31,
            SessionState.WAIT_CARBS,
            now_utc=1,
            calories_per_100g=100.0,
            serving_grams=100.0,
            protein_per_100g=10.0,
        )
        second = self.database.start_session(
            1,
            32,
            SessionState.WAIT_CARBS,
            now_utc=1,
            calories_per_100g=100.0,
            serving_grams=100.0,
        )
        self.database.complete_food_draft(first, 1_704_067_200)
        self.database.complete_food_draft(second, 1_704_067_201)

        stats = self.database.stats(
            1,
            1_704_067_200,
            1_704_153_600,
            "UTC",
            average_by_logged_day=True,
        )

        self.assertIsNone(stats.protein)
        self.assertEqual(0, stats.protein_coverage)
        self.assertEqual(1, stats.coverage_total)


class DailyGoalTests(DatabaseTestCase):
    def test_daily_goal_roundtrip_and_removal(self) -> None:
        self.assertIsNone(self.database.get_daily_goal(1))
        self.database.set_daily_goal(1, 2000.0, 2)
        self.assertEqual(2000.0, self.database.get_daily_goal(1))
        self.database.set_daily_goal(1, None, 3)
        self.assertIsNone(self.database.get_daily_goal(1))

    def test_goal_session_completion_is_atomic(self) -> None:
        session = self.database.start_session(
            1, 10, SessionState.WAIT_GOAL, now_utc=1_700_000_100
        )

        self.database.complete_goal_session(session, 1800.0, 1_700_000_101)

        self.assertEqual(1800.0, self.database.get_daily_goal(1))
        self.assertIsNone(self.database.get_session(1, 10))

    def test_invalid_goals_are_rejected_before_storage(self) -> None:
        for goal in (0.0, -1.0, 50_001.0, math.nan):
            with self.subTest(goal=goal):
                with self.assertRaises(ValidationError):
                    self.database.set_daily_goal(1, goal, 2)
        self.assertIsNone(self.database.get_daily_goal(1))

    def test_goal_for_unknown_user_raises(self) -> None:
        with self.assertRaises(NotFound):
            self.database.set_daily_goal(999, 2000.0, 2)


class QuickEntryTests(DatabaseTestCase):
    def test_add_entry_scales_and_stores(self) -> None:
        entry = self.database.add_entry(
            1, 1_700_000_100, "Rice", 250.0, 40.0, 10.0, 20.0, 30.0
        )

        stored = self.database.get_entry(1, entry.entry_id)

        self.assertEqual("Rice", stored.name)
        self.assertAlmostEqual(100.0, stored.nutrition.calories)
        self.assertAlmostEqual(4.0, stored.nutrition.protein)
        self.assertEqual(1_700_000_100, stored.eaten_at_utc)

    def test_add_entry_rejects_invalid_nutrition(self) -> None:
        with self.assertRaises(ValidationError):
            self.database.add_entry(1, 1_700_000_100, "Rice", -1.0, 40.0)

    def test_find_favorite_by_name_casefolds_and_scopes_to_owner(self) -> None:
        self.database.add_favorite(1, "Гречка", 313.0, 12.0, 3.0, 62.0, 1)

        self.assertIsNotNone(self.database.find_favorite_by_name(1, "  гречка "))
        self.assertIsNone(self.database.find_favorite_by_name(2, "гречка"))
        self.assertIsNone(self.database.find_favorite_by_name(1, "рис"))

    def test_find_favorite_by_name_prefers_the_newest_duplicate(self) -> None:
        self.database.add_favorite(1, "Rice", 200.0, None, None, None, 1)
        newest = self.database.add_favorite(1, "rice", 250.0, None, None, None, 2)

        found = self.database.find_favorite_by_name(1, "RICE")

        self.assertEqual(newest.favorite_id, found.favorite_id)

    def test_recent_templates_deduplicate_names_case_insensitively(self) -> None:
        self.database.add_entry(1, 100, "Rice", 250.0, 40.0)
        self.database.add_entry(1, 200, "Buckwheat", 313.0, 50.0)
        self.database.add_entry(1, 300, None, 100.0, 100.0)
        latest_rice = self.database.add_entry(1, 400, "rice", 250.0, 80.0)

        templates = self.database.recent_entry_templates(1, 10)

        self.assertEqual(
            (latest_rice.entry_id,),
            tuple(t.entry_id for t in templates if (t.name or "").casefold() == "rice"),
        )
        self.assertEqual(["rice", "Buckwheat"], [t.name for t in templates])


class RecentAndEditTests(DatabaseTestCase):
    def make_entry(self, **overrides: object):
        values = {
            "user_id": 1,
            "eaten_at_utc": 1_700_000_100,
            "name": "Rice",
            "calories_per_100g": 250.0,
            "grams": 40.0,
            "protein_per_100g": 10.0,
            "fat_per_100g": 20.0,
            "carbs_per_100g": 30.0,
        }
        values.update(overrides)
        return self.database.add_entry(**values)

    def test_use_selected_entry_repeats_with_new_grams(self) -> None:
        source = self.make_entry()
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_RECENT_GRAMS,
            now_utc=1_700_000_101,
            selected_entry_id=source.entry_id,
        )

        entry = self.database.use_selected_entry(1, 10, 80.0, 1_700_000_102)

        self.assertEqual("Rice", entry.name)
        self.assertAlmostEqual(200.0, entry.nutrition.calories)
        self.assertAlmostEqual(8.0, entry.nutrition.protein)
        self.assertIsNone(self.database.get_session(1, 10))

    def test_use_selected_entry_defaults_to_the_source_serving(self) -> None:
        source = self.make_entry()
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_RECENT_GRAMS,
            now_utc=1_700_000_101,
            selected_entry_id=source.entry_id,
        )

        entry = self.database.use_selected_entry(1, 10, None, 1_700_000_102)

        self.assertAlmostEqual(40.0, entry.nutrition.grams)
        self.assertAlmostEqual(100.0, entry.nutrition.calories)

    def test_update_entry_grams_recomputes_totals(self) -> None:
        source = self.make_entry()
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_ENTRY_GRAMS,
            now_utc=1_700_000_101,
            selected_entry_id=source.entry_id,
        )

        self.database.update_entry_grams(1, 10, 80.0, 1_700_000_102)

        stored = self.database.get_entry(1, source.entry_id)
        self.assertAlmostEqual(80.0, stored.nutrition.grams)
        self.assertAlmostEqual(200.0, stored.nutrition.calories)
        self.assertAlmostEqual(8.0, stored.nutrition.protein)
        self.assertIsNone(self.database.get_session(1, 10))

    def test_update_entry_time_moves_the_entry(self) -> None:
        source = self.make_entry()
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_ENTRY_TIME,
            now_utc=1_700_000_101,
            selected_entry_id=source.entry_id,
        )

        self.database.update_entry_time(1, 10, 1_600_000_000, 1_700_000_102)

        stored = self.database.get_entry(1, source.entry_id)
        self.assertEqual(1_600_000_000, stored.eaten_at_utc)
        self.assertAlmostEqual(100.0, stored.nutrition.calories)
        self.assertIsNone(self.database.get_session(1, 10))

    def test_entry_amendments_require_the_matching_owner(self) -> None:
        source = self.make_entry()
        self.database.start_session(
            2,
            20,
            SessionState.WAIT_ENTRY_GRAMS,
            now_utc=1_700_000_101,
            selected_entry_id=source.entry_id,
        )

        with self.assertRaises(NotFound):
            self.database.update_entry_grams(2, 20, 80.0, 1_700_000_102)

        stored = self.database.get_entry(1, source.entry_id)
        self.assertAlmostEqual(40.0, stored.nutrition.grams)

    def test_wrong_session_state_blocks_entry_amendments(self) -> None:
        source = self.make_entry()
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_ENTRY_TIME,
            now_utc=1_700_000_101,
            selected_entry_id=source.entry_id,
        )

        with self.assertRaises(StateConflict):
            self.database.update_entry_grams(1, 10, 80.0, 1_700_000_102)


class SchemaTests(DatabaseTestCase):
    def test_foreign_keys_are_enabled_for_every_connection(self) -> None:
        self.assertTrue(self.database.foreign_keys_enabled())
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.add_favorite(999, "Rice", 100, 1, 1, 1, 1)

    def test_schema_initialization_is_idempotent(self) -> None:
        self.database.initialize()
        self.assertTrue(self.database.foreign_keys_enabled())

    GOAL_COLUMN_LINE = (
        "    daily_calorie_goal REAL NULL CHECK (daily_calorie_goal IS NULL "
        "OR (daily_calorie_goal > 0 AND daily_calorie_goal <= 50000)),\n"
    )
    ENTRY_COLUMN_LINE = "    selected_entry_id INTEGER NULL,\n"
    PROMPT_COLUMN_LINE = (
        "    prompt_pending INTEGER NOT NULL DEFAULT 0 "
        "CHECK (prompt_pending IN (0, 1)),\n"
    )
    MESSAGE_COLUMN_LINE = "    last_message_id INTEGER NULL,\n"

    def _historic_schema(self, version: int, *removed_lines: str) -> str:
        schema = SCHEMA.replace(
            "PRAGMA user_version = 3;", f"PRAGMA user_version = {version};"
        )
        for line in removed_lines:
            self.assertIn(line, schema)
            schema = schema.replace(line, "")
        return schema

    def test_version_one_sessions_are_migrated_with_delivery_fields(self) -> None:
        version_one_path = Path(self.temporary_directory.name) / "version-one.db"
        version_one_schema = self._historic_schema(
            1,
            self.PROMPT_COLUMN_LINE,
            self.MESSAGE_COLUMN_LINE,
            self.ENTRY_COLUMN_LINE,
            self.GOAL_COLUMN_LINE,
        )
        with sqlite3.connect(version_one_path) as connection:
            connection.executescript(version_one_schema)
            connection.execute(
                """
                INSERT INTO users(user_id, created_at_utc, updated_at_utc)
                VALUES (1, 1, 1)
                """
            )
            connection.execute(
                """
                INSERT INTO sessions(user_id, chat_id, state, updated_at_utc)
                VALUES (1, 10, 'wait_calories', 1)
                """
            )

        migrated = Database(version_one_path)
        migrated.initialize()
        session = migrated.get_session(1, 10)

        self.assertFalse(session.prompt_pending)
        self.assertIsNone(session.last_message_id)
        self.assertIsNone(session.selected_entry_id)
        self.assertIsNone(migrated.get_daily_goal(1))

    def test_version_two_schema_gains_goal_and_entry_columns(self) -> None:
        version_two_path = Path(self.temporary_directory.name) / "version-two.db"
        version_two_schema = self._historic_schema(
            2, self.ENTRY_COLUMN_LINE, self.GOAL_COLUMN_LINE
        )
        with sqlite3.connect(version_two_path) as connection:
            connection.executescript(version_two_schema)
            connection.execute(
                """
                INSERT INTO users(user_id, created_at_utc, updated_at_utc)
                VALUES (1, 1, 1)
                """
            )
            connection.execute(
                """
                INSERT INTO sessions(user_id, chat_id, state, updated_at_utc)
                VALUES (1, 10, 'wait_grams', 1)
                """
            )

        migrated = Database(version_two_path)
        migrated.initialize()
        with sqlite3.connect(version_two_path) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]

        self.assertEqual(3, version)
        self.assertEqual("wait_grams", migrated.get_session(1, 10).state.value)
        migrated.set_daily_goal(1, 2000.0, 2)
        self.assertEqual(2000.0, migrated.get_daily_goal(1))

    def test_partially_applied_version_one_migration_is_recovered(self) -> None:
        version_one_path = (
            Path(self.temporary_directory.name) / "partial-version-one.db"
        )
        partial_schema = self._historic_schema(
            1,
            self.MESSAGE_COLUMN_LINE,
            self.ENTRY_COLUMN_LINE,
            self.GOAL_COLUMN_LINE,
        )
        with sqlite3.connect(version_one_path) as connection:
            connection.executescript(partial_schema)

        migrated = Database(version_one_path)
        migrated.initialize()
        with sqlite3.connect(version_one_path) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            session_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            user_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(users)")
            }

        self.assertEqual(3, version)
        self.assertIn("prompt_pending", session_columns)
        self.assertIn("last_message_id", session_columns)
        self.assertIn("selected_entry_id", session_columns)
        self.assertIn("daily_calorie_goal", user_columns)

    def test_nan_favorite_macro_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.database.add_favorite(1, "Rice", 100, math.nan, None, None, 1)

    def test_unversioned_legacy_schema_is_rejected(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                "CREATE TABLE users(user_id INTEGER PRIMARY KEY, state INTEGER)"
            )

        with self.assertRaisesRegex(RuntimeError, "legacy schema"):
            Database(legacy_path).initialize()


if __name__ == "__main__":
    unittest.main()
