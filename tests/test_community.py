"""Self roles, welcome messages and moderation."""

import pytest

from community_bot.service import DEFAULT_WELCOME
from tests.conftest import ALICE, BOB, CHANNEL, GUILD, MOD, OTHER_GUILD

ROLE_GAMER, ROLE_ART = 501, 502


# --------------------------------------------------------------- self roles

def check(position=1, top=5, managed=False, default=False, dangerous=False):
    from community_bot.service import CommunityService
    return CommunityService.check_self_role("Gamer", position, top, managed, default, dangerous)


def test_normal_role_is_allowed():
    assert check() is None


@pytest.mark.parametrize("kwargs,fragment", [
    ({"default": True}, "@everyone"),
    ({"managed": True}, "integration"),
    ({"dangerous": True}, "moderator or admin"),
    ({"position": 5, "top": 5}, "above my own role"),
    ({"position": 7, "top": 5}, "above my own role"),
])
def test_role_is_refused(kwargs, fragment):
    assert fragment in check(**kwargs)


def test_toggle_role(service):
    service.register_role_panel(GUILD, [ROLE_GAMER, ROLE_ART])
    result = service.toggle_role(GUILD, ROLE_GAMER, "Gamer", has_role=False)
    assert result.add is True and "now have the role **Gamer**" in result.reply.text
    result = service.toggle_role(GUILD, ROLE_GAMER, "Gamer", has_role=True)
    assert result.add is False and "Removed" in result.reply.text


def test_toggle_only_registered_roles(service):
    service.register_role_panel(GUILD, [ROLE_GAMER])
    for guild, role in ((GUILD, ROLE_ART), (OTHER_GUILD, ROLE_GAMER)):
        result = service.toggle_role(guild, role, "X", has_role=False)
        assert result.add is None and not result.reply.ok


def test_register_twice_is_fine(service, storage):
    service.register_role_panel(GUILD, [ROLE_GAMER])
    service.register_role_panel(GUILD, [ROLE_GAMER, ROLE_ART])
    assert storage.is_self_role(GUILD, ROLE_ART)


def test_role_panel_text(service):
    text = service.role_panel_text(["Gamer", "Artist"])
    assert "• **Gamer**\n• **Artist**" in text


# ------------------------------------------------------------------ welcome

def test_welcome_off_by_default(service):
    assert service.welcome_for(GUILD, ALICE, "Alice", "Cozy", 10) is None


def test_welcome_default_message(service):
    reply = service.set_welcome(GUILD, CHANNEL, None)
    assert reply.ok and f"<#{CHANNEL}>" in reply.text
    delivery = service.welcome_for(GUILD, ALICE, "Alice", "Cozy Corner", 42)
    assert delivery.channel_id == CHANNEL
    assert delivery.text == DEFAULT_WELCOME.replace("{server}", "Cozy Corner") \
        .replace("{user}", f"<@{ALICE}>").replace("{count}", "42")


def test_welcome_custom_message(service):
    service.set_welcome(GUILD, CHANNEL, "Hey {name}, read the rules! ({count})")
    assert service.welcome_for(GUILD, ALICE, "Alice", "S", 7).text == "Hey Alice, read the rules! (7)"
    assert service.welcome_for(OTHER_GUILD, ALICE, "Alice", "S", 7) is None


def test_welcome_message_too_long(service):
    reply = service.set_welcome(GUILD, CHANNEL, "x" * 1001)
    assert not reply.ok
    assert service.welcome_for(GUILD, ALICE, "A", "S", 1) is None


def test_welcome_change_and_disable(service):
    service.set_welcome(GUILD, CHANNEL, "first")
    service.set_welcome(GUILD, 99, "second")
    delivery = service.welcome_for(GUILD, ALICE, "A", "S", 1)
    assert (delivery.channel_id, delivery.text) == (99, "second")
    assert service.disable_welcome(GUILD).ok
    assert service.welcome_for(GUILD, ALICE, "A", "S", 1) is None
    assert not service.disable_welcome(GUILD).ok  # already off


# --------------------------------------------------------------- moderation

def test_warn_counts_up(service):
    first = service.warn(GUILD, BOB, MOD, "Spam")
    assert first.ok and "warning number **1**" in first.text and "> Spam" in first.text
    second = service.warn(GUILD, BOB, MOD, "More spam")
    assert "warning number **2**" in second.text
    # Warnings are counted per server.
    assert "number **1**" in service.warn(OTHER_GUILD, BOB, MOD, "Spam").text


@pytest.mark.parametrize("user,reason,is_bot,fragment", [
    (MOD, "x", False, "yourself"),
    (BOB, "x", True, "Bots"),
    (BOB, "  ", False, "reason"),
    (BOB, "x" * 301, False, "too long"),
])
def test_warn_validation(service, storage, user, reason, is_bot, fragment):
    reply = service.warn(GUILD, user, MOD, reason, target_is_bot=is_bot)
    assert not reply.ok and fragment in reply.text
    assert storage.warnings_for(GUILD, user) == []


def test_list_and_clear_warnings(service):
    assert "no warnings" in service.list_warnings(GUILD, BOB).text
    service.warn(GUILD, BOB, MOD, "Spam")
    service.warn(GUILD, BOB, ALICE, "Rude")
    text = service.list_warnings(GUILD, BOB).text
    assert f"Warnings for <@{BOB}> (2)" in text
    assert f"by <@{MOD}>: Spam" in text and f"by <@{ALICE}>: Rude" in text

    assert "Removed 2 warnings" in service.clear_warnings(GUILD, BOB).text
    assert not service.clear_warnings(GUILD, BOB).ok


def test_purge_text(service):
    assert service.purge_text(1) == "🧹 Deleted 1 message."
    assert service.purge_text(20) == "🧹 Deleted 20 messages."


def test_help_mentions_every_command(service):
    text = service.help()
    for name in ("remind", "reminders", "reminder-cancel", "poll", "poll-close", "roles-panel",
                 "welcome-set", "welcome-off", "welcome-test", "warn", "warnings",
                 "warnings-clear", "purge"):
        assert f"`/{name}`" in text
