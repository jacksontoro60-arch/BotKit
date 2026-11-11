"""Minecraft server online/offline alerts plus /mcstatus."""
import discord
from discord import app_commands
from discord.ext import commands, tasks
from mcstatus import JavaServer

class MCStatus(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.online = None
        self.apply_config()
        self.check.start()

    def apply_config(self):
        """Re-read the address, channel, and poll interval without resetting online/offline state."""
        cfg = self.bot.config.get("mcstatus") or {}
        address = cfg["address"]
        channel_id = int(cfg["channel_id"])
        interval = int(cfg.get("interval_seconds", 60))
        self.address = address
        self.channel_id = channel_id
        self.check.change_interval(seconds=interval)

    def cog_unload(self):
        self.check.cancel()

    async def probe(self):
        try:
            st = await JavaServer.lookup(self.address).async_status()
            return True, st
        except Exception:
            return False, None

    @tasks.loop(seconds=60)
    async def check(self):
        up, st = await self.probe()
        if self.online is not None and up != self.online:
            ch = self.bot.get_channel(self.channel_id)
            if ch:
                msg = (f"🟢 **{self.address}** is back online ({st.players.online}/{st.players.max} players)"
                       if up else f"🔴 **{self.address}** just went offline")
                await ch.send(msg)
        self.online = up

    @check.before_loop
    async def before(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="mcstatus", description="Check the Minecraft server right now")
    async def mcstatus(self, interaction: discord.Interaction):
        up, st = await self.probe()
        if up:
            await interaction.response.send_message(
                f"🟢 {self.address} is online with {st.players.online}/{st.players.max} players ({round(st.latency)} ms)")
        else:
            await interaction.response.send_message(f"🔴 {self.address} is offline")

async def setup(bot):
    await bot.add_cog(MCStatus(bot))
