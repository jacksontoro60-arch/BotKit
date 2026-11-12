"""Offline tests for tickets, welcome, server logs, and role menus. No token, no network."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import discord
from cogs import rolemenu as rm
from cogs import server_logs as logs
from cogs import tickets as tk
from cogs import welcome as wc


def run(coro):
    return asyncio.run(coro)


def http_exc(cls):
    return cls(MagicMock(status=403, reason="x"), "nope")


class AIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


def field(embed, name):
    for item in embed.fields:
        if item.name == name:
            return item.value
    raise AssertionError(name)


def bot_for(section, cfg):
    return SimpleNamespace(
        config={section: cfg},
        tree=MagicMock(),
        get_channel=MagicMock(return_value=None),
        get_guild=MagicMock(return_value=None),
        add_view=MagicMock(),
        user=SimpleNamespace(id=1, bot=True),
        intents=SimpleNamespace(message_content=False, members=True),
    )


class TicketHelpers(unittest.TestCase):
    def test_names_and_open_state(self):
        self.assertEqual(tk.ticket_name("Cool User!", 42), "ticket-cool-user-42")
        self.assertEqual(tk.ticket_name("***", 42), "ticket-user-42")
        self.assertEqual(tk.closed_name("ticket-user-42"), "closed-ticket-user-42")
        self.assertEqual(tk.topic_user_id("botkit-ticket:42"), "42")
        self.assertEqual(tk.topic_user_id("botkit-ticket-closed:42"), "42")
        self.assertIsNone(tk.topic_user_id("hello"))
        open_ch = SimpleNamespace(name="ticket-ada-42", topic="botkit-ticket:42", archived=False, locked=False,
                                  type=discord.ChannelType.text)
        self.assertTrue(tk.is_open_ticket(open_ch, 42))
        self.assertFalse(tk.is_open_ticket(open_ch, 7))
        open_ch.topic = "botkit-ticket-closed:42"
        open_ch.name = "closed-ticket-ada-42"
        self.assertTrue(tk.looks_like_ticket(open_ch))
        self.assertFalse(tk.is_open_state(open_ch))
        self.assertFalse(tk.looks_like_ticket(SimpleNamespace(name="general", topic=None)))

    def test_staff(self):
        member = SimpleNamespace(roles=[SimpleNamespace(id=8)],
                                 guild_permissions=SimpleNamespace(administrator=False, manage_channels=False, manage_threads=False))
        self.assertTrue(tk.is_staff(member, "8"))
        self.assertFalse(tk.is_staff(SimpleNamespace(roles=[], guild_permissions=member.guild_permissions), 8))
        admin = SimpleNamespace(roles=[], guild_permissions=SimpleNamespace(administrator=True, manage_channels=False, manage_threads=False))
        self.assertTrue(tk.is_staff(admin, 8))


class TicketsFlow(unittest.TestCase):
    def _cog(self, mode="thread"):
        cfg = {"staff_role_id": "8", "mode": mode, "title": "Support", "text": "Help"}
        if mode == "thread":
            cfg["channel_id"] = "100"
        else:
            cfg["category_id"] = "200"
        bot = bot_for("tickets", cfg)
        return tk.Tickets(bot), bot

    def _interaction(self, user_id=42):
        i = MagicMock()
        i.user.id = user_id
        i.user.name = "Ada"
        i.user.display_name = "Ada"
        i.user.mention = f"<@{user_id}>"
        i.user.roles = []
        i.user.guild_permissions = SimpleNamespace(administrator=False, manage_channels=False, manage_threads=False)
        i.response.defer = AsyncMock()
        i.response.send_message = AsyncMock()
        i.followup.send = AsyncMock()
        staff = discord.Object(id=8)
        staff.mention = "<@&8>"
        i.guild.get_role.return_value = staff
        i.guild.active_threads = AsyncMock(return_value=[])
        i.guild.text_channels = []
        i.guild.default_role = discord.Object(id=900)
        i.guild.me = discord.Object(id=1)
        return i

    def test_views_persist(self):
        cog, bot = self._cog()
        self.assertEqual({cmd.name for cmd in tk.Tickets.__cog_app_commands__}, {"ticket", "ticket_close", "ticket_setup"})
        self.assertEqual({cmd.name for cmd in rm.RoleMenu.__cog_app_commands__}, {"rolemenu_setup"})
        run(cog.cog_load())
        views = [call.args[0] for call in bot.add_view.call_args_list]
        self.assertEqual(len(views), 2)
        self.assertTrue(all(view.timeout is None and view.is_persistent() for view in views))
        self.assertEqual({view.children[0].custom_id for view in views}, {tk.OPEN_ID, tk.CLOSE_ID})

    def test_button_opens_private_thread(self):
        cog, _ = self._cog()
        i = self._interaction()
        parent = MagicMock()
        parent.threads = []
        thread = MagicMock()
        thread.id = 555
        thread.mention = "<#555>"
        thread.guild = i.guild
        thread.add_user = AsyncMock()
        thread.send = AsyncMock()
        parent.create_thread = AsyncMock(return_value=thread)
        i.guild.get_channel.side_effect = lambda cid: parent if cid == 100 else None
        run(tk.TicketOpenView(cog).open.callback(i))
        kwargs = parent.create_thread.await_args.kwargs
        self.assertEqual(kwargs["type"], discord.ChannelType.private_thread)
        self.assertFalse(kwargs["invitable"])
        self.assertTrue(kwargs["name"].endswith("-42"))
        thread.add_user.assert_awaited_once_with(i.user)
        thread.send.assert_awaited()
        self.assertIsInstance(thread.send.await_args.kwargs["view"], tk.TicketCloseView)
        self.assertIn("<#555>", i.followup.send.await_args.args[0])

    def test_existing_ticket_is_reused(self):
        cog, _ = self._cog()
        i = self._interaction()
        parent = MagicMock()
        parent.threads = []
        parent.create_thread = AsyncMock()
        i.guild.get_channel.side_effect = lambda cid: parent if cid == 100 else None
        i.guild.active_threads = AsyncMock(return_value=[SimpleNamespace(
            id=9, name="ticket-ada-42", mention="<#9>", type=discord.ChannelType.private_thread,
            archived=False, locked=False, topic=None)])
        run(cog.open_ticket(i, reason=None))
        parent.create_thread.assert_not_awaited()
        self.assertIn("<#9>", i.followup.send.await_args.args[0])

    def test_forbidden_is_explained(self):
        cog, _ = self._cog()
        i = self._interaction()
        parent = MagicMock()
        parent.threads = []
        parent.create_thread = AsyncMock(side_effect=http_exc(discord.Forbidden))
        i.guild.get_channel.side_effect = lambda cid: parent if cid == 100 else None
        run(cog.open_ticket(i))
        self.assertIn("permission", i.followup.send.await_args.args[0].lower())

    def test_channel_mode_overwrites(self):
        cog, _ = self._cog("channel")
        i = self._interaction()
        category = SimpleNamespace(id=200, channels=[])
        i.guild.get_channel.side_effect = lambda cid: category if cid == 200 else None
        created = MagicMock()
        created.mention = "<#77>"
        created.guild = i.guild
        created.send = AsyncMock()
        i.guild.create_text_channel = AsyncMock(return_value=created)
        run(tk.TicketOpenView(cog).open.callback(i))
        kwargs = i.guild.create_text_channel.await_args.kwargs
        self.assertIs(kwargs["category"], category)
        self.assertEqual(kwargs["topic"], "botkit-ticket:42")
        overwrites = kwargs["overwrites"]
        self.assertIs(overwrites[i.guild.default_role].view_channel, False)
        self.assertIs(overwrites[i.user].view_channel, True)
        staff = i.guild.get_role.return_value
        self.assertIs(overwrites[staff].view_channel, True)
        self.assertIn("<#77>", i.followup.send.await_args.args[0])

    def test_staff_closes_thread_and_strangers_cannot(self):
        cog, _ = self._cog()
        i = self._interaction()
        channel = MagicMock()
        channel.name = "ticket-ada-42"
        channel.type = discord.ChannelType.private_thread
        channel.archived = False
        channel.locked = False
        channel.topic = None
        channel.send = AsyncMock()
        channel.edit = AsyncMock()
        i.channel = channel
        i.user.roles = [SimpleNamespace(id=8)]
        run(tk.TicketCloseView(cog).close.callback(i))
        self.assertIs(channel.edit.await_args.kwargs["archived"], True)
        self.assertIs(channel.edit.await_args.kwargs["locked"], True)
        stranger = self._interaction()
        stranger.channel = channel
        channel.edit.reset_mock()
        run(tk.TicketCloseView(cog).close.callback(stranger))
        channel.edit.assert_not_awaited()
        self.assertIn("staff", stranger.response.send_message.await_args.args[0].lower())

    def test_close_channel_removes_opener(self):
        cog, _ = self._cog("channel")
        i = self._interaction()
        i.user.roles = [SimpleNamespace(id=8)]
        member = SimpleNamespace(id=42)
        channel = MagicMock()
        channel.name = "ticket-ada-42"
        channel.type = discord.ChannelType.text
        channel.topic = "botkit-ticket:42"
        channel.archived = False
        channel.locked = False
        channel.send = AsyncMock()
        channel.edit = AsyncMock()
        channel.set_permissions = AsyncMock()
        channel.guild.get_member.return_value = member
        i.channel = channel
        run(cog.close_from_interaction(i, reason="done"))
        channel.set_permissions.assert_awaited_once()
        self.assertIs(channel.set_permissions.await_args.args[0], member)
        self.assertTrue(channel.edit.await_args.kwargs["name"].startswith("closed-"))
        self.assertEqual(channel.edit.await_args.kwargs["topic"], "botkit-ticket-closed:42")

    def test_setup_posts_panel(self):
        cog, _ = self._cog()
        i = self._interaction()
        i.channel.send = AsyncMock()
        run(tk.Tickets.ticket_setup.callback(cog, i))
        view = i.channel.send.await_args.kwargs["view"]
        self.assertEqual(view.children[0].custom_id, tk.OPEN_ID)
        self.assertTrue(view.is_persistent())
        i.response.send_message.assert_awaited()


class WelcomeFlow(unittest.TestCase):
    def test_render(self):
        text = wc.render_welcome("Hi {member} ({name}) in {server} #{count} {nope}",
                                 mention="<@1>", server="Demo", name="Ada", count="3")
        self.assertEqual(text, "Hi <@1> (Ada) in Demo #3 {nope}")
        self.assertEqual(wc.render_welcome("{member", mention="<@1>", server="Demo", name="Ada", count="3"), "{member")

    def test_join_posts_and_skips_bots_and_leaves(self):
        sent = {}

        async def send(content, **kwargs):
            sent["content"] = content
            sent["mentions"] = kwargs.get("allowed_mentions")

        guild = SimpleNamespace(id=9, name="Demo", member_count=4, get_channel=lambda cid: SimpleNamespace(send=send) if cid == 3 else None)
        bot = bot_for("welcome", {"channel_id": "3", "message": "Welcome {member} to {server} (#{count})"})
        cog = wc.Welcome(bot)
        self.assertIn(("on_member_join", "on_member_join"), wc.Welcome.__cog_listeners__)
        self.assertFalse(hasattr(cog, "on_member_remove"))
        member = SimpleNamespace(id=5, bot=False, mention="<@5>", name="ada", display_name="Ada", guild=guild)
        run(cog.on_member_join(member))
        self.assertEqual(sent["content"], "Welcome <@5> to Demo (#4)")
        self.assertIs(sent["mentions"].everyone, False)
        sent.clear()
        run(cog.on_member_join(SimpleNamespace(id=6, bot=True, mention="<@6>", name="bot", display_name="Bot", guild=guild)))
        self.assertEqual(sent, {})

    def test_missing_channel_is_quiet(self):
        bot = bot_for("welcome", {"channel_id": "3"})
        cog = wc.Welcome(bot)
        guild = SimpleNamespace(name="Demo", member_count=1, get_channel=lambda cid: None)
        run(cog.on_member_join(SimpleNamespace(id=5, bot=False, mention="<@5>", name="a", display_name="A", guild=guild)))


class LogHelpers(unittest.TestCase):
    def test_events_and_edit_filter(self):
        self.assertEqual(logs.normalize_events("all"), list(logs.LOG_EVENTS))
        self.assertEqual(logs.normalize_events("join, leave"), ["join", "leave"])
        with self.assertRaises(ValueError):
            logs.normalize_events("join,nope")
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t1 = t0 + timedelta(seconds=5)
        self.assertFalse(logs.should_log_edit("hi", "hi", None, t1))  # unfurl, same text
        self.assertFalse(logs.should_log_edit("hi", "hi", t0, None))  # no edited timestamp
        self.assertFalse(logs.should_log_edit("", "", t0, t0))  # unfurl of an edited message
        self.assertTrue(logs.should_log_edit("old", "new", t0, t1))
        self.assertTrue(logs.should_log_edit("", "", None, t1))  # edit with content hidden

    def test_audit_actor(self):
        now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)
        mod = SimpleNamespace(id=8)
        good = SimpleNamespace(extra=SimpleNamespace(channel=SimpleNamespace(id=50)),
                               target=SimpleNamespace(id=5), created_at=now - timedelta(seconds=4), user=mod)
        stale = SimpleNamespace(extra=SimpleNamespace(channel=SimpleNamespace(id=50)),
                                target=SimpleNamespace(id=5), created_at=now - timedelta(seconds=40), user=SimpleNamespace(id=2))
        other = SimpleNamespace(extra=SimpleNamespace(channel=SimpleNamespace(id=99)),
                                target=SimpleNamespace(id=5), created_at=now, user=SimpleNamespace(id=3))
        self.assertIs(logs.audit_delete_actor([stale, other, good], channel_id=50, author_id=5, now=now), mod)
        self.assertIsNone(logs.audit_delete_actor([other], channel_id=50, author_id=5, now=now))


class LogFlow(unittest.TestCase):
    def _cog(self, events="all", intent=False):
        bot = bot_for("server_logs", {"channel_id": "77", "events": events})
        bot.intents.message_content = intent
        return logs.ServerLogs(bot), bot

    def _guild(self, log_ch, source=None, entries=()):
        def get_channel(cid):
            if cid == 77:
                return log_ch
            if cid == 50:
                return source
            return None

        guild = SimpleNamespace(id=9, name="Demo", member_count=4, get_channel=get_channel,
                                audit_logs=MagicMock(return_value=AIter(entries)))
        return guild

    def test_join_leave_and_roles(self):
        log_ch = SimpleNamespace(send=AsyncMock())
        guild = self._guild(log_ch)
        cog, _ = self._cog()
        self.assertEqual(
            {name for name, _meth in logs.ServerLogs.__cog_listeners__},
            {"on_member_join", "on_member_remove", "on_raw_message_delete", "on_raw_message_edit", "on_member_update"},
        )
        member = SimpleNamespace(id=5, mention="<@5>", name="ada", guild=guild, roles=[
            SimpleNamespace(id=9, mention="@everyone"), SimpleNamespace(id=3, mention="<@&3>", name="Member")])
        run(cog.on_member_join(member))
        run(cog.on_member_remove(member))
        titles = [call.kwargs["embed"].title for call in log_ch.send.await_args_list]
        self.assertEqual(titles, ["Member joined", "Member left"])
        self.assertIn("<@&3>", field(log_ch.send.await_args_list[1].kwargs["embed"], "Roles"))
        before = SimpleNamespace(id=5, mention="<@5>", name="ada", guild=guild, roles=[SimpleNamespace(id=3, mention="<@&3>", name="Member")])
        after = SimpleNamespace(id=5, mention="<@5>", name="ada", guild=guild, roles=[
            SimpleNamespace(id=3, mention="<@&3>", name="Member"), SimpleNamespace(id=4, mention="<@&4>", name="News")])
        run(cog.on_member_update(before, after))
        embed = log_ch.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Roles updated")
        self.assertIn("<@&4>", field(embed, "Added"))
        self.assertEqual(field(embed, "Removed"), "—")
        before_count = log_ch.send.await_count
        run(cog.on_member_update(after, after))
        self.assertEqual(log_ch.send.await_count, before_count)

    def test_delete_uses_audit_log_without_requiring_content(self):
        log_ch = SimpleNamespace(send=AsyncMock())
        source = SimpleNamespace(id=50, mention="<#50>")
        mod = SimpleNamespace(id=8, mention="<@8>", name="mod")
        now = discord.utils.utcnow()
        entry = SimpleNamespace(extra=SimpleNamespace(channel=source), target=SimpleNamespace(id=5),
                                created_at=now, user=mod)
        guild = self._guild(log_ch, source, [entry])
        cog, bot = self._cog(intent=False)
        bot.get_guild.return_value = guild
        author = SimpleNamespace(id=5, mention="<@5>", name="ada", bot=False)
        cached = SimpleNamespace(author=author, content="", guild=guild)
        payload = SimpleNamespace(guild_id=9, channel_id=50, message_id=123, cached_message=cached)
        run(cog.on_raw_message_delete(payload))
        embed = log_ch.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Message deleted")
        self.assertEqual(field(embed, "Deleted by"), "<@8> (`mod`)")
        self.assertEqual(field(embed, "Content"), "(content unavailable)")
        self.assertEqual(field(embed, "Channel"), "<#50>")
        guild.audit_logs.assert_called_once()
        self.assertEqual(guild.audit_logs.call_args.kwargs["action"], discord.AuditLogAction.message_delete)

    def test_skips_log_channel_own_messages_and_disabled_events(self):
        log_ch = SimpleNamespace(send=AsyncMock())
        cog, bot = self._cog(events=["delete"])
        guild = self._guild(log_ch)
        bot.get_guild.return_value = guild
        own = SimpleNamespace(guild_id=9, channel_id=50, message_id=1,
                              cached_message=SimpleNamespace(author=SimpleNamespace(id=1), content="x", guild=guild))
        run(cog.on_raw_message_delete(own))
        in_log = SimpleNamespace(guild_id=9, channel_id=77, message_id=2, cached_message=None)
        run(cog.on_raw_message_delete(in_log))
        log_ch.send.assert_not_awaited()
        join = SimpleNamespace(id=5, mention="<@5>", name="a", guild=guild, roles=[])
        run(cog.on_member_join(join))
        log_ch.send.assert_not_awaited()

    def test_edit_without_message_content(self):
        log_ch = SimpleNamespace(send=AsyncMock())
        guild = self._guild(log_ch, SimpleNamespace(id=50, mention="<#50>"))
        cog, bot = self._cog(intent=False)
        bot.get_guild.return_value = guild
        payload = SimpleNamespace(
            guild_id=9, channel_id=50, message_id=8, cached_message=None,
            data={"edited_timestamp": "2026-01-02T00:00:00+00:00", "content": "",
                  "author": {"id": "5", "username": "ada"}},
        )
        run(cog.on_raw_message_edit(payload))
        embed = log_ch.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Message edited")
        self.assertIn("/9/50/8", field(embed, "Jump"))
        self.assertEqual(field(embed, "Before"), "(content unavailable)")
        self.assertEqual(field(embed, "After"), "(content unavailable)")
        unfurl = SimpleNamespace(guild_id=9, channel_id=50, message_id=8, cached_message=None,
                                 data={"content": "hello", "embeds": [{}]})
        run(cog.on_raw_message_edit(unfurl))
        self.assertEqual(log_ch.send.await_count, 1)


class RoleMenuFlow(unittest.TestCase):
    def _cog(self, style="buttons"):
        bot = bot_for("rolemenu", {
            "style": style,
            "title": "Roles",
            "text": "Pick",
            "roles": [
                {"role_id": "5", "label": "News", "emoji": "📢"},
                {"role_id": "6", "label": "Events"},
            ],
        })
        return rm.RoleMenu(bot), bot

    def _interaction(self, role_ids):
        i = MagicMock()
        i.user.roles = [SimpleNamespace(id=rid) for rid in role_ids]
        i.user.add_roles = AsyncMock()
        i.user.remove_roles = AsyncMock()
        i.response.defer = AsyncMock()
        i.response.send_message = AsyncMock()
        i.followup.send = AsyncMock()
        roles = {5: SimpleNamespace(id=5, name="News"), 6: SimpleNamespace(id=6, name="Events")}
        i.guild.get_role.side_effect = lambda rid: roles.get(rid)
        return i

    def test_diff_and_change(self):
        self.assertEqual(rm.selection_diff([5, 9], [5, 6], ["6"]), ({6}, {5}))
        self.assertEqual(rm.summarize([(6, "added"), (5, "removed")], {5: "News", 6: "Events"}),
                         "Added: Events\nRemoved: News")
        role = SimpleNamespace(id=5)
        member = MagicMock(roles=[])
        member.add_roles = AsyncMock()
        self.assertEqual(run(rm.change_role(member, role, True)), "added")
        member.roles = [role]
        self.assertEqual(run(rm.change_role(member, role, True)), "already")
        member.remove_roles = AsyncMock()
        self.assertEqual(run(rm.change_role(member, role, False)), "removed")
        member.roles = []
        member.add_roles = AsyncMock(side_effect=http_exc(discord.Forbidden))
        self.assertEqual(run(rm.change_role(member, role, True)), "forbidden")
        self.assertEqual(run(rm.change_role(member, None, True)), "missing_role")

    def test_persistent_buttons_toggle(self):
        cog, bot = self._cog()
        run(cog.cog_load())
        view = bot.add_view.call_args.args[0]
        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent())
        self.assertEqual([child.custom_id for child in view.children], ["botkit:rolemenu:5", "botkit:rolemenu:6"])
        i = self._interaction([])
        run(view.children[0].callback(i))
        i.user.add_roles.assert_awaited_once()
        self.assertIn("added", i.followup.send.await_args.args[0].lower())
        i = self._interaction([5])
        run(view.children[0].callback(i))
        i.user.remove_roles.assert_awaited_once()
        self.assertIn("removed", i.followup.send.await_args.args[0].lower())
        i = self._interaction([])
        run(cog.handle_button(i, 99))
        self.assertIn("isn't on this menu", i.response.send_message.await_args.args[0])

    def test_select_applies_diff_and_setup(self):
        cog, _ = self._cog("select")
        view = rm.RoleMenuView(cog)
        select = view.children[0]
        self.assertEqual(select.custom_id, rm.SELECT_ID)
        self.assertEqual(select.min_values, 0)
        self.assertEqual(select.max_values, 2)
        self.assertTrue(view.is_persistent())
        select._values = ["6"]
        i = self._interaction([5])
        run(select.callback(i))
        i.user.add_roles.assert_awaited_once()
        i.user.remove_roles.assert_awaited_once()
        self.assertIn("Added: Events", i.followup.send.await_args.args[0])
        self.assertIn("Removed: News", i.followup.send.await_args.args[0])
        i = self._interaction([])
        i.channel.send = AsyncMock()
        run(rm.RoleMenu.rolemenu_setup.callback(cog, i))
        posted = i.channel.send.await_args.kwargs["view"]
        self.assertEqual(posted.children[0].custom_id, rm.SELECT_ID)
        i.response.send_message.assert_awaited()


class IntentFlags(unittest.TestCase):
    def _flags(self, cfg):
        bot_dir = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            (tmp / ".env").write_text("DISCORD_TOKEN=not-a-real-token\n", encoding="utf-8")
            env = dict(os.environ, BOTKIT_CONFIG=str(tmp / "config.json"), BOTKIT_ENV=str(tmp / ".env"))
            env.pop("DISCORD_TOKEN", None)
            code = ("import sys; sys.path.insert(0, sys.argv[1]); import bot; "
                    "print(int(bool(bot.intents.members)), int(bool(bot.intents.message_content)))")
            result = subprocess.run([sys.executable, "-c", code, str(bot_dir)], env=env,
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_members_intent_follows_welcome_and_logs_only(self):
        self.assertEqual(self._flags({"features": [], "message_content_intent": False}), "0 0")
        self.assertEqual(self._flags({"features": ["translate"], "message_content_intent": True}), "0 1")
        self.assertEqual(self._flags({"features": ["welcome"], "message_content_intent": False}), "1 0")
        self.assertEqual(self._flags({"features": ["server_logs"], "members_intent": False, "message_content_intent": False}), "1 0")
        self.assertEqual(self._flags({"features": ["tickets", "rolemenu"], "message_content_intent": False}), "0 0")
        self.assertEqual(self._flags({"features": ["pricealerts"], "members_intent": False, "message_content_intent": False}), "0 0")


if __name__ == "__main__":
    unittest.main()
