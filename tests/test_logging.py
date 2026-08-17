"""Tests for logging configuration and bot-token redaction."""

from __future__ import annotations

import io
import logging
import unittest
from pathlib import Path

from kcaloriebot.__main__ import (
    NOISY_LOGGERS,
    TokenRedactionFilter,
    configure_logging,
)
from kcaloriebot.config import Settings


TOKEN = "123456:ABC-secret_token"


def make_settings(log_level: int = logging.INFO) -> Settings:
    return Settings(
        bot_token=TOKEN,
        database_path=Path("unused.db"),
        log_level=log_level,
    )


class TokenRedactionFilterTest(unittest.TestCase):
    def render(self, record: logging.LogRecord) -> str:
        TokenRedactionFilter(TOKEN).filter(record)
        return record.getMessage()

    def test_redacts_token_in_message(self) -> None:
        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            __file__,
            1,
            f"HTTP Request: POST https://api.telegram.org/bot{TOKEN}/getMe",
            None,
            None,
        )
        rendered = self.render(record)
        self.assertNotIn(TOKEN, rendered)
        self.assertIn("<bot-token>", rendered)

    def test_redacts_token_in_formatted_args(self) -> None:
        record = logging.LogRecord(
            "httpx",
            logging.ERROR,
            __file__,
            1,
            "request to %s failed",
            (f"https://api.telegram.org/bot{TOKEN}/sendMessage",),
            None,
        )
        rendered = self.render(record)
        self.assertNotIn(TOKEN, rendered)
        self.assertIn("<bot-token>", rendered)

    def test_leaves_clean_records_unchanged(self) -> None:
        record = logging.LogRecord(
            "kcaloriebot.bot", logging.INFO, __file__, 1, "update %d", (7,), None
        )
        self.assertEqual(self.render(record), "update 7")


class ConfigureLoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.saved_handlers = self.root.handlers[:]
        self.saved_level = self.root.level
        self.saved_levels = {
            name: logging.getLogger(name).level
            for name in (*NOISY_LOGGERS, "kcaloriebot")
        }
        self.root.handlers = []

    def tearDown(self) -> None:
        for handler in self.root.handlers:
            handler.close()
        self.root.handlers = self.saved_handlers
        self.root.setLevel(self.saved_level)
        for name, level in self.saved_levels.items():
            logging.getLogger(name).setLevel(level)

    def test_noisy_loggers_are_capped_at_warning(self) -> None:
        configure_logging(make_settings(logging.DEBUG))
        for name in NOISY_LOGGERS:
            self.assertEqual(
                logging.getLogger(name).getEffectiveLevel(), logging.WARNING, name
            )
        self.assertEqual(
            logging.getLogger("kcaloriebot").getEffectiveLevel(), logging.DEBUG
        )

    def test_httpx_info_with_token_is_not_emitted(self) -> None:
        configure_logging(make_settings())
        stream = io.StringIO()
        self.root.handlers[0].setStream(stream)
        logging.getLogger("httpx").info(
            "HTTP Request: POST https://api.telegram.org/bot%s/getMe", TOKEN
        )
        self.assertEqual(stream.getvalue(), "")

    def test_emitted_records_have_token_redacted(self) -> None:
        configure_logging(make_settings())
        stream = io.StringIO()
        self.root.handlers[0].setStream(stream)
        logging.getLogger("httpx").error(
            "request to https://api.telegram.org/bot%s/getMe timed out", TOKEN
        )
        output = stream.getvalue()
        self.assertNotIn(TOKEN, output)
        self.assertIn("<bot-token>", output)

    def test_application_logger_still_logs_info(self) -> None:
        configure_logging(make_settings())
        stream = io.StringIO()
        self.root.handlers[0].setStream(stream)
        logging.getLogger("kcaloriebot.bot").info("polling started")
        self.assertIn("polling started", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
