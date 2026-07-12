from __future__ import annotations

import sqlite3
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from kcaloriebot.database import Database
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


class SchemaTests(DatabaseTestCase):
    def test_foreign_keys_are_enabled_for_every_connection(self) -> None:
        self.assertTrue(self.database.foreign_keys_enabled())
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.add_favorite(999, "Rice", 100, 1, 1, 1, 1)

    def test_schema_initialization_is_idempotent(self) -> None:
        self.database.initialize()
        self.assertTrue(self.database.foreign_keys_enabled())

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
