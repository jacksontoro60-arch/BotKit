"""Right-click any message > Apps > Flag for mods. Posts it to a private mod channel."""
import discord
from discord import app_commands
from discord.ext import commands

class ModFlag(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.apply_config()
        self.menu = app_commands.ContextMenu(name="Flag for mods", callback=self.flag)
        bot.tree.add_command(self.menu)

    def apply_config(self):
        """Re-read the mod channel. Called at startup and after /settings set."""
        self.mod_channel_id = int(self.bot.config["modflag"]["mod_channel_id"])

    async def cog_unload(self):
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)

    async def flag(self, interaction: discord.Interaction, message: discord.Message):
        ch = self.bot.get_channel(self.mod_channel_id)
        if ch is None:
            return await interaction.response.send_message("Mod channel isn't set up.", ephemeral=True)
        e = discord.Embed(title="🚩 Message flagged", description=message.content[:4000] or "(no text)",
                          color=discord.Color.red(), timestamp=message.created_at)
        e.add_field(name="Author", value=message.author.mention)
        e.add_field(name="Flagged by", value=interaction.user.mention)
        e.add_field(name="Where", value=f"[Jump to message]({message.jump_url})", inline=False)
        await ch.send(embed=e)
        await interaction.response.send_message("Thanks, the mods have been notified.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(ModFlag(bot))
