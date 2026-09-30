"""Scheduled messages: daily or weekly posts from config."""
import datetime as dt
from zoneinfo import ZoneInfo
from discord.ext import commands, tasks

DAYS = ["mon","tue","wed","thu","fri","sat","sun"]

class Scheduler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config.get("scheduler", {})
        self.tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
        self.posts = cfg.get("posts", [])
        self.sent = set()
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @tasks.loop(seconds=30)
    async def loop(self):
        now = dt.datetime.now(self.tz)
        for i, p in enumerate(self.posts):
            days = [d.lower()[:3] for d in p.get("days", DAYS)]
            if DAYS[now.weekday()] not in days or now.strftime("%H:%M") != p["time"]:
                continue
            key = (i, now.date())
            if key in self.sent:
                continue
            ch = self.bot.get_channel(int(p["channel_id"]))
            if ch:
                await ch.send(p["message"])
                self.sent.add(key)

    @loop.before_loop
    async def before(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(Scheduler(bot))
