"""Welcome message when a member joins.

Posts one configurable message in welcome.channel_id. Placeholders:
{member} mention, {name} display name, {server} server name, {count} member count.
Unknown placeholders are left as written. Bots are skipped.

Leaves, deletes, and role changes belong in server_logs, not here.
Requires the Server Members intent (members_intent in config, and the portal toggle).
Does not need Message Content.
"""
import logging

import discord
from discord.ext import commands

log = logging.getLogger(__name__)
DEFAULT_MESSAGE = "Welcome {member} to **{server}**! You are member #{count}."


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render_welcome(template, *, mention, server, name, count) -> str:
    """Fill the welcome template. A broken template is returned unchanged."""
    values = _Safe(member=mention, user=mention, server=server, name=name, count=count)
    try:
        return str(template).format_map(values)
    except (ValueError, IndexError, KeyError):
        return str(template)


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.apply_config()

    def apply_config(self):
        """Re-read welcome settings. Called at startup and after /settings set."""
        cfg = self.bot.config.get("welcome") or {}
        channel_id = int(cfg["channel_id"])
        message = cfg.get("message") or DEFAULT_MESSAGE
        self.channel_id = channel_id
        self.message = message

    def _channel(self, guild):
        channel = None
        if guild is not None and callable(getattr(guild, "get_channel", None)):
            channel = guild.get_channel(self.channel_id)
        if channel is None:
            channel = self.bot.get_channel(self.channel_id)
        return channel

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if getattr(member, "bot", False) is True:
            return
        guild = getattr(member, "guild", None)
        channel = self._channel(guild)
        if channel is None:
            log.warning("welcome channel %s is not available", self.channel_id)
            return
        count = getattr(guild, "member_count", None)
        count_s = str(count) if isinstance(count, int) else "?"
        mention = getattr(member, "mention", None)
        text = render_welcome(
            self.message,
            mention=mention if isinstance(mention, str) else f"<@{member.id}>",
            server=getattr(guild, "name", None) or "the server",
            name=getattr(member, "display_name", None) or getattr(member, "name", "member"),
            count=count_s,
        )
        try:
            await channel.send(
                text,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            )
        except discord.HTTPException:
            log.warning("could not send welcome message in %s", self.channel_id)


async def setup(bot):
    await bot.add_cog(Welcome(bot))
