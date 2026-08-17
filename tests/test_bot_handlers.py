from __future__ import annotations

import tempfile
import unittest
from itertools import count
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram.constants import ChatType
from telegram.error import NetworkError

from telegram import InlineKeyboardMarkup

from kcaloriebot.bot import (
    CANCEL_KEYBOARD,
    MAIN_KEYBOARD,
    _show_entries,
    add_command,
    handle_callback,
    handle_text,
    start,
    unknown_command,
    update_timezone,
    weight_command,
)
from kcaloriebot.database import Database
from kcaloriebot.domain import SessionState
from kcaloriebot.render import PER_SERVING_KEYBOARD


def keyboard_callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class FakeMessage:
    _ids = count(1)

    def __init__(
        self,
        text: str | None = None,
        accessible: bool = True,
        date: datetime | None = None,
        message_id: int | None = None,
    ):
        self.text = text
        self.is_accessible = accessible
        self.date = date
        self.message_id = next(self._ids) if message_id is None else message_id
        self.replies: list[tuple[str, Any]] = []

    async def reply_text(self, text: str, reply_markup: Any = None) -> None:
        self.replies.append((text, reply_markup))


class FailingMessage(FakeMessage):
    async def reply_text(self, text: str, reply_markup: Any = None) -> None:
        raise NetworkError("simulated send failure")


class FakeQuery:
    def __init__(self, data: str, message: FakeMessage):
        self.data = data
        self.message = message
        self.answers: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.edits: list[tuple[str, Any]] = []

    async def answer(self, *args: Any, **kwargs: Any) -> None:
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text: str, reply_markup: Any = None) -> None:
        self.edits.append((text, reply_markup))


def make_update(
    text: str | None = None,
    *,
    user_id: int = 1,
    chat_id: int = 10,
    query: FakeQuery | None = None,
    date: datetime | None = None,
    chat_type: str = ChatType.PRIVATE,
) -> SimpleNamespace:
    message = query.message if query is not None else FakeMessage(text, date=date)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_message=message,
        callback_query=query,
    )


class BotHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "bot.db")
        self.database.initialize()
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"database": self.database})
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_first_menu_text_starts_timezone_onboarding_without_consuming_text(
        self,
    ) -> None:
        update = make_update("Add Food")

        await handle_text(update, self.context)

        session = self.database.get_session(1, 10)
        self.assertEqual(SessionState.WAIT_TIMEZONE, session.state)
        self.assertIsNone(self.database.get_timezone(1))
        self.assertEqual(1, len(update.effective_message.replies))
        self.assertIn("timezone", update.effective_message.replies[0][0].lower())

    async def test_timezone_reply_completes_onboarding_atomically(self) -> None:
        first = make_update("Add Food")
        await handle_text(first, self.context)
        timezone_reply = make_update("Europe/Moscow")

        await handle_text(timezone_reply, self.context)

        self.assertEqual("Europe/Moscow", self.database.get_timezone(1))
        self.assertIsNone(self.database.get_session(1, 10))
        self.assertIs(MAIN_KEYBOARD, timezone_reply.effective_message.replies[-1][1])

    async def test_start_resumes_an_active_workflow(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_CALORIES,
            now_utc=self.database.now_epoch(),
        )
        update = make_update("/start")

        await start(update, self.context)

        self.assertEqual(
            SessionState.WAIT_CALORIES,
            self.database.get_session(1, 10).state,
        )
        self.assertIn("calories", update.effective_message.replies[-1][0].lower())
        self.assertIs(PER_SERVING_KEYBOARD, update.effective_message.replies[-1][1])

    async def test_favorite_search_restores_main_reply_keyboard(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_FAVORITE_SEARCH,
            now_utc=self.database.now_epoch(),
        )
        update = make_update("rice")

        await handle_text(update, self.context)

        self.assertEqual(2, len(update.effective_message.replies))
        self.assertIs(MAIN_KEYBOARD, update.effective_message.replies[-1][1])
        self.assertIsNone(self.database.get_session(1, 10))

    async def test_large_empty_page_offset_falls_back_once(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        update = make_update("Food Today")

        await _show_entries(update, self.context, 10_000)

        self.assertEqual(1, len(update.effective_message.replies))
        self.assertIn("No food entries", update.effective_message.replies[0][0])

    async def test_inaccessible_callback_is_acknowledged_without_edit(self) -> None:
        query = FakeQuery("entry:view:1", FakeMessage(accessible=False))
        update = make_update(query=query)

        await handle_callback(update, self.context)

        self.assertEqual(1, len(query.answers))
        self.assertEqual([], query.edits)

    async def test_callback_without_effective_chat_is_acknowledged(self) -> None:
        query = FakeQuery("entry:view:1", FakeMessage())
        update = make_update(query=query)
        update.effective_chat = None

        await handle_callback(update, self.context)

        self.assertEqual(1, len(query.answers))
        self.assertEqual([], query.edits)

    async def test_complete_add_food_and_save_favorite_dialog(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        messages = ["Add Food", "Rice", "250", "40", "10", "20", "30"]

        final_update = None
        for text in messages:
            final_update = make_update(text)
            await handle_text(final_update, self.context)

        self.assertIsNone(self.database.get_session(1, 10))
        entries = self.database.page_entries(1, 0, 4_000_000_000)
        self.assertEqual(1, len(entries.items))
        entry = entries.items[0]
        self.assertEqual("Rice", entry.name)
        self.assertAlmostEqual(100.0, entry.nutrition.calories)
        self.assertAlmostEqual(12.0, entry.nutrition.carbs)

        receipt_text, receipt_markup = final_update.effective_message.replies[-1]
        self.assertIn("Food entry added", receipt_text)
        callbacks = keyboard_callbacks(receipt_markup)
        self.assertIn(f"entry:view:{entry.entry_id}:0", callbacks)
        self.assertIn(f"entry:fav:{entry.entry_id}", callbacks)

        save_query = FakeQuery(f"entry:fav:{entry.entry_id}", FakeMessage())
        await handle_callback(make_update(query=save_query), self.context)
        favorites = self.database.page_favorites(1)
        self.assertEqual(1, len(favorites.items))
        self.assertEqual(30.0, favorites.items[0].carbs_per_100g)

    async def test_add_unnamed_food_with_skipped_macros_returns_to_menu(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        final_update = None

        for text in ("Add Food", "Skip", "100", "25", "Skip", "Skip", "Skip"):
            final_update = make_update(text)
            await handle_text(final_update, self.context)

        self.assertIsNone(self.database.get_session(1, 10))
        entry = self.database.page_entries(1, 0, 4_000_000_000).items[0]
        self.assertIsNone(entry.name)
        self.assertIsNone(entry.nutrition.protein)
        # The receipt keeps inline actions; the main keyboard is restored by
        # the preceding message. Unnamed entries get no Save-as-favorite row.
        self.assertIs(MAIN_KEYBOARD, final_update.effective_message.replies[-2][1])
        receipt_markup = final_update.effective_message.replies[-1][1]
        self.assertNotIn(
            f"entry:fav:{entry.entry_id}", keyboard_callbacks(receipt_markup)
        )

    async def test_invalid_food_value_keeps_the_same_step(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        await handle_text(make_update("Add Food"), self.context)
        await handle_text(make_update("Rice"), self.context)
        before = self.database.get_session(1, 10)
        invalid = make_update("nan")

        await handle_text(invalid, self.context)

        self.assertEqual(
            SessionState.WAIT_CALORIES,
            self.database.get_session(1, 10).state,
        )
        self.assertGreater(self.database.get_session(1, 10).revision, before.revision)
        self.assertIn("finite", invalid.effective_message.replies[-1][0])

    async def test_favorite_use_and_amend_dialogs_switch_to_cancel_keyboard(
        self,
    ) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        favorite = self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)
        use_query = FakeQuery(f"fav:use:{favorite.favorite_id}", FakeMessage())

        await handle_callback(make_update(query=use_query), self.context)

        self.assertEqual(
            SessionState.WAIT_FAVORITE_GRAMS,
            self.database.get_session(1, 10).state,
        )
        self.assertIs(CANCEL_KEYBOARD, use_query.message.replies[-1][1])
        grams = make_update("40")
        await handle_text(grams, self.context)
        self.assertIsNone(self.database.get_session(1, 10))
        self.assertIs(MAIN_KEYBOARD, grams.effective_message.replies[-2][1])
        self.assertIsInstance(
            grams.effective_message.replies[-1][1], InlineKeyboardMarkup
        )

        amend_query = FakeQuery(
            f"fav:field:{favorite.favorite_id}:protein", FakeMessage()
        )
        await handle_callback(make_update(query=amend_query), self.context)
        self.assertEqual(
            SessionState.WAIT_FAVORITE_AMENDMENT,
            self.database.get_session(1, 10).state,
        )
        self.assertIs(CANCEL_KEYBOARD, amend_query.message.replies[-1][1])
        amended = make_update("15")
        await handle_text(amended, self.context)
        self.assertEqual(
            15.0,
            self.database.get_favorite(1, favorite.favorite_id).protein_per_100g,
        )
        self.assertIs(MAIN_KEYBOARD, amended.effective_message.replies[-1][1])

    async def test_favorite_prompt_send_failure_remains_resumable(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        favorite = self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)
        query = FakeQuery(f"fav:use:{favorite.favorite_id}", FailingMessage())

        with self.assertRaises(NetworkError):
            await handle_callback(make_update(query=query), self.context)

        pending = self.database.get_session(1, 10)
        self.assertEqual(SessionState.WAIT_FAVORITE_GRAMS, pending.state)
        self.assertTrue(pending.prompt_pending)
        recovery = make_update("/start")
        await start(recovery, self.context)
        self.assertIn("serving weight", recovery.effective_message.replies[-1][0])
        self.assertFalse(self.database.get_session(1, 10).prompt_pending)

    async def test_retrying_callback_delivers_the_pending_prompt(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        favorite = self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)
        callback_data = f"fav:use:{favorite.favorite_id}"

        with self.assertRaises(NetworkError):
            await handle_callback(
                make_update(query=FakeQuery(callback_data, FailingMessage())),
                self.context,
            )
        retry = FakeQuery(callback_data, FakeMessage())
        await handle_callback(make_update(query=retry), self.context)

        self.assertIn("serving weight", retry.message.replies[-1][0])
        self.assertFalse(self.database.get_session(1, 10).prompt_pending)
        await handle_text(make_update("40"), self.context)
        self.assertIsNone(self.database.get_session(1, 10))

    async def test_timezone_command_delivers_an_active_pending_prompt(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_GRAMS,
            now_utc=self.database.now_epoch(),
            draft_name="Rice",
            calories_per_100g=100,
            prompt_pending=True,
        )

        command = make_update("/updatetimezone")
        await update_timezone(command, self.context)

        self.assertIn("serving weight", command.effective_message.replies[-1][0])
        self.assertFalse(self.database.get_session(1, 10).prompt_pending)
        await handle_text(make_update("40"), self.context)
        self.assertEqual(
            SessionState.WAIT_PROTEIN,
            self.database.get_session(1, 10).state,
        )

    async def test_dismissed_confirmation_does_not_cancel_active_workflow(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_CALORIES,
            now_utc=self.database.now_epoch(),
            draft_name="Rice",
        )
        query = FakeQuery("dismiss", FakeMessage())

        await handle_callback(make_update(query=query), self.context)

        self.assertEqual(
            SessionState.WAIT_CALORIES,
            self.database.get_session(1, 10).state,
        )
        self.assertEqual("No changes made.", query.edits[-1][0])

    async def test_owned_favorite_and_entry_delete_confirmations(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        favorite = self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)
        session = self.database.start_session(
            1,
            10,
            SessionState.WAIT_CARBS,
            now_utc=3,
            draft_name=None,
            calories_per_100g=100,
            serving_grams=100,
        )
        entry = self.database.complete_food_draft(session, 4)
        issued_at = self.database.now_epoch()

        favorite_query = FakeQuery(
            f"fav:delete-confirm:{favorite.favorite_id}:{issued_at}", FakeMessage()
        )
        await handle_callback(make_update(query=favorite_query), self.context)
        entry_query = FakeQuery(
            f"entry:delete-confirm:{entry.entry_id}:{issued_at}", FakeMessage()
        )
        await handle_callback(make_update(query=entry_query), self.context)

        self.assertIsNone(self.database.get_favorite(1, favorite.favorite_id))
        self.assertIsNone(self.database.get_entry(1, entry.entry_id))

    async def test_stale_favorite_callback_cannot_replace_active_food_draft(
        self,
    ) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_CALORIES,
            now_utc=self.database.now_epoch(),
            draft_name="Lunch",
        )
        favorite = self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)
        query = FakeQuery(f"fav:use:{favorite.favorite_id}", FakeMessage())

        await handle_callback(make_update(query=query), self.context)

        session = self.database.get_session(1, 10)
        self.assertEqual(SessionState.WAIT_CALORIES, session.state)
        self.assertEqual("Lunch", session.draft_name)
        self.assertIn("Finish the current input", query.message.replies[-1][0])

    async def test_final_food_message_uses_telegram_timestamp(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        for text in ("Add Food", "Skip", "100", "25", "Skip", "Skip"):
            await handle_text(make_update(text), self.context)
        sent_at = datetime(2024, 1, 1, 23, 59, tzinfo=timezone.utc)

        await handle_text(make_update("Skip", date=sent_at), self.context)

        entry = self.database.page_entries(1, 0, 4_000_000_000).items[0]
        self.assertEqual(int(sent_at.timestamp()), entry.eaten_at_utc)

    async def test_group_message_is_rejected_without_database_side_effects(
        self,
    ) -> None:
        update = make_update("Add Food", chat_type=ChatType.GROUP)

        await handle_text(update, self.context)

        self.assertIsNone(self.database.get_timezone(1))
        self.assertIsNone(self.database.get_session(1, 10))
        self.assertIn("private chat", update.effective_message.replies[-1][0])

    async def test_start_recovers_step_after_prompt_send_failure(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_CALORIES,
            now_utc=self.database.now_epoch(),
            draft_name="Rice",
        )
        failing_update = make_update("100")
        failing_update.effective_message = FailingMessage("100")

        with self.assertRaises(NetworkError):
            await handle_text(failing_update, self.context)

        self.assertEqual(
            SessionState.WAIT_GRAMS,
            self.database.get_session(1, 10).state,
        )
        recovery = make_update("/start")
        await start(recovery, self.context)
        self.assertIn("serving weight", recovery.effective_message.replies[-1][0])

    async def test_retry_after_prompt_failure_is_not_consumed_as_next_field(
        self,
    ) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_CALORIES,
            now_utc=self.database.now_epoch(),
            draft_name="Rice",
        )
        failing_update = make_update("100")
        failing_update.effective_message = FailingMessage("100")
        with self.assertRaises(NetworkError):
            await handle_text(failing_update, self.context)

        retry = make_update("100")
        await handle_text(retry, self.context)

        session = self.database.get_session(1, 10)
        self.assertEqual(SessionState.WAIT_GRAMS, session.state)
        self.assertIsNone(session.serving_grams)
        self.assertFalse(session.prompt_pending)
        self.assertIn("previous prompt", retry.effective_message.replies[-1][0].lower())

    async def test_unknown_command_delivers_a_pending_prompt(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_GRAMS,
            now_utc=self.database.now_epoch(),
            draft_name="Rice",
            calories_per_100g=100,
            prompt_pending=True,
        )

        command = make_update("/unknown")
        await unknown_command(command, self.context)

        session = self.database.get_session(1, 10)
        self.assertFalse(session.prompt_pending)
        self.assertIn("serving weight", command.effective_message.replies[-1][0])

    async def test_duplicate_message_id_is_not_applied_to_next_field(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_CALORIES,
            now_utc=self.database.now_epoch(),
            draft_name="Rice",
        )
        update = make_update("100")

        await handle_text(update, self.context)
        await handle_text(update, self.context)

        session = self.database.get_session(1, 10)
        self.assertEqual(SessionState.WAIT_GRAMS, session.state)
        self.assertIsNone(session.serving_grams)
        self.assertIn("already processed", update.effective_message.replies[-1][0])

    async def test_start_expires_week_old_session(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        self.database.start_session(
            1, 10, SessionState.WAIT_CALORIES, now_utc=1, draft_name="Old"
        )
        update = make_update("/start")

        await start(update, self.context)

        self.assertIsNone(self.database.get_session(1, 10))
        self.assertIs(MAIN_KEYBOARD, update.effective_message.replies[-1][1])

    async def test_receipt_undo_deletes_the_committed_entry(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        final_update = None
        for text in ("Add Food", "Rice", "100", "25", "Skip", "Skip", "Skip"):
            final_update = make_update(text)
            await handle_text(final_update, self.context)

        self.assertIsNone(self.database.get_session(1, 10))
        entry = self.database.page_entries(1, 0, 4_000_000_000).items[0]
        receipt_markup = final_update.effective_message.replies[-1][1]
        undo_callback = next(
            callback
            for callback in keyboard_callbacks(receipt_markup)
            if callback.startswith("entry:delete-confirm:")
        )

        undo_query = FakeQuery(undo_callback, FakeMessage())
        await handle_callback(make_update(query=undo_query), self.context)

        self.assertIsNone(self.database.get_entry(1, entry.entry_id))
        self.assertIn("deleted", undo_query.edits[-1][0].lower())

    async def test_stats_label_partial_macronutrient_coverage(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        now = self.database.now_epoch()
        first = self.database.start_session(
            1,
            20,
            SessionState.WAIT_CARBS,
            now_utc=now,
            calories_per_100g=100,
            serving_grams=100,
            protein_per_100g=10,
        )
        second = self.database.start_session(
            1,
            21,
            SessionState.WAIT_CARBS,
            now_utc=now,
            calories_per_100g=100,
            serving_grams=100,
        )
        self.database.complete_food_draft(first, now)
        self.database.complete_food_draft(second, now)
        update = make_update("Food Today")

        await handle_text(update, self.context)

        response = update.effective_message.replies[-1][0]
        self.assertIn("Protein: 10.00g", response)
        self.assertIn("partial: 1/2 entries", response)

    async def test_expired_delete_confirmation_is_non_destructive(self) -> None:
        self.database.ensure_user(1, 1)
        favorite = self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)
        query = FakeQuery(f"fav:delete-confirm:{favorite.favorite_id}:1", FakeMessage())

        await handle_callback(make_update(query=query), self.context)

        self.assertIsNotNone(self.database.get_favorite(1, favorite.favorite_id))
        self.assertIn("expired", query.edits[-1][0])


class NewFeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "bot.db")
        self.database.initialize()
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"database": self.database})
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def entries(self):
        return self.database.page_entries(1, 0, 4_000_000_000).items

    async def test_quick_add_message_logs_entry_with_progress(self) -> None:
        update = make_update("Rice 250 40")

        await handle_text(update, self.context)

        entries = self.entries()
        self.assertEqual(1, len(entries))
        self.assertEqual("Rice", entries[0].name)
        self.assertAlmostEqual(100.0, entries[0].nutrition.calories)
        reply = update.effective_message.replies[-1]
        self.assertIn("Today: 100", reply[0])
        self.assertIsInstance(reply[1], InlineKeyboardMarkup)
        callbacks = keyboard_callbacks(reply[1])
        self.assertIn(f"entry:fav:{entries[0].entry_id}", callbacks)
        self.assertTrue(any(c.startswith("entry:delete-confirm:") for c in callbacks))

    async def test_quick_add_reports_remaining_goal(self) -> None:
        self.database.set_daily_goal(1, 2000.0, 2)

        update = make_update("Rice 250 40")
        await handle_text(update, self.context)

        self.assertIn("2000 kcal (1900 left)", update.effective_message.replies[-1][0])

    async def test_quick_add_validation_error_is_reported(self) -> None:
        update = make_update("Rice 20000 40")

        await handle_text(update, self.context)

        self.assertEqual(0, len(self.entries()))
        self.assertIn("no more than", update.effective_message.replies[-1][0])

    async def test_non_food_text_falls_back_to_menu_hint(self) -> None:
        update = make_update("hello there")

        await handle_text(update, self.context)

        self.assertEqual(0, len(self.entries()))
        self.assertIn("keyboard", update.effective_message.replies[-1][0])

    async def test_add_command_logs_entry(self) -> None:
        update = make_update("/add Rice 250 40")

        await add_command(update, self.context)

        entries = self.entries()
        self.assertEqual(1, len(entries))
        self.assertEqual("Rice", entries[0].name)

    async def test_add_command_without_payload_shows_usage(self) -> None:
        update = make_update("/add")

        await add_command(update, self.context)

        self.assertEqual(0, len(self.entries()))
        self.assertIn("Example", update.effective_message.replies[-1][0])

    async def test_add_command_is_blocked_by_an_active_workflow(self) -> None:
        self.database.start_session(
            1,
            10,
            SessionState.WAIT_CALORIES,
            now_utc=self.database.now_epoch(),
            draft_name="Soup",
        )

        update = make_update("/add Rice 250 40")
        await add_command(update, self.context)

        self.assertEqual(0, len(self.entries()))
        self.assertIn("Finish or cancel", update.effective_message.replies[-1][0])

    async def test_add_food_matches_a_saved_favorite(self) -> None:
        favorite = self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)
        await handle_text(make_update("Add Food"), self.context)

        name_update = make_update("rice")
        await handle_text(name_update, self.context)

        session = self.database.get_session(1, 10)
        self.assertEqual(SessionState.WAIT_FAVORITE_GRAMS, session.state)
        self.assertEqual(favorite.favorite_id, session.selected_favorite_id)
        self.assertIn(
            "Found favorite Rice", name_update.effective_message.replies[-1][0]
        )

        await handle_text(make_update("40"), self.context)

        entries = self.entries()
        self.assertEqual(1, len(entries))
        self.assertAlmostEqual(100.0, entries[0].nutrition.calories)
        self.assertIsNone(self.database.get_session(1, 10))

    async def test_favorite_match_allows_manual_entry_override(self) -> None:
        self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)
        await handle_text(make_update("Add Food"), self.context)
        await handle_text(make_update("Rice"), self.context)

        await handle_text(make_update("Enter Manually"), self.context)

        session = self.database.get_session(1, 10)
        self.assertEqual(SessionState.WAIT_CALORIES, session.state)
        self.assertEqual("Rice", session.draft_name)
        self.assertIsNone(session.selected_favorite_id)

    async def test_daily_goal_menu_sets_and_removes_the_goal(self) -> None:
        await handle_text(make_update("Daily Goal"), self.context)
        self.assertEqual(SessionState.WAIT_GOAL, self.database.get_session(1, 10).state)

        await handle_text(make_update("2000"), self.context)
        self.assertEqual(2000.0, self.database.get_daily_goal(1))
        self.assertIsNone(self.database.get_session(1, 10))

        await handle_text(make_update("Daily Goal"), self.context)
        await handle_text(make_update("Remove"), self.context)
        self.assertIsNone(self.database.get_daily_goal(1))

    async def test_food_today_shows_goal_progress(self) -> None:
        self.database.set_daily_goal(1, 2000.0, 2)
        self.database.add_entry(1, self.database.now_epoch(), "Rice", 250.0, 40.0)

        update = make_update("Food Today")
        await handle_text(update, self.context)

        self.assertIn("2000 goal (1900 left)", update.effective_message.replies[-1][0])

    async def test_week_stats_show_whole_week_on_one_page(self) -> None:
        now = self.database.now_epoch()
        for day in range(7):
            self.database.add_entry(
                1, now - day * 86_400, "Rice", 250.0, 40.0, 10.0, 20.0, 30.0
            )

        update = make_update("Week Stats")
        await handle_text(update, self.context)

        text, keyboard = update.effective_message.replies[-1]
        self.assertIn("Last 7 days", text)
        self.assertEqual(7, text.count("kcal"))
        self.assertIn("Protein: 4.00g", text)
        day_callbacks = [
            callback
            for callback in keyboard_callbacks(keyboard)
            if callback.startswith("entry:list:")
        ]
        self.assertEqual(7, len(day_callbacks))

    async def test_month_stats_show_current_month_with_month_navigation(self) -> None:
        now = self.database.now_epoch()
        today = datetime.fromtimestamp(now, timezone.utc).date()
        self.database.add_entry(1, now, "Rice", 250.0, 40.0, 10.0, 20.0, 30.0)

        update = make_update("Month Stats")
        await handle_text(update, self.context)

        text, keyboard = update.effective_message.replies[-1]
        self.assertIn(f"{today:%B %Y}", text)
        self.assertIn("kcal", text)
        previous = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        month_row = keyboard.inline_keyboard[-1]
        self.assertEqual(
            [f"stats:month:{previous:%Y-%m}:0"],
            [button.callback_data for button in month_row],
        )

    async def test_month_stats_switch_to_the_selected_month(self) -> None:
        now = self.database.now_epoch()
        today = datetime.fromtimestamp(now, timezone.utc).date()
        previous = (today.replace(day=1) - timedelta(days=1)).replace(day=1)

        query = FakeQuery(f"stats:month:{previous:%Y-%m}:0", FakeMessage())
        await handle_callback(make_update(query=query), self.context)

        text, keyboard = query.edits[-1]
        self.assertIn(f"{previous:%B %Y}", text)
        month_callbacks = [
            button.callback_data for button in keyboard.inline_keyboard[-1]
        ]
        self.assertIn(f"stats:month:{today:%Y-%m}:0", month_callbacks)

    async def test_food_today_shows_totals_and_day_navigation(self) -> None:
        now = self.database.now_epoch()
        today = datetime.fromtimestamp(now, timezone.utc).date()
        yesterday = today - timedelta(days=1)
        self.database.add_entry(1, now, "Rice", 250.0, 40.0, 10.0, 20.0, 30.0)
        self.database.add_entry(1, now - 86_400, "Soup", 100.0, 200.0)

        update = make_update("Food Today")
        await handle_text(update, self.context)

        text, keyboard = update.effective_message.replies[-1]
        self.assertIn("Today's totals", text)
        self.assertIn("Today's food entries", text)
        last_row = [b.callback_data for b in keyboard.inline_keyboard[-1]]
        self.assertEqual([f"entry:list:{yesterday.isoformat()}:0"], last_row)

        query = FakeQuery(f"entry:list:{yesterday.isoformat()}:0", FakeMessage())
        await handle_callback(make_update(query=query), self.context)

        text, keyboard = query.edits[-1]
        self.assertIn("Yesterday's totals", text)
        self.assertIn("Calories: 200.00", text)
        callbacks = keyboard_callbacks(keyboard)
        self.assertIn("entry:list:0", callbacks)

    async def test_stats_yesterday_callback_still_opens_yesterday(self) -> None:
        query = FakeQuery("stats:yesterday", FakeMessage())

        await handle_callback(make_update(query=query), self.context)

        self.assertIn("No food entries found for", query.edits[-1][0])

    async def test_backdated_entry_is_editable_from_its_day_page(self) -> None:
        now = self.database.now_epoch()
        day = datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=3)
        entry = self.database.add_entry(1, now - 3 * 86_400, "Soup", 100.0, 200.0)

        query = FakeQuery(f"entry:list:{day.isoformat()}:0", FakeMessage())
        await handle_callback(make_update(query=query), self.context)

        text, keyboard = query.edits[-1]
        self.assertIn(f"Entries for {day.isoformat()}", text)
        view_callback = f"entry:view:{entry.entry_id}:0:{day.isoformat()}"
        self.assertIn(view_callback, keyboard_callbacks(keyboard))

        view_query = FakeQuery(view_callback, FakeMessage())
        await handle_callback(make_update(query=view_query), self.context)
        text, keyboard = view_query.edits[-1]
        self.assertIn("Soup", text)
        self.assertIn("Time:", text)
        callbacks = keyboard_callbacks(keyboard)
        self.assertIn(f"entry:grams:{entry.entry_id}", callbacks)
        self.assertIn(f"entry:name:{entry.entry_id}", callbacks)
        self.assertIn(f"entry:field:{entry.entry_id}:calories", callbacks)
        self.assertIn(f"entry:list:{day.isoformat()}:0", callbacks)

    async def test_week_stats_without_entries_report_empty_period(self) -> None:
        update = make_update("Week Stats")

        await handle_text(update, self.context)

        self.assertIn(
            "No food entries found for week",
            update.effective_message.replies[-1][0],
        )

    async def test_recent_foods_flow_repeats_the_last_serving(self) -> None:
        source = self.database.add_entry(
            1, self.database.now_epoch(), "Rice", 250.0, 40.0, 10.0, 20.0, 30.0
        )

        menu_update = make_update("Recent Foods")
        await handle_text(menu_update, self.context)
        self.assertIn("recent", menu_update.effective_message.replies[-1][0].lower())

        query = FakeQuery(f"recent:use:{source.entry_id}", FakeMessage())
        await handle_callback(make_update(query=query), self.context)
        self.assertEqual(
            SessionState.WAIT_RECENT_GRAMS, self.database.get_session(1, 10).state
        )

        await handle_text(make_update("Same as last time"), self.context)

        entries = self.entries()
        self.assertEqual(2, len(entries))
        self.assertAlmostEqual(40.0, entries[0].nutrition.grams)
        self.assertIsNone(self.database.get_session(1, 10))

    async def test_entry_grams_edit_recomputes_totals(self) -> None:
        entry = self.database.add_entry(
            1, self.database.now_epoch(), "Rice", 250.0, 40.0, 10.0, 20.0, 30.0
        )

        query = FakeQuery(f"entry:grams:{entry.entry_id}", FakeMessage())
        await handle_callback(make_update(query=query), self.context)
        self.assertEqual(
            SessionState.WAIT_ENTRY_GRAMS, self.database.get_session(1, 10).state
        )

        grams_update = make_update("80")
        await handle_text(grams_update, self.context)

        stored = self.database.get_entry(1, entry.entry_id)
        self.assertAlmostEqual(80.0, stored.nutrition.grams)
        self.assertAlmostEqual(200.0, stored.nutrition.calories)
        self.assertIn("updated", grams_update.effective_message.replies[-1][0])

    async def test_serving_wizard_bypasses_per_100g_macro_limit(self) -> None:
        for text in ("Add Food", "Pizza", "Per Serving", "900", "0.5", "40", "30"):
            await handle_text(make_update(text), self.context)
        final_update = make_update("120")

        await handle_text(final_update, self.context)

        self.assertIsNone(self.database.get_session(1, 10))
        entry = self.entries()[0]
        self.assertEqual("Pizza", entry.name)
        self.assertEqual(0.5, entry.nutrition.servings)
        self.assertIsNone(entry.nutrition.grams)
        self.assertAlmostEqual(450.0, entry.nutrition.calories)
        self.assertAlmostEqual(60.0, entry.nutrition.carbs)
        self.assertIn("0.5 servings", final_update.effective_message.replies[-1][0])

    async def test_favorite_conversion_and_fractional_serving_use(self) -> None:
        favorite = self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)

        convert_query = FakeQuery(f"fav:serving:{favorite.favorite_id}", FakeMessage())
        await handle_callback(make_update(query=convert_query), self.context)
        self.assertEqual(
            SessionState.WAIT_FAVORITE_TO_SERVING,
            self.database.get_session(1, 10).state,
        )
        grams_update = make_update("50")
        await handle_text(grams_update, self.context)

        converted = self.database.get_favorite(1, favorite.favorite_id)
        self.assertEqual("serving", converted.unit)
        self.assertEqual(50.0, converted.serving_grams)
        self.assertAlmostEqual(125.0, converted.calories_per_100g)

        use_query = FakeQuery(f"fav:use:{favorite.favorite_id}", FakeMessage())
        await handle_callback(make_update(query=use_query), self.context)
        self.assertEqual(
            SessionState.WAIT_FAVORITE_SERVINGS,
            self.database.get_session(1, 10).state,
        )
        await handle_text(make_update("0.5"), self.context)

        entry = self.entries()[0]
        self.assertEqual(0.5, entry.nutrition.servings)
        self.assertAlmostEqual(25.0, entry.nutrition.grams)
        self.assertAlmostEqual(62.5, entry.nutrition.calories)
        self.assertIsNone(self.database.get_session(1, 10))

    async def test_entry_calories_edit_rescales_totals(self) -> None:
        entry = self.database.add_entry(
            1, self.database.now_epoch(), "Rice", 250.0, 40.0, 10.0, 20.0, 30.0
        )

        query = FakeQuery(f"entry:field:{entry.entry_id}:calories", FakeMessage())
        await handle_callback(make_update(query=query), self.context)
        self.assertEqual(
            SessionState.WAIT_ENTRY_AMENDMENT, self.database.get_session(1, 10).state
        )
        self.assertIn("per 100g", query.message.replies[-1][0])

        await handle_text(make_update("300"), self.context)

        stored = self.database.get_entry(1, entry.entry_id)
        self.assertAlmostEqual(120.0, stored.nutrition.calories)
        self.assertAlmostEqual(4.0, stored.nutrition.protein)
        self.assertIsNone(self.database.get_session(1, 10))

    async def test_entry_name_edit_renames_the_entry(self) -> None:
        entry = self.database.add_entry(1, self.database.now_epoch(), None, 250.0, 40.0)

        query = FakeQuery(f"entry:name:{entry.entry_id}", FakeMessage())
        await handle_callback(make_update(query=query), self.context)
        rename_update = make_update("Buckwheat")
        await handle_text(rename_update, self.context)

        stored = self.database.get_entry(1, entry.entry_id)
        self.assertEqual("Buckwheat", stored.name)
        self.assertIn("renamed", rename_update.effective_message.replies[-1][0])

    async def test_weight_command_logs_and_reports_average(self) -> None:
        update = make_update("/weight 82.5")

        await weight_command(update, self.context)

        stored = self.database.latest_weight(1)
        self.assertEqual(82.5, stored.weight_kg)
        reply = update.effective_message.replies[-1][0]
        self.assertIn("Weight recorded", reply)
        self.assertIn("7-day average: 82.5 kg", reply)

    async def test_weight_menu_flow_records_measurement(self) -> None:
        await handle_text(make_update("Weight"), self.context)
        self.assertEqual(
            SessionState.WAIT_WEIGHT, self.database.get_session(1, 10).state
        )

        value_update = make_update("81,9")
        await handle_text(value_update, self.context)

        self.assertIsNone(self.database.get_session(1, 10))
        self.assertEqual(81.9, self.database.latest_weight(1).weight_kg)
        self.assertIn("7-day average", value_update.effective_message.replies[-1][0])

    async def test_weight_average_reports_week_over_week_delta(self) -> None:
        now = self.database.now_epoch()
        for days_ago, weight in ((10, 84.0), (9, 84.0), (3, 83.0), (1, 83.0)):
            self.database.add_weight(1, now - days_ago * 86_400, weight)

        update = make_update("/weight 83")
        await weight_command(update, self.context)

        reply = update.effective_message.replies[-1][0]
        self.assertIn("7-day average: 83.0 kg", reply)
        self.assertIn("-1.0 kg vs previous week", reply)

    async def test_entry_time_edit_backdates_the_entry(self) -> None:
        entry = self.database.add_entry(
            1, self.database.now_epoch(), "Rice", 250.0, 40.0
        )
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        text = yesterday.strftime("%Y-%m-%d") + " 12:00"
        expected = int(
            yesterday.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        )

        query = FakeQuery(f"entry:time:{entry.entry_id}", FakeMessage())
        await handle_callback(make_update(query=query), self.context)
        await handle_text(make_update(text), self.context)

        stored = self.database.get_entry(1, entry.entry_id)
        self.assertEqual(expected, stored.eaten_at_utc)
        self.assertIsNone(self.database.get_session(1, 10))

    async def test_future_entry_time_keeps_the_session_for_retry(self) -> None:
        entry = self.database.add_entry(
            1, self.database.now_epoch(), "Rice", 250.0, 40.0
        )
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)

        query = FakeQuery(f"entry:time:{entry.entry_id}", FakeMessage())
        await handle_callback(make_update(query=query), self.context)
        rejected = make_update(tomorrow.strftime("%Y-%m-%d") + " 12:00")
        await handle_text(rejected, self.context)

        self.assertEqual(
            SessionState.WAIT_ENTRY_TIME, self.database.get_session(1, 10).state
        )
        self.assertIn("future", rejected.effective_message.replies[-1][0])


if __name__ == "__main__":
    unittest.main()
