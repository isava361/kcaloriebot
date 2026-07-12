import logging
import tempfile
import unittest
from pathlib import Path

from kcaloriebot.config import ConfigError, load_settings


class SettingsTests(unittest.TestCase):
    def test_token_is_required_and_cannot_be_blank(self) -> None:
        for environ in ({}, {"BOT_TOKEN": ""}, {"BOT_TOKEN": "  \t "}):
            with self.subTest(environ=environ):
                with self.assertRaises(ConfigError):
                    load_settings(environ)

    def test_token_is_trimmed(self) -> None:
        settings = load_settings({"BOT_TOKEN": "  secret-token\n"})

        self.assertEqual(settings.bot_token, "secret-token")

    def test_default_database_path_is_absolute_and_inside_data_directory(self) -> None:
        settings = load_settings({"BOT_TOKEN": "secret-token"})

        self.assertTrue(settings.database_path.is_absolute())
        self.assertEqual(settings.database_path.name, "kcaloriebot.db")
        self.assertEqual(settings.database_path.parent.name, "data")

    def test_database_path_override_is_expanded_and_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            configured = Path(temporary_directory) / "nested" / "bot.db"
            settings = load_settings(
                {"BOT_TOKEN": "secret-token", "DATABASE_PATH": str(configured)}
            )

        self.assertEqual(settings.database_path, configured.resolve())

    def test_log_level_is_case_insensitive(self) -> None:
        settings = load_settings(
            {"BOT_TOKEN": "secret-token", "LOG_LEVEL": " warning "}
        )

        self.assertEqual(settings.log_level, logging.WARNING)

    def test_invalid_log_level_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_settings({"BOT_TOKEN": "secret-token", "LOG_LEVEL": "LOUD"})

    def test_settings_repr_redacts_token(self) -> None:
        secret = "do-not-log-this-token"
        settings = load_settings({"BOT_TOKEN": secret})

        representation = repr(settings)
        self.assertNotIn(secret, representation)
        self.assertIn("<redacted>", representation)


if __name__ == "__main__":
    unittest.main()
