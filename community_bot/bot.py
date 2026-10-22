"""The Discord layer: connects slash commands and buttons to CommunityService.

This file only translates: "user ran /remind 10m Tea" -> service.remind(...)
-> send the reply back. All decisions live in service.py.

Buttons survive a restart of the bot: their custom_id carries everything
needed ("poll:7:2" = poll 7, option 2; "role:123" = role 123), and
DynamicItem turns a click on any old message back into the right button.

SQLite calls are short and run directly on the event loop. Everything runs
in one thread, so there is no locking to worry about.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Settings
from .service import CommunityService, Delivery, Reply
from .storage import Poll, Storage

log = logging.getLogger(__name__)

# Never let user text ping @everyone or whole roles, only the people meant.
ONLY_USERS = discord.AllowedMentions(everyone=False, roles=False, users=True)
NO_PINGS = discord.AllowedMentions.none()

COLOR_OK = discord.Color.blurple()
COLOR_ERROR = discord.Color.red()
COLOR_CLOSED = discord.Color.dark_grey()

# A self role with one of these would turn a button into a moderator badge.
DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels",
             "manage_messages", "kick_members", "ban_members", "moderate_members",
             "mention_everyone", "manage_webhooks")


def embed(text: str, color: discord.Color = COLOR_OK, title: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=text, color=color)


async def answer(interaction: discord.Interaction, reply: Reply, *, ephemeral: bool | None = None,
                 title: str | None = None) -> None:
    """Send a service Reply. Errors are only visible to the user who caused them."""
    private = (not reply.ok) if ephemeral is None else ephemeral
    message = embed(reply.text, COLOR_OK if reply.ok else COLOR_ERROR, title)
    if interaction.response.is_done():
        await interaction.followup.send(embed=message, ephemeral=private, allowed_mentions=NO_PINGS)
    else:
        await interaction.response.send_message(embed=message, ephemeral=private,
                                                allowed_mentions=NO_PINGS)


# ------------------------------------------------------------------ buttons

class PollButton(discord.ui.DynamicItem[discord.ui.Button], template=r"poll:(?P<poll>\d+):(?P<option>\d+)"):
    def __init__(self, poll_id: int, option: int, label: str = "Vote") -> None:
        super().__init__(discord.ui.Button(
            label=label, style=discord.ButtonStyle.primary, custom_id=f"poll:{poll_id}:{option}",
        ))
        self.poll_id = poll_id
        self.option = option

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["poll"]), int(match["option"]), item.label or "Vote")

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.vote(interaction, self.poll_id, self.option)


class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"role:(?P<role>\d+)"):
    def __init__(self, role_id: int, label: str = "Role") -> None:
        super().__init__(discord.ui.Button(
            label=label, style=discord.ButtonStyle.secondary, custom_id=f"role:{role_id}",
        ))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["role"]), item.label or "Role")

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.toggle_role(interaction, self.role_id)


def poll_view(poll: Poll) -> discord.ui.View | None:
    if poll.closed:
        return None  # a closed poll shows the result without buttons
    view = discord.ui.View(timeout=None)
    for index, label in enumerate(poll.options):
        view.add_item(PollButton(poll.id, index, label))
    return view


# ---------------------------------------------------------------------- bot

class CommunityBot(discord.Client):
    def __init__(self, service: CommunityService, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.members = True  # needed for welcome messages (privileged intent)
        super().__init__(intents=intents, allowed_mentions=ONLY_USERS)
        self.service = service
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self.on_command_error
        register_commands(self)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(PollButton, RoleButton)
        if self.settings.guild_id:
            # Copying the commands to one server makes them appear instantly.
            # Great for testing; leave GUILD_ID empty to register them globally.
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Registered %d slash commands", len(synced))
        self.background.change_interval(seconds=self.settings.check_interval_seconds)
        self.background.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s, on %d server(s)", self.user, len(self.guilds))

    async def close(self) -> None:
        self.background.cancel()
        await super().close()
        self.service.storage.close()

    async def on_command_error(self, interaction: discord.Interaction,
                               error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            text = "You need the permission " + ", ".join(
                p.replace("_", " ").title() for p in error.missing_permissions) + "."
        elif isinstance(error, app_commands.BotMissingPermissions):
            text = "I am missing the permission " + ", ".join(
                p.replace("_", " ").title() for p in error.missing_permissions) + "."
        elif isinstance(error, app_commands.NoPrivateMessage):
            text = "This command only works on a server."
        else:
            log.error("Error in /%s", interaction.command and interaction.command.name, exc_info=error)
            text = "😕 Something went wrong. Please try again later."
        await answer(interaction, Reply(text, ok=False))

    # ----------------------------------------------------------- buttons

    async def vote(self, interaction: discord.Interaction, poll_id: int, option: int) -> None:
        reply = self.service.vote(poll_id, interaction.user.id, option)
        poll = self.service.get_poll(poll_id)
        if reply.ok and poll is not None:
            await interaction.response.edit_message(embed=self.poll_embed(poll), view=poll_view(poll))
        await answer(interaction, reply, ephemeral=True)

    async def toggle_role(self, interaction: discord.Interaction, role_id: int) -> None:
        role = interaction.guild.get_role(role_id) if interaction.guild else None
        if role is None:
            await answer(interaction, Reply("This role does not exist anymore.", ok=False))
            return
        member = interaction.user
        result = self.service.toggle_role(interaction.guild.id, role_id, role.name, role in member.roles)
        try:
            if result.add is True:
                await member.add_roles(role, reason="Self role button")
            elif result.add is False:
                await member.remove_roles(role, reason="Self role button")
        except discord.Forbidden:
            await answer(interaction, Reply(
                f"I am not allowed to change **{role.name}**. My role must be above it "
                "and I need the Manage Roles permission.", ok=False))
            return
        await answer(interaction, result.reply, ephemeral=True)

    # ---------------------------------------------------------- helpers

    def poll_embed(self, poll: Poll) -> discord.Embed:
        return embed(self.service.render_poll(poll), COLOR_CLOSED if poll.closed else COLOR_OK,
                     title=f"📊 {poll.question}")

    async def channel(self, channel_id: int):
        channel = self.get_channel(channel_id)
        return channel if channel is not None else await self.fetch_channel(channel_id)

    async def deliver(self, delivery: Delivery) -> None:
        """Send a reminder to its channel; if that fails, as a direct message."""
        try:
            channel = await self.channel(delivery.channel_id)
            await channel.send(delivery.text)
            return
        except (discord.Forbidden, discord.NotFound) as exc:
            log.warning("Channel %s not usable (%s), trying a DM", delivery.channel_id, exc)
        if delivery.user_id:
            try:
                user = await self.fetch_user(delivery.user_id)
                await user.send(delivery.text)
            except discord.HTTPException as exc:
                log.warning("Could not DM user %s: %s", delivery.user_id, exc)

    async def show_closed_poll(self, poll: Poll) -> None:
        if poll.message_id is None:
            return
        try:
            channel = await self.channel(poll.channel_id)
            message = await channel.fetch_message(poll.message_id)
            await message.edit(embed=self.poll_embed(poll), view=None)
            await message.reply(f"🔒 The poll **{poll.question}** has ended.",
                                allowed_mentions=NO_PINGS)
        except discord.HTTPException as exc:
            log.warning("Could not update poll %s: %s", poll.id, exc)

    # --------------------------------------------------- background loop

    @tasks.loop(seconds=30)
    async def background(self) -> None:
        """Every few seconds: send due reminders and close finished polls."""
        for delivery in self.service.pop_due_reminders():
            await self.deliver(delivery)  # one broken channel must not stop the others
        for poll in self.service.pop_due_polls():
            await self.show_closed_poll(poll)

    @background.before_loop
    async def _wait_until_ready(self) -> None:
        await self.wait_until_ready()

    @background.error
    async def _background_error(self, error: BaseException) -> None:
        log.error("Background loop crashed, restarting it", exc_info=error)
        self.background.restart()

    # --------------------------------------------------------- welcome

    async def on_member_join(self, member: discord.Member) -> None:
        delivery = self.service.welcome_for(member.guild.id, member.id, member.display_name,
                                            member.guild.name, member.guild.member_count or 0)
        if delivery is None:
            return
        await self.send_welcome(delivery, member)

    async def send_welcome(self, delivery: Delivery, member: discord.Member) -> None:
        message = embed(delivery.text)
        message.set_thumbnail(url=member.display_avatar.url)
        try:
            channel = await self.channel(delivery.channel_id)
            await channel.send(content=member.mention, embed=message)
        except discord.HTTPException as exc:
            log.warning("Could not send welcome message: %s", exc)


# ------------------------------------------------------------ slash commands

def register_commands(bot: CommunityBot) -> None:
    tree, service = bot.tree, bot.service

    @tree.command(name="help", description="Show everything this bot can do")
    async def help_(interaction: discord.Interaction) -> None:
        await answer(interaction, Reply(service.help()), ephemeral=True, title="🤖 Community Bot")

    # -------------------------------------------------------- reminders

    @tree.command(name="remind", description="Remind me of something later")
    @app_commands.describe(when="When? e.g. 10m, 2h30m, 1d", text="What should I remind you of?")
    @app_commands.guild_only()
    async def remind(interaction: discord.Interaction, when: str, text: str) -> None:
        reply = service.remind(interaction.guild.id, interaction.channel.id, interaction.user.id, when, text)
        await answer(interaction, reply)

    @tree.command(name="reminders", description="Show your open reminders")
    @app_commands.guild_only()
    async def reminders(interaction: discord.Interaction) -> None:
        await answer(interaction, service.list_reminders(interaction.guild.id, interaction.user.id),
                     ephemeral=True)

    @tree.command(name="reminder-cancel", description="Cancel one of your reminders")
    @app_commands.describe(id="The number from /reminders")
    @app_commands.guild_only()
    async def reminder_cancel(interaction: discord.Interaction, id: int) -> None:
        await answer(interaction, service.cancel_reminder(interaction.guild.id, interaction.user.id, id),
                     ephemeral=True)

    # ------------------------------------------------------------ polls

    @tree.command(name="poll", description="Start a poll with buttons")
    @app_commands.describe(question="What do you want to ask?",
                           options="2-10 options separated by ; e.g. Pizza; Burger; Sushi",
                           duration="Optional: close automatically after e.g. 30m, 1h, 2d")
    @app_commands.guild_only()
    async def poll(interaction: discord.Interaction, question: str, options: str,
                   duration: str | None = None) -> None:
        reply, created = service.create_poll(interaction.guild.id, interaction.channel.id,
                                             interaction.user.id, question, options, duration)
        if created is None:
            await answer(interaction, reply)
            return
        await interaction.response.send_message(embed=bot.poll_embed(created), view=poll_view(created))
        message = await interaction.original_response()
        service.attach_poll_message(created.id, message.id)

    @tree.command(name="poll-close", description="End a poll now and show the result")
    @app_commands.describe(id="The poll number shown under the poll")
    @app_commands.guild_only()
    async def poll_close(interaction: discord.Interaction, id: int) -> None:
        is_mod = interaction.user.guild_permissions.manage_messages
        reply, closed = service.close_poll(id, interaction.user.id, is_mod)
        await answer(interaction, reply, ephemeral=True)
        if closed is not None:
            await bot.show_closed_poll(closed)

    # ------------------------------------------------------------ roles

    @tree.command(name="roles-panel", description="Post buttons so members can pick roles")
    @app_commands.describe(role1="Role for the first button", title="Heading of the panel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def roles_panel(interaction: discord.Interaction, role1: discord.Role,
                          role2: discord.Role | None = None, role3: discord.Role | None = None,
                          role4: discord.Role | None = None, role5: discord.Role | None = None,
                          title: str = "Pick your roles") -> None:
        roles = list(dict.fromkeys(r for r in (role1, role2, role3, role4, role5) if r))
        top = interaction.guild.me.top_role.position
        for role in roles:
            dangerous = any(getattr(role.permissions, p) for p in DANGEROUS)
            error = service.check_self_role(role.name, role.position, top, role.managed,
                                            role.is_default(), dangerous)
            if error:
                await answer(interaction, Reply(error, ok=False))
                return
        service.register_role_panel(interaction.guild.id, [r.id for r in roles])
        view = discord.ui.View(timeout=None)
        for role in roles:
            view.add_item(RoleButton(role.id, role.name))
        await interaction.response.send_message(
            embed=embed(service.role_panel_text([r.name for r in roles]), title=f"🎭 {title[:200]}"),
            view=view,
        )

    # ---------------------------------------------------------- welcome

    @tree.command(name="welcome-set", description="Greet new members in a channel")
    @app_commands.describe(channel="Where to post the greeting",
                           message="Optional text. Placeholders: {user} {name} {server} {count}")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_set(interaction: discord.Interaction, channel: discord.TextChannel,
                          message: str | None = None) -> None:
        await answer(interaction, service.set_welcome(interaction.guild.id, channel.id, message),
                     ephemeral=True)

    @tree.command(name="welcome-off", description="Stop greeting new members")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_off(interaction: discord.Interaction) -> None:
        await answer(interaction, service.disable_welcome(interaction.guild.id), ephemeral=True)

    @tree.command(name="welcome-test", description="Post the welcome message as if you just joined")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def welcome_test(interaction: discord.Interaction) -> None:
        member = interaction.user
        delivery = service.welcome_for(interaction.guild.id, member.id, member.display_name,
                                       interaction.guild.name, interaction.guild.member_count or 0)
        if delivery is None:
            await answer(interaction, Reply("Welcome messages are off. Turn them on with `/welcome-set`.",
                                            ok=False))
            return
        await answer(interaction, Reply(f"Sent a test greeting to <#{delivery.channel_id}>."), ephemeral=True)
        await bot.send_welcome(delivery, member)

    # ------------------------------------------------------- moderation

    @tree.command(name="warn", description="Warn a member (saved in the bot's log)")
    @app_commands.describe(member="Who gets the warning?", reason="Why?")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(interaction: discord.Interaction, member: discord.Member, reason: str) -> None:
        reply = service.warn(interaction.guild.id, member.id, interaction.user.id, reason, member.bot)
        await answer(interaction, reply)
        if reply.ok:
            try:  # tell the member privately; they may have DMs turned off
                await member.send(f"⚠️ You received a warning on **{interaction.guild.name}**:\n> {reason}")
            except discord.HTTPException:
                log.info("Could not DM warning to %s", member)

    @tree.command(name="warnings", description="Show the warnings of a member")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(interaction: discord.Interaction, member: discord.Member) -> None:
        await answer(interaction, service.list_warnings(interaction.guild.id, member.id), ephemeral=True)

    @tree.command(name="warnings-clear", description="Delete all warnings of a member")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings_clear(interaction: discord.Interaction, member: discord.Member) -> None:
        await answer(interaction, service.clear_warnings(interaction.guild.id, member.id), ephemeral=True)

    @tree.command(name="purge", description="Delete the last messages in this channel")
    @app_commands.describe(amount="How many (1-100)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = await interaction.channel.purge(limit=amount, reason=f"/purge by {interaction.user}")
        await answer(interaction, Reply(service.purge_text(len(deleted))), ephemeral=True)


def build_bot(settings: Settings, service: CommunityService | None = None) -> CommunityBot:
    if service is None:
        service = CommunityService(Storage(settings.db_path),
                                   max_reminders_per_user=settings.max_reminders_per_user)
    return CommunityBot(service, settings)
