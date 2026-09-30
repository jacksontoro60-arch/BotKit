"""Auto-translate chosen channels + a 'Translate' right-click message menu.

Backends are pluggable: LibreTranslate-compatible (default, URL from config) or DeepL
(key from the DEEPL_API_KEY env var). No keys live in code or config.json.
Auto-translate needs "message_content_intent": true in config.json AND the Message
Content intent enabled for the bot in the Discord Developer Portal.
"""
import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)
DEFAULT_LT_URL = "https://libretranslate.com/translate"


class TranslateError(Exception):
    pass


class Backend:
    """Subclass and implement translate(); return (translated_text, detected_source_or_None)."""
    name = "base"

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def translate(self, text: str, target: str):
        raise NotImplementedError


class LibreTranslateBackend(Backend):
    name = "libretranslate"

    def __init__(self, session_factory, url=DEFAULT_LT_URL, api_key=None):
        super().__init__(session_factory)
        self.url = url
        self.api_key = api_key

    async def translate(self, text, target):
        payload = {"q": text, "source": "auto", "target": target, "format": "text"}
        if self.api_key:
            payload["api_key"] = self.api_key
        session = await self._session_factory()
        async with session.post(self.url, json=payload) as r:
            data = await r.json(content_type=None)
            if r.status != 200 or "translatedText" not in data:
                raise TranslateError(f"LibreTranslate HTTP {r.status}: {data.get('error', data)}")
        src = (data.get("detectedLanguage") or {}).get("language")
        return data["translatedText"], src


def deepl_endpoint(key: str) -> str:
    """DeepL Free keys end with ':fx' and use a different host."""
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    return f"https://{host}/v2/translate"


class DeepLBackend(Backend):
    name = "deepl"

    def __init__(self, session_factory, api_key):
        super().__init__(session_factory)
        if not api_key:
            raise TranslateError("DEEPL_API_KEY is not set")
        self.api_key = api_key
        self.url = deepl_endpoint(api_key)

    async def translate(self, text, target):
        headers = {"Authorization": f"DeepL-Auth-Key {self.api_key}"}
        session = await self._session_factory()
        async with session.post(self.url, json={"text": [text], "target_lang": target.upper()}, headers=headers) as r:
            data = await r.json(content_type=None)
            if r.status != 200 or not data.get("translations"):
                raise TranslateError(f"DeepL HTTP {r.status}: {data.get('message', data)}")
        t = data["translations"][0]
        return t["text"], (t.get("detected_source_language") or "").lower() or None


def make_backend(cfg: dict, session_factory, env=os.environ) -> Backend:
    kind = (cfg.get("backend") or "libretranslate").lower()
    if kind == "deepl":
        return DeepLBackend(session_factory, env.get("DEEPL_API_KEY"))
    if kind == "libretranslate":
        return LibreTranslateBackend(session_factory, cfg.get("libretranslate_url") or DEFAULT_LT_URL,
                                     env.get("LIBRETRANSLATE_API_KEY") or None)
    raise TranslateError(f"unknown translate backend {kind!r}")


def target_for_channel(channels: dict, channel_id) -> "str | None":
    """Per-channel routing: config keys are channel IDs as strings."""
    if not channels or channel_id is None:
        return None
    t = channels.get(str(channel_id))
    return t.lower() if t else None


def same_language(src, target) -> bool:
    """'en-US' vs 'en' count as the same; unknown source means 'translate anyway'."""
    if not src:
        return False
    return src.lower().split("-")[0] == target.lower().split("-")[0]


class Translate(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config.get("translate", {})
        self.channels = {str(k): v for k, v in cfg.get("channels", {}).items()}
        self.default_target = cfg.get("default_target", "en")
        self.max_chars = int(cfg.get("max_chars", 1500))
        self._session = None
        self.backend = make_backend(cfg, self._get_session)
        self.menu = app_commands.ContextMenu(name="Translate", callback=self.translate_menu)
        bot.tree.add_command(self.menu)

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def cog_unload(self):
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)
        if self._session and not self._session.closed:
            await self._session.close()

    async def translate_text(self, text, target):
        """Returns translated text, or None if it's already in the target language."""
        out, src = await self.backend.translate(text[: self.max_chars], target)
        if same_language(src, target) or out.strip() == text.strip():
            return None
        return out

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content.strip():
            return
        target = target_for_channel(self.channels, message.channel.id)
        if not target:
            return
        try:
            out = await self.translate_text(message.content, target)
        except Exception:
            log.exception("auto-translate failed in channel %s", message.channel.id)
            return
        if out:
            await message.reply(f"🌐 ({target}) {out}"[:2000], mention_author=False)

    async def translate_menu(self, interaction: discord.Interaction, message: discord.Message):
        if not message.content.strip():
            return await interaction.response.send_message("That message has no text to translate.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            out = await self.translate_text(message.content, self.default_target)
        except Exception:
            log.exception("translate menu failed")
            return await interaction.followup.send("Translation service is unavailable right now.", ephemeral=True)
        await interaction.followup.send((f"🌐 ({self.default_target}) {out}" if out else
                                         f"Already in `{self.default_target}`.")[:2000], ephemeral=True)


async def setup(bot):
    await bot.add_cog(Translate(bot))
