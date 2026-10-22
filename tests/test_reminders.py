from datetime import timedelta

import pytest

from tests.conftest import ALICE, BOB, CHANNEL, GUILD, OTHER_GUILD, START


def test_remind_saves_and_confirms(service, storage):
    reply = service.remind(GUILD, CHANNEL, ALICE, "2h30m", "  Drink water  ")
    assert reply.ok
    due = START + timedelta(hours=2, minutes=30)
    assert f"<t:{int(due.timestamp())}:R>" in reply.text
    assert "> Drink water" in reply.text
    [saved] = storage.reminders_for_user(GUILD, ALICE)
    assert saved.text == "Drink water"
    assert saved.due_at == due
    assert saved.channel_id == CHANNEL
    assert f"#{saved.id}" in reply.text


@pytest.mark.parametrize("when,fragment", [("soon", "don't understand"), ("30s", "at least 1 minute"),
                                          ("400d", "maximum is 365 days")])
def test_remind_rejects_bad_times(service, storage, when, fragment):
    reply = service.remind(GUILD, CHANNEL, ALICE, when, "Text")
    assert not reply.ok
    assert fragment in reply.text
    assert storage.count_reminders(GUILD, ALICE) == 0


@pytest.mark.parametrize("text", ["", "   ", None])
def test_remind_needs_text(service, text):
    reply = service.remind(GUILD, CHANNEL, ALICE, "10m", text)
    assert not reply.ok
    assert "add a text" in reply.text


def test_remind_text_length_limit(service):
    assert service.remind(GUILD, CHANNEL, ALICE, "10m", "x" * 500).ok
    reply = service.remind(GUILD, CHANNEL, ALICE, "10m", "x" * 501)
    assert not reply.ok
    assert "too long" in reply.text


def test_remind_limit_per_user_and_server(service):
    for i in range(3):
        assert service.remind(GUILD, CHANNEL, ALICE, "10m", f"r{i}").ok
    reply = service.remind(GUILD, CHANNEL, ALICE, "10m", "one too many")
    assert not reply.ok
    assert "already have 3" in reply.text
    # The limit counts per person and per server.
    assert service.remind(GUILD, CHANNEL, BOB, "10m", "bob").ok
    assert service.remind(OTHER_GUILD, CHANNEL, ALICE, "10m", "other server").ok


def test_list_reminders_sorted_by_due_time(service):
    service.remind(GUILD, CHANNEL, ALICE, "2h", "later")
    service.remind(GUILD, CHANNEL, ALICE, "5m", "sooner")
    service.remind(GUILD, CHANNEL, BOB, "1m", "not Alice's")
    text = service.list_reminders(GUILD, ALICE).text
    assert "Your reminders (2)" in text
    assert text.index("sooner") < text.index("later")
    assert "not Alice's" not in text


def test_list_reminders_empty(service):
    reply = service.list_reminders(GUILD, ALICE)
    assert reply.ok
    assert "no open reminders" in reply.text


def test_list_shortens_long_text(service):
    service.remind(GUILD, CHANNEL, ALICE, "5m", "word " * 50)
    line = service.list_reminders(GUILD, ALICE).text.splitlines()[1]
    assert line.endswith("…")


def test_cancel_own_reminder(service, storage):
    service.remind(GUILD, CHANNEL, ALICE, "10m", "Tea")
    [r] = storage.reminders_for_user(GUILD, ALICE)
    reply = service.cancel_reminder(GUILD, ALICE, r.id)
    assert reply.ok
    assert "Cancelled" in reply.text and "Tea" in reply.text
    assert storage.count_reminders(GUILD, ALICE) == 0


def test_cannot_cancel_someone_elses_reminder(service, storage):
    service.remind(GUILD, CHANNEL, ALICE, "10m", "Secret")
    [r] = storage.reminders_for_user(GUILD, ALICE)
    reply = service.cancel_reminder(GUILD, BOB, r.id)
    assert not reply.ok
    assert "Secret" not in reply.text  # Bob learns nothing about it
    assert service.cancel_reminder(OTHER_GUILD, ALICE, r.id).ok is False
    assert storage.count_reminders(GUILD, ALICE) == 1


def test_cancel_unknown_reminder(service):
    reply = service.cancel_reminder(GUILD, ALICE, 999)
    assert not reply.ok
    assert "#999" in reply.text


def test_due_reminders_are_delivered_once(service, storage, clock):
    service.remind(GUILD, CHANNEL, ALICE, "10m", "Tea is ready")
    service.remind(GUILD, CHANNEL, BOB, "1h", "Meeting")

    assert service.pop_due_reminders() == []
    clock.advance(minutes=10)
    [delivery] = service.pop_due_reminders()
    assert delivery.channel_id == CHANNEL
    assert delivery.user_id == ALICE
    assert delivery.text.startswith(f"⏰ <@{ALICE}>")
    assert "> Tea is ready" in delivery.text
    assert service.pop_due_reminders() == []  # not sent twice

    clock.advance(hours=5)  # the bot was offline: late reminders still arrive
    [late] = service.pop_due_reminders()
    assert late.user_id == BOB
    assert storage.due_reminders(clock()) == []


def test_reminders_survive_a_restart(tmp_path, clock):
    from community_bot.service import CommunityService
    from community_bot.storage import Storage

    path = tmp_path / "data" / "bot.db"
    first = Storage(path)
    CommunityService(first, clock=clock).remind(GUILD, CHANNEL, ALICE, "1d", "Birthday")
    first.close()

    second = Storage(path)
    clock.advance(days=1)
    [delivery] = CommunityService(second, clock=clock).pop_due_reminders()
    assert "Birthday" in delivery.text
    second.close()
