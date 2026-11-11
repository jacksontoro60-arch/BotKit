"""Support tickets: a panel button and /ticket open a private thread or channel.

Staff close it with the button in the ticket or /ticket_close. The open and close
buttons are persistent (timeout=None, fixed custom_ids, registered in cog_load),
so panels posted before a restart keep working.

mode "thread" (default) creates a private thread in tickets.channel_id. The bot
needs Create Private Threads, Send Messages in Threads, and Manage Threads.
Staff only see private threads if they have Manage Threads on that channel.

mode "channel" creates a text channel under tickets.category_id that @everyone
cannot see. The opener, the staff role, and the bot can. The bot needs Manage
Channels.

Closing a thread locks and archives it. Closing a channel removes the opener,
renames it closed-*, and leaves it for staff. Neither mode needs Message Content.
"""
import inspect
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

OPEN_ID = "botkit:ticket:open"
CLOSE_ID = "botkit:ticket:close"
OPEN_TOPIC = "botkit-ticket:"
CLOSED_TOPIC = "botkit-ticket-closed:"
TICKET_MODES = ("thread", "channel")
DEFAULT_TITLE = "Support"
DEFAULT_TEXT = "Click **Open a ticket** or use `/ticket` to talk privately with staff."
_TICKET_NAME = re.compile(r"ticket-[a-z0-9-]*-\d+\Z")


def normalize_mode(value) -> str:
    mode = str(value or "thread").strip().lower()
    if mode not in TICKET_MODES:
        raise ValueError("tickets.mode must be 'thread' or 'channel'")
    return mode


def ticket_name(username, user_id) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (username or "").lower()).strip("-")[:20].strip("-")
    slug = slug or "user"
    return f"ticket-{slug}-{user_id}"[:100]


def closed_name(name) -> str:
    base = _text(name) or "ticket"
    if base.startswith("closed-"):
        return base[:100]
    return f"closed-{base}"[:100]


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def topic_user_id(topic) -> Optional[str]:
    topic = _text(topic)
    for prefix in (CLOSED_TOPIC, OPEN_TOPIC):  # closed first; the open prefix is a prefix of it
        if topic.startswith(prefix):
            rest = topic[len(prefix):].strip().split()
            token = rest[0] if rest else ""
            return token if token.isdigit() else None
    return None


def looks_like_ticket(channel) -> bool:
    topic = _text(getattr(channel, "topic", None))
    if topic.startswith(OPEN_TOPIC) or topic.startswith(CLOSED_TOPIC):
        return True
    name = _text(getattr(channel, "name", None))
    if name.startswith("closed-"):
        name = name[len("closed-"):]
    return bool(_TICKET_NAME.fullmatch(name))


def is_open_state(channel) -> bool:
    if getattr(channel, "archived", False) is True or getattr(channel, "locked", False) is True:
        return False
    topic = _text(getattr(channel, "topic", None))
    if topic.startswith(CLOSED_TOPIC):
        return False
    name = _text(getattr(channel, "name", None))
    if name.startswith("closed-"):
        return False
    return True


def is_ticket_for(channel, user_id) -> bool:
    uid = str(user_id)
    if topic_user_id(getattr(channel, "topic", None)) == uid:
        return True
    name = _text(getattr(channel, "name", None))
    return name.endswith(f"-{uid}") and looks_like_ticket(channel)


def is_open_ticket(channel, user_id) -> bool:
    return is_ticket_for(channel, user_id) and is_open_state(channel)


def is_thread(channel) -> bool:
    return getattr(channel, "type", None) in (
        discord.ChannelType.private_thread,
        discord.ChannelType.public_thread,
        discord.ChannelType.news_thread,
    )


def is_staff(member, staff_role_id) -> bool:
    perms = getattr(member, "guild_permissions", None)
    if getattr(perms, "administrator", False) is True:
        return True
    if getattr(perms, "manage_channels", False) is True:
        return True
    if getattr(perms, "manage_threads", False) is True:
        return True
    try:
        sid = int(staff_role_id)
    except (TypeError, ValueError):
        return False
    for role in getattr(member, "roles", None) or []:
        if getattr(role, "id", None) == sid:
            return True
    return False


def channel_ref(channel) -> str:
    mention = getattr(channel, "mention", None)
    if isinstance(mention, str):
        return mention
    return _text(getattr(channel, "name", None)) or "your open ticket"


def clip(text, limit=900) -> str:
    text = str(text).replace("```", "'''")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _as_list(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


class TicketOpenView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Open a ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id=OPEN_ID)
    async def open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.open_ticket(interaction, reason=None)


class TicketCloseView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id=CLOSE_ID)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.close_from_interaction(interaction)


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config.get("tickets", {})
        self.staff_role_id = int(cfg["staff_role_id"])
        self.mode = normalize_mode(cfg.get("mode", "thread"))
        self.title = cfg.get("title") or DEFAULT_TITLE
        self.text = cfg.get("text") or DEFAULT_TEXT
        self.channel_id = None
        self.category_id = None
        if self.mode == "thread":
            if not cfg.get("channel_id"):
                raise ValueError("tickets.channel_id is required when mode is 'thread'")
            self.channel_id = int(cfg["channel_id"])
        else:
            if not cfg.get("category_id"):
                raise ValueError("tickets.category_id is required when mode is 'channel'")
            self.category_id = int(cfg["category_id"])
            if cfg.get("channel_id"):
                self.channel_id = int(cfg["channel_id"])
        self._opening = set()

    async def cog_load(self):
        self.bot.add_view(TicketOpenView(self))
        self.bot.add_view(TicketCloseView(self))

    async def _resolve(self, guild, channel_id):
        if channel_id is None or guild is None:
            return None
        getter = getattr(guild, "get_channel", None)
        found = getter(channel_id) if callable(getter) else None
        if found is not None:
            return found
        fetch = getattr(guild, "fetch_channel", None)
        if not inspect.iscoroutinefunction(fetch):
            return None
        try:
            return await fetch(channel_id)
        except discord.HTTPException:
            return None

    async def _active_threads(self, guild):
        fn = getattr(guild, "active_threads", None)
        if not inspect.iscoroutinefunction(fn):
            return []
        try:
            found = await fn()
        except discord.HTTPException:
            return []
        return list(found or [])

    async def find_open_ticket(self, guild, user_id):
        seen = set()
        candidates = []

        def add(channel):
            if channel is None:
                return
            ident = getattr(channel, "id", None)
            if ident in seen:
                return
            if ident is not None:
                seen.add(ident)
            candidates.append(channel)

        parent = await self._resolve(guild, self.channel_id)
        if parent is not None:
            for thread in _as_list(getattr(parent, "threads", None)):
                add(thread)
        category = await self._resolve(guild, self.category_id)
        if category is not None:
            for channel in _as_list(getattr(category, "channels", None)):
                add(channel)
        for thread in await self._active_threads(guild):
            add(thread)
        for channel in _as_list(getattr(guild, "text_channels", None)):
            add(channel)
        for channel in candidates:
            if is_open_ticket(channel, user_id):
                return channel
        return None

    async def open_ticket(self, interaction: discord.Interaction, reason: Optional[str] = None):
        if interaction.guild is None:
            return await interaction.response.send_message("Use this in the server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        if user.id in self._opening:
            return await interaction.followup.send("You're already opening a ticket.", ephemeral=True)
        self._opening.add(user.id)
        try:
            existing = await self.find_open_ticket(interaction.guild, user.id)
            if existing is not None:
                return await interaction.followup.send(
                    f"You already have a ticket: {channel_ref(existing)}", ephemeral=True)
            note = clip(reason.strip(), 500) if isinstance(reason, str) and reason.strip() else None
            try:
                if self.mode == "thread":
                    created = await self._open_thread(interaction.guild, user)
                else:
                    created = await self._open_channel(interaction.guild, user)
            except discord.Forbidden:
                return await interaction.followup.send(
                    "I don't have permission to create that ticket. "
                    "For threads I need Create Private Threads and Manage Threads; "
                    "for channels I need Manage Channels.",
                    ephemeral=True)
            except discord.HTTPException:
                log.warning("could not open a ticket for %s", user.id)
                return await interaction.followup.send(
                    "Couldn't open a ticket. Please tell a moderator.", ephemeral=True)
            if created is None:
                return await interaction.followup.send(
                    "Ticket channel isn't set up. Please tell a moderator.", ephemeral=True)
            await self._intro(created, user, note)
            await interaction.followup.send(f"Ticket opened: {channel_ref(created)}", ephemeral=True)
        finally:
            self._opening.discard(user.id)

    async def _open_thread(self, guild, user):
        parent = await self._resolve(guild, self.channel_id)
        if parent is None or not hasattr(parent, "create_thread"):
            return None
        thread = await parent.create_thread(
            name=ticket_name(getattr(user, "name", None), user.id),
            type=discord.ChannelType.private_thread,
            invitable=False,
            reason=f"Ticket opened by {user} ({user.id})",
        )
        try:
            await thread.add_user(user)
        except discord.HTTPException:
            log.warning("could not add %s to ticket thread %s", user.id, getattr(thread, "id", "?"))
        return thread

    async def _open_channel(self, guild, user):
        category = await self._resolve(guild, self.category_id)
        if category is None or not hasattr(guild, "create_text_channel"):
            return None
        staff = guild.get_role(self.staff_role_id) if hasattr(guild, "get_role") else None
        if staff is None:
            log.warning("tickets staff role %s is missing; the new channel won't include it", self.staff_role_id)
        overwrites = {}
        default_role = getattr(guild, "default_role", None)
        if default_role is not None:
            overwrites[default_role] = discord.PermissionOverwrite(view_channel=False, send_messages=False)
        overwrites[user] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True)
        if staff is not None:
            overwrites[staff] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_messages=True, attach_files=True, embed_links=True)
        me = getattr(guild, "me", None)
        if me is not None:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                manage_channels=True, manage_messages=True, attach_files=True, embed_links=True)
        return await guild.create_text_channel(
            ticket_name(getattr(user, "name", None), user.id),
            category=category,
            topic=f"{OPEN_TOPIC}{user.id}",
            overwrites=overwrites,
            reason=f"Ticket opened by {user} ({user.id})",
        )

    async def _intro(self, channel, user, note):
        staff = None
        guild = getattr(channel, "guild", None)
        if guild is not None and hasattr(guild, "get_role"):
            staff = guild.get_role(self.staff_role_id)
        embed = discord.Embed(
            title=f"Ticket for {getattr(user, 'display_name', None) or getattr(user, 'name', 'member')}",
            description=note or "No reason given.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Opened by", value=f"{user.mention} (`{user.id}`)")
        content = staff.mention if staff is not None and isinstance(getattr(staff, "mention", None), str) else None
        try:
            await channel.send(
                content=content,
                embed=embed,
                view=TicketCloseView(self),
                allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=True),
            )
        except discord.HTTPException:
            log.warning("could not post the ticket intro in %s", getattr(channel, "id", "?"))

    async def close_from_interaction(self, interaction: discord.Interaction, reason: Optional[str] = None):
        channel = interaction.channel
        if interaction.guild is None or channel is None or not looks_like_ticket(channel):
            return await interaction.response.send_message("This isn't a ticket.", ephemeral=True)
        if not is_staff(interaction.user, self.staff_role_id):
            return await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
        if not is_open_state(channel):
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        note = clip(reason.strip(), 500) if isinstance(reason, str) and reason.strip() else None
        status = await close_ticket(channel, interaction.user, note)
        if status == "forbidden":
            return await interaction.followup.send(
                "I can't close this ticket (missing Manage Threads or Manage Channels).", ephemeral=True)
        if status != "closed":
            return await interaction.followup.send("Couldn't close this ticket.", ephemeral=True)
        await interaction.followup.send("Ticket closed.", ephemeral=True)

    @app_commands.command(name="ticket", description="Open a support ticket")
    @app_commands.describe(reason="What do you need help with?")
    @app_commands.guild_only()
    async def ticket(self, interaction: discord.Interaction, reason: Optional[str] = None):
        await self.open_ticket(interaction, reason)

    @app_commands.command(name="ticket_close", description="Close the ticket in this channel")
    @app_commands.describe(reason="Optional note for the ticket")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def ticket_close(self, interaction: discord.Interaction, reason: Optional[str] = None):
        await self.close_from_interaction(interaction, reason)

    @app_commands.command(name="ticket_setup", description="Post the ticket panel in this channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def ticket_setup(self, interaction: discord.Interaction):
        embed = discord.Embed(title=self.title, description=self.text, color=discord.Color.blurple())
        await interaction.channel.send(embed=embed, view=TicketOpenView(self))
        where = f"<#{self.channel_id}>" if self.mode == "thread" and self.channel_id else "the configured category"
        await interaction.response.send_message(
            f"Ticket panel posted. New tickets are created in {where}.", ephemeral=True)


async def close_ticket(channel, by, reason: Optional[str] = None) -> str:
    """Lock a thread, or remove the opener from a ticket channel. Returns a status string."""
    if not looks_like_ticket(channel):
        return "not_ticket"
    if not is_open_state(channel):
        return "already"
    embed = discord.Embed(title="Ticket closed", color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
    mention = getattr(by, "mention", None)
    who = mention if isinstance(mention, str) else str(getattr(by, "id", "staff"))
    embed.add_field(name="Closed by", value=f"{who} (`{getattr(by, 'id', '?')}`)")
    if reason:
        embed.add_field(name="Note", value=clip(reason), inline=False)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        log.warning("could not post ticket close message in %s", getattr(channel, "id", "?"))
    why = f"Ticket closed by {by}"
    try:
        if is_thread(channel):
            await channel.edit(archived=True, locked=True, reason=why)
        else:
            opener = topic_user_id(getattr(channel, "topic", None))
            guild = getattr(channel, "guild", None)
            if opener and guild is not None and hasattr(guild, "get_member"):
                member = guild.get_member(int(opener))
                if member is not None and hasattr(channel, "set_permissions"):
                    await channel.set_permissions(member, overwrite=None, reason=why)
            topic = f"{CLOSED_TOPIC}{opener}" if opener else CLOSED_TOPIC
            await channel.edit(name=closed_name(getattr(channel, "name", None)), topic=topic, reason=why)
    except discord.Forbidden:
        return "forbidden"
    except discord.HTTPException:
        log.warning("could not close ticket %s", getattr(channel, "id", "?"))
        return "error"
    return "closed"


async def setup(bot):
    await bot.add_cog(Tickets(bot))
