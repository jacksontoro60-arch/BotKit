"""BotKit - one bot, plug-in features turned on per client in config.json."""
import asyncio, json, logging, os
from pathlib import Path
import discord
from discord.ext import commands
from dotenv import load_dotenv

ROOT = Path(__file__).parent
# Multi-client hosting: the supervisor (host.py) points each bot process at its own client
# folder with BOTKIT_CONFIG / BOTKIT_ENV. Without them the kit folder's files are used.
CONFIG_PATH = Path(os.environ.get("BOTKIT_CONFIG") or ROOT / "config.json")
ENV_PATH = Path(os.environ.get("BOTKIT_ENV") or ROOT / ".env")
load_dotenv(ENV_PATH)
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

intents = discord.Intents.default()
# Message Content is only for translate auto-translation. Welcome and server logs need
# the members intent (joins, leaves, role changes). Deletes and edits use gateway events.
intents.message_content = bool(CONFIG.get("message_content_intent", False))
_features = set(CONFIG.get("features") or [])
intents.members = bool(CONFIG.get("members_intent", False)) or bool(_features & {"welcome", "server_logs"})

class BotKit(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.config = CONFIG
        self.config_path = CONFIG_PATH

    async def setup_hook(self):
        for name in CONFIG.get("features", []):
            await self.load_extension(f"cogs.{name}")
            logging.info("loaded feature %s", name)
        guild_id = CONFIG.get("guild_id")
        if guild_id:
            g = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

    async def on_ready(self):
        logging.info("online as %s in %d server(s)", self.user, len(self.guilds))

async def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in .env")
    async with BotKit() as bot:
        await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
