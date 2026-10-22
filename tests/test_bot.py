"""The Discord layer, tested with fake Discord objects.

The bot is built but never logged in. Interactions, channels and members are
small stand-ins (AsyncMock records every call), so we can check what the bot
would send without a token or a network connection.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from community_bot.bot import PollButton, RoleButton, answer, build_bot, poll_view
from community_bot.config import Settings
from community_bot.service import Reply
from tests.conftest import ALICE, BOB, CHANNEL, GUILD

ADMIN_COMMANDS = {
    "roles-panel": "manage_roles", "welcome-set": "manage_guild", "welcome-off": "manage_guild",
    "welcome-test": "manage_guild", "warn": "moderate_members", "warnings": "moderate_members",
    "warnings-clear": "moderate_members", "purge": "manage_messages",
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bot(service):
    return build_bot(Settings(token="fake"), service)


def fake_interaction(user_id=ALICE, done=False):
    response = SimpleNamespace(is_done=MagicMock(return_value=done), send_message=AsyncMock(),
                               edit_message=AsyncMock())
    return SimpleNamespace(response=response, followup=SimpleNamespace(send=AsyncMock()),
                           user=SimpleNamespace(id=user_id), guild=SimpleNamespace(id=GUILD))


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="nope"), "simulated")


# ------------------------------------------------------------------ commands

def test_all_commands_registered(bot):
    names = {c.name for c in bot.tree.get_commands()}
    assert names == {"help", "remind", "reminders", "reminder-cancel", "poll", "poll-close",
                     *ADMIN_COMMANDS}


@pytest.mark.parametrize("name,permission", ADMIN_COMMANDS.items())
def test_admin_commands_are_hidden_from_normal_members(bot, name, permission):
    command = bot.tree.get_command(name)
    assert getattr(command.default_permissions, permission) is True
    assert command.guild_only


@pytest.mark.parametrize("name", ["remind", "reminders", "poll", "poll-close"])
def test_member_commands_are_open(bot, name):
    assert bot.tree.get_command(name).default_permissions is None


def test_purge_amount_is_limited(bot):
    [amount] = bot.tree.get_command("purge").parameters
    assert (amount.min_value, amount.max_value) == (1, 100)


# ------------------------------------------------------------------- buttons

def test_poll_buttons(service):
    _, poll = service.create_poll(GUILD, CHANNEL, ALICE, "Q?", "Yes; No")
    view = poll_view(poll)
    assert [(b.item.label, b.custom_id) for b in view.children] == [
        ("Yes", f"poll:{poll.id}:0"), ("No", f"poll:{poll.id}:1")]
    assert view.timeout is None  # buttons keep working forever
    poll.closed = True
    assert poll_view(poll) is None


@pytest.mark.parametrize("cls,custom_id,attrs", [
    (PollButton, "poll:12:3", {"poll_id": 12, "option": 3}),
    (RoleButton, "role:987654321", {"role_id": 987654321}),
])
def test_buttons_are_rebuilt_from_custom_id(cls, custom_id, attrs):
    """After a restart, a click on an old message must still find its button."""
    match = cls.__discord_ui_compiled_template__.fullmatch(custom_id)
    item = discord.ui.Button(label="Old label", custom_id=custom_id)
    button = run(cls.from_custom_id(None, item, match))
    for name, value in attrs.items():
        assert getattr(button, name) == value
    assert button.item.label == "Old label"


@pytest.mark.parametrize("custom_id", ["poll:x:1", "poll:1", "role:", "role:12a"])
def test_foreign_custom_ids_are_ignored(custom_id):
    assert PollButton.__discord_ui_compiled_template__.fullmatch(custom_id) is None
    assert RoleButton.__discord_ui_compiled_template__.fullmatch(custom_id) is None


# -------------------------------------------------------------------- replies

def test_answer_ok_is_public():
    interaction = fake_interaction()
    run(answer(interaction, Reply("Done")))
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is False
    assert kwargs["embed"].description == "Done"
    assert kwargs["embed"].color == discord.Color.blurple()
    assert kwargs["allowed_mentions"].everyone is False


def test_answer_error_is_private_and_red():
    interaction = fake_interaction()
    run(answer(interaction, Reply("Nope", ok=False)))
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].color == discord.Color.red()


def test_answer_uses_followup_after_first_response():
    interaction = fake_interaction(done=True)
    run(answer(interaction, Reply("Later"), ephemeral=True, title="T"))
    interaction.response.send_message.assert_not_called()
    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs["ephemeral"] is True and kwargs["embed"].title == "T"


def test_vote_updates_poll_message(bot, service):
    _, poll = service.create_poll(GUILD, CHANNEL, ALICE, "Q?", "Yes; No")
    interaction = fake_interaction(user_id=BOB, done=True)
    run(bot.vote(interaction, poll.id, 1))
    edited = interaction.response.edit_message.call_args.kwargs
    assert "**No**\n`██████████` 100% (1)" in edited["embed"].description
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True


def test_vote_on_closed_poll_does_not_edit(bot, service):
    _, poll = service.create_poll(GUILD, CHANNEL, ALICE, "Q?", "Yes; No")
    service.close_poll(poll.id, ALICE, False)
    interaction = fake_interaction(user_id=BOB)
    run(bot.vote(interaction, poll.id, 0))
    interaction.response.edit_message.assert_not_called()
    assert "closed" in interaction.response.send_message.call_args.kwargs["embed"].description


# ----------------------------------------------------------- roles + welcome

def role_interaction(service, has_role=False, fail=False):
    role = SimpleNamespace(id=501, name="Gamer")
    member = SimpleNamespace(id=BOB, roles=[role] if has_role else [],
                             add_roles=AsyncMock(), remove_roles=AsyncMock())
    if fail:
        member.add_roles.side_effect = http_error(discord.Forbidden, 403)
    interaction = fake_interaction(user_id=BOB)
    interaction.user = member
    interaction.guild = SimpleNamespace(id=GUILD, get_role=lambda rid: role if rid == 501 else None)
    service.register_role_panel(GUILD, [501])
    return interaction, member, role


def test_role_button_adds_and_removes(bot, service):
    interaction, member, role = role_interaction(service)
    run(bot.toggle_role(interaction, 501))
    member.add_roles.assert_awaited_once()
    interaction, member, role = role_interaction(service, has_role=True)
    run(bot.toggle_role(interaction, 501))
    member.remove_roles.assert_awaited_once()


def test_role_button_without_permission(bot, service):
    interaction, member, role = role_interaction(service, fail=True)
    run(bot.toggle_role(interaction, 501))
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True and "not allowed" in kwargs["embed"].description


def test_role_button_for_deleted_role(bot, service):
    interaction, member, role = role_interaction(service)
    run(bot.toggle_role(interaction, 999))
    member.add_roles.assert_not_called()


def test_new_member_is_welcomed(bot, service):
    service.set_welcome(GUILD, CHANNEL, "Hi {user} on {server}!")
    channel = SimpleNamespace(send=AsyncMock())
    bot.get_channel = lambda cid: channel if cid == CHANNEL else None
    member = SimpleNamespace(
        id=BOB, mention=f"<@{BOB}>", display_name="Bob",
        display_avatar=SimpleNamespace(url="https://cdn.example/avatar.png"),
        guild=SimpleNamespace(id=GUILD, name="Cozy", member_count=5),
    )
    run(bot.on_member_join(member))
    kwargs = channel.send.call_args.kwargs
    assert kwargs["content"] == f"<@{BOB}>"
    assert kwargs["embed"].description == f"Hi <@{BOB}> on Cozy!"


# ------------------------------------------------------------ background loop

def test_background_sends_due_reminders_and_closes_polls(bot, service, clock):
    service.remind(GUILD, CHANNEL, ALICE, "1m", "Stretch")
    _, poll = service.create_poll(GUILD, CHANNEL, ALICE, "Q?", "Yes; No", "1m")
    service.attach_poll_message(poll.id, 777)

    message = SimpleNamespace(edit=AsyncMock(), reply=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock(), fetch_message=AsyncMock(return_value=message))
    bot.get_channel = lambda cid: channel
    clock.advance(minutes=1)
    run(bot.background.coro(bot))  # one run of the loop body

    assert "Stretch" in channel.send.call_args.args[0]
    channel.fetch_message.assert_awaited_once_with(777)
    assert message.edit.call_args.kwargs["view"] is None
    assert "Poll closed" in message.edit.call_args.kwargs["embed"].description


def test_reminder_falls_back_to_dm(bot, service, clock):
    service.remind(GUILD, CHANNEL, ALICE, "1m", "Stretch")
    channel = SimpleNamespace(send=AsyncMock(side_effect=http_error(discord.Forbidden, 403)))
    user = SimpleNamespace(send=AsyncMock())
    bot.get_channel = lambda cid: channel
    bot.fetch_user = AsyncMock(return_value=user)
    clock.advance(minutes=1)
    run(bot.background.coro(bot))
    bot.fetch_user.assert_awaited_once_with(ALICE)
    assert "Stretch" in user.send.call_args.args[0]
