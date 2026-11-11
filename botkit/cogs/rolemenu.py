"""Self-assign roles from a persistent button row or select menu.

/rolemenu_setup posts the menu (admins), the same idea as /verify_setup.
custom_ids are fixed from the configured role ids and the view is registered in
cog_load, so a menu posted before a restart keeps working as long as those
roles are still in config. Buttons toggle one role. A select menu adds the
roles you pick and removes the other menu roles you don't.

At most 25 roles. The bot needs Manage Roles, and its role must sit above the
menu roles. Does not need a privileged intent.
"""
import discord
from discord import app_commands
from discord.ext import commands

BUTTON_PREFIX = "botkit:rolemenu:"
SELECT_ID = "botkit:rolemenu:select"
MAX_ROLES = 25
STYLES = ("buttons", "select")
DEFAULT_TITLE = "Roles"
DEFAULT_TEXT = (
    "Pick the roles you want. Buttons toggle a role. "
    "A select menu keeps only the roles you select."
)
STATUS = {
    "added": "Role added.",
    "removed": "Role removed.",
    "already": "You already have that role.",
    "absent": "You don't have that role.",
    "forbidden": "⚠️ I can't change that role (missing Manage Roles, or my role is below it).",
    "missing_role": "⚠️ That role doesn't exist anymore. Please tell a moderator.",
    "not_allowed": "That role isn't on this menu.",
}


def normalize_style(value) -> str:
    style = str(value or "buttons").strip().lower()
    if style not in STYLES:
        raise ValueError("rolemenu.style must be 'buttons' or 'select'")
    return style


def normalize_roles(raw):
    if not raw:
        raise ValueError("rolemenu.roles needs at least one role")
    if len(raw) > MAX_ROLES:
        raise ValueError(f"rolemenu supports at most {MAX_ROLES} roles")
    out = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or "role_id" not in item:
            raise ValueError("each rolemenu role needs a role_id")
        try:
            role_id = int(item["role_id"])
        except (TypeError, ValueError):
            raise ValueError(f"rolemenu role_id must be a Discord ID, got {item.get('role_id')!r}") from None
        if role_id in seen:
            raise ValueError(f"rolemenu lists role {role_id} twice")
        seen.add(role_id)
        label = str(item.get("label") or f"Role {role_id}").strip() or f"Role {role_id}"
        spec = {"role_id": role_id, "label": label[:80]}
        emoji = item.get("emoji")
        if isinstance(emoji, str) and emoji.strip():
            spec["emoji"] = emoji.strip()[:64]
        out.append(spec)
    return out


def _id_set(values):
    out = set()
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            out.add(value)
        elif isinstance(value, str) and value.isdigit():
            out.add(int(value))
    return out


def selection_diff(member_role_ids, menu_role_ids, selected_ids):
    """Return (to_add, to_remove) for a select menu. Ids outside the menu are ignored."""
    menu = _id_set(menu_role_ids)
    selected = _id_set(selected_ids) & menu
    have = _id_set(member_role_ids) & menu
    return selected - have, have - selected


def summarize(results, labels) -> str:
    added, removed, failed = [], [], []
    for role_id, status in results:
        label = labels.get(role_id, str(role_id))
        if status == "added":
            added.append(label)
        elif status == "removed":
            removed.append(label)
        elif status in ("forbidden", "missing_role"):
            failed.append(label)
    parts = []
    if added:
        parts.append("Added: " + ", ".join(added))
    if removed:
        parts.append("Removed: " + ", ".join(removed))
    if failed:
        parts.append("Couldn't change: " + ", ".join(failed) + ". Check Manage Roles and that my role is above those roles.")
    return "\n".join(parts) if parts else "No role changes."


async def change_role(member, role, add: bool) -> str:
    """Add or remove one role. Returns added, removed, already, absent, forbidden, or missing_role."""
    if role is None:
        return "missing_role"
    has = any(getattr(existing, "id", None) == role.id for existing in (getattr(member, "roles", None) or []))
    if add and has:
        return "already"
    if not add and not has:
        return "absent"
    try:
        if add:
            await member.add_roles(role, reason="BotKit role menu")
            return "added"
        await member.remove_roles(role, reason="BotKit role menu")
        return "removed"
    except discord.Forbidden:
        return "forbidden"
    except discord.NotFound:
        return "missing_role"


class RoleMenuButton(discord.ui.Button):
    def __init__(self, cog: "RoleMenu", spec: dict):
        kwargs = dict(
            label=spec["label"],
            style=discord.ButtonStyle.secondary,
            custom_id=f"{BUTTON_PREFIX}{spec['role_id']}",
        )
        emoji = spec.get("emoji")
        if emoji:
            kwargs["emoji"] = emoji
        try:
            super().__init__(**kwargs)
        except (TypeError, ValueError):
            kwargs.pop("emoji", None)
            super().__init__(**kwargs)
        self.role_id = spec["role_id"]
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_button(interaction, self.role_id)


class RoleMenuSelect(discord.ui.Select):
    def __init__(self, cog: "RoleMenu"):
        options = []
        for spec in cog.roles:
            kwargs = dict(label=spec["label"][:100], value=str(spec["role_id"]))
            if spec.get("emoji"):
                kwargs["emoji"] = spec["emoji"]
            try:
                options.append(discord.SelectOption(**kwargs))
            except (TypeError, ValueError):
                kwargs.pop("emoji", None)
                options.append(discord.SelectOption(**kwargs))
        super().__init__(
            custom_id=SELECT_ID,
            placeholder="Choose roles",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        selected = []
        for value in self.values:
            if str(value).isdigit():
                selected.append(int(value))
        await self.cog.handle_select(interaction, selected)


class RoleMenuView(discord.ui.View):
    def __init__(self, cog: "RoleMenu"):
        super().__init__(timeout=None)
        self.cog = cog
        if cog.style == "select":
            self.add_item(RoleMenuSelect(cog))
        else:
            for spec in cog.roles:
                self.add_item(RoleMenuButton(cog, spec))


class RoleMenu(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config.get("rolemenu", {})
        self.style = normalize_style(cfg.get("style", "buttons"))
        self.title = cfg.get("title") or DEFAULT_TITLE
        self.text = cfg.get("text") or DEFAULT_TEXT
        self.roles = normalize_roles(cfg.get("roles"))
        self._allowed = {spec["role_id"] for spec in self.roles}

    async def cog_load(self):
        view = RoleMenuView(self)
        if view.children and view.is_persistent():
            self.bot.add_view(view)

    async def handle_button(self, interaction: discord.Interaction, role_id: int):
        if interaction.guild is None:
            return await interaction.response.send_message("Use this in the server.", ephemeral=True)
        if role_id not in self._allowed:
            return await interaction.response.send_message(STATUS["not_allowed"], ephemeral=True)
        role = interaction.guild.get_role(role_id)
        has = any(getattr(existing, "id", None) == role_id for existing in (getattr(interaction.user, "roles", None) or []))
        await interaction.response.defer(ephemeral=True)
        status = await change_role(interaction.user, role, add=not has)
        await interaction.followup.send(STATUS[status], ephemeral=True)

    async def handle_select(self, interaction: discord.Interaction, selected_ids):
        if interaction.guild is None:
            return await interaction.response.send_message("Use this in the server.", ephemeral=True)
        roles = {spec["role_id"]: interaction.guild.get_role(spec["role_id"]) for spec in self.roles}
        have = [getattr(role, "id", None) for role in (getattr(interaction.user, "roles", None) or [])]
        add, remove = selection_diff(have, roles, selected_ids)
        await interaction.response.defer(ephemeral=True)
        results = []
        for role_id in sorted(add):
            results.append((role_id, await change_role(interaction.user, roles.get(role_id), True)))
        for role_id in sorted(remove):
            results.append((role_id, await change_role(interaction.user, roles.get(role_id), False)))
        labels = {spec["role_id"]: spec["label"] for spec in self.roles}
        await interaction.followup.send(summarize(results, labels), ephemeral=True)

    @app_commands.command(name="rolemenu_setup", description="Post the role menu in this channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def rolemenu_setup(self, interaction: discord.Interaction):
        embed = discord.Embed(title=self.title, description=self.text, color=discord.Color.blurple())
        await interaction.channel.send(embed=embed, view=RoleMenuView(self))
        await interaction.response.send_message("Role menu posted.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(RoleMenu(bot))
