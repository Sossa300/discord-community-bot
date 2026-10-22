"""What the bot does, without any Discord code.

Every command is a plain method that takes ids and the user's input, and
returns the reply text. That has two big advantages:

1. Tests can call these methods directly, no Discord, no token, no internet.
2. Moving to Telegram (or Slack, or a web dashboard) means rewriting only
   bot.py.

Discord ids (server, channel, user, role) are plain integers here. Replies
use Discord's markdown and mention syntax: <@user>, <#channel>, <@&role> and
<t:unix:R> for timestamps that every user sees in their own time zone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .storage import Poll, Reminder, Storage

log = logging.getLogger(__name__)

MAX_REMINDER_TEXT = 500
MAX_QUESTION = 200
MAX_OPTION = 80          # Discord's limit for a button label
MIN_OPTIONS, MAX_OPTIONS = 2, 10
MAX_WELCOME = 1000
MAX_REASON = 300
MIN_DURATION = timedelta(minutes=1)
MAX_DURATION = timedelta(days=365)

DEFAULT_WELCOME = "👋 Welcome to **{server}**, {user}! You are member #{count}."

HELP_TEXT = """**Reminders**
`/remind` `when` `text` - e.g. when `10m`, `2h30m`, `1d`
`/reminders` - your open reminders
`/reminder-cancel` `id` - cancel one

**Polls**
`/poll` `question` `options` `[duration]` - options separated by `;`
`/poll-close` `id` - end a poll early (author or moderator)

**Roles**
`/roles-panel` `roles` - post buttons so members can pick roles *(Manage Roles)*

**Welcome**
`/welcome-set` `channel` `[message]` - greet new members *(Manage Server)*
`/welcome-off` - stop greeting · `/welcome-test` - preview the message
Placeholders: `{user}` `{name}` `{server}` `{count}`

**Moderation**
`/warn` `member` `reason` · `/warnings` `member` · `/warnings-clear` `member`
`/purge` `amount` - delete the last 1-100 messages *(Manage Messages)*"""

_DURATION_PART = re.compile(r"(\d+)\s*([a-z]*)")
_UNITS = {
    "s": "seconds", "sec": "seconds", "secs": "seconds", "second": "seconds", "seconds": "seconds",
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "": "minutes",  # a bare number means minutes
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "week": "weeks", "weeks": "weeks",
}


@dataclass
class Reply:
    """Answer to a command. ok=False means a user error, shown only to them."""
    text: str
    ok: bool = True


@dataclass
class Delivery:
    """A message the background loop wants to send to a channel."""
    channel_id: int
    text: str
    user_id: int | None = None   # who gets pinged


@dataclass
class RoleToggle:
    """Result of a role button click: add (True), remove (False) or nothing (None)."""
    add: bool | None
    reply: Reply


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_duration(text: str) -> timedelta | None:
    """Turn "10m", "2h30m", "1 day", "90" (minutes) into a timedelta.

    Returns None if the text is not a duration. The whole string must be
    understood: "2h abc" is rejected instead of silently becoming 2 hours.
    """
    text = (text or "").strip().lower().replace(",", " ")
    if not text:
        return None
    total = timedelta()
    pos = 0
    for match in _DURATION_PART.finditer(text):
        if text[pos:match.start()].strip():
            return None  # junk between two parts
        unit = _UNITS.get(match.group(2))
        if unit is None:
            return None
        total += timedelta(**{unit: int(match.group(1))})
        pos = match.end()
    if pos == 0 or text[pos:].strip():
        return None
    return total


def duration_text(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    parts = []
    for name, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount} {name}{'s' if amount != 1 else ''}")
    return " ".join(parts) or "less than a minute"


def timestamp(moment: datetime, style: str = "R") -> str:
    """Discord timestamp: R = "in 2 hours", f = full date and time."""
    return f"<t:{int(moment.timestamp())}:{style}>"


def parse_options(raw: str) -> list[str]:
    """Split "Pizza; Burger; Sushi" into options, dropping empty ones."""
    return [part.strip() for part in (raw or "").split(";") if part.strip()]


def progress_bar(share: float, width: int = 10) -> str:
    filled = round(share * width)
    return "█" * filled + "░" * (width - filled)


def render_template(template: str, user_mention: str, name: str, server: str, count: int) -> str:
    """Fill in a welcome message.

    Plain replace() instead of str.format(): a server admin's template like
    "{user.__class__}" must not be able to reach Python internals.
    """
    return (template.replace("{user}", user_mention)
                    .replace("{name}", name)
                    .replace("{server}", server)
                    .replace("{count}", str(count)))


class CommunityService:
    def __init__(self, storage: Storage, max_reminders_per_user: int = 10,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.storage = storage
        self.max_reminders = max_reminders_per_user
        self.clock = clock  # tests pass a fake clock to "travel in time"

    def help(self) -> str:
        return HELP_TEXT

    # ----------------------------------------------------------- reminders

    def remind(self, guild_id: int, channel_id: int, user_id: int, when: str, text: str) -> Reply:
        delay = parse_duration(when)
        if delay is None:
            return Reply(f"I don't understand `{when}`. Try `10m`, `2h30m` or `1d`.", ok=False)
        if delay < MIN_DURATION:
            return Reply("Please choose at least 1 minute.", ok=False)
        if delay > MAX_DURATION:
            return Reply("That is too far away. The maximum is 365 days.", ok=False)
        text = (text or "").strip()
        if not text:
            return Reply("What should I remind you of? Please add a text.", ok=False)
        if len(text) > MAX_REMINDER_TEXT:
            return Reply(f"The text is too long ({len(text)} characters, max. {MAX_REMINDER_TEXT}).", ok=False)
        if self.storage.count_reminders(guild_id, user_id) >= self.max_reminders:
            return Reply(f"You already have {self.max_reminders} open reminders. "
                         "Cancel one with `/reminder-cancel` first.", ok=False)

        now = self.clock()
        reminder = self.storage.add_reminder(guild_id, channel_id, user_id, text, now + delay, now)
        # The fixed date comes first: the relative part ("in 2 hours") is
        # computed when the message is viewed and reads oddly once it has passed.
        return Reply(f"⏰ Got it! Reminder `#{reminder.id}` is set for "
                     f"{timestamp(reminder.due_at, 'f')} ({timestamp(reminder.due_at)}).\n"
                     f"> {text}\nCancel it with `/reminder-cancel {reminder.id}`")

    def list_reminders(self, guild_id: int, user_id: int) -> Reply:
        reminders = self.storage.reminders_for_user(guild_id, user_id)
        if not reminders:
            return Reply("You have no open reminders. Create one with `/remind`.")
        lines = [f"**Your reminders ({len(reminders)})**"]
        for r in reminders:
            lines.append(f"`#{r.id}` {timestamp(r.due_at)} - {_short(r.text, 80)}")
        return Reply("\n".join(lines))

    def cancel_reminder(self, guild_id: int, user_id: int, reminder_id: int) -> Reply:
        reminder = self.storage.get_reminder(reminder_id)
        # Same message for "does not exist" and "belongs to someone else":
        # nobody can find out what other people are being reminded of.
        if reminder is None or reminder.guild_id != guild_id or reminder.user_id != user_id:
            return Reply(f"You have no reminder `#{reminder_id}`. See `/reminders`.", ok=False)
        self.storage.delete_reminder(reminder_id)
        return Reply(f"🗑 Cancelled reminder `#{reminder_id}`: {_short(reminder.text, 80)}")

    def pop_due_reminders(self) -> list[Delivery]:
        """Take every reminder that is due out of the database (background loop)."""
        deliveries = []
        for r in self.storage.due_reminders(self.clock()):
            self.storage.delete_reminder(r.id)
            deliveries.append(self._reminder_delivery(r))
        if deliveries:
            log.info("%d reminder(s) due", len(deliveries))
        return deliveries

    @staticmethod
    def _reminder_delivery(r: Reminder) -> Delivery:
        return Delivery(r.channel_id, f"⏰ <@{r.user_id}>, here is your reminder "
                        f"(set {timestamp(r.created_at, 'f')}):\n> {r.text}", user_id=r.user_id)

    # --------------------------------------------------------------- polls

    def create_poll(self, guild_id: int, channel_id: int, author_id: int, question: str,
                    options_raw: str, duration: str | None = None) -> tuple[Reply, Poll | None]:
        question = (question or "").strip()
        if not question:
            return Reply("Please write a question.", ok=False), None
        if len(question) > MAX_QUESTION:
            return Reply(f"The question is too long (max. {MAX_QUESTION} characters).", ok=False), None

        options = parse_options(options_raw)
        if len(options) < MIN_OPTIONS:
            return Reply("A poll needs at least 2 options, separated by `;`\n"
                         "Example: `Pizza; Burger; Sushi`", ok=False), None
        if len(options) > MAX_OPTIONS:
            return Reply(f"Too many options ({len(options)}). The maximum is {MAX_OPTIONS}.", ok=False), None
        too_long = [o for o in options if len(o) > MAX_OPTION]
        if too_long:
            return Reply(f"Option “{_short(too_long[0], 30)}” is too long "
                         f"(max. {MAX_OPTION} characters).", ok=False), None
        if len({o.lower() for o in options}) != len(options):
            return Reply("Two options are the same. Please make every option unique.", ok=False), None

        ends_at = None
        if duration:
            delay = parse_duration(duration)
            if delay is None:
                return Reply(f"I don't understand the duration `{duration}`. Try `30m`, `1h` or `2d`.", ok=False), None
            if not MIN_DURATION <= delay <= MAX_DURATION:
                return Reply("A poll can run between 1 minute and 365 days.", ok=False), None
            ends_at = self.clock() + delay

        poll = self.storage.add_poll(guild_id, channel_id, author_id, question, options,
                                     ends_at, self.clock())
        return Reply(self.render_poll(poll)), poll

    def attach_poll_message(self, poll_id: int, message_id: int) -> None:
        """Remember which Discord message shows the poll, so it can be edited later."""
        self.storage.set_poll_message(poll_id, message_id)

    def get_poll(self, poll_id: int) -> Poll | None:
        return self.storage.get_poll(poll_id)

    def vote(self, poll_id: int, user_id: int, option: int) -> Reply:
        """Button click. Same option again = take the vote back."""
        poll = self.storage.get_poll(poll_id)
        if poll is None or not 0 <= option < len(poll.options):
            return Reply("This poll does not exist anymore.", ok=False)
        if poll.closed:
            return Reply("This poll is closed.", ok=False)

        before = self.storage.vote(poll_id, user_id, option)
        label = poll.options[option]
        if before == option:
            self.storage.remove_vote(poll_id, user_id)
            return Reply(f"Your vote for **{label}** was removed.")
        if before is None:
            return Reply(f"✅ You voted for **{label}**.")
        return Reply(f"🔄 Changed your vote from **{poll.options[before]}** to **{label}**.")

    def close_poll(self, poll_id: int, user_id: int, is_moderator: bool) -> tuple[Reply, Poll | None]:
        poll = self.storage.get_poll(poll_id)
        if poll is None:
            return Reply(f"There is no poll `#{poll_id}`.", ok=False), None
        if poll.author_id != user_id and not is_moderator:
            return Reply("Only the person who started the poll or a moderator can close it.", ok=False), None
        if poll.closed:
            return Reply(f"Poll `#{poll_id}` is already closed.", ok=False), None
        self.storage.close_poll(poll_id)
        poll.closed = True
        return Reply(f"🔒 Poll `#{poll_id}` is closed."), poll

    def pop_due_polls(self) -> list[Poll]:
        """Close every poll whose time is up and return them (background loop)."""
        polls = self.storage.due_polls(self.clock())
        for poll in polls:
            self.storage.close_poll(poll.id)
            poll.closed = True
        return polls

    def render_poll(self, poll: Poll) -> str:
        """The poll message: one line per option with a bar, and the result once closed."""
        counts = self.storage.vote_counts(poll.id, len(poll.options))
        total = sum(counts)
        lines = []
        for label, n in zip(poll.options, counts):
            share = n / total if total else 0
            lines.append(f"**{label}**\n`{progress_bar(share)}` {share:.0%} ({n})")

        votes = f"{total} vote{'s' if total != 1 else ''}"
        if poll.closed:
            lines.append(f"\n🔒 **Poll closed** · {votes}")
            if total:
                best = max(counts)
                winners = [label for label, n in zip(poll.options, counts) if n == best]
                title = "Winner" if len(winners) == 1 else "Tie"
                lines.append(f"🏆 {title}: **{' / '.join(winners)}**")
        else:
            end = f" · ends {timestamp(poll.ends_at)}" if poll.ends_at else ""
            lines.append(f"\n{votes}{end} · started by <@{poll.author_id}> · poll `#{poll.id}`")
        return "\n".join(lines)

    # ---------------------------------------------------------- self roles

    @staticmethod
    def check_self_role(name: str, position: int, bot_top_position: int,
                        managed: bool, is_default: bool, dangerous: bool) -> str | None:
        """Can this role be handed out by button? Returns an error text or None.

        Discord only lets a bot give roles that are *below* its own highest
        role. Roles with admin/moderation rights are refused on purpose:
        a button must never make someone a moderator.
        """
        if is_default:
            return "@everyone can't be used as a self role."
        if managed:
            return f"**{name}** belongs to an integration or bot and can't be assigned."
        if dangerous:
            return f"**{name}** has moderator or admin permissions, so it can't be a self role."
        if position >= bot_top_position:
            return (f"**{name}** is above my own role. Drag my role above it in "
                    "Server Settings → Roles, then try again.")
        return None

    def register_role_panel(self, guild_id: int, role_ids: list[int]) -> None:
        self.storage.add_self_roles(guild_id, role_ids)

    def toggle_role(self, guild_id: int, role_id: int, role_name: str, has_role: bool) -> RoleToggle:
        """Button click on a role panel: give the role or take it away."""
        if not self.storage.is_self_role(guild_id, role_id):
            return RoleToggle(None, Reply("This role is not available anymore.", ok=False))
        if has_role:
            return RoleToggle(False, Reply(f"➖ Removed the role **{role_name}**."))
        return RoleToggle(True, Reply(f"➕ You now have the role **{role_name}**."))

    @staticmethod
    def role_panel_text(role_names: list[str]) -> str:
        roles = "\n".join(f"• **{name}**" for name in role_names)
        return f"Click a button to get a role. Click again to remove it.\n\n{roles}"

    # ------------------------------------------------------------- welcome

    def set_welcome(self, guild_id: int, channel_id: int, message: str | None) -> Reply:
        message = (message or "").strip() or DEFAULT_WELCOME
        if len(message) > MAX_WELCOME:
            return Reply(f"The message is too long (max. {MAX_WELCOME} characters).", ok=False)
        self.storage.set_welcome(guild_id, channel_id, message)
        return Reply(f"✅ New members will be greeted in <#{channel_id}>.\n"
                     f"Preview with `/welcome-test`.\n> {message}")

    def disable_welcome(self, guild_id: int) -> Reply:
        if self.storage.get_welcome(guild_id) is None:
            return Reply("Welcome messages are already off.", ok=False)
        self.storage.set_welcome(guild_id, None, None)
        return Reply("🔕 Welcome messages are off.")

    def welcome_for(self, guild_id: int, user_id: int, name: str, server: str,
                    member_count: int) -> Delivery | None:
        """The greeting for a new member, or None if welcome messages are off."""
        config = self.storage.get_welcome(guild_id)
        if config is None:
            return None
        text = render_template(config.message, f"<@{user_id}>", name, server, member_count)
        return Delivery(config.channel_id, text, user_id=user_id)

    # ---------------------------------------------------------- moderation

    def warn(self, guild_id: int, user_id: int, moderator_id: int, reason: str,
             target_is_bot: bool = False) -> Reply:
        reason = (reason or "").strip()
        if user_id == moderator_id:
            return Reply("You can't warn yourself.", ok=False)
        if target_is_bot:
            return Reply("Bots can't be warned.", ok=False)
        if not reason:
            return Reply("Please give a reason.", ok=False)
        if len(reason) > MAX_REASON:
            return Reply(f"The reason is too long (max. {MAX_REASON} characters).", ok=False)
        self.storage.add_warning(guild_id, user_id, moderator_id, reason, self.clock())
        count = len(self.storage.warnings_for(guild_id, user_id))
        return Reply(f"⚠️ <@{user_id}> has been warned by <@{moderator_id}>.\n"
                     f"> {reason}\nThis is warning number **{count}**.")

    def list_warnings(self, guild_id: int, user_id: int) -> Reply:
        warnings = self.storage.warnings_for(guild_id, user_id)
        if not warnings:
            return Reply(f"<@{user_id}> has no warnings. ✨")
        lines = [f"**Warnings for <@{user_id}> ({len(warnings)})**"]
        for w in warnings:
            lines.append(f"`#{w.id}` {timestamp(w.created_at, 'd')} by <@{w.moderator_id}>: "
                         f"{_short(w.reason, 100)}")
        return Reply("\n".join(lines))

    def clear_warnings(self, guild_id: int, user_id: int) -> Reply:
        removed = self.storage.clear_warnings(guild_id, user_id)
        if not removed:
            return Reply(f"<@{user_id}> has no warnings.", ok=False)
        return Reply(f"🧹 Removed {removed} warning{'s' if removed != 1 else ''} from <@{user_id}>.")

    @staticmethod
    def purge_text(deleted: int) -> str:
        return f"🧹 Deleted {deleted} message{'s' if deleted != 1 else ''}."


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())  # one line, for lists
    return text if len(text) <= limit else text[: limit - 1] + "…"
