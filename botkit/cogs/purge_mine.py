"""/purge_mine: members delete their OWN messages in the current channel, even old ones."""
import asyncio, datetime as dt
import discord
from discord import app_commands
from discord.ext import commands

class PurgeMine(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="purge_mine", description="Delete your own messages in this channel")
    @app_commands.describe(limit="How many of your messages to delete (max 500)", older_than_days="Only delete messages older than this many days")
    async def purge_mine(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 500] = 50,
                         older_than_days: app_commands.Range[int, 0, 3650] = 0):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cutoff = discord.utils.utcnow() - dt.timedelta(days=older_than_days)
        recent_cut = discord.utils.utcnow() - dt.timedelta(days=13, hours=12)
        bulk, single = [], []
        async for m in interaction.channel.history(limit=None, before=cutoff if older_than_days else None):
            if m.author.id == interaction.user.id:
                (bulk if m.created_at > recent_cut else single).append(m)
                if len(bulk) + len(single) >= limit:
                    break
        deleted = 0
        for i in range(0, len(bulk), 100):
            chunk = bulk[i:i+100]
            if len(chunk) == 1:
                await chunk[0].delete()
            else:
                await interaction.channel.delete_messages(chunk)
            deleted += len(chunk)
        for m in single:  # Discord only bulk-deletes messages under 14 days old
            try:
                await m.delete(); deleted += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(1.1)
        await interaction.followup.send(f"Deleted {deleted} of your messages.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(PurgeMine(bot))
