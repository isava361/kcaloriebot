from __future__ import annotations

import logging

from .bot import build_application
from .config import ConfigError, Settings, load_settings


# Third-party loggers that log request details at INFO. httpx in particular
# logs the full Bot API request URL, which contains the bot token.
NOISY_LOGGERS = ("httpx", "httpcore", "telegram", "telegram.ext")


class TokenRedactionFilter(logging.Filter):
    """Replace the bot token with a placeholder in every log record.

    Defense in depth for records that still slip through at WARNING and
    above (for example httpx errors that include the request URL).
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self._token:
            rendered = record.getMessage()
            if self._token in rendered:
                record.msg = rendered.replace(self._token, "<bot-token>")
                record.args = ()
        return True


def configure_logging(settings: Settings) -> None:
    """Configure logging so the bot token never reaches the logs.

    The root logger stays at WARNING and LOG_LEVEL applies only to this
    application's logger, so third-party INFO records (such as httpx request
    URLs containing the token) are not emitted. A redaction filter on every
    root handler scrubs the token from anything that is emitted.
    """
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("kcaloriebot").setLevel(settings.log_level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    redaction = TokenRedactionFilter(settings.bot_token)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redaction)


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    configure_logging(settings)
    application = build_application(settings)
    application.run_polling(
        allowed_updates=("message", "callback_query"),
        drop_pending_updates=False,
        close_loop=True,
    )


if __name__ == "__main__":
    main()
