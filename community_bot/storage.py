"""SQLite storage for reminders, polls, warnings and server settings.

SQLite is a whole database in a single file, built into Python. No server,
no setup, and it survives restarts of the bot. For a client project with
many servers you could swap this class for PostgreSQL without touching the
command logic, because service.py only calls the methods below.

All times are stored as ISO strings in UTC ("2026-10-21T18:30:00+00:00").
Strings in the same format sort like the times they describe, so SQL can
compare them directly (due_at <= now).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    due_at      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS polls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    author_id   INTEGER NOT NULL,
    question    TEXT    NOT NULL,
    options     TEXT    NOT NULL,          -- JSON list of strings
    ends_at     TEXT,                      -- NULL = runs until closed by hand
    closed      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_votes (
    poll_id     INTEGER NOT NULL REFERENCES polls (id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    option      INTEGER NOT NULL,
    PRIMARY KEY (poll_id, user_id)         -- one vote per person
);

CREATE TABLE IF NOT EXISTS warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS self_roles (
    guild_id    INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id            INTEGER PRIMARY KEY,
    welcome_channel_id  INTEGER,
    welcome_message     TEXT
);
"""


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


@dataclass
class Reminder:
    id: int
    guild_id: int
    channel_id: int
    user_id: int
    text: str
    due_at: datetime
    created_at: datetime


@dataclass
class Poll:
    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    author_id: int
    question: str
    options: list[str]
    ends_at: datetime | None
    closed: bool
    created_at: datetime


@dataclass
class Warning:
    id: int
    guild_id: int
    user_id: int
    moderator_id: int
    reason: str
    created_at: datetime


@dataclass
class WelcomeConfig:
    channel_id: int
    message: str


def _to_reminder(row: sqlite3.Row) -> Reminder:
    data = dict(row)
    data["due_at"] = from_iso(data["due_at"])
    data["created_at"] = from_iso(data["created_at"])
    return Reminder(**data)


def _to_poll(row: sqlite3.Row) -> Poll:
    data = dict(row)
    data["options"] = json.loads(data["options"])
    data["ends_at"] = from_iso(data["ends_at"]) if data["ends_at"] else None
    data["closed"] = bool(data["closed"])
    data["created_at"] = from_iso(data["created_at"])
    return Poll(**data)


def _to_warning(row: sqlite3.Row) -> Warning:
    data = dict(row)
    data["created_at"] = from_iso(data["created_at"])
    return Warning(**data)


class Storage:
    """All database access goes through this class."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------- reminders

    def add_reminder(self, guild_id: int, channel_id: int, user_id: int, text: str,
                     due_at: datetime, now: datetime) -> Reminder:
        with self.conn:  # commits on success, rolls back on error
            cur = self.conn.execute(
                """INSERT INTO reminders (guild_id, channel_id, user_id, text, due_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, channel_id, user_id, text, iso(due_at), iso(now)),
            )
        return self.get_reminder(cur.lastrowid)

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        row = self.conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return _to_reminder(row) if row else None

    def reminders_for_user(self, guild_id: int, user_id: int) -> list[Reminder]:
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE guild_id = ? AND user_id = ? ORDER BY due_at, id",
            (guild_id, user_id),
        ).fetchall()
        return [_to_reminder(r) for r in rows]

    def count_reminders(self, guild_id: int, user_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()[0]

    def delete_reminder(self, reminder_id: int, user_id: int | None = None) -> Reminder | None:
        """Delete one reminder. With user_id, only the owner's reminder is deleted."""
        reminder = self.get_reminder(reminder_id)
        if reminder is None or (user_id is not None and reminder.user_id != user_id):
            return None
        with self.conn:
            self.conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return reminder

    def due_reminders(self, now: datetime) -> list[Reminder]:
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE due_at <= ? ORDER BY due_at, id", (iso(now),)
        ).fetchall()
        return [_to_reminder(r) for r in rows]

    # --------------------------------------------------------------- polls

    def add_poll(self, guild_id: int, channel_id: int, author_id: int, question: str,
                 options: list[str], ends_at: datetime | None, now: datetime) -> Poll:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO polls (guild_id, channel_id, author_id, question, options,
                                      ends_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, channel_id, author_id, question, json.dumps(options),
                 iso(ends_at) if ends_at else None, iso(now)),
            )
        return self.get_poll(cur.lastrowid)

    def get_poll(self, poll_id: int) -> Poll | None:
        row = self.conn.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
        return _to_poll(row) if row else None

    def set_poll_message(self, poll_id: int, message_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE polls SET message_id = ? WHERE id = ?", (message_id, poll_id))

    def close_poll(self, poll_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE polls SET closed = 1 WHERE id = ?", (poll_id,))

    def due_polls(self, now: datetime) -> list[Poll]:
        rows = self.conn.execute(
            "SELECT * FROM polls WHERE closed = 0 AND ends_at IS NOT NULL AND ends_at <= ? ORDER BY id",
            (iso(now),),
        ).fetchall()
        return [_to_poll(r) for r in rows]

    def vote(self, poll_id: int, user_id: int, option: int) -> int | None:
        """Save a vote. Returns the option the user had before (None = first vote)."""
        row = self.conn.execute(
            "SELECT option FROM poll_votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id)
        ).fetchone()
        with self.conn:
            self.conn.execute(
                """INSERT INTO poll_votes (poll_id, user_id, option) VALUES (?, ?, ?)
                   ON CONFLICT (poll_id, user_id) DO UPDATE SET option = excluded.option""",
                (poll_id, user_id, option),
            )
        return row["option"] if row else None

    def remove_vote(self, poll_id: int, user_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id)
            )

    def vote_counts(self, poll_id: int, option_count: int) -> list[int]:
        counts = [0] * option_count
        rows = self.conn.execute(
            "SELECT option, COUNT(*) AS n FROM poll_votes WHERE poll_id = ? GROUP BY option",
            (poll_id,),
        ).fetchall()
        for row in rows:
            if 0 <= row["option"] < option_count:
                counts[row["option"]] = row["n"]
        return counts

    # ------------------------------------------------------------ warnings

    def add_warning(self, guild_id: int, user_id: int, moderator_id: int, reason: str,
                    now: datetime) -> Warning:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (guild_id, user_id, moderator_id, reason, iso(now)),
            )
        row = self.conn.execute("SELECT * FROM warnings WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _to_warning(row)

    def warnings_for(self, guild_id: int, user_id: int) -> list[Warning]:
        rows = self.conn.execute(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY id",
            (guild_id, user_id),
        ).fetchall()
        return [_to_warning(r) for r in rows]

    def clear_warnings(self, guild_id: int, user_id: int) -> int:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )
        return cur.rowcount

    # ---------------------------------------------------------- self roles

    def add_self_roles(self, guild_id: int, role_ids: list[int]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO self_roles (guild_id, role_id) VALUES (?, ?)",
                [(guild_id, r) for r in role_ids],
            )

    def is_self_role(self, guild_id: int, role_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM self_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id)
        ).fetchone() is not None

    # ------------------------------------------------------ guild settings

    def set_welcome(self, guild_id: int, channel_id: int | None, message: str | None) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO guild_settings (guild_id, welcome_channel_id, welcome_message)
                   VALUES (?, ?, ?)
                   ON CONFLICT (guild_id) DO UPDATE SET
                       welcome_channel_id = excluded.welcome_channel_id,
                       welcome_message = excluded.welcome_message""",
                (guild_id, channel_id, message),
            )

    def get_welcome(self, guild_id: int) -> WelcomeConfig | None:
        row = self.conn.execute(
            "SELECT welcome_channel_id, welcome_message FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if row is None or row["welcome_channel_id"] is None:
            return None
        return WelcomeConfig(row["welcome_channel_id"], row["welcome_message"] or "")
