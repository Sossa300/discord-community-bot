"""The small pure helpers: durations, options, bars, templates."""

from datetime import timedelta

import pytest

from community_bot.service import (
    duration_text, parse_duration, parse_options, progress_bar, render_template, timestamp,
)
from tests.conftest import START


@pytest.mark.parametrize("text,expected", [
    ("10m", timedelta(minutes=10)),
    ("10", timedelta(minutes=10)),
    ("2h30m", timedelta(hours=2, minutes=30)),
    ("2h 30m", timedelta(hours=2, minutes=30)),
    ("1d", timedelta(days=1)),
    ("1 day", timedelta(days=1)),
    ("3 days, 4 hours", timedelta(days=3, hours=4)),
    ("1w", timedelta(weeks=1)),
    ("90s", timedelta(seconds=90)),
    ("15 MIN", timedelta(minutes=15)),
])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "abc", "10x", "2h abc", "h", "tomorrow", "-5m", None])
def test_parse_duration_rejects(text):
    assert parse_duration(text) is None


@pytest.mark.parametrize("delta,expected", [
    (timedelta(minutes=1), "1 minute"),
    (timedelta(hours=2, minutes=30), "2 hours 30 minutes"),
    (timedelta(days=1, hours=1), "1 day 1 hour"),
    (timedelta(seconds=20), "less than a minute"),
])
def test_duration_text(delta, expected):
    assert duration_text(delta) == expected


def test_timestamp():
    unix = int(START.timestamp())
    assert timestamp(START) == f"<t:{unix}:R>"
    assert timestamp(START, "f") == f"<t:{unix}:f>"


def test_parse_options():
    assert parse_options(" Pizza ;Burger;; Sushi ; ") == ["Pizza", "Burger", "Sushi"]
    assert parse_options("") == []
    assert parse_options(None) == []


@pytest.mark.parametrize("share,bar", [(0, "░" * 10), (1, "█" * 10), (0.5, "█" * 5 + "░" * 5),
                                       (0.34, "███░░░░░░░")])
def test_progress_bar(share, bar):
    assert progress_bar(share) == bar


def test_render_template():
    text = render_template("Hi {user} ({name}), welcome to {server}! #{count} {user}",
                           "<@1>", "Alice", "Cozy Corner", 42)
    assert text == "Hi <@1> (Alice), welcome to Cozy Corner! #42 <@1>"


def test_render_template_ignores_python_format_tricks():
    # str.format would evaluate attribute access; replace() just leaves it alone.
    text = render_template("{user.__class__} {0} {unknown}", "<@1>", "A", "S", 1)
    assert text == "{user.__class__} {0} {unknown}"
