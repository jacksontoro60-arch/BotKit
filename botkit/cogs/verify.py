"""Button verification: /verify_setup posts an embed with a Verify button that grants a role.

Optional simple math captcha (modal). Every attempt is logged to a mod channel.
The button view is persistent (timeout=None + fixed custom_id, registered with
bot.add_view on load), so buttons posted before a restart keep working.
The bot needs Manage Roles and its top role must sit ABOVE the verified role.
"""
import logging
import random
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)
CUSTOM_ID = "botkit:verify"


def make_captcha(rng=None):
    """Returns (question, answer) for a single-digit addition."""
    rng = rng or random
    a, b = rng.randint(1, 9), rng.randint(1, 9)
    return f"What is {a} + {b}?", a + b


def check_answer(text, answer) -> bool:
    """True if the first whole number in text equals answer (whitespace/extra text ignored)."""
    if not isinstance(text, str):
        return False
    m = re.search(r"-?\d+", text.strip())
    return bool(m) and int(m.group()) == answer


async def assign_role(member, role) -> str:
    """Role-assignment path. Returns 'added', 'already', 'forbidden' or 'missing_role'."""
    if role is None:
        return "missing_role"
    if any(r.id == role.id for r in member.roles):
        return "already"
    try:
        await member.add_roles(role, reason="BotKit verification")
        return "added"
    except discord.Forbidden:
        return "forbidden"
    except discord.NotFound:
        return "missing_role"


STATUS_MSG = {
    "added": "✅ You're verified. Welcome!",
    "already": "You're already verified.",
    "forbidden": "⚠️ I can't give that role (missing Manage Roles, or my role is below it). Please tell a moderator.",
    "missing_role": "⚠️ The verification role isn't set up. Please tell a moderator.",
}


class CaptchaModal(discord.ui.Modal, title="Quick check"):
    def __init__(self, cog: "Verify", question: str, answer: int):
        super().__init__(timeout=300)
        self.cog, self.answer = cog, answer
        self.reply = discord.ui.TextInput(label=question, placeholder="Type the number", max_length=4)
        self.add_item(self.reply)

    async def on_submit(self, interaction: discord.Interaction):
        if check_answer(self.reply.value, self.answer):
            await self.cog.grant(interaction, captcha="passed")
        else:
            await interaction.response.send_message("❌ Wrong answer. Click Verify to try again.", ephemeral=True)
            await self.cog.log(interaction.user, "captcha_failed")


class VerifyView(discord.ui.View):
    def __init__(self, cog: "Verify"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="✅", custom_id=CUSTOM_ID)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            return await interaction.response.send_message("Use this in the server.", ephemeral=True)
        role = interaction.guild.get_role(self.cog.role_id)
        if role is not None and any(r.id == role.id for r in interaction.user.roles):
            return await interaction.response.send_message(STATUS_MSG["already"], ephemeral=True)
        if self.cog.captcha:
            q, a = make_captcha()
            return await interaction.response.send_modal(CaptchaModal(self.cog, q, a))
        await self.cog.grant(interaction)


class Verify(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config.get("verify", {})
        self.role_id = int(cfg["role_id"])
        self.log_channel_id = int(cfg.get("log_channel_id") or 0) or None
        self.captcha = bool(cfg.get("captcha", False))
        self.title = cfg.get("title", "Verification")
        self.text = cfg.get("text", "Click **Verify** to get access to the server.")

    async def cog_load(self):
        self.bot.add_view(VerifyView(self))  # re-attach to buttons posted before a restart

    async def grant(self, interaction: discord.Interaction, captcha: str = "off"):
        role = interaction.guild.get_role(self.role_id)
        status = await assign_role(interaction.user, role)
        await interaction.response.send_message(STATUS_MSG[status], ephemeral=True)
        await self.log(interaction.user, status, captcha)

    async def log(self, member, status: str, captcha: Optional[str] = None):
        if not self.log_channel_id:
            return
        ch = self.bot.get_channel(self.log_channel_id)
        if ch is None:
            return
        ok = status in ("added", "already")
        e = discord.Embed(title="Verification", color=discord.Color.green() if ok else discord.Color.red(),
                          timestamp=discord.utils.utcnow())
        e.add_field(name="Member", value=f"{member.mention} (`{member.id}`)")
        e.add_field(name="Result", value=status)
        if captcha:
            e.add_field(name="Captcha", value=captcha)
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            log.warning("could not write to verify log channel %s", self.log_channel_id)

    @app_commands.command(name="verify_setup", description="Post the verification button in this channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def verify_setup(self, interaction: discord.Interaction):
        e = discord.Embed(title=self.title, description=self.text, color=discord.Color.blurple())
        await interaction.channel.send(embed=e, view=VerifyView(self))
        await interaction.response.send_message("Verification message posted.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Verify(bot))
