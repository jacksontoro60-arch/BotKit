"""Moderation log: joins, leaves, message deletes, message edits, and role changes.

Posts embeds to server_logs.channel_id. This is a practical set, not a full audit log.
Joins, leaves, and role changes need the Server Members intent. Deletes and edits use
gateway events and do not need Message Content. Deleted or edited text is included
only when Discord already gave the bot the content (cached, and Message Content on).
A recent audit-log entry can name who deleted a message when the bot has View Audit Log.

The bot's own messages are skipped. Events in the log channel itself are skipped.
"""
import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
from discord.ext import commands

log = logging.getLogger(__name__)
LOG_EVENTS = ("join", "leave", "delete", "edit", "roles")
DELETE_AUDIT_WINDOW = 20


def normalize_events(value):
    """Return a list of log event names. None or 'all' means every event."""
    if value is None:
        return list(LOG_EVENTS)
    if isinstance(value, str):
        if value.strip().lower() in ("", "all"):
            return list(LOG_EVENTS)
        value = [part for part in re.split(r"[,\s]+", value.strip().lower()) if part]
    out = []
    for item in value:
        name = str(item).strip().lower()
        if name not in LOG_EVENTS:
            raise ValueError(
                f"unknown log event {item!r}; choose from {', '.join(LOG_EVENTS)} (or all)")
        if name not in out:
            out.append(name)
    if not out:
        raise ValueError("pick at least one log event (join, leave, delete, edit, roles)")
    return out


def clip(text, limit=900) -> str:
    text = str(text).replace("```", "'''")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def as_utc(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        return discord.utils.parse_time(value)
    return None


def should_log_edit(before_content, after_content, before_edited_at, after_edited_at) -> bool:
    """True when a message update is a user edit rather than an embed unfurl.

    Unfurls do not advance edited_at. Identical visible text is ignored.
    Empty content still counts when edited_at moves forward, so edits are logged
    with the Message Content intent off.
    """
    after_at = as_utc(after_edited_at)
    if after_at is None:
        return False
    if _visible(before_content) and _visible(after_content) and before_content == after_content:
        return False
    before_at = as_utc(before_edited_at)
    if before_at is not None and after_at <= before_at:
        return _visible(before_content) and _visible(after_content) and before_content != after_content
    return True


def _visible(text) -> bool:
    return isinstance(text, str) and text != ""


def audit_delete_actor(entries, *, channel_id, author_id, now, window=DELETE_AUDIT_WINDOW):
    """Newest message-delete audit entry for this channel and author, or None.

    Discord does not put the message id on this audit action. A matching entry
    within `window` seconds is treated as the deleter. Members deleting their
    own messages usually leave no entry.
    """
    best = None
    moment = as_utc(now)
    for entry in entries:
        extra = getattr(entry, "extra", None)
        channel = getattr(extra, "channel", None)
        if getattr(channel, "id", None) != channel_id:
            continue
        target_id = getattr(getattr(entry, "target", None), "id", None)
        if author_id is not None and target_id is not None and target_id != author_id:
            continue
        created = as_utc(getattr(entry, "created_at", None))
        if created is None or moment is None:
            continue
        age = (moment - created).total_seconds()
        if age < -5 or age > window:
            continue
        if best is None or created > as_utc(best.created_at):
            best = entry
    if best is None:
        return None
    return getattr(best, "user", None)


def message_jump(guild_id, channel_id, message_id) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def user_label(user) -> str:
    if user is None:
        return "Unknown"
    mention = getattr(user, "mention", None)
    name = getattr(user, "name", None) or getattr(user, "id", "unknown")
    if isinstance(mention, str):
        return f"{mention} (`{name}`)"
    return f"`{name}`"


def channel_label(channel, channel_id) -> str:
    mention = getattr(channel, "mention", None)
    if isinstance(mention, str):
        return mention
    return f"<#{channel_id}>"


def shown_content(text, *, known: bool, content_intent: bool) -> str:
    """Text for a log field. Hidden content stays marked unavailable when the intent is off."""
    if isinstance(text, str) and text.strip():
        return clip(text.strip())
    if known and content_intent and text == "":
        return "(no text)"
    return "(content unavailable)"


def role_names(member) -> str:
    guild_id = getattr(getattr(member, "guild", None), "id", None)
    parts = []
    roles = getattr(member, "roles", None)
    if not isinstance(roles, (list, tuple)):
        return ""
    for role in roles:
        if getattr(role, "id", None) == guild_id:
            continue
        mention = getattr(role, "mention", None)
        parts.append(mention if isinstance(mention, str) else str(getattr(role, "name", role.id)))
    return clip(" ".join(parts), 1000)


class ServerLogs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.apply_config()

    def apply_config(self):
        """Re-read log settings. Called at startup and after /settings set."""
        cfg = self.bot.config.get("server_logs") or {}
        channel_id = int(cfg["channel_id"])
        events = set(normalize_events(cfg.get("events", "all")))
        self.channel_id = channel_id
        self.events = events

    def _guild(self, guild_id):
        getter = getattr(self.bot, "get_guild", None)
        if guild_id is None or not callable(getter):
            return None
        return getter(guild_id)

    def _log_channel(self, guild):
        channel = None
        if guild is not None and callable(getattr(guild, "get_channel", None)):
            channel = guild.get_channel(self.channel_id)
        if channel is None:
            channel = self.bot.get_channel(self.channel_id)
        return channel

    async def _send(self, guild, embed):
        channel = self._log_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.warning("could not write to log channel %s", self.channel_id)

    def _wants(self, name) -> bool:
        return name in self.events

    def _content_intent(self) -> bool:
        return getattr(getattr(self.bot, "intents", None), "message_content", False) is True

    def _is_self(self, user) -> bool:
        me = getattr(self.bot, "user", None)
        return me is not None and user is not None and getattr(user, "id", None) == getattr(me, "id", None)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if not self._wants("join"):
            return
        guild = getattr(member, "guild", None)
        embed = discord.Embed(title="Member joined", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Member", value=user_label(member))
        embed.add_field(name="ID", value=str(member.id))
        count = getattr(guild, "member_count", None)
        if isinstance(count, int):
            embed.add_field(name="Members", value=str(count))
        await self._send(guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        if not self._wants("leave"):
            return
        guild = getattr(member, "guild", None)
        embed = discord.Embed(title="Member left", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Member", value=user_label(member))
        embed.add_field(name="ID", value=str(member.id))
        roles = role_names(member)
        if roles:
            embed.add_field(name="Roles", value=roles, inline=False)
        await self._send(guild, embed)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if not self._wants("delete"):
            return
        guild_id = getattr(payload, "guild_id", None)
        channel_id = getattr(payload, "channel_id", None)
        if guild_id is None or channel_id == self.channel_id:
            return
        cached = getattr(payload, "cached_message", None)
        author = getattr(cached, "author", None) if cached is not None else None
        if self._is_self(author):
            return
        guild = self._guild(guild_id) or getattr(cached, "guild", None)
        actor = await self._delete_actor(guild, channel_id, getattr(author, "id", None))
        source = guild.get_channel(channel_id) if guild is not None and callable(getattr(guild, "get_channel", None)) else None
        embed = discord.Embed(title="Message deleted", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Channel", value=channel_label(source, channel_id))
        embed.add_field(name="Message ID", value=str(payload.message_id))
        embed.add_field(name="Author", value=user_label(author))
        if actor is not None:
            embed.add_field(name="Deleted by", value=user_label(actor))
        embed.add_field(name="Content", value=shown_content(
            getattr(cached, "content", None) if cached is not None else None,
            known=cached is not None,
            content_intent=self._content_intent(),
        ), inline=False)
        await self._send(guild, embed)

    async def _delete_actor(self, guild, channel_id, author_id):
        if guild is None or not callable(getattr(guild, "audit_logs", None)):
            return None
        try:
            entries = []
            async for entry in guild.audit_logs(action=discord.AuditLogAction.message_delete, limit=6):
                entries.append(entry)
        except (discord.HTTPException, TypeError):
            return None
        return audit_delete_actor(
            entries, channel_id=channel_id, author_id=author_id, now=discord.utils.utcnow())

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if not self._wants("edit"):
            return
        data = getattr(payload, "data", None) or {}
        if not isinstance(data, dict):
            return
        guild_id = getattr(payload, "guild_id", None)
        channel_id = getattr(payload, "channel_id", None)
        if guild_id is None or channel_id == self.channel_id:
            return
        cached = getattr(payload, "cached_message", None)
        before = getattr(cached, "content", None) if cached is not None else None
        after = data.get("content") if "content" in data else None
        before_at = getattr(cached, "edited_at", None) if cached is not None else None
        if not should_log_edit(before, after, before_at, data.get("edited_timestamp")):
            return
        author = getattr(cached, "author", None) if cached is not None else None
        raw_author = data.get("author") if isinstance(data.get("author"), dict) else None
        if author is None and raw_author and str(raw_author.get("id", "")).isdigit():
            author = SimpleNamespace(
                id=int(raw_author["id"]),
                name=raw_author.get("username") or raw_author["id"],
                mention=f"<@{raw_author['id']}>",
            )
        if self._is_self(author):
            return
        guild = self._guild(guild_id) or (getattr(cached, "guild", None) if cached is not None else None)
        source = guild.get_channel(channel_id) if guild is not None and callable(getattr(guild, "get_channel", None)) else None
        jump = message_jump(guild_id, channel_id, payload.message_id)
        embed = discord.Embed(title="Message edited", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Channel", value=channel_label(source, channel_id))
        embed.add_field(name="Author", value=user_label(author))
        embed.add_field(name="Jump", value=f"[Jump to message]({jump})", inline=False)
        intent = self._content_intent()
        embed.add_field(name="Before", value=shown_content(before, known=cached is not None, content_intent=intent), inline=False)
        embed.add_field(
            name="After",
            value=shown_content(after if "content" in data else None, known="content" in data, content_intent=intent),
            inline=False,
        )
        await self._send(guild, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if not self._wants("roles"):
            return
        before_roles = _role_map(before)
        after_roles = _role_map(after)
        if before_roles.keys() == after_roles.keys():
            return
        added = [after_roles[rid] for rid in after_roles.keys() - before_roles.keys()]
        removed = [before_roles[rid] for rid in before_roles.keys() - after_roles.keys()]
        guild = getattr(after, "guild", None) or getattr(before, "guild", None)
        embed = discord.Embed(title="Roles updated", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        embed.add_field(name="Member", value=user_label(after))
        embed.add_field(name="Added", value=_mentions(added) or "—", inline=False)
        embed.add_field(name="Removed", value=_mentions(removed) or "—", inline=False)
        await self._send(guild, embed)


def _role_map(member):
    roles = getattr(member, "roles", None)
    if not isinstance(roles, (list, tuple)):
        return {}
    guild_id = getattr(getattr(member, "guild", None), "id", None)
    out = {}
    for role in roles:
        rid = getattr(role, "id", None)
        if rid is None or rid == guild_id:
            continue
        out[rid] = role
    return out


def _mentions(roles) -> str:
    parts = []
    for role in roles:
        mention = getattr(role, "mention", None)
        parts.append(mention if isinstance(mention, str) else str(getattr(role, "name", role.id)))
    return clip(" ".join(parts), 1000)


async def setup(bot):
    await bot.add_cog(ServerLogs(bot))
