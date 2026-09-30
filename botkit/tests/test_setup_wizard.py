"""Offline tests for setup_wizard.py. All Discord calls are mocked: no token, no network."""
import io, json, os, sys, tempfile, unittest, urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import setup_wizard as sw

FAKE_TOKEN = "MTAx.fake.tokenvalue"
GID = "900000000000000001"
CHANNELS = [
    {"id": "11", "name": "general", "type": 0, "position": 0},
    {"id": "12", "name": "mods", "type": 0, "position": 2},
    {"id": "13", "name": "Voice", "type": 2, "position": 1},
    {"id": "14", "name": "news", "type": 5, "position": 1},
    {"id": "15", "name": "Category", "type": 4, "position": 0},
]
ROLES = [
    {"id": GID, "name": "@everyone", "position": 0},
    {"id": "21", "name": "Member", "position": 1},
    {"id": "22", "name": "BotKit", "position": 3, "managed": True},
    {"id": "23", "name": "Verified", "position": 2},
]


class FakeAPI:
    """Stands in for DiscordAPI; records calls."""
    instances = []

    def __init__(self, token, guilds=None):
        self.token, self.calls = token, []
        self._guilds = guilds if guilds is not None else [{"id": GID, "name": "Demo"}, {"id": "2", "name": "Other"}]
        FakeAPI.instances.append(self)

    def me(self):
        self.calls.append("me"); return {"id": "555", "username": "BotKit", "bot": True}

    def application(self):
        self.calls.append("app"); return {"id": "555"}

    def guilds(self):
        self.calls.append("guilds"); return self._guilds

    def channels(self, gid):
        self.calls.append(("channels", gid)); return CHANNELS

    def roles(self, gid):
        self.calls.append(("roles", gid)); return ROLES

    def create_role(self, gid, name="Verified"):
        self.calls.append(("create_role", gid, name)); return {"id": "99", "name": name}


FULL_ANSWERS = {
    "token": FAKE_TOKEN,
    "guild": "Demo",
    "features": "all",
    "scheduler": {"timezone": "Europe/Berlin",
                  "posts": [{"channel": "#general", "days": "weekdays", "time": "9:05", "message": "hi"}]},
    "mcstatus": {"address": "play.example.com", "channel": "news", "interval_seconds": 10},
    "modflag": {"mod_channel": "mods"},
    "verify": {"role": "Verified", "captcha": False},
    "translate": {"libretranslate_url": "http://127.0.0.1:5000/translate", "channels": {"general": "EN"},
                  "check_url": True},
}


def build(answers, **kw):
    kw.setdefault("url_checker", lambda url: (True, "ok"))
    out = []
    cfg, token, info = sw.build(sw.FromAnswers(answers, env={}), api_factory=FakeAPI, out=out.append, **kw)
    return cfg, token, info, out


class Helpers(unittest.TestCase):
    def test_filters(self):
        self.assertEqual([c["name"] for c in sw.text_channels(CHANNELS)], ["general", "news", "mods"])
        self.assertEqual([r["name"] for r in sw.assignable_roles(ROLES, GID)], ["Verified", "Member"])

    def test_invite_url(self):
        u = sw.invite_url("555")
        self.assertIn("client_id=555", u)
        self.assertIn("scope=bot+applications.commands", u)
        self.assertIn("permissions=268528640", u)

    def test_days_time_tz_features(self):
        self.assertEqual(sw.parse_days("weekdays"), ["mon", "tue", "wed", "thu", "fri"])
        self.assertEqual(sw.parse_days("Monday, fri"), ["mon", "fri"])
        self.assertEqual(sw.parse_days(""), sw.DAYS)
        with self.assertRaises(sw.WizardError): sw.parse_days("funday")
        self.assertEqual(sw.valid_time("9:05"), "09:05")
        with self.assertRaises(sw.WizardError): sw.valid_time("25:00")
        self.assertEqual(sw.valid_timezone("America/New_York"), "America/New_York")
        with self.assertRaises(sw.WizardError): sw.valid_timezone("Mars/Base")
        self.assertEqual(sw.parse_features("1, translate"), ["scheduler", "translate"])
        self.assertEqual(len(sw.parse_features("all")), 6)
        with self.assertRaises(sw.WizardError): sw.parse_features("nope")

    def test_match_item(self):
        items = sw.text_channels(CHANNELS)
        self.assertEqual(sw.match_item(items, "12")["name"], "mods")
        self.assertEqual(sw.match_item(items, "#General")["id"], "11")
        self.assertEqual(sw.match_item(items, 2)["name"], "news")
        with self.assertRaises(sw.WizardError): sw.match_item(items, "missing")

    def test_token_shape(self):
        self.assertTrue(sw.looks_like_token(FAKE_TOKEN))
        self.assertFalse(sw.looks_like_token("abc"))
        self.assertFalse(sw.looks_like_token(""))


class DiscordAPIHttp(unittest.TestCase):
    def _resp(self, payload):
        r = MagicMock(); r.read.return_value = json.dumps(payload).encode()
        r.__enter__.return_value = r
        return r

    def test_request_sends_bot_auth_and_parses(self):
        seen = {}
        def opener(req, timeout):
            seen["req"] = req; return self._resp({"id": "1", "bot": True})
        api = sw.DiscordAPI(FAKE_TOKEN, opener=opener)
        self.assertEqual(api.me()["id"], "1")
        req = seen["req"]
        self.assertEqual(req.full_url, sw.API + "/users/@me")
        self.assertEqual(req.get_header("Authorization"), f"Bot {FAKE_TOKEN}")
        self.assertTrue(req.get_header("User-agent").startswith("DiscordBot"))

    def test_create_role_posts(self):
        seen = {}
        def opener(req, timeout):
            seen["req"] = req; return self._resp({"id": "99"})
        sw.DiscordAPI(FAKE_TOKEN, opener=opener).create_role(GID)
        self.assertEqual(seen["req"].get_method(), "POST")
        self.assertEqual(json.loads(seen["req"].data)["name"], "Verified")

    def test_401_is_friendly_and_hides_token(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        with self.assertRaises(sw.WizardError) as cm:
            sw.DiscordAPI(FAKE_TOKEN, opener=opener).me()
        self.assertIn("401", str(cm.exception))
        self.assertNotIn(FAKE_TOKEN, str(cm.exception))

    def test_429_retries_then_succeeds(self):
        calls, slept = [], []
        def opener(req, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "0.5"}, None)
            return self._resp([{"id": "1", "name": "g"}])
        api = sw.DiscordAPI(FAKE_TOKEN, opener=opener, sleep=slept.append)
        self.assertEqual(api.guilds()[0]["id"], "1")
        self.assertEqual((len(calls), slept), (2, [0.5]))

    def test_translate_url_check(self):
        ok, msg = sw.check_translate_url("http://x/translate", opener=lambda req, timeout: self._resp({"translatedText": "Hello"}))
        self.assertTrue(ok); self.assertIn("Hello", msg)
        def boom(req, timeout): raise urllib.error.URLError("refused")
        self.assertFalse(sw.check_translate_url("http://x/translate", opener=boom)[0])


class BuildNonInteractive(unittest.TestCase):
    def setUp(self):
        FakeAPI.instances.clear()

    def test_full_config(self):
        cfg, token, info, out = build(FULL_ANSWERS)
        self.assertEqual(token, FAKE_TOKEN)
        self.assertEqual(cfg["guild_id"], GID)
        self.assertEqual(cfg["features"], sw.FEATURES)
        self.assertEqual(cfg["scheduler"], {"timezone": "Europe/Berlin", "posts": [
            {"channel_id": "11", "days": ["mon", "tue", "wed", "thu", "fri"], "time": "09:05", "message": "hi"}]})
        self.assertEqual(cfg["mcstatus"], {"address": "play.example.com", "channel_id": "14", "interval_seconds": 30})
        self.assertEqual(cfg["modflag"], {"mod_channel_id": "12"})
        self.assertEqual(cfg["verify"]["role_id"], "23")
        self.assertEqual(cfg["verify"]["log_channel_id"], "12")  # falls back to mod channel
        self.assertFalse(cfg["verify"]["captcha"])
        self.assertEqual(cfg["translate"]["channels"], {"11": "en"})
        self.assertEqual(cfg["translate"]["libretranslate_url"], "http://127.0.0.1:5000/translate")
        self.assertTrue(cfg["message_content_intent"])
        self.assertEqual(info["client_id"], "555")
        self.assertIn("permissions=268528640", info["invite_url"])
        self.assertFalse(any(FAKE_TOKEN in line for line in out))
        self.assertNotIn(("create_role", GID, "Verified"), FakeAPI.instances[0].calls)

    def test_config_loads_in_bot_cogs(self):
        """The generated translate/verify sections are accepted by the real cogs."""
        from types import SimpleNamespace
        from cogs import translate as tr, verify as vf
        cfg, *_ = build(FULL_ANSWERS)
        bot = SimpleNamespace(config=cfg, tree=MagicMock(), get_channel=MagicMock(), add_view=MagicMock())
        self.assertEqual(tr.Translate(bot).backend.url, "http://127.0.0.1:5000/translate")
        self.assertEqual(vf.Verify(bot).role_id, 23)

    def test_create_role(self):
        a = dict(FULL_ANSWERS, features=["verify"], verify={"role": "create"})
        cfg, *_ = build(a)
        self.assertEqual(cfg["verify"]["role_id"], "99")
        self.assertIn(("create_role", GID, "Verified"), FakeAPI.instances[0].calls)
        with self.assertRaises(sw.WizardError):  # read-only mode refuses to create
            build(a, allow_create_role=False)

    def test_minimal_features_and_guild_by_number(self):
        cfg, *_ = build({"token": FAKE_TOKEN, "guild": 2, "features": ["purge_mine"]})
        self.assertEqual(cfg, {"guild_id": "2", "features": ["purge_mine"], "message_content_intent": False})

    def test_errors(self):
        with self.assertRaises(sw.WizardError):
            build({"token": "bad", "guild": 1})
        with self.assertRaises(sw.WizardError):  # two guilds, none chosen
            build({"token": FAKE_TOKEN, "features": ["purge_mine"]})
        with self.assertRaises(sw.WizardError):
            build({"token": FAKE_TOKEN, "guild": 1, "features": ["mcstatus_alert"]})
        with self.assertRaises(sw.WizardError):
            sw.build(sw.FromAnswers({"token": FAKE_TOKEN}, env={}), out=lambda *_: None,
                     api_factory=lambda t: FakeAPI(t, guilds=[]))

    def test_token_from_env(self):
        src = sw.FromAnswers({"token_env": "MY_TOK"}, env={"MY_TOK": FAKE_TOKEN})
        self.assertEqual(src.token(), FAKE_TOKEN)


class BuildInteractive(unittest.TestCase):
    def test_interactive_session(self):
        answers = iter([
            "1",            # guild -> Demo
            "3,4,5,6",      # features: modflag, purge_mine, verify, translate
            "3",            # mod channel -> #mods (sorted: general, news, mods)
            "c",            # verify: create role
            "",             # log channel -> none (falls back to mod channel)
            "n",            # captcha
            "", "",         # title, text defaults
            "",             # backend default
            "",             # default target
            "",             # URL default
            "n",            # don't test URL
            "y", "1", "es", # auto-translate #general -> es
            "n",            # no more channels
        ])
        printed = []
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_TOKEN", None)
            src = sw.Interactive(inp=lambda p: next(answers), secret=lambda p: FAKE_TOKEN, out=printed.append)
            FakeAPI.instances.clear()
            cfg, token, info = sw.build(src, api_factory=FakeAPI, out=printed.append)
        self.assertEqual(token, FAKE_TOKEN)
        self.assertEqual(cfg["features"], ["modflag", "purge_mine", "verify", "translate"])
        self.assertEqual(cfg["modflag"]["mod_channel_id"], "12")
        self.assertEqual(cfg["verify"]["role_id"], "99")
        self.assertEqual(cfg["translate"]["channels"], {"11": "es"})
        self.assertFalse(any(FAKE_TOKEN in str(p) for p in printed))
        self.assertTrue(any("#general" in str(p) for p in printed))


class MainWrites(unittest.TestCase):
    def test_main_writes_config_and_env_only_token_in_env(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "answers.json").write_text(json.dumps(FULL_ANSWERS))
            (d / ".env").write_text("DISCORD_TOKEN=old\nDEEPL_API_KEY=keepme\n")
            (d / "config.json").write_text('{"old": true}')
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = sw.main(["--answers", str(d / "answers.json"), "--config-out", str(d / "config.json"),
                              "--env-out", str(d / ".env")], api_factory=FakeAPI, url_checker=lambda u: (True, "ok"))
            self.assertEqual(rc, 0)
            cfg = json.loads((d / "config.json").read_text())
            self.assertEqual(cfg["guild_id"], GID)
            self.assertEqual(json.loads((d / "config.json.bak").read_text()), {"old": True})
            env = (d / ".env").read_text()
            self.assertIn(f"DISCORD_TOKEN={FAKE_TOKEN}", env)
            self.assertIn("DEEPL_API_KEY=keepme", env)
            self.assertNotIn("=old", env)
            self.assertNotIn(FAKE_TOKEN, (d / "config.json").read_text())
            self.assertNotIn(FAKE_TOKEN, buf.getvalue())
            self.assertIn("permissions=268528640", buf.getvalue())
            if os.name == "posix":
                self.assertEqual(os.stat(d / ".env").st_mode & 0o777, 0o600)

    def test_main_error_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.json"; p.write_text(json.dumps({"token": "bad"}))
            with patch("sys.stderr", io.StringIO()):
                self.assertEqual(sw.main(["--answers", str(p), "--config-out", str(Path(d) / "c.json"), "--no-env"]), 2)
            self.assertFalse((Path(d) / "c.json").exists())


if __name__ == "__main__":
    unittest.main()
