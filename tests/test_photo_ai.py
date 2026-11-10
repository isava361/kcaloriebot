from __future__ import annotations

import asyncio
import base64
import math
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from telegram.error import BadRequest

from kcaloriebot.bot import (
    ADMIN_USER_ID,
    MAIN_KEYBOARD,
    MAX_DESCRIPTION_CHARS,
    PHOTO_ALBUM_FULL_TEXT,
    PHOTO_CORRECTION_FAILED_TEXT,
    PHOTO_EXPIRED_TEXT,
    PHOTO_FAILED_TEXT,
    PHOTO_RESTART_TEXT,
    TEXT_FAILED_TEXT,
    TEXT_REPLY_IGNORED_TEXT,
    TEXT_RESTART_TEXT,
    TEXT_TOO_LONG_TEXT,
    _stop_photo_tasks,
    handle_callback,
    handle_photo,
    handle_photo_callback,
    handle_text,
    unknown_command,
)
from kcaloriebot.config import ConfigError, load_settings
from kcaloriebot.database import Database
from kcaloriebot.domain import (
    UNIT_100G,
    UNIT_SERVING,
    FavoriteFood,
    FoodEntry,
    NutritionTotals,
    ValidationError,
    parse_quick_add,
)
from kcaloriebot.photo_ai import (
    EYE_ESTIMATE_UPWARD_SHARE,
    GRAMS_SOURCES,
    MAX_OWNER_FOOD_NAME_CHARS,
    MAX_OWNER_FOODS_CHARS,
    NUTRITION_SOURCES,
    RECORD_SCHEMA,
    TOOLS,
    FoodGuess,
    OwnerFood,
    PhotoAnalysis,
    PhotoAnalyzer,
    PhotoImage,
    PhotoUnavailable,
    build_analyzer,
    correction_text,
    owner_food_line,
    owner_foods_from,
    parse_analysis,
)
from kcaloriebot.photo_proposals import (
    MAX_ALERT_CHARS,
    PROPOSAL_TTL_SECONDS,
    PhotoProposal,
    PhotoProposalStore,
    missing_weight_alert,
    proposal_text,
)
from tests.test_bot_handlers import (
    FakeMessage,
    FakeQuery,
    keyboard_callbacks,
    make_update,
)


class FakeTelegramFile:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self.data)


class FakePhotoSize:
    def __init__(self, data: bytes = b"jpeg-bytes") -> None:
        self.data = data
        self.file_size = 1000

    async def get_file(self) -> FakeTelegramFile:
        return FakeTelegramFile(self.data)


JPEG = PhotoImage(b"jpeg-bytes", "image/jpeg")


class FakeAnalyzer:
    def __init__(self, result: PhotoAnalysis | Exception) -> None:
        self.result = result
        self.calls: list[tuple[tuple[PhotoImage, ...], str | None]] = []
        self.revisions: list[dict[str, Any]] = []
        # Cleared to hold the analysis until a test releases it.
        self.gate = asyncio.Event()
        self.gate.set()

    async def analyze(
        self,
        images: tuple[PhotoImage, ...],
        caption: str | None = None,
        **revision: Any,
    ) -> PhotoAnalysis:
        self.calls.append((tuple(images), caption))
        self.revisions.append(revision)
        await self.gate.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class SentMessage(FakeMessage):
    """A message the bot sent; it can be edited or deleted later."""

    def __init__(self, text: str, reply_markup: Any = None) -> None:
        super().__init__(text)
        self.reply_markup = reply_markup
        self.edits: list[tuple[str, Any]] = []
        self.deleted = False

    async def edit_text(self, text: str, reply_markup: Any = None) -> SentMessage:
        self.edits.append((text, reply_markup))
        self.text, self.reply_markup = text, reply_markup
        return self

    async def delete(self) -> bool:
        self.deleted = True
        return True


class ChatMessage(FakeMessage):
    """An incoming message that keeps the bot's replies as editable messages."""

    def __init__(self, text: str | None = None) -> None:
        super().__init__(text)
        self.sent: list[SentMessage] = []
        self.quoted: list[bool] = []

    async def reply_text(
        self, text: str, reply_markup: Any = None, **kwargs: Any
    ) -> SentMessage:
        await super().reply_text(text, reply_markup)
        self.quoted.append(bool(kwargs.get("do_quote")))
        sent = SentMessage(text, reply_markup)
        self.sent.append(sent)
        return sent


def photo_update(
    user_id: int = ADMIN_USER_ID,
    caption: str | None = None,
    media_group_id: str | None = None,
    data: bytes = b"jpeg-bytes",
) -> Any:
    update = make_update(user_id=user_id)
    message = ChatMessage()
    message.date = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    message.photo = (FakePhotoSize(b"thumbnail"), FakePhotoSize(data))
    message.document = None
    message.caption = caption
    message.media_group_id = media_group_id
    update.effective_message = message
    return update


def text_update(text: str, reply_to: Any = None) -> Any:
    update = make_update(user_id=ADMIN_USER_ID)
    message = ChatMessage(text)
    message.reply_to_message = reply_to
    update.effective_message = message
    return update


def tap(data: str, message: FakeMessage) -> Any:
    return make_update(user_id=ADMIN_USER_ID, query=FakeQuery(data, message))


class ParseAnalysisTests(unittest.TestCase):
    def test_valid_items_are_kept_and_invalid_ones_dropped(self) -> None:
        analysis = parse_analysis(
            {
                "foods": [
                    {
                        "name": "  Гречка   отварная ",
                        "grams": 180,
                        "calories_per_100g": 110,
                        "protein_per_100g": 4.2,
                        "fat_per_100g": 1.1,
                        "carbs_per_100g": 21.3,
                    },
                    {"name": "", "grams": 10, "calories_per_100g": 10},
                    {"name": "Вода", "grams": 0, "calories_per_100g": 0},
                    {
                        "name": "Котлета",
                        "grams": 90,
                        "calories_per_100g": 220,
                        "protein_per_100g": 60,
                        "fat_per_100g": 50,
                        "carbs_per_100g": 10,
                    },
                ],
                "weight_kg": 0,
                "comment": " Обед. ",
            }
        )

        self.assertEqual(
            (
                FoodGuess("Гречка отварная", 180.0, 110.0, 4.2, 1.1, 21.3),
                FoodGuess("Котлета", 90.0, 220.0, None, None, None),
            ),
            analysis.foods,
        )
        self.assertIsNone(analysis.weight_kg)
        self.assertEqual("Обед.", analysis.comment)

    def test_scale_reading_is_reported(self) -> None:
        analysis = parse_analysis({"foods": [], "weight_kg": 81.4, "comment": ""})

        self.assertEqual((), analysis.foods)
        self.assertEqual(81.4, analysis.weight_kg)

    def test_non_object_is_rejected(self) -> None:
        with self.assertRaises(PhotoUnavailable):
            parse_analysis([])


class FakeMessages:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        # Snapshot the history: the analyzer appends to the same list later.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def response(stop_reason: str, *content: Any) -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason, _request_id="req_test", content=list(content)
    )


def record_block(payload: dict[str, Any]) -> Any:
    return SimpleNamespace(type="tool_use", name="record_foods", input=payload)


SEARCH_BLOCK = SimpleNamespace(type="server_tool_use", name="web_search", input={})
EMPTY_RECORD = {"foods": [], "weight_kg": 0, "comment": "Пусто."}


def fake_client(*responses: Any) -> tuple[Any, FakeMessages]:
    messages = FakeMessages(*responses)
    return SimpleNamespace(messages=messages), messages


class PhotoAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_sends_image_caption_tools_and_date(self) -> None:
        client, messages = fake_client(response("tool_use", record_block(EMPTY_RECORD)))
        analyzer = PhotoAnalyzer(client, "claude-test", "low")
        analyzer._today = lambda: date(2026, 10, 17)

        analysis = await analyzer.analyze([PhotoImage(b"img", "image/png")], "200 г")

        self.assertEqual("Пусто.", analysis.comment)
        kwargs = messages.calls[0]
        self.assertEqual("claude-test", kwargs["model"])
        self.assertEqual({"effort": "low"}, kwargs["output_config"])
        self.assertIn("The current date is 2026-10-17.", kwargs["system"])
        search, record = kwargs["tools"]
        self.assertEqual(
            ("web_search_20250305", 3), (search["type"], search["max_uses"])
        )
        self.assertEqual(("record_foods", True), (record["name"], record["strict"]))
        image, caption = kwargs["messages"][0]["content"]
        self.assertEqual("image/png", image["source"]["media_type"])
        self.assertEqual(b"img", base64.b64decode(image["source"]["data"]))
        self.assertEqual("Caption: 200 г", caption["text"])

    async def test_correction_sends_previous_answer_and_corrections(self) -> None:
        client, messages = fake_client(response("tool_use", record_block(EMPTY_RECORD)))
        previous = PhotoAnalysis(
            (FoodGuess("Гречка", 200.0, 110.0, 4.0, None, 21.0),), 81.4, "Обед."
        )

        await PhotoAnalyzer(client, "claude-test", "low").analyze(
            [PhotoImage(b"img", "image/jpeg")],
            "обед",
            previous=previous,
            corrections=("гречки было 150 г", "курица без масла"),
        )

        image, caption, correction = messages.calls[0]["messages"][0]["content"]
        self.assertEqual("Caption: обед", caption["text"])
        text = correction["text"]
        self.assertIn("- Гречка: 200 g; per 100 g: 110 kcal", text)
        self.assertIn("fat unknown", text)
        self.assertIn("- scale reading: 81.4 kg", text)
        self.assertIn("- гречки было 150 г\n- курица без масла", text)

    async def test_paused_search_turn_is_resumed(self) -> None:
        paused = response("pause_turn", SEARCH_BLOCK)
        client, messages = fake_client(
            paused, response("tool_use", record_block(EMPTY_RECORD))
        )

        analysis = await PhotoAnalyzer(client, "claude-test", "low").analyze([JPEG])

        self.assertEqual("Пусто.", analysis.comment)
        self.assertEqual(2, len(messages.calls))
        self.assertEqual(
            {"role": "assistant", "content": paused.content},
            messages.calls[1]["messages"][-1],
        )

    async def test_answer_without_record_is_unavailable(self) -> None:
        for stop_reason in ("refusal", "end_turn", "max_tokens"):
            with self.subTest(stop_reason=stop_reason):
                client, _ = fake_client(
                    response(stop_reason, SimpleNamespace(type="text", text="?"))
                )
                with self.assertRaises(PhotoUnavailable):
                    await PhotoAnalyzer(client, "claude-test", "low").analyze([JPEG])

    async def test_endless_pausing_gives_up(self) -> None:
        client, messages = fake_client(
            *(response("pause_turn", SEARCH_BLOCK) for _ in range(10))
        )

        with self.assertRaises(PhotoUnavailable):
            await PhotoAnalyzer(client, "claude-test", "low").analyze([JPEG])
        self.assertEqual(4, len(messages.calls))


class HandlePhotoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "bot.db")
        self.database.initialize()
        self.analyzer = FakeAnalyzer(
            PhotoAnalysis(
                (
                    FoodGuess("Гречка", 200.0, 110.0, 4.0, 1.0, 21.0),
                    FoodGuess("Котлета", 100.0, 220.0, None, None, None),
                ),
                None,
                "Гречка с котлетой.",
            )
        )
        self.context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={"database": self.database, "photo_analyzer": self.analyzer}
            )
        )
        self.database.ensure_user(ADMIN_USER_ID, 1)
        self.database.set_timezone(ADMIN_USER_ID, "Europe/Moscow", 1)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def entries(self, user_id: int = ADMIN_USER_ID) -> list[Any]:
        return list(self.database.page_entries(user_id, 0, 2**40, 0, 10).items)

    async def settle(self) -> None:
        """Wait for the background photo tasks started so far."""
        tasks = list(self.context.application.bot_data.get("photo_tasks", ()))
        await asyncio.gather(*tasks)

    async def propose(self, caption: str | None = None) -> tuple[Any, SentMessage]:
        update = photo_update(caption=caption)
        await handle_photo(update, self.context)
        await self.settle()
        return update, update.effective_message.sent[-1]

    async def test_photo_is_proposed_in_background_without_logging(self) -> None:
        self.analyzer.gate.clear()
        update = photo_update(caption="обед")

        await handle_photo(update, self.context)

        # The handler returns while Claude is still working.
        message = update.effective_message
        self.assertEqual([("Распознаю фото…", None)], message.replies)
        status = message.sent[0]
        self.analyzer.gate.set()
        await self.settle()

        self.assertEqual([((JPEG,), "обед")], self.analyzer.calls)
        self.assertEqual([], self.entries())
        # One proposal message, quoting the photo; the status is removed.
        self.assertEqual(2, len(message.replies))
        self.assertEqual([False, True], message.quoted)
        self.assertTrue(status.deleted)
        text, markup = message.replies[-1]
        self.assertTrue(text.startswith("Гречка с котлетой.\n\n"))
        self.assertIn("• Гречка — 200 г · 220 ккал · Б 8 · Ж 2 · У 42", text)
        self.assertIn(
            "• Котлета — 100 г · 220 ккал · на глаз: вес и калорийность\n", text
        )
        self.assertIn("Итого: 440 ккал\n", text)
        self.assertIn("ответьте на это сообщение", text)
        key = message.message_id
        self.assertEqual(
            [f"photo:save:{key}", f"photo:cancel:{key}"], keyboard_callbacks(markup)
        )

    async def test_save_logs_once_even_on_double_tap(self) -> None:
        update, proposal = await self.propose()
        first = tap(f"photo:save:{update.effective_message.message_id}", proposal)
        second = tap(f"photo:save:{update.effective_message.message_id}", proposal)

        await asyncio.gather(
            handle_photo_callback(first, self.context),
            handle_photo_callback(second, self.context),
        )

        entries = self.entries()
        self.assertEqual(
            # Saved at the photo's time (12:00 UTC), not at the tap.
            [("Гречка", 220.0, 1792238400), ("Котлета", 220.0, 1792238400)],
            sorted(
                (entry.name, entry.nutrition.calories, entry.eaten_at_utc)
                for entry in entries
            ),
        )
        self.assertEqual([((), {})], first.callback_query.answers)
        self.assertEqual([(("Уже записано.",), {})], second.callback_query.answers)
        self.assertEqual([], second.callback_query.edits)
        (summary, markup), *_ = first.callback_query.edits
        self.assertIn("Записано в дневник — 440 ккал:", summary)
        self.assertIn("• Гречка — 200 г · 220 ккал", summary)
        self.assertIn("Today:", summary)
        callbacks = keyboard_callbacks(markup)
        for entry in entries:
            self.assertIn(f"entry:view:{entry.entry_id}:0", callbacks)
        undo = next(data for data in callbacks if data.startswith("entry:undo:"))

        # The summary reuses the diary's undo action.
        await handle_callback(tap(undo, SentMessage(summary, markup)), self.context)
        self.assertEqual(1, len(self.entries()))

    async def test_cancel_logs_nothing(self) -> None:
        update, proposal = await self.propose()
        key = update.effective_message.message_id

        cancel = tap(f"photo:cancel:{key}", proposal)
        await handle_photo_callback(cancel, self.context)
        late_save = tap(f"photo:save:{key}", proposal)
        await handle_photo_callback(late_save, self.context)

        self.assertEqual([], self.entries())
        self.assertEqual(
            [("Отменено — ничего не записано.", None)], cancel.callback_query.edits
        )
        self.assertEqual(
            [(("Предложение уже отменено.",), {})], late_save.callback_query.answers
        )

    async def test_reply_correction_reruns_analysis_and_replaces_proposal(
        self,
    ) -> None:
        update, proposal = await self.propose(caption="обед")
        old_analysis = self.analyzer.result
        self.analyzer.result = PhotoAnalysis(
            (FoodGuess("Гречка", 150.0, 110.0, 4.0, 1.0, 21.0),),
            None,
            "Исправил вес гречки.",
        )
        self.analyzer.gate.clear()
        correction = text_update("гречки было 150 г", reply_to=proposal)

        await handle_text(correction, self.context)

        # Old buttons are gone and the proposal cannot be saved mid-analysis.
        self.assertEqual(
            ("Исправление принято — новое предложение ниже.", None),
            proposal.edits[-1],
        )
        key = update.effective_message.message_id
        busy = tap(f"photo:save:{key}", proposal)
        await handle_photo_callback(busy, self.context)
        self.assertIn("подождите", busy.callback_query.answers[0][0][0])
        self.analyzer.gate.set()
        await self.settle()

        self.assertEqual(((JPEG,), "обед"), self.analyzer.calls[-1])
        self.assertEqual(
            {
                "owner_foods": (),
                "previous": old_analysis,
                "corrections": ("гречки было 150 г",),
            },
            self.analyzer.revisions[-1],
        )
        replies = correction.effective_message.replies
        self.assertEqual("Пересчитываю с учётом исправления…", replies[0][0])
        self.assertTrue(correction.effective_message.sent[0].deleted)
        self.assertEqual(2, len(replies))
        text, markup = replies[-1]
        self.assertIn("Исправил вес гречки.", text)
        self.assertIn("• Гречка — 150 г · 165 ккал", text)
        self.assertNotIn("Котлета", text)
        self.assertEqual(f"photo:save:{key}", keyboard_callbacks(markup)[0])
        self.assertEqual([], self.entries())

        replacement = correction.effective_message.sent[-1]
        await handle_photo_callback(tap(f"photo:save:{key}", replacement), self.context)
        self.assertEqual(
            [("Гречка", 165.0)],
            [(entry.name, entry.nutrition.calories) for entry in self.entries()],
        )

    async def test_a_second_correction_keeps_the_first(self) -> None:
        update, proposal = await self.propose()
        first = text_update("гречки было 150 г", reply_to=proposal)
        await handle_text(first, self.context)
        await self.settle()

        # Replying to the photo itself works as well as replying to the proposal.
        second = text_update("курица без масла", reply_to=update.effective_message)
        await handle_text(second, self.context)
        await self.settle()

        self.assertEqual(
            ("гречки было 150 г", "курица без масла"),
            self.analyzer.revisions[-1]["corrections"],
        )

    async def test_failed_correction_keeps_the_previous_proposal(self) -> None:
        update, proposal = await self.propose()
        self.analyzer.result = PhotoUnavailable("down")
        correction = text_update("без котлеты", reply_to=proposal)

        with self.assertLogs("kcaloriebot.bot", "WARNING"):
            await handle_text(correction, self.context)
            await self.settle()

        text, _ = correction.effective_message.replies[-1]
        self.assertTrue(text.startswith(PHOTO_CORRECTION_FAILED_TEXT))
        self.assertIn("Котлета", text)
        key = update.effective_message.message_id
        replacement = correction.effective_message.sent[-1]
        await handle_photo_callback(tap(f"photo:save:{key}", replacement), self.context)
        self.assertEqual(2, len(self.entries()))

    async def test_texts_that_are_not_corrections_keep_their_meaning(self) -> None:
        _, proposal = await self.propose()

        menu = text_update("Diary", reply_to=proposal)
        await handle_text(menu, self.context)
        unrelated = text_update("oatmeal 370 60", reply_to=FakeMessage("hello"))
        await handle_text(unrelated, self.context)
        quick_add = text_update("rice 130 200")
        await handle_text(quick_add, self.context)

        self.assertEqual(1, len(self.analyzer.calls))
        self.assertEqual({"oatmeal", "rice"}, {entry.name for entry in self.entries()})
        for update in (menu, unrelated, quick_add):
            self.assertNotIn("Пересчитываю", update.effective_message.replies[0][0])

    async def test_expired_proposal_is_answered_gracefully(self) -> None:
        now = [1000.0]
        self.context.application.bot_data["photo_proposals"] = PhotoProposalStore(
            clock=lambda: now[0]
        )
        update, proposal = await self.propose()
        key = update.effective_message.message_id
        now[0] += PROPOSAL_TTL_SECONDS + 1

        expired = tap(f"photo:save:{key}", proposal)
        await handle_photo_callback(expired, self.context)
        # A button from before a restart: the store has never seen this photo.
        unknown = tap("photo:save:999999", FakeMessage("old proposal"))
        await handle_photo_callback(unknown, self.context)
        reply = text_update("гречки было 150 г", reply_to=proposal)
        await handle_text(reply, self.context)

        self.assertEqual([], self.entries())
        self.assertEqual([(PHOTO_EXPIRED_TEXT, None)], expired.callback_query.edits)
        self.assertEqual([(PHOTO_EXPIRED_TEXT, None)], unknown.callback_query.edits)
        self.assertEqual(
            [(PHOTO_EXPIRED_TEXT, MAIN_KEYBOARD)], reply.effective_message.replies
        )
        self.assertEqual(1, len(self.analyzer.calls))

    async def test_other_users_photos_are_not_analyzed(self) -> None:
        update = photo_update(user_id=7)

        await handle_photo(update, self.context)

        self.assertEqual([], self.analyzer.calls)
        self.assertEqual([], self.entries(7))
        self.assertEqual(
            ("Choose a text command from the keyboard.", MAIN_KEYBOARD),
            update.effective_message.replies[-1],
        )

    async def test_scale_photo_records_weight_after_confirmation(self) -> None:
        self.analyzer.result = PhotoAnalysis((), 81.4, "")
        update, proposal = await self.propose()

        self.assertIn("Вес на весах: 81.4 кг", proposal.text)
        self.assertIsNone(self.database.latest_weight(ADMIN_USER_ID))
        save = tap(f"photo:save:{update.effective_message.message_id}", proposal)
        await handle_photo_callback(save, self.context)

        self.assertEqual([], self.entries())
        record = self.database.latest_weight(ADMIN_USER_ID)
        self.assertEqual(81.4, record.weight_kg)
        summary, markup = save.callback_query.edits[-1]
        self.assertIn("Вес записан: 81.4 кг", summary)
        self.assertIn(f"weight:undo:{record.weight_id}", keyboard_callbacks(markup))

    async def test_analysis_failure_is_reported_from_the_background_task(
        self,
    ) -> None:
        for error, level in (
            (PhotoUnavailable("down"), "WARNING"),
            (RuntimeError("bug"), "ERROR"),
        ):
            with self.subTest(error=error):
                self.analyzer.result = error
                update = photo_update()

                with self.assertLogs("kcaloriebot.bot", level):
                    await handle_photo(update, self.context)
                    await self.settle()

                self.assertEqual([], self.entries())
                self.assertEqual(
                    (PHOTO_FAILED_TEXT, MAIN_KEYBOARD),
                    update.effective_message.replies[-1],
                )
                self.assertTrue(update.effective_message.sent[0].deleted)

    async def test_result_arrives_when_the_status_cannot_be_deleted(self) -> None:
        update = photo_update()
        original_reply = update.effective_message.reply_text

        async def broken_delete() -> None:
            raise BadRequest("Message to delete not found")

        async def reply_with_undeletable_status(
            text: str, reply_markup: Any = None, **kwargs: Any
        ) -> SentMessage:
            sent = await original_reply(text, reply_markup, **kwargs)
            sent.delete = broken_delete
            return sent

        update.effective_message.reply_text = reply_with_undeletable_status
        with self.assertLogs("kcaloriebot.bot", "WARNING"):
            await handle_photo(update, self.context)
            await self.settle()

        self.assertIn("Итого: 440 ккал", update.effective_message.replies[-1][0])

    async def test_several_photos_get_their_own_proposals(self) -> None:
        self.analyzer.gate.clear()
        first, second = photo_update(), photo_update()
        await handle_photo(first, self.context)
        await handle_photo(second, self.context)
        self.analyzer.gate.set()
        await self.settle()
        first_proposal = first.effective_message.sent[-1]
        second_proposal = second.effective_message.sent[-1]

        await handle_photo_callback(
            tap(f"photo:save:{second.effective_message.message_id}", second_proposal),
            self.context,
        )
        await handle_photo_callback(
            tap(f"photo:cancel:{first.effective_message.message_id}", first_proposal),
            self.context,
        )

        self.assertEqual(2, len(self.analyzer.calls))
        self.assertEqual(2, len(self.entries()))

    async def test_shutdown_interrupts_running_analysis_and_tells_the_owner(
        self,
    ) -> None:
        self.analyzer.gate.clear()
        update = photo_update()
        await handle_photo(update, self.context)
        await asyncio.sleep(0)

        await _stop_photo_tasks(self.context.application)

        self.assertEqual(set(), self.context.application.bot_data["photo_tasks"])
        self.assertEqual(
            (PHOTO_RESTART_TEXT, MAIN_KEYBOARD), update.effective_message.replies[-1]
        )
        self.assertEqual([], self.entries())

    async def test_owner_foods_are_sent_and_reused_for_corrections(self) -> None:
        self.database.save_favorite(
            ADMIN_USER_ID, "Творог 5%", UNIT_100G, 121, 17, 5, 1.8
        )
        self.database.add_entry(
            ADMIN_USER_ID, self.database.now_epoch(), "Гречка", 110, 200, 4, 1, 21
        )
        self.database.ensure_user(7, 1)
        self.database.save_favorite(7, "Чужой торт", UNIT_100G, 400, 5, 20, 50)

        _, proposal = await self.propose()
        correction = text_update("гречки было 150 г", reply_to=proposal)
        await handle_text(correction, self.context)
        await self.settle()

        first, second = self.analyzer.revisions
        self.assertEqual(
            (
                OwnerFood("Творог 5%", 121.0, 17.0, 5.0, 1.8),
                OwnerFood("Гречка", 110.0, 4.0, 1.0, 21.0),
            ),
            first["owner_foods"],
        )
        # Corrections keep the same list, so saved_food numbers stay valid.
        self.assertIs(first["owner_foods"], second["owner_foods"])

    async def test_save_is_blocked_until_every_weight_is_known(self) -> None:
        buckwheat = FoodGuess("Гречка", 200.0, 110.0, 4.0, 1.0, 21.0)
        self.analyzer.result = PhotoAnalysis(
            (
                FoodGuess(
                    "Йогурт",
                    None,
                    60.0,
                    5.0,
                    2.0,
                    6.0,
                    nutrition_source="label",
                    grams_source="unknown",
                ),
                buckwheat,
            ),
            None,
            "Йогурт по этикетке.",
        )
        update, proposal = await self.propose()
        key = update.effective_message.message_id

        self.assertIn("• Йогурт — вес не указан · 60 ккал на 100 г\n", proposal.text)
        self.assertIn("Сколько грамм съели: «Йогурт»?", proposal.text)
        save = tap(f"photo:save:{key}", proposal)
        await handle_photo_callback(save, self.context)

        self.assertEqual([], self.entries())
        [(args, kwargs)] = save.callback_query.answers
        self.assertEqual({"show_alert": True}, kwargs)
        self.assertIn("Не указан вес: «Йогурт»", args[0])
        self.assertEqual([], save.callback_query.edits)

        # The weight, given by replying, makes the proposal savable.
        self.analyzer.result = PhotoAnalysis(
            (
                FoodGuess(
                    "Йогурт",
                    120.0,
                    60.0,
                    5.0,
                    2.0,
                    6.0,
                    nutrition_source="label",
                    grams_source="owner",
                ),
                buckwheat,
            ),
            None,
            "",
        )
        reply = text_update("йогурта 120 г", reply_to=proposal)
        await handle_text(reply, self.context)
        await self.settle()
        replacement = reply.effective_message.sent[-1]
        await handle_photo_callback(tap(f"photo:save:{key}", replacement), self.context)
        self.assertEqual(
            [("Гречка", 220.0), ("Йогурт", 72.0)],
            sorted((e.name, e.nutrition.calories) for e in self.entries()),
        )

    async def test_question_is_shown_and_answered_by_reply(self) -> None:
        self.analyzer.result = PhotoAnalysis(
            (FoodGuess("Котлета", 100.0, 220.0, None, None, None),),
            None,
            "Котлета.",
            "Котлета жарилась на масле?",
        )
        _, proposal = await self.propose()

        self.assertIn("Вопрос: Котлета жарилась на масле?\n", proposal.text)
        answer = text_update("да, на масле", reply_to=proposal)
        await handle_text(answer, self.context)
        await self.settle()

        revision = self.analyzer.revisions[-1]
        self.assertEqual(("да, на масле",), revision["corrections"])
        self.assertEqual("Котлета жарилась на масле?", revision["previous"].question)

    async def send(self, *updates: Any) -> None:
        for update in updates:
            await handle_photo(update, self.context)
        await self.settle()

    async def test_album_is_analyzed_as_one_request_and_one_proposal(self) -> None:
        dish = photo_update(media_group_id="album-1", data=b"dish")
        label = photo_update(
            media_group_id="album-1", data=b"label", caption="съел половину"
        )
        side = photo_update(media_group_id="album-1", data=b"side")
        with mock.patch("kcaloriebot.bot.ALBUM_QUIET_SECONDS", 0.2):
            await self.send(dish, label, side)

        images = tuple(
            PhotoImage(data, "image/jpeg") for data in (b"dish", b"label", b"side")
        )
        self.assertEqual([(images, "съел половину")], self.analyzer.calls)
        # One status and one proposal, both answering the first photo.
        self.assertEqual(2, len(dish.effective_message.replies))
        self.assertEqual([], label.effective_message.replies)
        self.assertEqual([], side.effective_message.replies)
        proposal = dish.effective_message.sent[-1]
        key = dish.effective_message.message_id
        self.assertEqual(
            [f"photo:save:{key}", f"photo:cancel:{key}"],
            keyboard_callbacks(proposal.reply_markup),
        )

        # A reply to any photo of the album corrects it, with every image again.
        correction = text_update("гречки было 150 г", reply_to=side.effective_message)
        await handle_text(correction, self.context)
        await self.settle()
        self.assertEqual([(images, "съел половину")] * 2, self.analyzer.calls)
        self.assertEqual(
            ("гречки было 150 г",), self.analyzer.revisions[-1]["corrections"]
        )
        await handle_photo_callback(
            tap(f"photo:save:{key}", correction.effective_message.sent[-1]),
            self.context,
        )
        self.assertEqual(2, len(self.entries()))

    async def test_photo_outside_an_album_is_not_delayed(self) -> None:
        with (
            mock.patch("kcaloriebot.bot.ALBUM_QUIET_SECONDS", 60),
            mock.patch("kcaloriebot.bot.ALBUM_MAX_WAIT_SECONDS", 60),
        ):
            update = photo_update()
            await handle_photo(update, self.context)
            await asyncio.wait_for(self.settle(), 5)

        self.assertEqual([((JPEG,), None)], self.analyzer.calls)
        self.assertIn("Итого: 440 ккал", update.effective_message.replies[-1][0])

    async def test_albums_are_kept_apart_and_bounded(self) -> None:
        first, second, third = (
            photo_update(media_group_id="a", data=data)
            for data in (b"a1", b"a2", b"a3")
        )
        other = photo_update(media_group_id="b", data=b"b1")
        with (
            mock.patch("kcaloriebot.bot.ALBUM_QUIET_SECONDS", 0.2),
            mock.patch("kcaloriebot.bot.MAX_IMAGES_PER_REQUEST", 2),
        ):
            await self.send(first, other, second, third)

        self.assertEqual(
            [
                [b"a1", b"a2"],
                [b"b1"],
            ],
            sorted(
                [image.data for image in images] for images, _ in self.analyzer.calls
            ),
        )
        self.assertEqual(
            [(PHOTO_ALBUM_FULL_TEXT, MAIN_KEYBOARD)], third.effective_message.replies
        )

    async def test_album_waits_no_longer_than_the_limit(self) -> None:
        early = photo_update(media_group_id="slow", data=b"early")
        late = photo_update(media_group_id="slow", data=b"late")
        with (
            mock.patch("kcaloriebot.bot.ALBUM_QUIET_SECONDS", 60),
            mock.patch("kcaloriebot.bot.ALBUM_MAX_WAIT_SECONDS", 0.1),
        ):
            await handle_photo(early, self.context)
            await asyncio.sleep(0.3)
            # The album was closed; a straggler gets an analysis of its own.
            await self.send(late)

        self.assertEqual(
            [[b"early"], [b"late"]],
            [[image.data for image in images] for images, _ in self.analyzer.calls],
        )
        self.assertEqual({}, self.context.application.bot_data["photo_albums"])

    async def test_shutdown_while_an_album_is_collected(self) -> None:
        update = photo_update(media_group_id="album")
        with mock.patch("kcaloriebot.bot.ALBUM_QUIET_SECONDS", 60):
            await handle_photo(update, self.context)
            await asyncio.sleep(0)
            await _stop_photo_tasks(self.context.application)

        self.assertEqual([], self.analyzer.calls)
        self.assertEqual(
            (PHOTO_RESTART_TEXT, MAIN_KEYBOARD), update.effective_message.replies[-1]
        )
        self.assertEqual({}, self.context.application.bot_data["photo_albums"])


MEAL = "съел борщ и два куска хлеба"
TEXT_HINT = (
    "Choose an option from the keyboard, or log a food in one message, "
    "for example: oatmeal 370 60."
)


def described(
    text: str = MEAL, user_id: int = ADMIN_USER_ID, reply_to: Any = None
) -> Any:
    update = make_update(user_id=user_id)
    message = ChatMessage(text)
    message.date = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    message.reply_to_message = reply_to
    update.effective_message = message
    return update


class HandleDescriptionTests(unittest.IsolatedAsyncioTestCase):
    """A meal the owner describes in words goes through the photo proposal."""

    setUp = HandlePhotoTests.setUp
    tearDown = HandlePhotoTests.tearDown
    entries = HandlePhotoTests.entries
    settle = HandlePhotoTests.settle

    async def describe(self, text: str = MEAL) -> tuple[Any, SentMessage]:
        update = described(text)
        await handle_text(update, self.context)
        await self.settle()
        return update, update.effective_message.sent[-1]

    async def assert_not_sent(self, update: Any, reply: Any) -> None:
        await handle_text(update, self.context)
        await self.settle()
        self.assertEqual([], self.analyzer.calls)
        self.assertEqual(reply, update.effective_message.replies[-1])

    async def test_description_is_proposed_in_background_without_logging(
        self,
    ) -> None:
        self.database.save_favorite(
            ADMIN_USER_ID, "Хлеб бородинский", UNIT_100G, 210, 7, 1.5, 41
        )
        self.analyzer.gate.clear()
        update = described()

        await handle_text(update, self.context)

        message = update.effective_message
        self.assertEqual([("Считаю калории…", None)], message.replies)
        status = message.sent[0]
        self.analyzer.gate.set()
        await self.settle()

        # No images: the message itself is the description.
        self.assertEqual([((), MEAL)], self.analyzer.calls)
        self.assertEqual(
            (OwnerFood("Хлеб бородинский", 210.0, 7.0, 1.5, 41.0),),
            self.analyzer.revisions[0]["owner_foods"],
        )
        self.assertEqual([], self.entries())
        self.assertEqual([False, True], message.quoted)
        self.assertTrue(status.deleted)
        text, markup = message.replies[-1]
        self.assertIn("• Гречка — 200 г · 220 ккал", text)
        self.assertIn("Итого: 440 ккал\n", text)
        key = message.message_id
        self.assertEqual(
            [f"photo:save:{key}", f"photo:cancel:{key}"], keyboard_callbacks(markup)
        )

    async def test_save_logs_once_at_the_message_time(self) -> None:
        update, proposal = await self.describe()
        key = update.effective_message.message_id
        first = tap(f"photo:save:{key}", proposal)
        second = tap(f"photo:save:{key}", proposal)

        await asyncio.gather(
            handle_photo_callback(first, self.context),
            handle_photo_callback(second, self.context),
        )

        self.assertEqual(
            [("Гречка", 220.0, 1792238400), ("Котлета", 220.0, 1792238400)],
            sorted(
                (entry.name, entry.nutrition.calories, entry.eaten_at_utc)
                for entry in self.entries()
            ),
        )
        self.assertEqual([(("Уже записано.",), {})], second.callback_query.answers)
        late = described("хлеба было три куска", reply_to=proposal)
        await handle_text(late, self.context)
        self.assertEqual(
            [("Это уже записано.", MAIN_KEYBOARD)], late.effective_message.replies
        )
        self.assertEqual(1, len(self.analyzer.calls))

    async def test_reply_correction_reruns_the_same_text_without_images(
        self,
    ) -> None:
        update, proposal = await self.describe()
        old_analysis = self.analyzer.result
        self.analyzer.result = PhotoAnalysis(
            (FoodGuess("Борщ", 300.0, 50.0, None, None, None),), None, ""
        )
        correction = described("хлеба не было", reply_to=proposal)

        await handle_text(correction, self.context)
        await self.settle()

        self.assertEqual([((), MEAL)] * 2, self.analyzer.calls)
        self.assertEqual(
            {
                "owner_foods": (),
                "previous": old_analysis,
                "corrections": ("хлеба не было",),
            },
            self.analyzer.revisions[-1],
        )
        self.assertEqual(
            ("Исправление принято — новое предложение ниже.", None),
            proposal.edits[-1],
        )
        # A reply to the owner's own message works as well.
        second = described("борща 400 г", reply_to=update.effective_message)
        await handle_text(second, self.context)
        await self.settle()
        self.assertEqual(
            ("хлеба не было", "борща 400 г"),
            self.analyzer.revisions[-1]["corrections"],
        )
        self.assertEqual([((), MEAL)] * 3, self.analyzer.calls)
        key = update.effective_message.message_id
        replacement = second.effective_message.sent[-1]
        await handle_photo_callback(tap(f"photo:save:{key}", replacement), self.context)
        self.assertEqual(
            [("Борщ", 150.0)],
            [(entry.name, entry.nutrition.calories) for entry in self.entries()],
        )

    async def test_weight_without_calories_reaches_claude_and_blocks_until_known(
        self,
    ) -> None:
        self.analyzer.result = PhotoAnalysis(
            (FoodGuess("Творог 5%", None, 121.0, 17.0, 5.0, 1.8),), None, ""
        )
        update, proposal = await self.describe("творог 5% 200 г")

        self.assertEqual([((), "творог 5% 200 г")], self.analyzer.calls)
        save = tap(f"photo:save:{update.effective_message.message_id}", proposal)
        await handle_photo_callback(save, self.context)
        self.assertEqual([], self.entries())
        self.assertIn("Не указан вес", save.callback_query.answers[0][0][0])

    async def test_quick_add_stays_instant_and_never_reaches_claude(self) -> None:
        quick = described("oatmeal 370 60")
        await handle_text(quick, self.context)

        self.assertEqual(["oatmeal"], [entry.name for entry in self.entries()])
        self.assertEqual([], self.analyzer.calls)
        self.assertNotIn("Считаю", quick.effective_message.replies[0][0])
        for text in ("bread 99999 150", "суп 300 г 200 г"):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError) as error:
                    parse_quick_add(text)
                await self.assert_not_sent(
                    described(text), (str(error.exception), MAIN_KEYBOARD)
                )
        self.assertEqual(1, len(self.entries()))

    async def test_menu_words_commands_and_sessions_never_reach_claude(self) -> None:
        # Buttons outside their input, or typed in another letter case.
        for text in ("diary", "SETTINGS", "Skip", "per serving", "main menu"):
            with self.subTest(text=text):
                await self.assert_not_sent(described(text), (TEXT_HINT, MAIN_KEYBOARD))
        await self.assert_not_sent(
            described("Main menu"), ("Select an option:", MAIN_KEYBOARD)
        )
        command = described("/борщ")
        await unknown_command(command, self.context)
        self.assertIn("Unknown command", command.effective_message.replies[-1][0])

        # Inside an input session the text is that session's answer.
        await handle_text(described("Add food"), self.context)
        name = described("борщ со сметаной")
        await handle_text(name, self.context)
        await self.settle()
        self.assertEqual([], self.analyzer.calls)
        self.assertIn("calories", name.effective_message.replies[-1][0])
        session = self.database.get_session(ADMIN_USER_ID, 10)
        self.assertEqual("борщ со сметаной", session.draft_name)

    async def test_stray_text_is_not_sent(self) -> None:
        for text in ("ок", "+", "?", "1", "😀😀😀", "ok!!"):
            with self.subTest(text=text):
                await self.assert_not_sent(described(text), (TEXT_HINT, MAIN_KEYBOARD))
        await self.assert_not_sent(
            described("а" * (MAX_DESCRIPTION_CHARS + 1)),
            (TEXT_TOO_LONG_TEXT, MAIN_KEYBOARD),
        )
        # A reply that is not to an open proposal is not a new meal either.
        await self.assert_not_sent(
            described(MEAL, reply_to=FakeMessage("Saved: oatmeal")),
            (TEXT_REPLY_IGNORED_TEXT, MAIN_KEYBOARD),
        )
        # Three letters are enough for a real meal.
        await self.describe("чай")
        self.assertEqual([((), "чай")], self.analyzer.calls)

    async def test_others_and_no_analyzer_keep_todays_behaviour(self) -> None:
        self.database.ensure_user(7, 1)
        self.database.set_timezone(7, "Europe/Moscow", 1)
        await self.assert_not_sent(described(user_id=7), (TEXT_HINT, MAIN_KEYBOARD))
        self.assertEqual([], self.entries(7))

        del self.context.application.bot_data["photo_analyzer"]
        await self.assert_not_sent(described(), (TEXT_HINT, MAIN_KEYBOARD))
        self.assertNotIn("photo_tasks", self.context.application.bot_data)

    async def test_nothing_to_log_is_answered_without_a_proposal(self) -> None:
        for comment, reply in (
            ("Это не описание еды.", "Это не описание еды."),
            ("", "В сообщении не нашлось еды — ничего не записано."),
        ):
            with self.subTest(comment=comment):
                self.analyzer.result = PhotoAnalysis((), None, comment)
                update, _ = await self.describe("спасибо")

                self.assertEqual(
                    (reply, MAIN_KEYBOARD), update.effective_message.replies[-1]
                )
                self.assertEqual([], self.entries())
        store = self.context.application.bot_data["photo_proposals"]
        self.assertEqual({}, store._items)

    async def test_analysis_failure_is_reported(self) -> None:
        for error, level in (
            (PhotoUnavailable("down"), "WARNING"),
            (RuntimeError("bug"), "ERROR"),
        ):
            with self.subTest(error=error):
                self.analyzer.result = error
                update = described()

                with self.assertLogs("kcaloriebot.bot", level):
                    await handle_text(update, self.context)
                    await self.settle()

                self.assertEqual([], self.entries())
                self.assertEqual(
                    (TEXT_FAILED_TEXT, MAIN_KEYBOARD),
                    update.effective_message.replies[-1],
                )
                self.assertTrue(update.effective_message.sent[0].deleted)

    async def test_shutdown_interrupts_the_analysis_and_tells_the_owner(
        self,
    ) -> None:
        self.analyzer.gate.clear()
        update = described()
        await handle_text(update, self.context)
        await asyncio.sleep(0)

        await _stop_photo_tasks(self.context.application)

        self.assertEqual(
            (TEXT_RESTART_TEXT, MAIN_KEYBOARD), update.effective_message.replies[-1]
        )
        self.assertEqual([], self.entries())

    async def test_expired_description_proposal(self) -> None:
        now = [1000.0]
        self.context.application.bot_data["photo_proposals"] = PhotoProposalStore(
            clock=lambda: now[0]
        )
        update, proposal = await self.describe()
        now[0] += PROPOSAL_TTL_SECONDS + 1

        expired = tap(f"photo:save:{update.effective_message.message_id}", proposal)
        await handle_photo_callback(expired, self.context)
        reply = described("хлеба не было", reply_to=proposal)
        await handle_text(reply, self.context)

        self.assertEqual([(PHOTO_EXPIRED_TEXT, None)], expired.callback_query.edits)
        self.assertEqual(
            [(PHOTO_EXPIRED_TEXT, MAIN_KEYBOARD)], reply.effective_message.replies
        )
        self.assertEqual(1, len(self.analyzer.calls))
        self.assertEqual([], self.entries())


def food_item(**overrides: Any) -> dict[str, Any]:
    item = {
        "name": "Гречка",
        "nutrition_source": "estimate",
        "saved_food": 0,
        "calories_per_100g": 110,
        "protein_per_100g": 4,
        "fat_per_100g": 1,
        "carbs_per_100g": 21,
        "grams_source": "estimate",
        "grams": 200,
        "grams_low": 150,
        "grams_high": 260,
    }
    item.update(overrides)
    return item


def unknown_weight(**overrides: Any) -> dict[str, Any]:
    return food_item(
        grams_source="unknown", grams=0, grams_low=0, grams_high=0, **overrides
    )


def model_record(*foods: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "foods": list(foods),
        "weight_kg": 0,
        "question": "",
        "comment": "",
        **fields,
    }


OWNER_FOODS = (
    OwnerFood("Творог 5%", 121.0, 17.0, 5.0, 1.8),
    OwnerFood("Батончик", 200.0, 10.0, 8.0, 20.0, per_serving=True),
)


class OwnerFoodsTests(unittest.TestCase):
    def test_favorites_and_recent_foods_are_converted_to_per_100g(self) -> None:
        def favorite(name: str, *values: Any, **kwargs: Any) -> FavoriteFood:
            return FavoriteFood(1, ADMIN_USER_ID, name, *values, **kwargs)

        def logged(name: str, *totals: Any, **kwargs: Any) -> FoodEntry:
            return FoodEntry(
                1, ADMIN_USER_ID, 0, name, NutritionTotals(*totals, **kwargs)
            )

        foods = owner_foods_from(
            [
                favorite("Творог 5%", 121, 17, 5, 1.8),
                favorite(
                    "Сырники", 330, 15, 12, 30, unit=UNIT_SERVING, serving_grams=150
                ),
                favorite("Батончик", 200, 10, 8, 20, unit=UNIT_SERVING),
            ],
            [
                logged("творог 5%", 242, 200, 34, 10, 3.6),
                logged("Гречка", 220, 200, 8, 2, 42),
                logged("Пельмени", 500, 250, 20, 25, 50, servings=2),
                logged("Кофе", 0, None, None, None, None),
            ],
        )

        self.assertEqual(
            (
                OwnerFood("Творог 5%", 121.0, 17.0, 5.0, 1.8),
                OwnerFood("Сырники", 220.0, 10.0, 8.0, 20.0, False, 150.0),
                # Per serving without a weight: never relabeled as per 100 g.
                OwnerFood("Батончик", 200.0, 10.0, 8.0, 20.0, True),
                OwnerFood("Гречка", 110.0, 4.0, 1.0, 21.0),
                OwnerFood("Пельмени", 200.0, 8.0, 10.0, 20.0, False, 125.0),
            ),
            foods,
        )
        self.assertEqual(
            "2. Сырники: one serving = 150 g; per 100 g: 220 kcal, protein 10, "
            "fat 8, carbs 20",
            owner_food_line(2, foods[1]),
        )
        self.assertEqual(
            "3. Батончик: per serving (serving weight not saved): 200 kcal, "
            "protein 10, fat 8, carbs 20",
            owner_food_line(3, foods[2]),
        )

    def test_owner_foods_are_bounded(self) -> None:
        favorites = [
            FavoriteFood(
                i,
                ADMIN_USER_ID,
                f"{i} " + "очень длинное название " * 10,
                100,
                None,
                None,
                None,
            )
            for i in range(100)
        ]

        foods = owner_foods_from(favorites, [])

        self.assertLess(len(foods), 100)
        self.assertTrue(all(len(f.name) <= MAX_OWNER_FOOD_NAME_CHARS for f in foods))
        lines = "\n".join(owner_food_line(n, f) for n, f in enumerate(foods, 1))
        self.assertLessEqual(len(lines), MAX_OWNER_FOODS_CHARS)


class ParseSourcesTests(unittest.TestCase):
    def parse_one(self, **overrides: Any) -> FoodGuess:
        [food] = parse_analysis(model_record(food_item(**overrides)), OWNER_FOODS).foods
        return food

    def test_eye_estimate_is_logged_above_the_best_guess(self) -> None:
        food = self.parse_one()

        self.assertEqual(200 + EYE_ESTIMATE_UPWARD_SHARE * (260 - 200), food.grams)
        self.assertEqual((150.0, 200.0, 260.0), food.grams_range)
        self.assertEqual(
            ("estimate", "estimate"), (food.nutrition_source, food.grams_source)
        )

    def test_weights_that_were_not_eyeballed_are_logged_as_reported(self) -> None:
        for source in ("package", "owner", "saved"):
            with self.subTest(source=source):
                food = self.parse_one(grams_source=source, saved_food=1)
                self.assertEqual(
                    (source, 200.0, None),
                    (food.grams_source, food.grams, food.grams_range),
                )

    def test_a_wild_range_is_capped(self) -> None:
        food = self.parse_one(grams=100, grams_low=80, grams_high=1000)

        self.assertEqual(100 + EYE_ESTIMATE_UPWARD_SHARE * 100, food.grams)

    def test_an_inconsistent_range_keeps_the_best_estimate(self) -> None:
        for low, high in (
            (300, 100),
            (150, 180),
            (-1, 260),
            (150, math.nan),
            ("150", 260),
        ):
            with self.subTest(low=low, high=high):
                food = self.parse_one(grams_low=low, grams_high=high)
                self.assertEqual(
                    (200.0, None, "estimate"),
                    (food.grams, food.grams_range, food.grams_source),
                )

    def test_unknown_weight_is_kept_for_the_owner_to_give(self) -> None:
        analysis = parse_analysis(
            model_record(
                unknown_weight(name="Йогурт", nutrition_source="label"),
                # A weight next to "unknown" is not trusted either.
                food_item(name="Сок", grams_source="unknown", grams=500),
                # Nothing to count: asking for the weight would only block saving.
                unknown_weight(name="Вода", calories_per_100g=0),
            )
        )

        self.assertEqual(("Йогурт", "Сок"), analysis.missing_weight)
        self.assertEqual([None, None], [food.grams for food in analysis.foods])

    def test_bad_numbers_drop_or_downgrade_items(self) -> None:
        dropped = [
            {"grams": 0},
            {"grams": -5},
            {"grams": math.nan},
            {"grams": "200"},
            {"calories_per_100g": -1},
            {"calories_per_100g": math.inf},
            {"calories_per_100g": None},
            {"name": "  "},
        ]
        for overrides in dropped:
            with self.subTest(**{k: repr(v) for k, v in overrides.items()}):
                self.assertEqual(
                    (), parse_analysis(model_record(food_item(**overrides))).foods
                )
        food = self.parse_one(protein_per_100g=60, fat_per_100g=50)
        self.assertEqual(
            (110.0, None, None, None),
            (
                food.calories_per_100g,
                food.protein_per_100g,
                food.fat_per_100g,
                food.carbs_per_100g,
            ),
        )

    def test_sources_are_validated(self) -> None:
        cases = [
            ({"nutrition_source": " LABEL "}, "label", 0),
            ({"nutrition_source": "photo"}, "estimate", 0),
            ({"nutrition_source": "saved", "saved_food": 0}, "estimate", 0),
            ({"nutrition_source": "saved", "saved_food": 3}, "estimate", 0),
            ({"nutrition_source": "saved", "saved_food": 1.5}, "estimate", 0),
            ({"nutrition_source": "saved", "saved_food": True}, "estimate", 0),
            ({"nutrition_source": "label", "saved_food": 1}, "label", 0),
            ({"nutrition_source": "saved", "saved_food": 1.0}, "saved", 1),
        ]
        for overrides, source, saved in cases:
            with self.subTest(**{k: repr(v) for k, v in overrides.items()}):
                food = self.parse_one(**overrides)
                self.assertEqual(
                    (source, saved), (food.nutrition_source, food.saved_food)
                )
        # An unknown weight source counts as an eye estimate.
        food = self.parse_one(grams_source="scale")
        self.assertEqual(("estimate", 230.0), (food.grams_source, food.grams))

    def test_saved_food_uses_the_owners_own_numbers(self) -> None:
        exact = self.parse_one(
            nutrition_source="saved",
            saved_food=1,
            calories_per_100g=125,
            protein_per_100g=16,
        )
        self.assertEqual(
            ("saved", 121.0, 17.0, 5.0, 1.8),
            (
                exact.nutrition_source,
                exact.calories_per_100g,
                exact.protein_per_100g,
                exact.fat_per_100g,
                exact.carbs_per_100g,
            ),
        )
        # A per-serving food without a weight: the model's conversion is kept.
        converted = self.parse_one(
            nutrition_source="saved", saved_food=2, calories_per_100g=400
        )
        self.assertEqual(
            ("saved", 2, 400.0),
            (
                converted.nutrition_source,
                converted.saved_food,
                converted.calories_per_100g,
            ),
        )

    def test_question_and_comment_are_cleaned(self) -> None:
        analysis = parse_analysis(
            model_record(
                question="  Котлета \n жарилась  на масле? ", comment=" Обед. "
            )
        )
        self.assertEqual(
            ("Котлета жарилась на масле?", "Обед."),
            (analysis.question, analysis.comment),
        )
        self.assertEqual("", parse_analysis(model_record(question=7)).question)
        long = parse_analysis(model_record(question="а" * 5000)).question
        self.assertLessEqual(len(long), 300)

    def test_record_schema_avoids_what_strict_tools_reject(self) -> None:
        [record_tool] = [tool for tool in TOOLS if tool.get("name") == "record_foods"]
        self.assertTrue(record_tool["strict"])
        self.assertIs(RECORD_SCHEMA, record_tool["input_schema"])
        unsupported = {
            "anyOf",
            "oneOf",
            "allOf",
            "not",
            "$ref",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minLength",
            "maxLength",
            "pattern",
            "format",
            "minItems",
            "maxItems",
        }

        def check(node: dict[str, Any], path: str) -> None:
            self.assertFalse(unsupported & node.keys(), path)
            # No type unions (nullable fields): they count against a small limit.
            self.assertIsInstance(node["type"], str, path)
            if node["type"] == "object":
                self.assertIs(False, node["additionalProperties"], path)
                # No optional parameters either.
                self.assertEqual(list(node["properties"]), node["required"], path)
                for name, child in node["properties"].items():
                    check(child, f"{path}.{name}")
            elif node["type"] == "array":
                check(node["items"], f"{path}[]")
            if "enum" in node:
                self.assertTrue(all(isinstance(value, str) for value in node["enum"]))

        check(RECORD_SCHEMA, "record")
        item = RECORD_SCHEMA["properties"]["foods"]["items"]["properties"]
        self.assertEqual(list(NUTRITION_SOURCES), item["nutrition_source"]["enum"])
        self.assertEqual(list(GRAMS_SOURCES), item["grams_source"]["enum"])

    def test_correction_shows_the_honest_estimate_and_the_question(self) -> None:
        previous = parse_analysis(
            model_record(
                food_item(),
                unknown_weight(name="Йогурт", nutrition_source="label"),
                question="Гречка с маслом?",
            )
        )

        text = correction_text(previous, ["с маслом", "йогурта 120 г"])

        self.assertIn(
            "- Гречка: 200 g (by eye, plausible 150-260 g); per 100 g: 110 kcal", text
        )
        self.assertNotIn("230", text)
        self.assertIn("- Йогурт: weight unknown; per 100 g: 110 kcal", text)
        self.assertIn("nutrition from label, weight from unknown", text)
        self.assertIn("You asked the owner: Гречка с маслом?", text)
        self.assertIn("- с маслом\n- йогурта 120 г", text)


class AnalyzerRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_foods_come_first_and_album_images_are_labelled(self) -> None:
        payload = model_record(
            food_item(
                name="Творог",
                nutrition_source="saved",
                saved_food=1,
                calories_per_100g=130,
            )
        )
        client, messages = fake_client(response("tool_use", record_block(payload)))

        analysis = await PhotoAnalyzer(client, "claude-test", "low").analyze(
            [PhotoImage(b"dish", "image/jpeg"), PhotoImage(b"label", "image/png")],
            "обед",
            owner_foods=OWNER_FOODS,
        )

        content = messages.calls[0]["messages"][0]["content"]
        self.assertEqual(
            ["text", "text", "image", "text", "image", "text"],
            [block["type"] for block in content],
        )
        foods = content[0]["text"]
        self.assertIn(
            "1. Творог 5%: per 100 g: 121 kcal, protein 17, fat 5, carbs 1.8", foods
        )
        self.assertIn(
            "2. Батончик: per serving (serving weight not saved): 200 kcal", foods
        )
        self.assertEqual(
            ["Image 1:", "Image 2:"], [content[1]["text"], content[3]["text"]]
        )
        self.assertEqual(
            [(b"dish", "image/jpeg"), (b"label", "image/png")],
            [
                (
                    base64.b64decode(block["source"]["data"]),
                    block["source"]["media_type"],
                )
                for block in (content[2], content[4])
            ],
        )
        self.assertEqual("Caption: обед", content[5]["text"])
        # The owner's numbers replace the model's copy of them.
        self.assertEqual(121.0, analysis.foods[0].calories_per_100g)

    async def test_text_description_is_sent_without_images(self) -> None:
        client, messages = fake_client(
            response("tool_use", record_block(EMPTY_RECORD)),
            response("tool_use", record_block(EMPTY_RECORD)),
        )
        analyzer = PhotoAnalyzer(client, "claude-test", "low")

        await analyzer.analyze(
            (), "  кофе с молоком и круассан ", owner_foods=OWNER_FOODS
        )

        kwargs = messages.calls[0]
        content = kwargs["messages"][0]["content"]
        self.assertEqual(["text", "text"], [block["type"] for block in content])
        self.assertIn("1. Творог 5%: per 100 g: 121 kcal", content[0]["text"])
        self.assertEqual(
            "No photos. The owner's message: кофе с молоком и круассан",
            content[1]["text"],
        )
        # One prompt covers both kinds of request; web search stays available.
        self.assertIn("or describes in a message what he ate", kwargs["system"])
        self.assertIn("a typical portion you assume", kwargs["system"])
        self.assertEqual("web_search", kwargs["tools"][0]["name"])

        previous = PhotoAnalysis(
            (FoodGuess("Круассан", 60.0, 400.0, None, None, None),), None, ""
        )
        await analyzer.analyze(
            (),
            "кофе с молоком и круассан",
            previous=previous,
            corrections=("круассан с шоколадом",),
        )
        content = messages.calls[1]["messages"][0]["content"]
        self.assertEqual(["text", "text"], [block["type"] for block in content])
        self.assertTrue(content[1]["text"].startswith("Your previous answer:\n"))
        self.assertIn("- круассан с шоколадом", content[1]["text"])
        self.assertNotIn("photo", content[1]["text"])

    async def test_nothing_to_analyze_is_a_programming_error(self) -> None:
        client, messages = fake_client()
        for caption in (None, "", "   "):
            with self.subTest(caption=caption):
                with self.assertRaises(ValueError):
                    await PhotoAnalyzer(client, "claude-test", "low").analyze(
                        (), caption
                    )
        self.assertEqual([], messages.calls)


class ProposalTextTests(unittest.TestCase):
    def test_estimates_missing_weight_and_question_are_shown(self) -> None:
        analysis = parse_analysis(
            model_record(
                food_item(),
                food_item(
                    name="Йогурт",
                    nutrition_source="label",
                    calories_per_100g=60,
                    protein_per_100g=5,
                    fat_per_100g=2,
                    carbs_per_100g=6,
                    grams_source="package",
                    grams=120,
                    grams_low=120,
                    grams_high=120,
                ),
                unknown_weight(
                    name="Соус", nutrition_source="manufacturer", calories_per_100g=200
                ),
                question="Гречка с маслом?",
                comment="Обед.",
            )
        )

        text = proposal_text(analysis)

        self.assertIn(
            "• Гречка — 230 г · 253 ккал · Б 9 · Ж 2 · У 48 · на глаз: вес 150–260 г "
            "и калорийность\n",
            text,
        )
        # Exact numbers carry no mark.
        self.assertIn("• Йогурт — 120 г · 72 ккал · Б 6 · Ж 2 · У 7\n", text)
        self.assertIn("• Соус — вес не указан · 200 ккал на 100 г\n", text)
        self.assertIn("Итого без продуктов без веса: 325 ккал", text)
        self.assertIn("Сколько грамм съели: «Соус»?", text)
        self.assertIn("Вопрос: Гречка с маслом?\n", text)

    def test_alert_fits_telegram(self) -> None:
        alert = missing_weight_alert(["Х" * 150, "У" * 150])

        self.assertLessEqual(len(alert), MAX_ALERT_CHARS)
        self.assertTrue(alert.startswith("Не указан вес: «Х"))
        self.assertTrue(alert.endswith("или попросите убрать продукт."))


class ProposalStoreTests(unittest.TestCase):
    def proposal(
        self, key: int, size: int, photo_ids: frozenset[int] = frozenset()
    ) -> PhotoProposal:
        return PhotoProposal(
            key,
            ADMIN_USER_ID,
            1,
            (PhotoImage(b"x" * size, "image/jpeg"),),
            None,
            0,
            PhotoAnalysis((), 81.0, ""),
            photo_ids=photo_ids,
        )

    def test_image_bytes_are_bounded_and_album_photos_find_the_proposal(self) -> None:
        store = PhotoProposalStore(max_image_bytes=10)
        store.add(self.proposal(1, 6))
        store.add(self.proposal(2, 6, frozenset({2, 3, 4})))

        self.assertIsNone(store.get(1, ADMIN_USER_ID, 1))
        self.assertEqual(2, store.find_by_message(ADMIN_USER_ID, 1, 4).key)
        # The newest proposal stays even when it alone is over the budget.
        store.add(self.proposal(5, 50))
        self.assertIsNotNone(store.get(5, ADMIN_USER_ID, 1))
        self.assertIsNone(store.get(2, ADMIN_USER_ID, 1))


class PhotoSettingsTests(unittest.TestCase):
    def test_api_key_is_optional_and_not_in_repr(self) -> None:
        self.assertIsNone(load_settings({"BOT_TOKEN": "t"}).anthropic_api_key)
        settings = load_settings({"BOT_TOKEN": "t", "ANTHROPIC_API_KEY": " sk-x "})
        self.assertEqual("sk-x", settings.anthropic_api_key)
        self.assertNotIn("sk-x", repr(settings))
        self.assertIsNone(settings.anthropic_workspace_id)

    def test_workspace_id_is_sent_as_a_header(self) -> None:
        settings = load_settings(
            {
                "BOT_TOKEN": "t",
                "ANTHROPIC_API_KEY": "k",
                "ANTHROPIC_WORKSPACE_ID": " ws ",
            }
        )
        analyzer = build_analyzer(
            settings.anthropic_api_key,
            settings.photo_model,
            settings.photo_effort,
            settings.anthropic_workspace_id,
        )
        self.assertEqual(
            "ws", analyzer._client.default_headers["anthropic-workspace-id"]
        )
        plain = build_analyzer("k", "m", "high")
        self.assertNotIn("anthropic-workspace-id", plain._client.default_headers)

    def test_photo_model_and_effort_default_and_override(self) -> None:
        settings = load_settings({"BOT_TOKEN": "t"})
        self.assertEqual(
            ("claude-haiku-5-5", "high"), (settings.photo_model, settings.photo_effort)
        )
        settings = load_settings(
            {
                "BOT_TOKEN": "t",
                "PHOTO_MODEL": " claude-sonnet-5-5 ",
                "PHOTO_EFFORT": "Medium",
            }
        )
        self.assertEqual(
            ("claude-sonnet-5-5", "medium"),
            (settings.photo_model, settings.photo_effort),
        )
        with self.assertRaises(ConfigError):
            load_settings({"BOT_TOKEN": "t", "PHOTO_EFFORT": "turbo"})
