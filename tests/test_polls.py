from datetime import timedelta

import pytest

from tests.conftest import ALICE, BOB, CHANNEL, GUILD, MOD, START


def make_poll(service, options="Pizza; Burger; Sushi", duration=None):
    reply, poll = service.create_poll(GUILD, CHANNEL, ALICE, "Dinner?", options, duration)
    assert reply.ok, reply.text
    return poll


def test_create_poll(service):
    reply, poll = service.create_poll(GUILD, CHANNEL, ALICE, " Dinner? ", "Pizza; Burger; Sushi")
    assert reply.ok
    assert poll.question == "Dinner?"
    assert poll.options == ["Pizza", "Burger", "Sushi"]
    assert poll.ends_at is None and not poll.closed
    assert "**Pizza**" in reply.text and "0 votes" in reply.text
    assert f"poll `#{poll.id}`" in reply.text


def test_create_poll_with_duration(service):
    poll = make_poll(service, duration="1h")
    assert poll.ends_at == START + timedelta(hours=1)
    assert f"<t:{int(poll.ends_at.timestamp())}:R>" in service.render_poll(poll)


@pytest.mark.parametrize("question,options,duration,fragment", [
    ("", "a;b", None, "write a question"),
    ("q" * 201, "a;b", None, "question is too long"),
    ("Q?", "only one", None, "at least 2 options"),
    ("Q?", ";".join(str(i) for i in range(11)), None, "maximum is 10"),
    ("Q?", "a;" + "b" * 81, None, "too long"),
    ("Q?", "Yes; yes", None, "same"),
    ("Q?", "a;b", "later", "don't understand"),
    ("Q?", "a;b", "10s", "between 1 minute and 365 days"),
])
def test_create_poll_validation(service, storage, question, options, duration, fragment):
    reply, poll = service.create_poll(GUILD, CHANNEL, ALICE, question, options, duration)
    assert poll is None
    assert not reply.ok
    assert fragment in reply.text
    assert storage.due_polls(START + timedelta(days=999)) == []


def test_ten_options_are_allowed(service):
    poll = make_poll(service, options=";".join(f"Option {i}" for i in range(10)))
    assert len(poll.options) == 10


def test_vote_change_and_take_back(service, storage):
    poll = make_poll(service)
    assert service.vote(poll.id, BOB, 0).text == "✅ You voted for **Pizza**."
    assert storage.vote_counts(poll.id, 3) == [1, 0, 0]

    reply = service.vote(poll.id, BOB, 2)
    assert "from **Pizza** to **Sushi**" in reply.text
    assert storage.vote_counts(poll.id, 3) == [0, 0, 1]

    reply = service.vote(poll.id, BOB, 2)  # same button again
    assert "removed" in reply.text
    assert storage.vote_counts(poll.id, 3) == [0, 0, 0]


def test_one_vote_per_person(service, storage):
    poll = make_poll(service)
    for user in (ALICE, BOB, MOD):
        service.vote(poll.id, user, 1)
    service.vote(poll.id, BOB, 1)  # takes Bob's vote back
    assert storage.vote_counts(poll.id, 3) == [0, 2, 0]


def test_vote_on_unknown_poll_or_option(service):
    poll = make_poll(service)
    assert not service.vote(999, BOB, 0).ok
    assert not service.vote(poll.id, BOB, 3).ok
    assert not service.vote(poll.id, BOB, -1).ok


def test_render_poll_bars_and_percent(service):
    poll = make_poll(service)
    for user, option in ((ALICE, 0), (BOB, 0), (MOD, 1)):
        service.vote(poll.id, user, option)
    text = service.render_poll(poll)
    assert "**Pizza**\n`███████░░░` 67% (2)" in text
    assert "**Burger**\n`███░░░░░░░` 33% (1)" in text
    assert "**Sushi**\n`░░░░░░░░░░` 0% (0)" in text
    assert "3 votes" in text
    assert f"started by <@{ALICE}>" in text


def test_author_can_close_poll(service):
    poll = make_poll(service)
    service.vote(poll.id, BOB, 1)
    reply, closed = service.close_poll(poll.id, ALICE, is_moderator=False)
    assert reply.ok and closed.closed
    text = service.render_poll(closed)
    assert "Poll closed" in text
    assert "🏆 Winner: **Burger**" in text
    assert not service.vote(poll.id, MOD, 0).ok  # no more votes


def test_moderator_can_close_but_others_cannot(service):
    poll = make_poll(service)
    reply, closed = service.close_poll(poll.id, BOB, is_moderator=False)
    assert closed is None and not reply.ok
    reply, closed = service.close_poll(poll.id, MOD, is_moderator=True)
    assert closed is not None
    reply, again = service.close_poll(poll.id, MOD, is_moderator=True)
    assert again is None and "already closed" in reply.text


def test_close_unknown_poll(service):
    reply, poll = service.close_poll(42, ALICE, True)
    assert poll is None and "#42" in reply.text


def test_tie_and_no_votes(service):
    poll = make_poll(service)
    service.vote(poll.id, ALICE, 0)
    service.vote(poll.id, BOB, 2)
    _, closed = service.close_poll(poll.id, ALICE, False)
    assert "🏆 Tie: **Pizza / Sushi**" in service.render_poll(closed)

    empty = make_poll(service)
    _, closed = service.close_poll(empty.id, ALICE, False)
    text = service.render_poll(closed)
    assert "0 votes" in text and "🏆" not in text


def test_polls_close_automatically(service, clock):
    timed = make_poll(service, duration="30m")
    open_end = make_poll(service)
    assert service.pop_due_polls() == []
    clock.advance(minutes=30)
    [due] = service.pop_due_polls()
    assert due.id == timed.id and due.closed
    assert service.get_poll(timed.id).closed
    assert not service.get_poll(open_end.id).closed
    assert service.pop_due_polls() == []


def test_attach_message(service):
    poll = make_poll(service)
    service.attach_poll_message(poll.id, 555)
    assert service.get_poll(poll.id).message_id == 555
