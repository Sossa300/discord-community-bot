"""Settings from environment variables (or a local .env file).

The bot token is a password: whoever has it controls the bot. It therefore
lives in .env, which is listed in .gitignore and never committed. The repo
only ships .env.example with placeholder values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when a required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    token: str
    guild_id: int | None = None
    db_path: Path = Path("data/community.db")
    check_interval_seconds: float = 30.0
    max_reminders_per_user: int = 10
    log_level: str = "INFO"


def _number(env: dict, name: str, default: float, minimum: float) -> float:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


def _guild_id(env: dict) -> int | None:
    raw = (env.get("GUILD_ID") or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ConfigError(f"GUILD_ID must be a server ID (only digits), got {raw!r}")
    return int(raw)


def load_settings(env: dict | None = None, dotenv_path: str | None = ".env") -> Settings:
    """Build Settings from env (default: os.environ after reading .env).

    Passing env explicitly lets the tests check every rule without touching
    the real environment or needing a real token.
    """
    if env is None:
        if dotenv_path:
            load_dotenv(dotenv_path)
        env = dict(os.environ)

    token = env.get("DISCORD_BOT_TOKEN", "").strip()
    if not token or token.startswith("your-"):
        raise ConfigError(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and paste "
            "the token from the Discord Developer Portal (Bot -> Reset Token)."
        )

    return Settings(
        token=token,
        guild_id=_guild_id(env),
        db_path=Path(env.get("DB_PATH") or "data/community.db"),
        # Minimum 5 seconds: reminders are not more precise than that anyway.
        check_interval_seconds=_number(env, "CHECK_INTERVAL_SECONDS", 30.0, 5.0),
        max_reminders_per_user=int(_number(env, "MAX_REMINDERS_PER_USER", 10, 1)),
        log_level=(env.get("LOG_LEVEL") or "INFO").upper(),
    )
