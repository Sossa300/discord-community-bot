from pathlib import Path

import pytest

from community_bot.config import ConfigError, load_settings

TOKEN = "fake-token-for-tests"


def test_defaults():
    s = load_settings({"DISCORD_BOT_TOKEN": TOKEN})
    assert s.token == TOKEN
    assert s.guild_id is None
    assert s.db_path == Path("data/community.db")
    assert s.check_interval_seconds == 30
    assert s.max_reminders_per_user == 10
    assert s.log_level == "INFO"


def test_all_values():
    s = load_settings({
        "DISCORD_BOT_TOKEN": f"  {TOKEN} ", "GUILD_ID": " 123456789012345678 ",
        "DB_PATH": "x/y.db", "CHECK_INTERVAL_SECONDS": "10",
        "MAX_REMINDERS_PER_USER": "5", "LOG_LEVEL": "debug",
    })
    assert s.token == TOKEN
    assert s.guild_id == 123456789012345678
    assert s.db_path == Path("x/y.db")
    assert s.check_interval_seconds == 10
    assert s.max_reminders_per_user == 5
    assert s.log_level == "DEBUG"


@pytest.mark.parametrize("env", [{}, {"DISCORD_BOT_TOKEN": ""},
                                 {"DISCORD_BOT_TOKEN": "your-token-from-the-developer-portal"}])
def test_missing_token(env):
    with pytest.raises(ConfigError, match="DISCORD_BOT_TOKEN"):
        load_settings(env)


def test_guild_id_must_be_digits():
    with pytest.raises(ConfigError, match="GUILD_ID"):
        load_settings({"DISCORD_BOT_TOKEN": TOKEN, "GUILD_ID": "my-server"})


@pytest.mark.parametrize("name,value", [("CHECK_INTERVAL_SECONDS", "abc"),
                                        ("CHECK_INTERVAL_SECONDS", "1"),
                                        ("MAX_REMINDERS_PER_USER", "0")])
def test_bad_numbers(name, value):
    with pytest.raises(ConfigError, match=name):
        load_settings({"DISCORD_BOT_TOKEN": TOKEN, name: value})


def test_reads_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("DISCORD_BOT_TOKEN=from-file\n", encoding="utf-8")
    assert load_settings(dotenv_path=str(env_file)).token == "from-file"
