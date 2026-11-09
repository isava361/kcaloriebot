from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram.error import BadRequest

from kcaloriebot.bot import (
    ADMIN_USER_ID,
    MAIN_KEYBOARD,
    PHOTO_CORRECTION_FAILED_TEXT,
    PHOTO_EXPIRED_TEXT,
    PHOTO_FAILED_TEXT,
    PHOTO_RESTART_TEXT,
    _stop_photo_tasks,
    handle_callback,
    handle_photo,
    handle_photo_callback,
    handle_text,
)
from kcaloriebot.config import ConfigError, load_settings
from kcaloriebot.database import Database
from kcaloriebot.photo_ai import (
    FoodGuess,
    PhotoAnalysis,
    PhotoAnalyzer,
    PhotoUnavailable,
    build_analyzer,
    parse_analysis,
)
from kcaloriebot.photo_proposals import PROPOSAL_TTL_SECONDS, PhotoProposalStore
from tests.test_bot_handlers import (
    FakeMessage,
    FakeQuery,
    keyboard_callbacks,
    make_update,
)


class FakeTelegramFile:
    async def download_as_bytearray(self) -> bytearray:
        return bytearray(b"jpeg-bytes")


class FakePhotoSize:
    file_size = 1000

    async def get_file(self) -> FakeTelegramFile:
        return FakeTelegramFile()


class FakeAnalyzer:
    def __init__(self, result: PhotoAnalysis | Exception) -> None:
        self.result = result
        self.calls: list[tuple[bytes, str, str | None]] = []
        self.revisions: list[dict[str, Any]] = []
        # Cleared to hold the analysis until a test releases it.
        self.gate = asyncio.Event()
        self.gate.set()

    async def analyze(
        self,
        image: bytes,
        media_type: str,
        caption: str | None = None,
        **revision: Any,
    ) -> PhotoAnalysis:
        self.calls.append((image, media_type, caption))
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


def photo_update(user_id: int = ADMIN_USER_ID, caption: str | None = None) -> Any:
    update = make_update(user_id=user_id)
    message = ChatMessage()
    message.date = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    message.photo = (FakePhotoSize(), FakePhotoSize())
    message.document = None
    message.caption = caption
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

        analysis = await analyzer.analyze(b"img", "image/png", "200 г")

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
            b"img",
            "image/jpeg",
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

        analysis = await PhotoAnalyzer(client, "claude-test", "low").analyze(
            b"img", "image/jpeg"
        )

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
                    await PhotoAnalyzer(client, "claude-test", "low").analyze(
                        b"img", "image/jpeg"
                    )

    async def test_endless_pausing_gives_up(self) -> None:
        client, messages = fake_client(
            *(response("pause_turn", SEARCH_BLOCK) for _ in range(10))
        )

        with self.assertRaises(PhotoUnavailable):
            await PhotoAnalyzer(client, "claude-test", "low").analyze(
                b"img", "image/jpeg"
            )
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

        self.assertEqual([(b"jpeg-bytes", "image/jpeg", "обед")], self.analyzer.calls)
        self.assertEqual([], self.entries())
        # One proposal message, quoting the photo; the status is removed.
        self.assertEqual(2, len(message.replies))
        self.assertEqual([False, True], message.quoted)
        self.assertTrue(status.deleted)
        text, markup = message.replies[-1]
        self.assertTrue(text.startswith("Гречка с котлетой.\n\n"))
        self.assertIn("• Гречка — 200 г · 220 ккал · Б 8 · Ж 2 · У 42", text)
        self.assertIn("• Котлета — 100 г · 220 ккал\n", text)
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

        self.assertEqual((b"jpeg-bytes", "image/jpeg", "обед"), self.analyzer.calls[-1])
        self.assertEqual(
            {"previous": old_analysis, "corrections": ("гречки было 150 г",)},
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
