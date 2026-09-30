"""BotKit setup wizard: builds config.json and .env for a new client.

    python setup_wizard.py                          # interactive
    python setup_wizard.py --answers answers.json   # non-interactive (for testing / automation)

Interactive mode asks for the bot token with hidden input and writes it to .env ONLY.
It never prints the token. It checks the token against the Discord API, then lets you pick
the server, channels and roles by number, and writes config.json.

Non-interactive answers file (every key is optional except the ones the chosen features need):
{
  "token_env": "DISCORD_TOKEN",           # read the token from this env var (or use "token": "...")
  "guild": "123..." | "Server name" | 1,  # id, exact name, or 1-based number from the list
  "features": ["scheduler", "modflag", ...],
  "scheduler": {"timezone": "America/New_York",
                "posts": [{"channel": "general", "days": "mon,fri", "time": "09:00", "message": "hi"}]},
  "mcstatus": {"address": "play.example.com", "channel": "alerts", "interval_seconds": 60},
  "modflag": {"mod_channel": "mods"},
  "verify": {"role": "Verified" | "123..." | 2 | "create", "log_channel": "mods" | null,
             "captcha": true, "title": "...", "text": "..."},
  "translate": {"backend": "libretranslate", "libretranslate_url": "http://127.0.0.1:5000/translate",
                "default_target": "en", "channels": {"general": "en"}, "check_url": false}
}
Channels and roles can be given as an ID, a name, or a 1-based number from the wizard's list.
"""
import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/botkit, 1.0) BotKitSetupWizard"
FEATURES = ["scheduler", "mcstatus_alert", "modflag", "purge_mine", "verify", "translate"]
FEATURE_HELP = {
    "scheduler": "scheduled announcement posts",
    "mcstatus_alert": "Minecraft server online/offline alerts",
    "modflag": "right-click 'Flag for mods'",
    "purge_mine": "/purge_mine: members delete their own messages",
    "verify": "Verify button that grants a role",
    "translate": "auto-translate channels + right-click Translate",
}
# View Channel, Send Messages, Manage Messages, Embed Links, Read Message History, Manage Roles
INVITE_PERMISSIONS = 268528640
TEXT_CHANNEL_TYPES = {0, 5}  # text, announcement
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class WizardError(Exception):
    pass


# ---------------------------------------------------------------- Discord REST (read-mostly)
class DiscordAPI:
    """Tiny urllib client. `opener` is injectable so tests never touch the network."""

    def __init__(self, token, opener=None, base=API, sleep=None):
        import time
        self._token = token
        self._sleep = sleep or time.sleep
        self._open = opener or urllib.request.urlopen
        self.base = base

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bot {self._token}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        })
        for attempt in range(4):
            try:
                with self._open(req, timeout=15) as r:
                    raw = r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    self._sleep(self._retry_after(e))
                    continue
                self._raise(e, method, path)
            except urllib.error.URLError as e:
                raise WizardError(f"Could not reach Discord: {e.reason}") from None
        return json.loads(raw) if raw else None

    @staticmethod
    def _retry_after(e):
        try:
            return min(10.0, float((e.headers or {}).get("Retry-After") or 1))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _raise(e, method, path):
        if e.code == 401:
            raise WizardError("Discord rejected the token (401 Unauthorized). Copy it again from "
                              "Developer Portal > Bot > Reset Token.") from None
        if e.code == 403:
            raise WizardError(f"Discord says the bot lacks permission for {method} {path} (403).") from None
        if e.code == 429:
            raise WizardError("Discord rate-limited the wizard (429). Wait a minute and try again.") from None
        raise WizardError(f"Discord API error {e.code} on {method} {path}") from None

    def me(self):
        return self.request("GET", "/users/@me")

    def application(self):
        return self.request("GET", "/oauth2/applications/@me")

    def guilds(self):
        return self.request("GET", "/users/@me/guilds")

    def channels(self, guild_id):
        return self.request("GET", f"/guilds/{guild_id}/channels")

    def roles(self, guild_id):
        return self.request("GET", f"/guilds/{guild_id}/roles")

    def create_role(self, guild_id, name="Verified"):
        return self.request("POST", f"/guilds/{guild_id}/roles", {"name": name, "permissions": "0"})


def text_channels(channels):
    chans = [c for c in channels if c.get("type") in TEXT_CHANNEL_TYPES]
    return sorted(chans, key=lambda c: (c.get("position", 0), c.get("name", "")))


def assignable_roles(roles, guild_id):
    """Skip @everyone (id == guild id) and managed roles (bot/integration roles can't be granted)."""
    rs = [r for r in roles if str(r["id"]) != str(guild_id) and not r.get("managed")]
    return sorted(rs, key=lambda r: -r.get("position", 0))


def invite_url(client_id, permissions=INVITE_PERMISSIONS):
    q = urllib.parse.urlencode({"client_id": client_id, "scope": "bot applications.commands",
                                "permissions": permissions})
    return f"https://discord.com/oauth2/authorize?{q}"


def looks_like_token(token):
    return bool(token) and token.count(".") == 2 and " " not in token


def parse_days(s):
    if isinstance(s, list):
        items = s
    else:
        s = (s or "").strip().lower()
        if s in ("", "all", "daily", "every day"):
            return list(DAYS)
        if s == "weekdays":
            return DAYS[:5]
        if s == "weekends":
            return DAYS[5:]
        items = re.split(r"[,\s]+", s)
    out = []
    for d in items:
        d = str(d).strip().lower()[:3]
        if d not in DAYS:
            raise WizardError(f"unknown day {d!r} (use mon,tue,wed,thu,fri,sat,sun)")
        if d not in out:
            out.append(d)
    if not out:
        raise WizardError("pick at least one day")
    return out


def valid_time(s):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(s).strip())
    if not m:
        raise WizardError(f"time must be HH:MM (24 h), got {s!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def valid_timezone(tz):
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(tz)
    except Exception:
        raise WizardError(f"unknown time zone {tz!r} (use an IANA name like America/New_York)") from None
    return tz


def check_translate_url(url, opener=None):
    """POST a tiny test translation. Returns (ok, message). Never raises."""
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(url, method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"q": "Hola", "source": "auto", "target": "en",
                                                  "format": "text"}).encode())
    try:
        with opener(req, timeout=15) as r:
            data = json.loads(r.read() or b"{}")
        if "translatedText" in data:
            return True, f"OK ('Hola' -> {data['translatedText']!r})"
        return False, f"unexpected reply: {str(data)[:120]}"
    except Exception as e:  # noqa: BLE001 - reported to the user, not fatal
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- question sources
class Interactive:
    def __init__(self, inp=input, secret=getpass.getpass, out=print):
        self.inp, self.secret, self.out = inp, secret, out

    def token(self):
        env = os.environ.get("DISCORD_TOKEN")
        if env and self.yes("Use the DISCORD_TOKEN already set in the environment?", True):
            return env
        return self.secret("Bot token (input hidden): ").strip()

    def ask(self, key, prompt, default=None):
        suffix = f" [{default}]" if default not in (None, "") else ""
        v = self.inp(f"{prompt}{suffix}: ").strip()
        return v if v else default

    def yes(self, prompt, default=True, key=None):
        d = "Y/n" if default else "y/N"
        while True:
            v = self.inp(f"{prompt} [{d}]: ").strip().lower()
            if not v:
                return default
            if v in ("y", "yes"):
                return True
            if v in ("n", "no"):
                return False

    def pick(self, key, prompt, items, label, allow_none=False, extra=None):
        """items: list of dicts. extra: optional (token, text) choice, e.g. ('c', 'create a Verified role')."""
        if not items and not extra:
            raise WizardError(f"nothing to choose for: {prompt}")
        self.out(f"\n{prompt}")
        for i, it in enumerate(items, 1):
            self.out(f"  {i:>2}. {label(it)}")
        if extra:
            self.out(f"   {extra[0]}. {extra[1]}")
        if allow_none:
            self.out("   (Enter = none)")
        while True:
            v = self.inp("Number: ").strip().lower()
            if not v and allow_none:
                return None
            if extra and v == extra[0]:
                return extra[0]
            if v.isdigit() and 1 <= int(v) <= len(items):
                return items[int(v) - 1]
            self.out("  Please enter one of the numbers above.")

    def features(self):
        self.out("\nFeatures:")
        for i, f in enumerate(FEATURES, 1):
            self.out(f"  {i}. {f:<15} {FEATURE_HELP[f]}")
        while True:
            v = self.inp("Enable which? (numbers or names, comma-separated, 'all') [all]: ").strip().lower()
            try:
                return parse_features(v or "all")
            except WizardError as e:
                self.out(f"  {e}")

    def retry(self, fn, *a):
        while True:
            try:
                return fn(*a)
            except WizardError as e:
                self.out(f"  {e}")
                a = (self.inp("Try again: ").strip(),)


class FromAnswers:
    """Answers come from a JSON dict. Missing keys fall back to defaults; missing required picks fail."""

    def __init__(self, answers, env=os.environ, out=print):
        self.a, self.env, self.out = answers, env, out

    def _get(self, key, default=None):
        cur = self.a
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def token(self):
        if self.a.get("token"):
            return self.a["token"]
        return self.env.get(self.a.get("token_env", "DISCORD_TOKEN"), "")

    def ask(self, key, prompt, default=None):
        v = self._get(key, default)
        return v if v not in (None, "") else default

    def yes(self, prompt, default=True, key=None):
        v = self._get(key, default) if key else default
        return bool(v)

    def pick(self, key, prompt, items, label, allow_none=False, extra=None):
        v = self._get(key)
        if v in (None, ""):
            if allow_none:
                return None
            if len(items) == 1:
                return items[0]
            raise WizardError(f"answers file needs '{key}' ({prompt})")
        if extra and str(v).lower() == extra[2]:
            return extra[0]
        return match_item(items, v, key)

    def features(self):
        return parse_features(self.a.get("features", "all"))

    def retry(self, fn, *a):
        return fn(*a)


def match_item(items, v, key="item"):
    """Match by id, then exact name (case-insensitive, leading # ignored), then 1-based number."""
    s = str(v).strip()
    for it in items:
        if str(it["id"]) == s:
            return it
    name = s.lstrip("#").lower()
    for it in items:
        if it.get("name", "").lower() == name:
            return it
    if isinstance(v, int) or (s.isdigit() and len(s) < 6):
        n = int(s)
        if 1 <= n <= len(items):
            return items[n - 1]
    raise WizardError(f"'{key}': no match for {v!r}")


def parse_features(v):
    if isinstance(v, list):
        items = v
    else:
        v = str(v).strip().lower()
        if v == "all":
            return list(FEATURES)
        items = [x for x in re.split(r"[,\s]+", v) if x]
    out = []
    for x in items:
        x = str(x).strip().lower()
        f = FEATURES[int(x) - 1] if x.isdigit() and 1 <= int(x) <= len(FEATURES) else x
        if f not in FEATURES:
            raise WizardError(f"unknown feature {x!r}; choose from {', '.join(FEATURES)}")
        if f not in out:
            out.append(f)
    return out


# ---------------------------------------------------------------- the wizard
def chan_label(c):
    return f"#{c['name']}  ({c['id']})"


def role_label(r):
    return f"{r['name']}  ({r['id']})"


def build(src, api_factory=DiscordAPI, out=print, allow_create_role=True, url_checker=check_translate_url):
    """Runs the questions. Returns (config_dict, token, info_dict). Never prints the token."""
    token = src.token()
    if not looks_like_token(token):
        raise WizardError("That doesn't look like a bot token (expected three dot-separated parts).")
    api = api_factory(token)
    me = api.me()
    if not me.get("bot"):
        raise WizardError("That token belongs to a user account, not a bot.")
    out(f"Token OK: logged in as {me['username']} (id {me['id']}).")
    app = api.application()
    client_id = str(app.get("id") or me["id"])
    url = invite_url(client_id)

    guilds = api.guilds()
    if not guilds:
        out(f"\nThe bot isn't in any server yet. Invite it first, then run the wizard again:\n  {url}")
        raise WizardError("bot is not in any server")
    guild = src.pick("guild", "Which server is this config for?", guilds, lambda g: f"{g['name']}  ({g['id']})")
    gid = str(guild["id"])
    chans = text_channels(api.channels(gid))
    if not chans:
        raise WizardError("the bot can't see any text channels in that server")
    roles = assignable_roles(api.roles(gid), gid)

    def pick_channel(key, prompt, allow_none=False):
        c = src.pick(key, prompt, chans, chan_label, allow_none=allow_none)
        return str(c["id"]) if c else None

    features = src.features()
    cfg = {"guild_id": gid, "features": features, "message_content_intent": False}

    if "scheduler" in features:
        tz = src.retry(valid_timezone, src.ask("scheduler.timezone", "Scheduler time zone (IANA)", "America/New_York"))
        posts = []
        preset = src.ask("scheduler.posts", "", None) if isinstance(src, FromAnswers) else None
        if preset is not None:
            for i, p in enumerate(preset):
                c = match_item(chans, p["channel"], f"scheduler.posts[{i}].channel")
                posts.append({"channel_id": str(c["id"]), "days": parse_days(p.get("days", "all")),
                              "time": valid_time(p["time"]), "message": p["message"]})
        else:
            while True:
                n = len(posts) + 1
                ch = pick_channel(None, f"Scheduled post #{n}: which channel?")
                days = src.retry(parse_days, src.ask(None, "Days (e.g. mon,wed,fri / weekdays / all)", "mon"))
                t = src.retry(valid_time, src.ask(None, "Time HH:MM (24 h)", "09:00"))
                msg = src.ask(None, "Message text", "📅 Weekly check-in: what are you working on this week?")
                posts.append({"channel_id": ch, "days": days, "time": t, "message": msg})
                if not src.yes("Add another scheduled post?", False):
                    break
        cfg["scheduler"] = {"timezone": tz, "posts": posts}

    if "mcstatus_alert" in features:
        addr = src.ask("mcstatus.address", "Minecraft server address (host or host:port)")
        if not addr:
            raise WizardError("mcstatus_alert needs a server address")
        ch = pick_channel("mcstatus.channel", "Channel for Minecraft up/down alerts?")
        interval = int(src.ask("mcstatus.interval_seconds", "Poll interval in seconds", 60))
        cfg["mcstatus"] = {"address": addr, "channel_id": ch, "interval_seconds": max(30, interval)}

    if "modflag" in features:
        cfg["modflag"] = {"mod_channel_id": pick_channel("modflag.mod_channel", "Private mod channel for flagged messages?")}

    if "verify" in features:
        extra = ("c", "create a new 'Verified' role now", "create") if allow_create_role else None
        r = src.pick("verify.role", "Role to grant on verification?", roles, role_label, extra=extra)
        if r == "c":
            created = api.create_role(gid, "Verified")
            out(f"Created role Verified ({created['id']}). Drag the bot's role ABOVE it in Server Settings > Roles.")
            role_id = str(created["id"])
        else:
            role_id = str(r["id"])
        default_log = cfg.get("modflag", {}).get("mod_channel_id")
        log_ch = pick_channel("verify.log_channel", "Channel to log verification attempts? (optional)", allow_none=True)
        cfg["verify"] = {
            "role_id": role_id,
            "log_channel_id": log_ch or default_log,
            "captcha": src.yes("Require a simple math captcha?", True, key="verify.captcha"),
            "title": src.ask("verify.title", "Embed title", "Verification"),
            "text": src.ask("verify.text", "Embed text", "Click **Verify** to get access to the server."),
        }
        if cfg["verify"]["log_channel_id"] is None:
            del cfg["verify"]["log_channel_id"]

    if "translate" in features:
        backend = str(src.ask("translate.backend", "Translate backend (libretranslate / deepl)", "libretranslate")).lower()
        if backend not in ("libretranslate", "deepl"):
            raise WizardError("translate.backend must be 'libretranslate' or 'deepl'")
        t = {"backend": backend, "default_target": src.ask("translate.default_target", "Default target language", "en"),
             "max_chars": 1500, "channels": {}}
        if backend == "libretranslate":
            t["libretranslate_url"] = src.ask("translate.libretranslate_url", "LibreTranslate URL",
                                              "http://127.0.0.1:5000/translate")
            if src.yes("Test that URL now?", True, key="translate.check_url"):
                ok, msg = url_checker(t["libretranslate_url"])
                out(f"Translate backend check: {msg}" if ok else f"WARNING: translate backend check failed: {msg}")
        else:
            out("DeepL: put DEEPL_API_KEY=... in .env (Free keys end in :fx).")
        preset = src.ask("translate.channels", "", None) if isinstance(src, FromAnswers) else None
        if preset is not None:
            for k, lang in preset.items():
                t["channels"][str(match_item(chans, k, "translate.channels")["id"])] = str(lang).lower()
        else:
            while src.yes("Add an auto-translate channel?", not t["channels"]):
                ch = pick_channel(None, "Auto-translate which channel?")
                t["channels"][ch] = src.ask(None, "Translate messages there into (language code)", "en").lower()
        cfg["translate"] = t
        cfg["message_content_intent"] = bool(t["channels"])

    info = {"bot": me["username"], "bot_id": str(me["id"]), "client_id": client_id, "invite_url": url,
            "guild_name": guild["name"], "channels_seen": len(chans), "roles_seen": len(roles)}
    return cfg, token, info


def write_env(path, token):
    """Set DISCORD_TOKEN in .env, keeping any other lines (e.g. DEEPL_API_KEY). chmod 600 where supported."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [ln for ln in lines if not re.match(r"\s*DISCORD_TOKEN\s*=", ln)]
    path.write_text("\n".join([f"DISCORD_TOKEN={token}"] + lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_config(path, cfg):
    path = Path(path)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Backed up existing {path.name} to {backup.name}")
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv=None, api_factory=DiscordAPI, url_checker=check_translate_url):
    p = argparse.ArgumentParser(description="Build config.json and .env for a BotKit client.")
    p.add_argument("--answers", help="JSON answers file (implies --non-interactive)")
    p.add_argument("--non-interactive", action="store_true", help="read all answers from --answers")
    p.add_argument("--config-out", default=str(ROOT / "config.json"))
    p.add_argument("--env-out", default=str(ROOT / ".env"))
    p.add_argument("--no-env", action="store_true", help="don't write the token to .env")
    p.add_argument("--no-create-role", action="store_true", help="never create roles (read-only API use)")
    args = p.parse_args(argv)

    if args.non_interactive and not args.answers:
        p.error("--non-interactive needs --answers FILE")
    try:
        if args.answers:
            src = FromAnswers(json.loads(Path(args.answers).read_text(encoding="utf-8")))
        else:
            print("BotKit setup wizard. Press Ctrl+C at any time to quit without writing anything.\n")
            src = Interactive()
        cfg, token, info = build(src, api_factory=api_factory, allow_create_role=not args.no_create_role,
                                 url_checker=url_checker)
    except WizardError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled, nothing written.")
        return 130

    write_config(args.config_out, cfg)
    print(f"Wrote {args.config_out} for {info['guild_name']} with features: {', '.join(cfg['features'])}")
    if not args.no_env:
        write_env(args.env_out, token)
        print(f"Wrote DISCORD_TOKEN to {args.env_out} (not shown).")
    print(f"\nInvite / re-authorize URL (scopes bot + applications.commands):\n  {info['invite_url']}")
    if cfg.get("message_content_intent"):
        print("\nReminder: enable the Message Content intent in Developer Portal > Bot for auto-translate.")
    if "verify" in cfg["features"]:
        print("Reminder: the bot's role must be ABOVE the verified role (Server Settings > Roles).")
    print("\nStart the bot with:  python bot.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
