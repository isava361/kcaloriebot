from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from kcaloriebot.bot import handle_callback, handle_text, start, weight_command
from kcaloriebot.callbacks import parse_callback
from kcaloriebot.database import Database, SCHEMA, SCHEMA_VERSION
from kcaloriebot.domain import SessionState, UNIT_SERVING, local_date
from kcaloriebot.render import (
    MAIN_KEYBOARD,
    SETTINGS_KEYBOARD,
    STATS_KEYBOARD,
    TIMEZONE_REQUIRED_MARKUP,
    entry_action_rows,
    nutrient_edit_prompt,
    session_prompt,
)
from tests.test_bot_handlers import (
    FakeMessage,
    FakeQuery,
    make_update,
    keyboard_callbacks,
)


def button_data(markup, label):
    return next(
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.text == label
    )


class UXFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "bot.db")
        self.database.initialize()
        self.database.set_timezone(1, "UTC")
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"database": self.database})
        )

    def tearDown(self):
        self.temporary.cleanup()

    async def tap(self, data, user_id=1):
        query = FakeQuery(data, FakeMessage())
        await handle_callback(make_update(query=query, user_id=user_id), self.context)
        return query

    async def say(self, text):
        update = make_update(text)
        await handle_text(update, self.context)
        return update.effective_message

    async def test_menu_groups_and_timezone_choices_are_reachable(self):
        labels = [button.text for row in MAIN_KEYBOARD.keyboard for button in row]
        self.assertEqual(
            ["Add food", "Diary", "Recent foods", "Favorites", "Progress", "Settings"],
            labels,
        )
        for label, keyboard in [
            ("Progress", STATS_KEYBOARD),
            ("Settings", SETTINGS_KEYBOARD),
            ("Main menu", MAIN_KEYBOARD),
        ]:
            self.assertIs((await self.say(label)).replies[-1][1], keyboard)
        update = make_update("/start", user_id=2)
        await start(update, self.context)
        text, keyboard = update.effective_message.replies[-1]
        self.assertIn("so meals appear on the right day", text)
        self.assertIs(TIMEZONE_REQUIRED_MARKUP, keyboard)
        update = make_update("Europe/London", user_id=2)
        await handle_text(update, self.context)
        self.assertEqual("Europe/London", self.database.get_timezone(2))
        self.assertIs(MAIN_KEYBOARD, update.effective_message.replies[-1][1])

    async def test_nutrition_prompt_survives_restart_with_current_value_and_basis(self):
        entry = self.database.add_entry(
            1, self.database.now_epoch(), "Oatmeal", 370, 60
        )
        query = await self.tap(f"entry:field:{entry.entry_id}:calories")
        prompt = query.message.replies[-1][0]
        self.assertIn("370 kcal per 100 g", prompt)
        self.assertIn("222 kcal logged", prompt)
        self.context.application.bot_data["database"] = Database(self.database.path)
        update = make_update("/start")
        await start(update, self.context)
        self.assertEqual(prompt, update.effective_message.replies[-1][0])
        await self.say("400")
        self.assertEqual(
            240, self.database.get_entry(1, entry.entry_id).nutrition.calories
        )

    async def test_serving_favorite_edit_retains_units_and_page(self):
        favorite = self.database.add_favorite(1, "Yogurt", 100, 8, None, None)
        # Conversion is exercised through the same UI as a user.
        await self.tap(f"fav:serving:{favorite.favorite_id}|5|")
        message = await self.say("150")
        self.assertIn("fav:list:5", keyboard_callbacks(message.replies[-1][1]))
        favorite = self.database.get_favorite(1, favorite.favorite_id)
        self.assertEqual(UNIT_SERVING, favorite.unit)
        await self.tap(f"fav:field:{favorite.favorite_id}:protein|5|")
        session = self.database.get_session(1, 10)
        self.assertIn("12 g per serving", session_prompt(session, True)[0])
        message = await self.say("10")
        self.assertIn("fav:list:5", keyboard_callbacks(message.replies[-1][1]))
        self.assertEqual(
            10, self.database.get_favorite(1, favorite.favorite_id).protein_per_100g
        )

    async def test_historical_entry_edit_and_cancel_preserve_source_page(self):
        timestamp = self.database.now_epoch() - 3 * 86400
        day = local_date(timestamp, "UTC").isoformat()
        entry = self.database.add_entry(1, timestamp, "Rice", 250, 40)
        for action, answer in [
            ("grams", "80"),
            ("name", "Brown rice"),
            ("field", "300"),
            ("time", f"{day} 01:00"),
        ]:
            with self.subTest(action=action):
                target = f"entry:{action}:{entry.entry_id}" + (
                    ":calories" if action == "field" else ""
                )
                await self.tap(f"{target}|5|{day}")
                message = await self.say(answer)
                self.assertIn(
                    f"entry:list:{day}:5", keyboard_callbacks(message.replies[-1][1])
                )
                self.assertIsNone(self.database.get_session(1, 10))
        await self.tap(f"entry:grams:{entry.entry_id}|5|{day}")
        message = await self.say("Cancel")
        self.assertIn(f"entry:list:{day}:5", keyboard_callbacks(message.replies[-1][1]))
        self.assertEqual(80, self.database.get_entry(1, entry.entry_id).nutrition.grams)

    async def test_delete_keep_and_confirm_preserve_day_and_page(self):
        timestamp = self.database.now_epoch() - 86400
        day = local_date(timestamp, "UTC").isoformat()
        entry = self.database.add_entry(1, timestamp, "Rice", 250, 40)
        for keep in (True, False):
            query = await self.tap(f"entry:delete:{entry.entry_id}|5|{day}")
            self.assertIn("Rice", query.edits[-1][0])
            query = await self.tap(
                button_data(query.edits[-1][1], "Keep" if keep else "Delete")
            )
            self.assertIn(f"entry:list:{day}:5", keyboard_callbacks(query.edits[-1][1]))
            self.assertEqual(
                keep, self.database.get_entry(1, entry.entry_id) is not None
            )
        favorite = self.database.add_favorite(1, "Bread", 250, None, None, None)
        for keep in (True, False):
            query = await self.tap(f"fav:delete:{favorite.favorite_id}|5|")
            query = await self.tap(
                button_data(query.edits[-1][1], "Keep" if keep else "Delete")
            )
            self.assertIn("fav:list:5", keyboard_callbacks(query.edits[-1][1]))

    async def test_expired_undo_opens_the_entry_without_deleting_it(self):
        entry = self.database.add_entry(1, self.database.now_epoch(), "Rice", 250, 40)
        query = await self.tap(
            f"entry:undo:{entry.entry_id}:{self.database.now_epoch() - 901}"
        )
        self.assertIn("Undo is no longer available", query.edits[-1][0])
        self.assertIsNotNone(self.database.get_entry(1, entry.entry_id))
        query = await self.tap(button_data(query.edits[-1][1], "Open entry"))
        self.assertIn("Rice", query.edits[-1][0])
        self.assertIsNotNone(button_data(query.edits[-1][1], "Delete"))

    async def test_empty_favorites_and_failed_search_offer_working_actions(self):
        message = await self.say("Favorites")
        self.assertIn("Save as favorite", message.replies[-1][0])
        await self.tap(button_data(message.replies[-1][1], "Search favorites"))
        message = await self.say("missing")
        markup = next(
            markup
            for _, markup in message.replies
            if hasattr(markup, "inline_keyboard")
        )
        await self.tap(button_data(markup, "Search again"))
        self.assertEqual(
            SessionState.WAIT_FAVORITE_SEARCH, self.database.get_session(1, 10).state
        )
        await self.say("Cancel")
        await self.tap(button_data(markup, "Add food"))
        self.assertEqual(
            SessionState.WAIT_FOOD_NAME, self.database.get_session(1, 10).state
        )

    async def test_weight_edit_changes_selected_record_preserves_date_and_can_be_removed(
        self,
    ):
        old = self.database.add_weight(1, self.database.now_epoch() - 86400, 85)
        latest = self.database.add_weight(1, self.database.now_epoch(), 80)
        await self.tap(f"weight:edit:{old.weight_id}")
        await self.say("not a weight")
        self.assertEqual(85, self.database.get_weight(1, old.weight_id).weight_kg)
        self.context.application.bot_data["database"] = Database(self.database.path)
        message = await self.say("82.5")
        stored = self.database.get_weight(1, old.weight_id)
        self.assertEqual(old.measured_at_utc, stored.measured_at_utc)
        self.assertEqual(82.5, stored.weight_kg)
        self.assertEqual(latest, self.database.latest_weight(1))
        query = await self.tap(
            button_data(message.replies[-1][1], "Remove measurement")
        )
        self.assertIn("removed", query.edits[-1][0])
        self.assertIsNone(self.database.get_weight(1, old.weight_id))
        query = await self.tap(f"weight:undo:{old.weight_id}")
        self.assertIn("no longer available", query.edits[-1][0])

    async def test_weight_receipts_offer_undo_and_other_users_cannot_change_measurements(
        self,
    ):
        update = make_update("/weight 82.5")
        await weight_command(update, self.context)
        markup = update.effective_message.replies[-1][1]
        record = self.database.latest_weight(1)
        for label in ("Edit measurement", "Undo"):
            await self.tap(button_data(markup, label), user_id=2)
            self.assertEqual(record, self.database.get_weight(1, record.weight_id))
            self.assertIsNone(self.database.get_session(2, 10))
        await self.tap(button_data(markup, "Undo"))
        self.assertIsNone(self.database.latest_weight(1))
        await self.say("Weight")
        message = await self.say("81")
        await self.tap(button_data(message.replies[-1][1], "Edit measurement"))
        await self.say("Cancel")
        self.assertEqual(81, self.database.latest_weight(1).weight_kg)

    async def test_compact_receipt_and_partial_macros_keep_missing_values_distinct(
        self,
    ):
        message = await self.say("oatmeal 370 60")
        text, _ = message.replies[-1]
        self.assertIn("60 g", text)
        self.assertIn("222 kcal", text)
        self.assertEqual(1, text.count("Macros not recorded"))
        message = await self.say("oatmeal 370 60 p0")
        text, _ = message.replies[-1]
        self.assertIn("Protein: 0 g", text)
        self.assertIn("Fat: not recorded", text)
        favorite = self.database.add_favorite(1, "Rice", 250, None, None, None)
        self.assertIn(
            "Current protein: not recorded", nutrient_edit_prompt(favorite, "protein")
        )

    def test_context_callbacks_validate_and_fit_telegram_limit(self):
        entry = self.database.add_entry(1, self.database.now_epoch(), "Rice", 250, 40)
        entry = replace(entry, entry_id=9223372036854775807)
        callbacks = [
            button.callback_data
            for row in entry_action_rows(entry, 10000, "2026-09-01")
            for button in row
        ]
        callbacks.append("entry:dc:9223372036854775807:1790000000|10000|2026-09-01")
        for callback in callbacks:
            self.assertLessEqual(len(callback.encode()), 64)
            self.assertIsNotNone(parse_callback(callback))
        for callback in (
            "entry:grams:1|3|2026-09-01",
            "entry:grams:1|0|2026-02-30",
            "menu:add|0|",
            "weight:edit:0",
        ):
            self.assertIsNone(parse_callback(callback))
        parsed = parse_callback("entry:dc:1:1790000000|5|2026-09-01")
        self.assertEqual(date(2026, 9, 1), parsed.day)
        self.assertEqual(5, parsed.offset)


class UXMigrationTests(unittest.TestCase):
    def test_v4_upgrade_preserves_records_and_persists_edit_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v4.db"
            connection = sqlite3.connect(path)
            try:
                connection.executescript(SCHEMA)
                connection.execute(
                    "INSERT INTO users(user_id, timezone, created_at_utc, updated_at_utc) VALUES (1, 'UTC', 1, 1)"
                )
                connection.execute(
                    "INSERT INTO sessions(user_id, chat_id, state, draft_name, updated_at_utc) VALUES (1, 10, 'wait_calories', 'Rice', 1)"
                )
                connection.execute(
                    "INSERT INTO weights(user_id, measured_at_utc, weight_kg) VALUES (1, 1234, 82.5)"
                )
                connection.commit()
            finally:
                connection.close()
            database = Database(path)
            database.initialize()
            database.initialize()
            self.assertEqual("Rice", database.get_session(1, 10).draft_name)
            self.assertEqual(82.5, database.latest_weight(1).weight_kg)
            session = database.start_session(
                1,
                10,
                SessionState.WAIT_WEIGHT_EDIT,
                selected_weight_id=1,
                return_day="2026-09-01",
                return_offset=5,
                prompt_text="Correct 82.5 kg:",
            )
            session = database.update_session(
                session, replace(session, prompt_pending=True)
            )
            self.assertEqual(session, Database(path).get_session(1, 10))
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    SCHEMA_VERSION,
                    connection.execute("PRAGMA user_version").fetchone()[0],
                )
            finally:
                connection.close()
