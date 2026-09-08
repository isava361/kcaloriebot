from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True, repr=False)
class Settings:
    bot_token: str
    database_path: Path
    log_level: int
    miniapp_url: Optional[str] = None

    def __repr__(self) -> str:
        return (
            "Settings(bot_token='<redacted>', "
            f"database_path={self.database_path!r}, log_level={self.log_level!r})"
        )


def load_settings(environ: Optional[Mapping[str, str]] = None) -> Settings:
    values = os.environ if environ is None else environ
    token = values.get("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("BOT_TOKEN is required and cannot be blank.")

    project_root = Path(__file__).resolve().parent.parent
    configured_path = values.get("DATABASE_PATH", "").strip()
    database_path = (
        Path(configured_path).expanduser().resolve()
        if configured_path
        else project_root / "data" / "kcaloriebot.db"
    )

    level_name = values.get("LOG_LEVEL", "INFO").strip().upper()
    log_level = getattr(logging, level_name, None)
    if not isinstance(log_level, int):
        raise ConfigError(f"Invalid LOG_LEVEL: {level_name}")
    miniapp_url = values.get("MINIAPP_URL", "").strip() or None
    if miniapp_url:
        try:
            parsed = urlsplit(miniapp_url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("MINIAPP_URL must be a public HTTPS URL.")
    return Settings(
        bot_token=token,
        database_path=database_path,
        log_level=log_level,
        miniapp_url=miniapp_url,
    )
