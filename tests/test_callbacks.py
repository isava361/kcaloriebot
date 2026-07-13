import importlib
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


class _TelegramStub:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


def _load_callback_parser():
    """Load parser code without requiring Telegram's transport modules."""
    telegram = ModuleType("telegram")
    for name in (
        "InlineKeyboardButton",
        "InlineKeyboardMarkup",
        "KeyboardButton",
        "ReplyKeyboardMarkup",
        "Update",
    ):
        setattr(telegram, name, _TelegramStub)

    constants = ModuleType("telegram.constants")
    constants.ChatType = SimpleNamespace(PRIVATE="private")
    errors = ModuleType("telegram.error")
    errors.TelegramError = type("TelegramError", (Exception,), {})
    extensions = ModuleType("telegram.ext")
    for name in (
        "Application",
        "ApplicationBuilder",
        "CallbackQueryHandler",
        "CommandHandler",
        "ContextTypes",
        "MessageHandler",
    ):
        setattr(extensions, name, _TelegramStub)
    extensions.filters = SimpleNamespace(TEXT=None, COMMAND=None)

    modules = {
        "telegram": telegram,
        "telegram.constants": constants,
        "telegram.error": errors,
        "telegram.ext": extensions,
    }
    with patch.dict(sys.modules, modules):
        return importlib.import_module("kcaloriebot.bot")


_bot = _load_callback_parser()
CallbackAction = _bot.CallbackAction
parse_callback = _bot.parse_callback


class CallbackParsingTests(unittest.TestCase):
    def test_current_callback_formats(self) -> None:
        cases = {
            "cancel": CallbackAction("cancel"),
            "cancel_all": CallbackAction("cancel"),
            "dismiss": CallbackAction("dismiss"),
            "entry:list:10": CallbackAction("entry_list", offset=10),
            "fav:list:5": CallbackAction("favorite_list", offset=5),
            "entry:view:7": CallbackAction("entry_view", record_id=7),
            "entry:view:7:10": CallbackAction("entry_view", record_id=7, offset=10),
            "entry:delete:7": CallbackAction("entry_delete", record_id=7),
            "entry:delete-confirm:7": CallbackAction(
                "entry_delete_confirm", record_id=7
            ),
            "entry:delete-confirm:7:1700000000": CallbackAction(
                "entry_delete_confirm", record_id=7, issued_at=1_700_000_000
            ),
            "fav:view:11": CallbackAction("favorite_view", record_id=11),
            "fav:view:11:5": CallbackAction("favorite_view", record_id=11, offset=5),
            "fav:use:11": CallbackAction("favorite_use", record_id=11),
            "fav:edit:11": CallbackAction("favorite_edit", record_id=11),
            "fav:delete:11": CallbackAction("favorite_delete", record_id=11),
            "fav:delete-confirm:11": CallbackAction(
                "favorite_delete_confirm", record_id=11
            ),
            "fav:delete-confirm:11:1700000000": CallbackAction(
                "favorite_delete_confirm", record_id=11, issued_at=1_700_000_000
            ),
            "fav:field:11:carbs": CallbackAction(
                "favorite_field", record_id=11, nutrient="carbs"
            ),
        }
        for data, expected in cases.items():
            with self.subTest(data=data):
                self.assertEqual(parse_callback(data), expected)

    def test_every_supported_favorite_nutrient_is_parsed(self) -> None:
        for nutrient in ("calories", "protein", "fat", "carbs"):
            with self.subTest(nutrient=nutrient):
                self.assertEqual(
                    parse_callback(f"fav:field:9:{nutrient}"),
                    CallbackAction("favorite_field", record_id=9, nutrient=nutrient),
                )

    def test_legacy_callback_formats_remain_supported(self) -> None:
        cases = {
            "previous:5": CallbackAction("entry_list", offset=5),
            "next:10": CallbackAction("entry_list", offset=10),
            "previous_fav:5": CallbackAction("favorite_list", offset=5),
            "next_fav:10": CallbackAction("favorite_list", offset=10),
            "entry_choose_3": CallbackAction("entry_view", record_id=3),
            "entry_delete_3": CallbackAction("entry_delete", record_id=3),
            "entry_confirm_delete_3": CallbackAction(
                "entry_delete_confirm", record_id=3
            ),
            "entry_cancel_delete_3": CallbackAction("dismiss", record_id=3),
            "choose_favorite_4": CallbackAction("favorite_view", record_id=4),
            "favorite_4": CallbackAction("favorite_use", record_id=4),
            "fave_amend_4": CallbackAction("favorite_edit", record_id=4),
            "favedelete_4": CallbackAction("favorite_delete", record_id=4),
            "fave_confirm_delete_4": CallbackAction(
                "favorite_delete_confirm", record_id=4
            ),
            "fave_cancel_delete_4": CallbackAction("dismiss", record_id=4),
            "protein_amend_4": CallbackAction(
                "favorite_field", record_id=4, nutrient="protein"
            ),
        }
        for data, expected in cases.items():
            with self.subTest(data=data):
                self.assertEqual(parse_callback(data), expected)

    def test_invalid_ids_are_rejected(self) -> None:
        for data in (
            "entry:view:0",
            "entry:view:-1",
            "entry:view:not-an-id",
            "fav:use:0",
            "fav:field:-1:protein",
            "favorite_0",
        ):
            with self.subTest(data=data):
                self.assertIsNone(parse_callback(data))

    def test_invalid_offsets_are_rejected(self) -> None:
        for data in (
            "entry:list:-5",
            "entry:list:1",
            "entry:list:1000005",
            "entry:list:10005",
            "fav:list:not-an-offset",
            "next:6",
        ):
            with self.subTest(data=data):
                self.assertIsNone(parse_callback(data))

    def test_unknown_or_malformed_actions_are_rejected(self) -> None:
        for data in (
            "",
            "unknown",
            "entry:view",
            "entry:view:1:extra",
            "entry:edit:1",
            "fav:field:1:sodium",
            "fav:field:1",
            "entry:delete-confirm:1:0",
        ):
            with self.subTest(data=data):
                self.assertIsNone(parse_callback(data))


if __name__ == "__main__":
    unittest.main()
