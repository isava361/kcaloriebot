from __future__ import annotations

import tempfile
import unittest
from itertools import count
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram.constants import ChatType
from telegram.error import NetworkError

from kcaloriebot.bot import (
    CANCEL_KEYBOARD,
    MAIN_KEYBOARD,
    _show_entries,
    handle_callback,
    handle_text,
    start,
    unknown_command,
    update_timezone,
)
from kcaloriebot.database import Database
from kcaloriebot.domain import SessionState


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
        self.assertIs(CANCEL_KEYBOARD, update.effective_message.replies[-1][1])

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
        messages = [
            "Add Food",
            "Rice",
            "250",
            "40",
            "10",
            "20",
            "30",
            "Yes",
        ]

        for text in messages:
            await handle_text(make_update(text), self.context)

        self.assertIsNone(self.database.get_session(1, 10))
        entries = self.database.page_entries(1, 0, 4_000_000_000)
        favorites = self.database.page_favorites(1)
        self.assertEqual(1, len(entries.items))
        self.assertEqual(1, len(favorites.items))
        self.assertEqual("Rice", entries.items[0].name)
        self.assertAlmostEqual(100.0, entries.items[0].nutrition.calories)
        self.assertAlmostEqual(12.0, entries.items[0].nutrition.carbs)
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
        self.assertIs(MAIN_KEYBOARD, final_update.effective_message.replies[-1][1])

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
        self.assertIs(MAIN_KEYBOARD, grams.effective_message.replies[-1][1])

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

    async def test_cancel_after_entry_commit_only_skips_favorite(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "UTC", 1)
        for text in ("Add Food", "Rice", "100", "25", "Skip", "Skip", "Skip"):
            await handle_text(make_update(text), self.context)
        cancel_update = make_update("Cancel")

        await handle_text(cancel_update, self.context)

        self.assertIsNone(self.database.get_session(1, 10))
        self.assertEqual(
            1,
            len(self.database.page_entries(1, 0, 4_000_000_000).items),
        )
        self.assertIn("entry remains", cancel_update.effective_message.replies[-1][0])

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
        update = make_update("Today Stats")

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


if __name__ == "__main__":
    unittest.main()
