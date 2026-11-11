"""Offline tests for in-Discord settings. No token, no network."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from cogs import admin
from cogs.admin import Admin, SettingError, can_manage_settings, change_settings, render_settings, save_config, setting_choices
from cogs.mcstatus_alert import MCStatus
from cogs.rolemenu import RoleMenu
from cogs.scheduler import Scheduler
from cogs.server_logs import ServerLogs
from cogs.tickets import Tickets
from cogs.translate import Translate
from cogs.verify import Verify
from cogs.welcome import Welcome

TOKEN = "M" * 24 + "." + "a" * 6 + "." + "b" * 27
GUILD = 900


def run(coro):
    return asyncio.run(coro)


def perms(administrator=False, manage_channels=False, manage_threads=False):
    return SimpleNamespace(
        administrator=administrator, manage_channels=manage_channels, manage_threads=manage_threads)


def member(role_ids=(), **flags):
    return SimpleNamespace(id=42, roles=[SimpleNamespace(id=rid) for rid in role_ids], guild_permissions=perms(**flags))


def base_config():
    return {
        "guild_id": str(GUILD),
        "features": [
            "scheduler", "mcstatus_alert", "modflag", "purge_mine", "translate", "verify",
            "tickets", "welcome", "server_logs", "rolemenu", "admin",
        ],
        "message_content_intent": False,
        "members_intent": True,
        "admin": {"staff_role_id": "8"},
        "welcome": {"channel_id": "14", "message": "Welcome {member}"},
        "server_logs": {"channel_id": "12", "events": ["join", "leave"]},
        "tickets": {"staff_role_id": "8", "mode": "thread", "channel_id": "11", "title": "Support", "text": "Help"},
        "rolemenu": {"style": "buttons", "title": "Roles", "text": "Pick",
                     "roles": [{"role_id": "21", "label": "Member"}]},
        "verify": {"role_id": "23", "log_channel_id": "12", "captcha": True, "title": "Verification", "text": "Click"},
        "modflag": {"mod_channel_id": "12"},
        "mcstatus": {"address": "play.example.com", "channel_id": "14", "interval_seconds": 60},
        "scheduler": {"timezone": "America/New_York", "posts": [
            {"channel_id": "11", "days": ["mon"], "time": "09:00", "message": "hi"}]},
        "translate": {"backend": "libretranslate", "libretranslate_url": "http://127.0.0.1:5000/translate",
                      "default_target": "en", "max_chars": 1500, "channels": {"11": "en"}},
        "note": "keep me",
    }


def text_channel(cid):
    return SimpleNamespace(id=cid, type=discord.ChannelType.text)


def category(cid):
    return SimpleNamespace(id=cid, type=discord.ChannelType.category)


class Access(unittest.TestCase):
    def test_staff_gate_matches_ticket_staff(self):
        cfg = base_config()
        self.assertTrue(can_manage_settings(member(administrator=True), cfg))
        self.assertTrue(can_manage_settings(member(manage_channels=True), cfg))
        self.assertTrue(can_manage_settings(member(manage_threads=True), cfg))
        self.assertTrue(can_manage_settings(member((8,)), cfg))
        self.assertFalse(can_manage_settings(member((9,)), cfg))
        self.assertFalse(can_manage_settings(member(), cfg))

    def test_admin_role_overrides_the_tickets_role(self):
        cfg = base_config()
        cfg["tickets"]["staff_role_id"] = "4"
        self.assertTrue(can_manage_settings(member((8,)), cfg))
        self.assertFalse(can_manage_settings(member((4,)), cfg))
        cfg["admin"] = {}
        self.assertTrue(can_manage_settings(member((4,)), cfg))
        self.assertFalse(can_manage_settings(member((8,)), cfg))

    def test_home_guild(self):
        cfg = base_config()
        self.assertIsNone(admin.home_guild_error(cfg, SimpleNamespace(id=GUILD)))
        self.assertIn("configured server", admin.home_guild_error(cfg, SimpleNamespace(id=1)))
        self.assertIn("server", admin.home_guild_error(cfg, None))
        open_cfg = dict(cfg)
        open_cfg.pop("guild_id")
        self.assertIsNone(admin.home_guild_error(open_cfg, SimpleNamespace(id=1)))


class RenderAndSecrets(unittest.TestCase):
    def test_show_redacts_secrets_and_skips_env(self):
        cfg = base_config()
        cfg["discord_token"] = TOKEN
        cfg["welcome"]["message"] = "Hello " + TOKEN
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(f"DISCORD_TOKEN={TOKEN}\nDEEPL_API_KEY=deepl-secret-value\n", encoding="utf-8")
            with patch.dict(os.environ, {"BOTKIT_ENV": str(env)}, clear=False):
                os.environ.pop("DISCORD_TOKEN", None)
                os.environ.pop("DEEPL_API_KEY", None)
                text = render_settings(cfg)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("deepl-secret-value", text)
        self.assertIn("[hidden]", text)
        self.assertIn("welcome.channel", text)
        self.assertIn("cannot be changed from Discord", text)
        self.assertIn("Read-only here", text)
        self.assertIn("message_content_intent=False", text)
        self.assertIn("API keys stay in the environment", text)
        self.assertNotIn("keep me", text)  # unknown keys are not dumped

    def test_refuses_secret_names_values_and_restart_keys(self):
        cfg = base_config()
        for key in ("discord_token", "token", "features", "guild_id", "message_content_intent", "members_intent"):
            with self.assertRaises(SettingError) as caught:
                change_settings(cfg, key, TOKEN)
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertEqual(cfg["welcome"]["message"], "Welcome {member}")
        with self.assertRaises(SettingError) as caught:
            change_settings(cfg, "welcome.message", "hi " + TOKEN)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("secret", str(caught.exception).lower())

    def test_does_not_read_env_file_when_editing(self):
        cfg = base_config()
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("DEEPL_API_KEY=deepl-secret-value\nDISCORD_TOKEN=" + TOKEN + "\n", encoding="utf-8")
            with patch.dict(os.environ, {"BOTKIT_ENV": str(env)}, clear=False):
                os.environ.pop("DEEPL_API_KEY", None)
                os.environ.pop("DISCORD_TOKEN", None)
                new = change_settings(cfg, "welcome.message", "Hello {member}", env={})
        blob = json.dumps(new)
        self.assertNotIn("deepl-secret-value", blob)
        self.assertNotIn(TOKEN, blob)
        self.assertEqual(new["note"], "keep me")
        self.assertEqual(cfg["welcome"]["message"], "Welcome {member}")


class Mutations(unittest.TestCase):
    def test_welcome_logs_verify_and_modflag(self):
        cfg = base_config()
        new = change_settings(cfg, "welcome.channel", channel_id=15, channel_type=discord.ChannelType.news)
        self.assertEqual(new["welcome"]["channel_id"], "15")
        self.assertEqual(new["welcome"]["message"], "Welcome {member}")
        with self.assertRaises(SettingError):
            change_settings(cfg, "welcome.channel", channel_id=13, channel_type=discord.ChannelType.voice)
        new = change_settings(cfg, "welcome.message", "Hello {member}")
        self.assertEqual(new["welcome"]["message"], "Hello {member}")
        new = change_settings(cfg, "server_logs.events", "join, edit")
        self.assertEqual(new["server_logs"]["events"], ["join", "edit"])
        with self.assertRaises(SettingError):
            change_settings(cfg, "server_logs.events", "join,nope")
        new = change_settings(cfg, "verify.captcha", "off")
        self.assertFalse(new["verify"]["captcha"])
        new = change_settings(cfg, "verify.log_channel", "off")
        self.assertNotIn("log_channel_id", new["verify"])
        new = change_settings(cfg, "verify.role", role_id=21, role_name="Member")
        self.assertEqual(new["verify"]["role_id"], "21")
        new = change_settings(cfg, "modflag.channel", "12")
        self.assertEqual(new["modflag"]["mod_channel_id"], "12")

    def test_tickets_mode_switch_needs_the_other_id(self):
        cfg = base_config()
        with self.assertRaises(SettingError):
            change_settings(cfg, "tickets.mode", "channel")
        stepped = change_settings(cfg, "tickets.category", channel_id=15, channel_type=discord.ChannelType.category)
        switched = change_settings(stepped, "tickets.mode", "channel")
        self.assertEqual(switched["tickets"]["mode"], "channel")
        self.assertEqual(switched["tickets"]["category_id"], "15")
        self.assertEqual(switched["tickets"]["channel_id"], "11")
        with self.assertRaises(SettingError):
            change_settings(cfg, "tickets.category", channel_id=11, channel_type=discord.ChannelType.text)

    def test_rolemenu_add_remove_and_cap(self):
        cfg = base_config()
        added = change_settings(cfg, "rolemenu.role.add", "News|📰", role_id=22, role_name="News")
        self.assertEqual(added["rolemenu"]["roles"][1], {"role_id": "22", "label": "News", "emoji": "📰"})
        renamed = change_settings(added, "rolemenu.role.add", "People", role_id=21, role_name="Member")
        self.assertEqual(len(renamed["rolemenu"]["roles"]), 2)
        self.assertEqual(renamed["rolemenu"]["roles"][0]["label"], "People")
        styled = change_settings(renamed, "rolemenu.style", "select")
        self.assertEqual(styled["rolemenu"]["style"], "select")
        with self.assertRaises(SettingError):
            change_settings(cfg, "rolemenu.role.remove", role_id=21)
        full = base_config()
        full["rolemenu"]["roles"] = [{"role_id": str(1000 + i), "label": f"R{i}"} for i in range(25)]
        with self.assertRaises(SettingError):
            change_settings(full, "rolemenu.role.add", role_id=2000, role_name="Extra")

    def test_scheduler_and_mcstatus(self):
        cfg = base_config()
        added = change_settings(
            cfg, "scheduler.post.add", "mon,fri|9:05|Standup",
            channel_id=14, channel_type=discord.ChannelType.text)
        post = added["scheduler"]["posts"][1]
        self.assertEqual(post["days"], ["mon", "fri"])
        self.assertEqual(post["time"], "09:05")
        self.assertEqual(post["message"], "Standup")
        self.assertEqual(post["channel_id"], "14")
        removed = change_settings(added, "scheduler.post.remove", "1")
        self.assertEqual(len(removed["scheduler"]["posts"]), 1)
        self.assertEqual(removed["scheduler"]["posts"][0]["message"], "Standup")
        moved = change_settings(cfg, "scheduler.timezone", "Europe/Berlin")
        self.assertEqual(moved["scheduler"]["timezone"], "Europe/Berlin")
        with self.assertRaises(SettingError):
            change_settings(cfg, "scheduler.timezone", "Mars/Base")
        with self.assertRaises(SettingError):
            change_settings(cfg, "scheduler.post.add", "mon 09:00 hello", channel_id=11, channel_type=discord.ChannelType.text)
        slowed = change_settings(cfg, "mcstatus.interval_seconds", "45")
        self.assertEqual(slowed["mcstatus"]["interval_seconds"], 45)
        for bad in ("10", "99999", "soon"):
            with self.assertRaises(SettingError):
                change_settings(cfg, "mcstatus.interval_seconds", bad)
        addressed = change_settings(cfg, "mcstatus.address", "play.other.net")
        self.assertEqual(addressed["mcstatus"]["address"], "play.other.net")

    def test_translate_never_stores_a_key(self):
        cfg = base_config()
        routed = change_settings(
            cfg, "translate.channel", "EN", channel_id=14, channel_type=discord.ChannelType.text, env={})
        self.assertEqual(routed["translate"]["channels"]["14"], "en")
        cleared = change_settings(
            routed, "translate.channel", "off", channel_id=11, channel_type=discord.ChannelType.text, env={})
        self.assertNotIn("11", cleared["translate"]["channels"])
        with self.assertRaises(SettingError) as caught:
            change_settings(cfg, "translate.url", "http://user:s3cret@127.0.0.1/translate", env={})
        self.assertNotIn("s3cret", str(caught.exception))
        with self.assertRaises(SettingError) as caught:
            change_settings(cfg, "translate.backend", "deepl", env={})
        self.assertIn("DEEPL_API_KEY", str(caught.exception))
        self.assertNotIn("abc:fx", str(caught.exception))
        self.assertEqual(cfg["translate"]["backend"], "libretranslate")
        switched = change_settings(cfg, "translate.backend", "deepl", env={"DEEPL_API_KEY": "abc:fx"})
        self.assertEqual(switched["translate"]["backend"], "deepl")
        self.assertNotIn("abc:fx", json.dumps(switched))
        self.assertEqual(cfg["translate"]["backend"], "libretranslate")

    def test_disabled_feature_and_unknown_setting(self):
        cfg = base_config()
        cfg["features"] = ["admin"]
        with self.assertRaises(SettingError):
            change_settings(cfg, "welcome.message", "Hi")
        with self.assertRaises(SettingError):
            change_settings(base_config(), "welcome.nope", "Hi")
        choices = setting_choices(base_config(), "")
        self.assertEqual(len(choices), 25)
        self.assertIn("welcome.message", [choice.value for choice in choices])
        self.assertNotIn("scheduler.timezone", [choice.value for choice in choices])
        sched = setting_choices(base_config(), "sched")
        self.assertIn("scheduler.timezone", [choice.value for choice in sched])
        self.assertTrue(all(choice.value.startswith("scheduler.") for choice in sched))
        off = setting_choices(cfg, "welcome")
        self.assertEqual(off, [])


class CommandPath(unittest.TestCase):
    def _bot(self, cfg, path):
        cogs = {}
        bot = SimpleNamespace(
            config=cfg, config_path=path, add_view=MagicMock(), tree=MagicMock(),
            get_channel=MagicMock(return_value=None), wait_until_ready=AsyncMock(),
        )
        bot.get_cog = lambda name: cogs.get(name)
        cogs["Welcome"] = Welcome(bot)
        cogs["ServerLogs"] = ServerLogs(bot)
        cogs["Tickets"] = Tickets(bot)
        cogs["RoleMenu"] = RoleMenu(bot)
        cogs["Verify"] = Verify(bot)
        cogs["Translate"] = Translate(bot)
        cogs["Admin"] = Admin(bot)
        return bot, cogs

    def _interaction(self, bot, user, guild_id=GUILD):
        interaction = MagicMock()
        interaction.client = bot
        interaction.guild = SimpleNamespace(id=guild_id)
        interaction.user = user
        interaction.response.send_message = AsyncMock()
        return interaction

    def _reply(self, interaction):
        return interaction.response.send_message.await_args.args[0]

    def test_commands_are_registered(self):
        _bot, cogs = self._bot(base_config(), Path("unused.json"))
        group = cogs["Admin"].get_app_commands()[0]
        self.assertEqual(group.name, "settings")
        self.assertEqual({command.name for command in group.commands}, {"show", "set"})

    def test_show_and_set_are_staff_only_and_persist(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                cfg = base_config()
                path.write_text(json.dumps(cfg), encoding="utf-8")
                bot, cogs = self._bot(cfg, path)
                scheduler = Scheduler(bot)
                scheduler.loop.cancel()
                cogs["Scheduler"] = scheduler
                mc = MCStatus(bot)
                mc.probe = AsyncMock(return_value=(False, None))
                mc.check.cancel()
                cogs["MCStatus"] = mc
                try:
                    stranger = self._interaction(bot, member())
                    await Admin.set_setting.callback(cogs["Admin"], stranger, "welcome.message", "Pwned")
                    self.assertIn("staff", self._reply(stranger).lower())
                    self.assertTrue(stranger.response.send_message.await_args.kwargs["ephemeral"])
                    self.assertEqual(cfg["welcome"]["message"], "Welcome {member}")
                    self.assertNotIn("Pwned", path.read_text(encoding="utf-8"))

                    outsider = self._interaction(bot, member(administrator=True), guild_id=1)
                    await Admin.show.callback(cogs["Admin"], outsider)
                    self.assertIn("configured server", self._reply(outsider))
                    self.assertNotIn("Welcome {member}", self._reply(outsider))

                    staff = self._interaction(bot, member((8,)))
                    await Admin.show.callback(cogs["Admin"], staff, "welcome")
                    shown = self._reply(staff)
                    self.assertIn("welcome.message", shown)
                    self.assertIn("<#14>", shown)
                    self.assertTrue(staff.response.send_message.await_args.kwargs["ephemeral"])
                    self.assertNotIn(TOKEN, shown)

                    await Admin.set_setting.callback(
                        cogs["Admin"], staff, "welcome.message", "Hello {member}")
                    reply = self._reply(staff)
                    self.assertIn("Saved welcome.message", reply)
                    self.assertIn("in effect", reply)
                    self.assertEqual(cogs["Welcome"].message, "Hello {member}")
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(saved["welcome"]["message"], "Hello {member}")
                    self.assertEqual(saved["note"], "keep me")
                    self.assertFalse((path.parent / (path.name + ".tmp")).exists())

                    await Admin.set_setting.callback(
                        cogs["Admin"], staff, "server_logs.events", "delete, roles")
                    self.assertEqual(cogs["ServerLogs"].events, {"delete", "roles"})

                    await Admin.set_setting.callback(
                        cogs["Admin"], staff, "tickets.category", None, category(15))
                    await Admin.set_setting.callback(
                        cogs["Admin"], staff, "tickets.mode", "channel")
                    self.assertEqual(cogs["Tickets"].mode, "channel")
                    self.assertEqual(cogs["Tickets"].category_id, 15)

                    await Admin.set_setting.callback(
                        cogs["Admin"], staff, "rolemenu.style", "select")
                    self.assertEqual(cogs["RoleMenu"].style, "select")
                    self.assertTrue(bot.add_view.called)

                    await Admin.set_setting.callback(
                        cogs["Admin"], staff, "mcstatus.interval_seconds", "45")
                    self.assertEqual(cogs["MCStatus"].check._seconds, 45.0)
                    self.assertIsNone(cogs["MCStatus"].online)

                    await Admin.set_setting.callback(
                        cogs["Admin"], staff, "scheduler.timezone", "Europe/Berlin")
                    self.assertEqual(str(cogs["Scheduler"].tz), "Europe/Berlin")

                    await Admin.set_setting.callback(
                        cogs["Admin"], staff, "verify.captcha", "false")
                    self.assertFalse(cogs["Verify"].captcha)

                    with patch.dict(os.environ, {"DEEPL_API_KEY": "abc:fx"}, clear=False):
                        os.environ.pop("DISCORD_TOKEN", None)
                        await Admin.set_setting.callback(
                            cogs["Admin"], staff, "translate.backend", "deepl")
                    deepl_reply = self._reply(staff)
                    self.assertIn("Saved translate.backend", deepl_reply)
                    self.assertNotIn("abc:fx", deepl_reply)
                    self.assertNotIn("abc:fx", path.read_text(encoding="utf-8"))
                    self.assertEqual(cogs["Translate"].backend.name, "deepl")
                    self.assertIn("Message Content", deepl_reply)

                    await Admin.set_setting.callback(
                        cogs["Admin"], staff, "admin.staff_role", None, None, SimpleNamespace(id=4, name="Mods"))
                    self.assertEqual(str(cogs["Admin"].staff_role_id), "4")
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["admin"]["staff_role_id"], "4")
                    self.assertNotIn("abc:fx", path.read_text(encoding="utf-8"))
                finally:
                    scheduler.loop.cancel()
                    mc.check.cancel()

        run(body())

    def test_failed_save_leaves_memory_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = base_config()
            bot, cogs = self._bot(cfg, Path(tmp) / "missing" / "config.json")
            staff = self._interaction(bot, member(administrator=True))
            run(Admin.set_setting.callback(cogs["Admin"], staff, "welcome.message", "Nope"))
            self.assertIn("Nothing was changed", self._reply(staff))
            self.assertEqual(cfg["welcome"]["message"], "Welcome {member}")
            self.assertEqual(cogs["Welcome"].message, "Welcome {member}")

    def test_save_refuses_env_files(self):
        cfg = base_config()
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("DISCORD_TOKEN=keep-this\n", encoding="utf-8")
            with self.assertRaises(SettingError):
                save_config(env, cfg)
            self.assertEqual(env.read_text(encoding="utf-8"), "DISCORD_TOKEN=keep-this\n")
            disguised = Path(tmp) / "config.json"
            disguised.write_text("DISCORD_TOKEN=keep-this\n", encoding="utf-8")
            with patch.dict(os.environ, {"BOTKIT_ENV": str(disguised)}, clear=False):
                with self.assertRaises(SettingError):
                    save_config(disguised, cfg)
            self.assertIn("DISCORD_TOKEN=keep-this", disguised.read_text(encoding="utf-8"))

    def test_autocomplete_hides_settings_from_strangers(self):
        bot, _cogs = self._bot(base_config(), Path("unused.json"))
        stranger = self._interaction(bot, member())
        stranger.client = bot
        choices = run(admin.complete_setting(stranger, "welcome"))
        self.assertEqual(choices, [])
        staff = self._interaction(bot, member((8,)))
        choices = run(admin.complete_setting(staff, "welcome"))
        self.assertIn("welcome.message", [choice.value for choice in choices])


if __name__ == "__main__":
    unittest.main()
