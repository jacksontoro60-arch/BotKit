"""Offline tests for translate/verify core logic. No token, no network.
Run from the kit folder:  python -m unittest discover -s tests -v
"""
import asyncio, os, sys, unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import discord
from cogs import translate as tr, verify as vf


def run(coro):
    return asyncio.run(coro)


def fake_bot(cfg):
    return SimpleNamespace(config=cfg, tree=MagicMock(), get_channel=MagicMock(return_value=None), add_view=MagicMock())


class FakeBackend(tr.Backend):
    def __init__(self, out, src):
        self.out, self.src, self.calls = out, src, []

    async def translate(self, text, target):
        self.calls.append((text, target))
        return self.out, self.src


def fake_message(content, channel_id, bot=False):
    m = MagicMock()
    m.content, m.channel.id, m.author.bot = content, channel_id, bot
    m.reply = AsyncMock()
    return m


class TranslateRouting(unittest.TestCase):
    def test_target_for_channel(self):
        ch = {"111": "EN", "222": "de"}
        self.assertEqual(tr.target_for_channel(ch, 111), "en")
        self.assertEqual(tr.target_for_channel(ch, "222"), "de")
        self.assertIsNone(tr.target_for_channel(ch, 333))
        self.assertIsNone(tr.target_for_channel({}, 111))

    def test_same_language(self):
        self.assertTrue(tr.same_language("en-US", "en"))
        self.assertFalse(tr.same_language("fr", "en"))
        self.assertFalse(tr.same_language(None, "en"))

    def test_backend_selection_and_deepl_endpoint(self):
        f = AsyncMock()
        self.assertIsInstance(tr.make_backend({}, f, env={}), tr.LibreTranslateBackend)
        lt = tr.make_backend({"libretranslate_url": "http://127.0.0.1:5000/translate"}, f, env={"LIBRETRANSLATE_API_KEY": "k"})
        self.assertEqual((lt.url, lt.api_key), ("http://127.0.0.1:5000/translate", "k"))
        d = tr.make_backend({"backend": "deepl"}, f, env={"DEEPL_API_KEY": "abc:fx"})
        self.assertIn("api-free.deepl.com", d.url)
        self.assertIn("//api.deepl.com", tr.deepl_endpoint("abc"))
        with self.assertRaises(tr.TranslateError):
            tr.make_backend({"backend": "deepl"}, f, env={})
        with self.assertRaises(tr.TranslateError):
            tr.make_backend({"backend": "nope"}, f, env={})

    def _cog(self, out, src):
        bot = fake_bot({"translate": {"channels": {"111": "en"}}})
        cog = tr.Translate(bot)
        cog.backend = FakeBackend(out, src)
        return cog

    def test_on_message_routes_and_replies(self):
        cog = self._cog("Hello", "es")
        cog.bot.tree.add_command.assert_called_once()
        m = fake_message("Hola", 111)
        run(cog.on_message(m))
        self.assertEqual(cog.backend.calls, [("Hola", "en")])
        m.reply.assert_awaited_once()
        self.assertIn("Hello", m.reply.call_args.args[0])

    def test_on_message_skips(self):
        cog = self._cog("Hello", "en")
        run(cog.on_message(fake_message("Hello", 111)))            # already English -> no reply
        run(cog.on_message(fake_message("Hola", 999)))             # channel not configured
        run(cog.on_message(fake_message("Hola", 111, bot=True)))   # bots ignored
        self.assertEqual(len(cog.backend.calls), 1)

    def test_backend_error_is_swallowed(self):
        cog = self._cog("x", "es")
        cog.backend.translate = AsyncMock(side_effect=tr.TranslateError("down"))
        m = fake_message("Hola", 111)
        run(cog.on_message(m))
        m.reply.assert_not_awaited()


def http_exc(cls):
    return cls(MagicMock(status=403, reason="x"), "nope")


class VerifyLogic(unittest.TestCase):
    def test_captcha(self):
        import random
        q, a = vf.make_captcha(random.Random(1))
        x, y = [int(t) for t in q.replace("What is ", "").rstrip("?").split(" + ")]
        self.assertEqual(x + y, a)
        self.assertTrue(vf.check_answer(f"  {a} ", a))
        self.assertTrue(vf.check_answer(f"it's {a}", a))
        self.assertFalse(vf.check_answer("abc", a))
        self.assertFalse(vf.check_answer(str(a + 1), a))
        self.assertFalse(vf.check_answer(None, a))

    def test_assign_role_paths(self):
        role = SimpleNamespace(id=5)
        m = MagicMock(roles=[]); m.add_roles = AsyncMock()
        self.assertEqual(run(vf.assign_role(m, role)), "added")
        m.add_roles.assert_awaited_once()
        self.assertEqual(run(vf.assign_role(MagicMock(roles=[SimpleNamespace(id=5)]), role)), "already")
        self.assertEqual(run(vf.assign_role(m, None)), "missing_role")
        m2 = MagicMock(roles=[]); m2.add_roles = AsyncMock(side_effect=http_exc(discord.Forbidden))
        self.assertEqual(run(vf.assign_role(m2, role)), "forbidden")

    def _cog(self, captcha):
        bot = fake_bot({"verify": {"role_id": "5", "log_channel_id": "77", "captcha": captcha}})
        log_ch = MagicMock(); log_ch.send = AsyncMock()
        bot.get_channel.return_value = log_ch
        return vf.Verify(bot), log_ch

    def _interaction(self, has_role=False):
        role = SimpleNamespace(id=5)
        i = MagicMock()
        i.guild.get_role.return_value = role
        i.user.roles = [role] if has_role else []
        i.user.add_roles = AsyncMock()
        i.response.send_message = AsyncMock()
        i.response.send_modal = AsyncMock()
        return i

    def test_persistent_view_registered(self):
        cog, _ = self._cog(False)
        run(cog.cog_load())
        view = cog.bot.add_view.call_args.args[0]
        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent())
        self.assertEqual(view.children[0].custom_id, vf.CUSTOM_ID)

    def test_button_no_captcha_grants_and_logs(self):
        cog, log_ch = self._cog(False)
        i = self._interaction()
        async def go():
            view = vf.VerifyView(cog)
            await view.verify.callback(i)
        run(go())
        i.user.add_roles.assert_awaited_once()
        i.response.send_message.assert_awaited_once()
        log_ch.send.assert_awaited_once()

    def test_button_with_captcha_opens_modal(self):
        cog, _ = self._cog(True)
        i = self._interaction()
        async def go():
            await vf.VerifyView(cog).verify.callback(i)
        run(go())
        i.response.send_modal.assert_awaited_once()
        i.user.add_roles.assert_not_awaited()

    def test_modal_right_and_wrong(self):
        cog, log_ch = self._cog(True)
        async def go(answer_text):
            i = self._interaction()
            modal = vf.CaptchaModal(cog, "What is 2 + 3?", 5)
            modal.reply._value = answer_text
            await modal.on_submit(i)
            return i
        ok = run(go("5")); bad = run(go("4"))
        ok.user.add_roles.assert_awaited_once()
        bad.user.add_roles.assert_not_awaited()
        self.assertEqual(log_ch.send.await_count, 2)

    def test_already_verified(self):
        cog, _ = self._cog(False)
        i = self._interaction(has_role=True)
        async def go():
            await vf.VerifyView(cog).verify.callback(i)
        run(go())
        i.user.add_roles.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
