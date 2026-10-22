"""Start the bot:  python main.py

Stop it with Ctrl+C. Settings come from .env (see .env.example).
"""

import logging
import sys

import discord

from community_bot.bot import build_bot
from community_bot.config import ConfigError, load_settings


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # discord.py logs every gateway event at DEBUG; INFO is enough for us.
    logging.getLogger("discord").setLevel(max(logging.INFO, logging.getLogger().level))

    try:
        # log_handler=None: keep our logging setup instead of discord.py's own.
        build_bot(settings).run(settings.token, log_handler=None)
    except discord.LoginFailure:
        print("Login failed: the token in .env is wrong. Reset it in the "
              "Developer Portal and paste the new one.", file=sys.stderr)
        return 1
    except discord.PrivilegedIntentsRequired:
        print("Discord refused the Server Members intent. Turn on 'Server Members "
              "Intent' in the Developer Portal under Bot -> Privileged Gateway Intents.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
