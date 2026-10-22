"""Shared test helpers.

No test talks to Discord or the internet. The database lives in memory and
time comes from a fake clock that the tests can move forward.
"""

from datetime import datetime, timedelta, timezone

import pytest

from community_bot.service import CommunityService
from community_bot.storage import Storage

START = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
GUILD, OTHER_GUILD = 1000, 2000
CHANNEL = 10
ALICE, BOB, MOD = 1, 2, 3


class FakeClock:
    """Call it to get "now"; advance() moves time forward."""

    def __init__(self, start: datetime = START):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def storage():
    store = Storage(":memory:")
    yield store
    store.close()


@pytest.fixture
def service(storage, clock):
    return CommunityService(storage, max_reminders_per_user=3, clock=clock)
