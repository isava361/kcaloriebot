from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram.constants import ChatType

from kcaloriebot.bot import (
    MAIN_KEYBOARD,
    _show_entries,
    handle_callback,
    handle_text,
    start,
)
from kcaloriebot.database import Database
from kcaloriebot.domain import SessionState


class FakeMessage:
    def __init__(self, text: str | None = None, accessible: bool = True):
        self.text = text
        self.is_accessible = accessible
        self.replies: list[tuple[str, Any]] = []

    async def reply_text(self, text: str, reply_markup: Any = None) -> None:
        self.replies.append((text, reply_markup))


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
) -> SimpleNamespace:
    message = query.message if query is not None else FakeMessage(text)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=ChatType.PRIVATE),
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

    async def test_start_resets_an_active_workflow(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        self.database.start_session(1, 10, SessionState.WAIT_CALORIES, now_utc=2)
        update = make_update("/start")

        await start(update, self.context)

        self.assertIsNone(self.database.get_session(1, 10))
        self.assertIs(MAIN_KEYBOARD, update.effective_message.replies[-1][1])

    async def test_favorite_search_restores_main_reply_keyboard(self) -> None:
        self.database.ensure_user(1, 1)
        self.database.set_timezone(1, "Europe/Moscow", 1)
        self.database.add_favorite(1, "Rice", 250, 10, 20, 30, 2)
        self.database.start_session(1, 10, SessionState.WAIT_FAVORITE_SEARCH, now_utc=3)
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


if __name__ == "__main__":
    unittest.main()
