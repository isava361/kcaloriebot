from __future__ import annotations

import logging

from .bot import build_application
from .config import ConfigError, load_settings


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    application = build_application(settings)
    application.run_polling(
        allowed_updates=("message", "callback_query"),
        drop_pending_updates=False,
        close_loop=True,
    )


if __name__ == "__main__":
    main()
