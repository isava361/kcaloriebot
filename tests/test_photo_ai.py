from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kcaloriebot.bot import ADMIN_USER_ID, MAIN_KEYBOARD, handle_photo
from kcaloriebot.config import load_settings
from kcaloriebot.database import Database
from kcaloriebot.photo_ai import (
    MODEL,
    FoodGuess,
    PhotoAnalysis,
    PhotoAnalyzer,
    PhotoUnavailable,
    parse_analysis,
)
from tests.test_bot_handlers import FakeMessage, make_update


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

    async def analyze(
        self, image: bytes, media_type: str, caption: str | None = None
    ) -> PhotoAnalysis:
        self.calls.append((image, media_type, caption))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def photo_update(user_id: int = ADMIN_USER_ID, caption: str | None = None) -> Any:
    update = make_update(user_id=user_id)
    message: FakeMessage = update.effective_message
    message.date = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    message.photo = (FakePhotoSize(), FakePhotoSize())
    message.document = None
    message.caption = caption
    return update


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
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def fake_client(stop_reason: str, text: str) -> tuple[Any, FakeMessages]:
    response = SimpleNamespace(
        stop_reason=stop_reason,
        _request_id="req_test",
        content=[SimpleNamespace(type="text", text=text)],
    )
    messages = FakeMessages(response)
    return SimpleNamespace(messages=messages), messages


class PhotoAnalyzerTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_sends_image_caption_and_schema(self) -> None:
        payload = {"foods": [], "weight_kg": 0, "comment": "Пусто."}
        client, messages = fake_client("end_turn", json.dumps(payload))

        analysis = await PhotoAnalyzer(client).analyze(b"img", "image/png", "200 г")

        self.assertEqual("Пусто.", analysis.comment)
        kwargs = messages.kwargs
        self.assertEqual(MODEL, kwargs["model"])
        self.assertEqual("json_schema", kwargs["output_config"]["format"]["type"])
        image, caption = kwargs["messages"][0]["content"]
        self.assertEqual("image/png", image["source"]["media_type"])
        self.assertEqual(b"img", base64.b64decode(image["source"]["data"]))
        self.assertEqual("Caption: 200 г", caption["text"])

    async def test_refusal_is_unavailable(self) -> None:
        client, _ = fake_client("refusal", "")

        with self.assertRaises(PhotoUnavailable):
            await PhotoAnalyzer(client).analyze(b"img", "image/jpeg")


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

    async def test_admin_photo_logs_every_recognized_food(self) -> None:
        update = photo_update(caption="обед")

        await handle_photo(update, self.context)

        self.assertEqual([(b"jpeg-bytes", "image/jpeg", "обед")], self.analyzer.calls)
        entries = self.entries()
        self.assertEqual({"Гречка", "Котлета"}, {entry.name for entry in entries})
        self.assertEqual(
            [220.0, 220.0], [entry.nutrition.calories for entry in entries]
        )
        replies = [text for text, _ in update.effective_message.replies]
        self.assertIn("Гречка с котлетой.", replies)
        self.assertEqual(2, sum(text.startswith("Added ") for text in replies))

    async def test_other_users_photos_are_not_analyzed(self) -> None:
        update = photo_update(user_id=7)

        await handle_photo(update, self.context)

        self.assertEqual([], self.analyzer.calls)
        self.assertEqual([], self.entries(7))
        self.assertEqual(
            ("Choose a text command from the keyboard.", MAIN_KEYBOARD),
            update.effective_message.replies[-1],
        )

    async def test_scale_photo_records_weight(self) -> None:
        self.analyzer.result = PhotoAnalysis((), 81.4, "")
        update = photo_update()

        await handle_photo(update, self.context)

        self.assertEqual([], self.entries())
        self.assertEqual(81.4, self.database.latest_weight(ADMIN_USER_ID).weight_kg)

    async def test_analysis_failure_logs_nothing(self) -> None:
        self.analyzer.result = PhotoUnavailable("down")
        update = photo_update()

        with self.assertLogs("kcaloriebot.bot", "WARNING"):
            await handle_photo(update, self.context)

        self.assertEqual([], self.entries())
        self.assertIn("Не удалось", update.effective_message.replies[-1][0])


class PhotoSettingsTests(unittest.TestCase):
    def test_api_key_is_optional_and_not_in_repr(self) -> None:
        self.assertIsNone(load_settings({"BOT_TOKEN": "t"}).anthropic_api_key)
        settings = load_settings({"BOT_TOKEN": "t", "ANTHROPIC_API_KEY": " sk-x "})
        self.assertEqual("sk-x", settings.anthropic_api_key)
        self.assertNotIn("sk-x", repr(settings))
