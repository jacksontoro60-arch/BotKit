"""Change routine bot settings from Discord.

Optional feature: add "admin" to config features and set admin.staff_role_id.
Nothing here loads unless that feature is on.

/settings show and /settings set are limited to the same people tickets.is_staff
allows: the configured staff role, a server administrator, or a member who can
manage channels or threads. If admin.staff_role_id is unset, the tickets staff
role is used. Commands only run in the configured guild.

Edits are written to the same config.json the process loaded (BOTKIT_CONFIG when
the supervisor sets it) and applied to the running cogs. The Discord token,
.env, and API keys are never read or written here. Turning features or intents
on or off still needs a restart, because those change the gateway connection.
"""
import asyncio
import copy
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from cogs.rolemenu import MAX_ROLES, normalize_roles, normalize_style
from cogs.server_logs import normalize_events
from cogs.tickets import is_staff, normalize_mode
from cogs.translate import TranslateError, make_backend
from cogs.pricealerts import (
    AlertError,
    normalize_delivery,
    normalize_interval_minutes,
    normalize_max_alerts,
)
from cogs.welcome import DEFAULT_MESSAGE as WELCOME_MESSAGE

log = logging.getLogger(__name__)

RESTART_ONLY = ("features", "guild_id", "message_content_intent", "members_intent")
SECRET_KEY = re.compile(r"(token|secret|password|api[_-]?key|credential|authorization)", re.I)
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{18,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{15,}(?![A-Za-z0-9_-])")
ENV_ASSIGN = re.compile(r"(?i)\b(DISCORD_TOKEN|DEEPL_API_KEY|LIBRETRANSLATE_API_KEY|BOT_TOKEN)\s*=")
LANG_RE = re.compile(r"[a-z]{2,8}(-[a-z0-9]{2,8})?")
TEXT_TYPES = {discord.ChannelType.text, discord.ChannelType.news}

# Feature name in config["features"], which is not always the config section.
FEATURE_OF = {
    "admin": "admin",
    "welcome": "welcome",
    "server_logs": "server_logs",
    "tickets": "tickets",
    "rolemenu": "rolemenu",
    "verify": "verify",
    "modflag": "modflag",
    "mcstatus": "mcstatus_alert",
    "scheduler": "scheduler",
    "translate": "translate",
    "pricealerts": "pricealerts",
}
COG_BY_FEATURE = {
    "admin": "Admin",
    "welcome": "Welcome",
    "server_logs": "ServerLogs",
    "tickets": "Tickets",
    "rolemenu": "RoleMenu",
    "verify": "Verify",
    "modflag": "ModFlag",
    "mcstatus_alert": "MCStatus",
    "scheduler": "Scheduler",
    "translate": "Translate",
    "pricealerts": "PriceAlerts",
}
SECTION_ALIAS = {
    "logs": "server_logs",
    "log": "server_logs",
    "mc": "mcstatus",
    "minecraft": "mcstatus",
    "mcstatus_alert": "mcstatus",
    "roles": "rolemenu",
    "prices": "pricealerts",
    "pricealert": "pricealerts",
}
NOTES = {
    "rolemenu.style": "Run /rolemenu_setup to update a menu that is already posted. New clicks use the saved roles now.",
    "rolemenu.role.add": "Run /rolemenu_setup to put a new role on a menu that is already posted. New clicks use the saved roles now.",
    "rolemenu.role.remove": "That role stops being granted immediately. Run /rolemenu_setup to update a menu that is already posted.",
    "rolemenu.title": "Run /rolemenu_setup to repost the menu text.",
    "rolemenu.text": "Run /rolemenu_setup to repost the menu text.",
    "verify.title": "Run /verify_setup to repost the panel. The button already uses the saved role and captcha.",
    "verify.text": "Run /verify_setup to repost the panel. The button already uses the saved role and captcha.",
    "verify.role": "The verify button uses this role now. Repost with /verify_setup only if you want new panel text.",
    "verify.captcha": "The verify button uses this captcha setting now.",
    "tickets.title": "Run /ticket_setup to repost the panel. New tickets already use the saved mode, channel, and staff role.",
    "tickets.text": "Run /ticket_setup to repost the panel. New tickets already use the saved mode, channel, and staff role.",
    "pricealerts.interval_minutes": "The next price check uses this interval.",
    "pricealerts.market_hours_only": "While this is on, prices are checked only on weekdays from 9:30 to 16:00 America/New_York. Holidays are not excluded.",
    "pricealerts.delivery": "Channel posts go to the alerts channel and mention the member. DM delivery needs that member to allow DMs.",
}


class SettingError(Exception):
    """A rejected edit. The message is safe to show in Discord."""


class _Spec:
    def __init__(self, key, feature, help, fn):
        self.key, self.feature, self.help, self.fn = key, feature, help, fn


def contains_secret(value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    return TOKEN_RE.search(value) is not None or ENV_ASSIGN.search(value) is not None


def redact_text(value) -> str:
    return TOKEN_RE.sub("[hidden]", ENV_ASSIGN.sub("[hidden]", str(value)))


def is_secret_key(name) -> bool:
    return bool(SECRET_KEY.search(str(name)))


def manager_role_id(config):
    """Staff role for /settings. Falls back to the tickets staff role when unset."""
    admin = config.get("admin") if isinstance(config.get("admin"), dict) else {}
    if admin.get("staff_role_id") not in (None, ""):
        return admin.get("staff_role_id")
    tickets = config.get("tickets") if isinstance(config.get("tickets"), dict) else {}
    return tickets.get("staff_role_id")


def can_manage_settings(member, config) -> bool:
    """True for the configured staff role, using the tickets staff check."""
    return is_staff(member, manager_role_id(config))


def home_guild_error(config, guild) -> Optional[str]:
    if guild is None:
        return "Use this in the server."
    raw = config.get("guild_id") if isinstance(config, dict) else None
    if raw in (None, ""):
        return None
    try:
        expected = int(raw)
        actual = int(getattr(guild, "id", 0))
    except (TypeError, ValueError):
        return "Settings for this bot can only be changed in its configured server."
    if actual != expected:
        return "Settings for this bot can only be changed in its configured server."
    return None


def _enabled(config, feature) -> bool:
    return feature in set(config.get("features") or [])


def _snowflake(value) -> str:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise SettingError("That is not a Discord id.") from None
    if number < 1 or number >= 2**63:
        raise SettingError("That is not a Discord id.")
    return str(number)


def _pasted_id(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    match = re.fullmatch(r"<(#|@&)(\d+)>", text)
    if match:
        return int(match.group(2))
    if text.isdigit():
        return int(text)
    return None


def _channel_id(channel_id, channel_type, value, kind):
    chosen = channel_id if channel_id is not None else _pasted_id(value)
    if channel_type is not None:
        if kind == "text" and channel_type not in TEXT_TYPES:
            raise SettingError("Choose a text channel.")
        if kind == "category" and channel_type != discord.ChannelType.category:
            raise SettingError("Choose a category.")
    if chosen is None:
        raise SettingError("Choose a text channel." if kind == "text" else "Choose a category.")
    return _snowflake(chosen)


def _role_id(role_id, value):
    chosen = role_id if role_id is not None else _pasted_id(value)
    if chosen is None:
        raise SettingError("Choose a role.")
    return _snowflake(chosen)


def _obj(cfg, name):
    current = cfg.get(name)
    if not isinstance(current, dict):
        current = {}
        cfg[name] = current
    return current


def _text(value, limit, label):
    if value is None or not str(value).strip():
        raise SettingError(f"{label} cannot be empty.")
    text = str(value)
    if len(text) > limit:
        raise SettingError(f"{label} must be {limit} characters or less.")
    if contains_secret(text):
        raise SettingError("That value looks like a secret and was not saved.")
    return text


def _bool(value):
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    raise SettingError("Use true or false.")


def _days_time():
    from setup_wizard import WizardError, parse_days, valid_time, valid_timezone
    return WizardError, parse_days, valid_time, valid_timezone


def _guard_wizard(fn, value):
    WizardError, *_ = _days_time()
    try:
        return fn(value)
    except WizardError as exc:
        raise SettingError(str(exc)) from None


def _welcome_channel(cfg, value, channel_id, channel_type, role_id, role_name, env):
    section = _obj(cfg, "welcome")
    section["channel_id"] = _channel_id(channel_id, channel_type, value, "text")
    section.setdefault("message", WELCOME_MESSAGE)


def _welcome_message(cfg, value, channel_id, channel_type, role_id, role_name, env):
    section = _obj(cfg, "welcome")
    section["message"] = _text(value, 2000, "Welcome message")


def _logs_channel(cfg, value, channel_id, channel_type, role_id, role_name, env):
    section = _obj(cfg, "server_logs")
    section["channel_id"] = _channel_id(channel_id, channel_type, value, "text")
    section.setdefault("events", list(normalize_events("all")))


def _logs_events(cfg, value, channel_id, channel_type, role_id, role_name, env):
    if value is None or not str(value).strip():
        raise SettingError("List events such as join,leave,delete, or all.")
    section = _obj(cfg, "server_logs")
    try:
        section["events"] = normalize_events(value)
    except ValueError as exc:
        raise SettingError(str(exc)) from None


def _tickets_role(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "tickets")["staff_role_id"] = _role_id(role_id, value)


def _tickets_mode(cfg, value, channel_id, channel_type, role_id, role_name, env):
    if value is None or not str(value).strip():
        raise SettingError("tickets.mode must be thread or channel.")
    section = _obj(cfg, "tickets")
    try:
        mode = normalize_mode(value)
    except ValueError as exc:
        raise SettingError(str(exc)) from None
    if mode == "thread" and not section.get("channel_id"):
        raise SettingError("Set tickets.channel before switching to thread mode.")
    if mode == "channel" and not section.get("category_id"):
        raise SettingError("Set tickets.category before switching to channel mode.")
    section["mode"] = mode


def _tickets_channel(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "tickets")["channel_id"] = _channel_id(channel_id, channel_type, value, "text")


def _tickets_category(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "tickets")["category_id"] = _channel_id(channel_id, channel_type, value, "category")


def _tickets_title(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "tickets")["title"] = _text(value, 256, "Panel title")


def _tickets_text(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "tickets")["text"] = _text(value, 2000, "Panel text")


def _split_label(value, role_name):
    if value is None or not str(value).strip():
        if not role_name:
            raise SettingError("Add a label, or pick the role so its name can be the label.")
        return str(role_name).strip()[:80], None
    text = str(value).strip()
    if contains_secret(text):
        raise SettingError("That value looks like a secret and was not saved.")
    if "|" in text:
        label, emoji = text.split("|", 1)
        label = label.strip() or (str(role_name).strip() if role_name else "")
        emoji = emoji.strip()[:64]
        if not label:
            raise SettingError("Add a label for that role.")
        return label[:80], (emoji or None)
    return text[:80], None


def _role_add(cfg, value, channel_id, channel_type, role_id, role_name, env):
    section = _obj(cfg, "rolemenu")
    if role_id is not None:
        rid = _role_id(role_id, None)
        label, emoji = _split_label(value, role_name or f"Role {rid}")
    else:
        rid = _role_id(None, value)
        label, emoji = _split_label(None, role_name or f"Role {rid}")
    roles = []
    for item in section.get("roles") or []:
        if isinstance(item, dict):
            roles.append(dict(item))
    found = False
    for item in roles:
        if str(item.get("role_id")) == rid:
            item["label"] = label
            if emoji:
                item["emoji"] = emoji
            found = True
            break
    if not found:
        entry = {"role_id": rid, "label": label}
        if emoji:
            entry["emoji"] = emoji
        roles.append(entry)
    try:
        normalize_roles(roles)
    except ValueError as exc:
        raise SettingError(str(exc)) from None
    section["roles"] = roles
    section.setdefault("style", "buttons")


def _role_remove(cfg, value, channel_id, channel_type, role_id, role_name, env):
    section = _obj(cfg, "rolemenu")
    rid = _role_id(role_id, value)
    roles = [dict(item) for item in (section.get("roles") or []) if isinstance(item, dict)]
    kept = [item for item in roles if str(item.get("role_id")) != rid]
    if len(kept) == len(roles):
        raise SettingError("That role is not on the menu.")
    if not kept:
        raise SettingError("The role menu needs at least one role.")
    try:
        normalize_roles(kept)
    except ValueError as exc:
        raise SettingError(str(exc)) from None
    section["roles"] = kept


def _role_style(cfg, value, channel_id, channel_type, role_id, role_name, env):
    if value is None or not str(value).strip():
        raise SettingError("rolemenu.style must be buttons or select.")
    section = _obj(cfg, "rolemenu")
    try:
        section["style"] = normalize_style(value)
    except ValueError as exc:
        raise SettingError(str(exc)) from None


def _role_title(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "rolemenu")["title"] = _text(value, 256, "Menu title")


def _role_text(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "rolemenu")["text"] = _text(value, 2000, "Menu text")


def _verify_role(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "verify")["role_id"] = _role_id(role_id, value)


def _verify_log(cfg, value, channel_id, channel_type, role_id, role_name, env):
    section = _obj(cfg, "verify")
    if value is not None and str(value).strip().lower() in ("none", "off", "clear") and channel_id is None:
        section.pop("log_channel_id", None)
        return
    section["log_channel_id"] = _channel_id(channel_id, channel_type, value, "text")


def _verify_captcha(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "verify")["captcha"] = _bool(value)


def _verify_title(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "verify")["title"] = _text(value, 256, "Embed title")


def _verify_text(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "verify")["text"] = _text(value, 2000, "Embed text")


def _mod_channel(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "modflag")["mod_channel_id"] = _channel_id(channel_id, channel_type, value, "text")


def _mc_address(cfg, value, channel_id, channel_type, role_id, role_name, env):
    text = _text(value, 253, "Minecraft address")
    if any(ch.isspace() for ch in text):
        raise SettingError("Use a host or host:port with no spaces.")
    _obj(cfg, "mcstatus")["address"] = text


def _mc_channel(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "mcstatus")["channel_id"] = _channel_id(channel_id, channel_type, value, "text")


def _mc_interval(cfg, value, channel_id, channel_type, role_id, role_name, env):
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        raise SettingError("Interval must be a number of seconds.") from None
    if seconds < 30 or seconds > 86400:
        raise SettingError("Interval must be between 30 seconds and 24 hours.")
    _obj(cfg, "mcstatus")["interval_seconds"] = seconds


def _sched_tz(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _, _, _, valid_timezone = _days_time()
    _obj(cfg, "scheduler")["timezone"] = _guard_wizard(valid_timezone, str(value or "").strip())


def _split_post(value):
    if not isinstance(value, str) or value.count("|") < 2:
        raise SettingError("Use days|HH:MM|message, for example mon,fri|09:00|Weekly check-in.")
    days, when, message = value.split("|", 2)
    return days.strip(), when.strip(), message.strip()


def _sched_add(cfg, value, channel_id, channel_type, role_id, role_name, env):
    section = _obj(cfg, "scheduler")
    days_s, when, message = _split_post(value)
    _, parse_days, valid_time, _ = _days_time()
    post = {
        "channel_id": _channel_id(channel_id, channel_type, None, "text"),
        "days": _guard_wizard(parse_days, days_s),
        "time": _guard_wizard(valid_time, when),
        "message": _text(message, 1800, "Message"),
    }
    posts = [dict(item) for item in (section.get("posts") or []) if isinstance(item, dict)]
    if len(posts) >= 25:
        raise SettingError("The scheduler holds at most 25 posts.")
    posts.append(post)
    section["posts"] = posts
    section.setdefault("timezone", "America/New_York")


def _sched_remove(cfg, value, channel_id, channel_type, role_id, role_name, env):
    section = _obj(cfg, "scheduler")
    posts = [dict(item) for item in (section.get("posts") or []) if isinstance(item, dict)]
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise SettingError("Use the post number from /settings show, starting at 1.") from None
    if number < 1 or number > len(posts):
        raise SettingError(f"There is no post {number}.")
    posts.pop(number - 1)
    section["posts"] = posts


def _tr_backend(cfg, value, channel_id, channel_type, role_id, role_name, env):
    kind = str(value or "").strip().lower()
    if kind not in ("libretranslate", "deepl"):
        raise SettingError("translate.backend must be libretranslate or deepl.")
    if kind == "deepl" and not (env or {}).get("DEEPL_API_KEY"):
        raise SettingError(
            "DeepL needs DEEPL_API_KEY in the environment. It cannot be set from Discord, and the key is not shown here.")
    section = _obj(cfg, "translate")
    section["backend"] = kind
    if kind == "libretranslate" and not section.get("libretranslate_url"):
        section["libretranslate_url"] = "http://127.0.0.1:5000/translate"


def _clean_url(value):
    url = _text(value, 500, "URL")
    if any(ch.isspace() for ch in url):
        raise SettingError("The URL cannot contain spaces.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SettingError("The URL must start with http:// or https://.")
    if parts.username or parts.password:
        raise SettingError("The URL cannot contain a username or password.")
    return url


def _tr_url(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "translate")["libretranslate_url"] = _clean_url(value)


def _tr_target(cfg, value, channel_id, channel_type, role_id, role_name, env):
    lang = str(value or "").strip().lower()
    if not LANG_RE.fullmatch(lang):
        raise SettingError("Use a language code such as en or pt-br.")
    _obj(cfg, "translate")["default_target"] = lang


def _tr_max(cfg, value, channel_id, channel_type, role_id, role_name, env):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise SettingError("max_chars must be a number.") from None
    if number < 1 or number > 4000:
        raise SettingError("max_chars must be between 1 and 4000.")
    _obj(cfg, "translate")["max_chars"] = number


def _tr_channel(cfg, value, channel_id, channel_type, role_id, role_name, env):
    section = _obj(cfg, "translate")
    cid = _channel_id(channel_id, channel_type, None if channel_id is not None else value, "text")
    channels = {str(key): lang for key, lang in (section.get("channels") or {}).items()}
    clearing = channel_id is not None and str(value or "").strip().lower() in ("", "off", "remove", "none")
    if clearing:
        if cid not in channels and channel_id is None:
            # value was the id and also the word off; can't be both
            raise SettingError("Say which channel to remove, and pass off as the value.")
        if cid not in channels:
            raise SettingError("That channel is not auto-translated.")
        del channels[cid]
    else:
        lang_source = value if channel_id is not None else None
        if lang_source is None:
            raise SettingError("Choose the channel, and set value to a language code such as en.")
        lang = str(lang_source).strip().lower()
        if not LANG_RE.fullmatch(lang):
            raise SettingError("Use a language code such as en or pt-br.")
        channels[cid] = lang
    section["channels"] = channels


def _price_error(exc):
    raise SettingError(str(exc)) from None


def _price_channel(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "pricealerts")["channel_id"] = _channel_id(channel_id, channel_type, value, "text")


def _price_interval(cfg, value, channel_id, channel_type, role_id, role_name, env):
    try:
        minutes = normalize_interval_minutes(value)
    except AlertError as exc:
        _price_error(exc)
    _obj(cfg, "pricealerts")["interval_minutes"] = minutes


def _price_hours(cfg, value, channel_id, channel_type, role_id, role_name, env):
    _obj(cfg, "pricealerts")["market_hours_only"] = _bool(value)


def _price_delivery(cfg, value, channel_id, channel_type, role_id, role_name, env):
    try:
        delivery = normalize_delivery(value)
    except AlertError as exc:
        _price_error(exc)
    section = _obj(cfg, "pricealerts")
    if delivery == "channel" and not section.get("channel_id"):
        raise SettingError("Set pricealerts.channel before delivering alerts in a channel.")
    section["delivery"] = delivery


def _price_cap(cfg, value, channel_id, channel_type, role_id, role_name, env):
    try:
        cap = normalize_max_alerts(value)
    except AlertError as exc:
        _price_error(exc)
    _obj(cfg, "pricealerts")["max_alerts_per_user"] = cap


def _admin_role(cfg, value, channel_id, channel_type, role_id, role_name, env):
    section = _obj(cfg, "admin")
    if value is not None and str(value).strip().lower() in ("none", "off", "clear") and role_id is None:
        section.pop("staff_role_id", None)
        return
    section["staff_role_id"] = _role_id(role_id, value)


CATALOG = [
    _Spec("admin.staff_role", "admin", "role that can use /settings", _admin_role),
    _Spec("welcome.channel", "welcome", "channel for join messages", _welcome_channel),
    _Spec("welcome.message", "welcome", "join message ({member}, {server})", _welcome_message),
    _Spec("server_logs.channel", "server_logs", "channel for server logs", _logs_channel),
    _Spec("server_logs.events", "server_logs", "join, leave, delete, edit, roles, or all", _logs_events),
    _Spec("tickets.mode", "tickets", "thread or channel", _tickets_mode),
    _Spec("tickets.staff_role", "tickets", "role that can close tickets", _tickets_role),
    _Spec("tickets.channel", "tickets", "parent channel for thread tickets", _tickets_channel),
    _Spec("tickets.category", "tickets", "category for channel tickets", _tickets_category),
    _Spec("tickets.title", "tickets", "panel title", _tickets_title),
    _Spec("tickets.text", "tickets", "panel text", _tickets_text),
    _Spec("rolemenu.style", "rolemenu", "buttons or select", _role_style),
    _Spec("rolemenu.title", "rolemenu", "menu title", _role_title),
    _Spec("rolemenu.text", "rolemenu", "menu text", _role_text),
    _Spec("rolemenu.role.add", "rolemenu", "add or relabel a role (label|emoji)", _role_add),
    _Spec("rolemenu.role.remove", "rolemenu", "remove a role from the menu", _role_remove),
    _Spec("verify.role", "verify", "role granted by the verify button", _verify_role),
    _Spec("verify.log_channel", "verify", "mod log channel, or off", _verify_log),
    _Spec("verify.captcha", "verify", "true or false", _verify_captcha),
    _Spec("verify.title", "verify", "embed title", _verify_title),
    _Spec("verify.text", "verify", "embed text", _verify_text),
    _Spec("modflag.channel", "modflag", "channel for flagged messages", _mod_channel),
    _Spec("mcstatus.address", "mcstatus_alert", "Minecraft host or host:port", _mc_address),
    _Spec("mcstatus.channel", "mcstatus_alert", "channel for up/down alerts", _mc_channel),
    _Spec("mcstatus.interval_seconds", "mcstatus_alert", "poll interval, at least 30", _mc_interval),
    _Spec("scheduler.timezone", "scheduler", "IANA time zone", _sched_tz),
    _Spec("scheduler.post.add", "scheduler", "days|HH:MM|message, plus a channel", _sched_add),
    _Spec("scheduler.post.remove", "scheduler", "post number from /settings show", _sched_remove),
    _Spec("translate.backend", "translate", "libretranslate or deepl", _tr_backend),
    _Spec("translate.url", "translate", "LibreTranslate /translate URL", _tr_url),
    _Spec("translate.default_target", "translate", "language for the right-click menu", _tr_target),
    _Spec("translate.max_chars", "translate", "per-message character cap", _tr_max),
    _Spec("translate.channel", "translate", "language for a channel, or off", _tr_channel),
    _Spec("pricealerts.channel", "pricealerts", "channel for price alerts", _price_channel),
    _Spec("pricealerts.interval_minutes", "pricealerts", "poll interval in minutes, at least 1", _price_interval),
    _Spec("pricealerts.market_hours_only", "pricealerts", "true for US market hours, false for 24/7", _price_hours),
    _Spec("pricealerts.delivery", "pricealerts", "channel or dm", _price_delivery),
    _Spec("pricealerts.max_per_user", "pricealerts", "how many alerts one member can have", _price_cap),
]
CATALOG_BY_KEY = {spec.key: spec for spec in CATALOG}


def _validate(feature, cfg, env):
    if feature == "welcome":
        section = cfg.get("welcome") or {}
        _snowflake(section.get("channel_id"))
        _text(section.get("message") or WELCOME_MESSAGE, 2000, "Welcome message")
    elif feature == "server_logs":
        section = cfg.get("server_logs") or {}
        _snowflake(section.get("channel_id"))
        try:
            normalize_events(section.get("events", "all"))
        except ValueError as exc:
            raise SettingError(str(exc)) from None
    elif feature == "tickets":
        section = cfg.get("tickets") or {}
        _snowflake(section.get("staff_role_id"))
        try:
            mode = normalize_mode(section.get("mode", "thread"))
        except ValueError as exc:
            raise SettingError(str(exc)) from None
        if mode == "thread":
            _snowflake(section.get("channel_id"))
        else:
            _snowflake(section.get("category_id"))
        _text(section.get("title") or "Support", 256, "Panel title")
        _text(section.get("text") or "Help", 2000, "Panel text")
    elif feature == "rolemenu":
        section = cfg.get("rolemenu") or {}
        try:
            normalize_style(section.get("style", "buttons"))
            normalize_roles(section.get("roles"))
        except ValueError as exc:
            raise SettingError(str(exc)) from None
    elif feature == "verify":
        section = cfg.get("verify") or {}
        _snowflake(section.get("role_id"))
        if section.get("log_channel_id") not in (None, ""):
            _snowflake(section.get("log_channel_id"))
    elif feature == "modflag":
        _snowflake((cfg.get("modflag") or {}).get("mod_channel_id"))
    elif feature == "mcstatus_alert":
        section = cfg.get("mcstatus") or {}
        _text(section.get("address"), 253, "Minecraft address")
        _snowflake(section.get("channel_id"))
    elif feature == "scheduler":
        section = cfg.get("scheduler") or {}
        _, _, _, valid_timezone = _days_time()
        _guard_wizard(valid_timezone, section.get("timezone") or "America/New_York")
        posts = section.get("posts") or []
        if not isinstance(posts, list):
            raise SettingError("scheduler.posts must be a list.")
    elif feature == "translate":
        section = cfg.get("translate") or {}
        try:
            make_backend(section, lambda: None, env=env if env is not None else {})
        except TranslateError as exc:
            message = str(exc)
            if "DEEPL_API_KEY" in message:
                raise SettingError(
                    "DeepL needs DEEPL_API_KEY in the environment. It cannot be set from Discord, "
                    "and the key is not shown here.") from None
            raise SettingError("That translate setting was not saved.") from None
    elif feature == "admin":
        raw = (cfg.get("admin") or {}).get("staff_role_id")
        if raw not in (None, ""):
            _snowflake(raw)
    elif feature == "pricealerts":
        section = cfg.get("pricealerts") or {}
        try:
            delivery = normalize_delivery(section.get("delivery", "channel"))
            normalize_interval_minutes(section.get("interval_minutes", 5))
            normalize_max_alerts(section.get("max_alerts_per_user", 10))
        except AlertError as exc:
            _price_error(exc)
        if delivery == "channel":
            if section.get("channel_id") in (None, ""):
                raise SettingError("Set pricealerts.channel before changing other price alert settings.")
            _snowflake(section.get("channel_id"))


def change_settings(config, setting, value=None, *, channel_id=None, channel_type=None,
                    role_id=None, role_name=None, env=None):
    """Return a new config with one setting changed. Does not touch the input or the disk."""
    if not isinstance(config, dict):
        raise SettingError("Config is not a JSON object.")
    key = str(setting or "").strip().lower()
    if not key or is_secret_key(key) or any(is_secret_key(part) for part in key.split(".")):
        raise SettingError("That setting is a secret. The token and .env cannot be read or changed from Discord.")
    root = key.split(".", 1)[0]
    if root in RESTART_ONLY:
        raise SettingError(
            f"{key} is not changed from Discord. Edit config and restart to change features, "
            "the server id, or intents.")
    spec = CATALOG_BY_KEY.get(key)
    if spec is None:
        raise SettingError(f"Unknown setting {key!r}. Use /settings show to list them.")
    if not _enabled(config, spec.feature):
        raise SettingError(f"{spec.feature} is not enabled. Add it to features and restart before changing it here.")
    if contains_secret(value if isinstance(value, str) else ""):
        raise SettingError("That value looks like a secret and was not saved.")
    new = copy.deepcopy(config)
    spec.fn(new, value, channel_id, channel_type, role_id, role_name, env if env is not None else os.environ)
    _validate(spec.feature, new, env if env is not None else os.environ)
    return new


def setting_choices(config, current):
    """Autocomplete rows for enabled settings. At most 25, which is Discord's limit."""
    needle = (current or "").strip().lower()
    enabled = set(config.get("features") or [])
    out = []
    for spec in CATALOG:
        if spec.feature not in enabled:
            continue
        haystack = f"{spec.key} {spec.help}".lower()
        if needle and needle not in haystack:
            continue
        out.append(app_commands.Choice(name=f"{spec.key} — {spec.help}"[:100], value=spec.key))
        if len(out) == 25:
            break
    return out


def _mention_channel(value):
    if value in (None, ""):
        return "(not set)"
    return f"<#{value}>"


def _mention_role(value):
    if value in (None, ""):
        return "(not set)"
    return f"<@&{value}>"


def _clip(value, limit=180):
    text = redact_text(value).replace("```", "'''")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _section_lines(config, section):
    if section == "admin":
        role = (config.get("admin") or {}).get("staff_role_id")
        return ["**admin**", f"admin.staff_role: {_mention_role(role)}"]
    if section == "welcome":
        cfg = config.get("welcome") or {}
        return ["**welcome**", f"welcome.channel: {_mention_channel(cfg.get('channel_id'))}",
                f"welcome.message: {_clip(cfg.get('message') or WELCOME_MESSAGE)}"]
    if section == "server_logs":
        cfg = config.get("server_logs") or {}
        events = cfg.get("events", "all")
        shown = ", ".join(events) if isinstance(events, (list, tuple)) else events
        return ["**server_logs**", f"server_logs.channel: {_mention_channel(cfg.get('channel_id'))}",
                f"server_logs.events: {_clip(shown)}"]
    if section == "tickets":
        cfg = config.get("tickets") or {}
        return ["**tickets**", f"tickets.mode: {_clip(cfg.get('mode') or 'thread')}",
                f"tickets.staff_role: {_mention_role(cfg.get('staff_role_id'))}",
                f"tickets.channel: {_mention_channel(cfg.get('channel_id'))}",
                f"tickets.category: {_mention_channel(cfg.get('category_id'))}",
                f"tickets.title: {_clip(cfg.get('title') or 'Support')}",
                f"tickets.text: {_clip(cfg.get('text') or '')}"]
    if section == "rolemenu":
        cfg = config.get("rolemenu") or {}
        lines = ["**rolemenu**", f"rolemenu.style: {_clip(cfg.get('style') or 'buttons')}",
                 f"rolemenu.title: {_clip(cfg.get('title') or 'Roles')}",
                 f"rolemenu.text: {_clip(cfg.get('text') or '')}", "rolemenu.roles:"]
        roles = cfg.get("roles") or []
        if not roles:
            lines.append("(none)")
        for index, item in enumerate(roles, 1):
            if not isinstance(item, dict):
                continue
            emoji = f" {item['emoji']}" if item.get("emoji") else ""
            lines.append(f"{index}. {_mention_role(item.get('role_id'))} {_clip(item.get('label') or '')}{emoji}")
        lines.append(f"(at most {MAX_ROLES})")
        return lines
    if section == "verify":
        cfg = config.get("verify") or {}
        return ["**verify**", f"verify.role: {_mention_role(cfg.get('role_id'))}",
                f"verify.log_channel: {_mention_channel(cfg.get('log_channel_id'))}",
                f"verify.captcha: {bool(cfg.get('captcha', False))}",
                f"verify.title: {_clip(cfg.get('title') or 'Verification')}",
                f"verify.text: {_clip(cfg.get('text') or '')}"]
    if section == "modflag":
        cfg = config.get("modflag") or {}
        return ["**modflag**", f"modflag.channel: {_mention_channel(cfg.get('mod_channel_id'))}"]
    if section == "mcstatus":
        cfg = config.get("mcstatus") or {}
        return ["**mcstatus**", f"mcstatus.address: {_clip(cfg.get('address') or '(not set)')}",
                f"mcstatus.channel: {_mention_channel(cfg.get('channel_id'))}",
                f"mcstatus.interval_seconds: {cfg.get('interval_seconds', 60)}"]
    if section == "scheduler":
        cfg = config.get("scheduler") or {}
        lines = ["**scheduler**", f"scheduler.timezone: {_clip(cfg.get('timezone') or 'America/New_York')}",
                 "scheduler.posts:"]
        posts = cfg.get("posts") or []
        if not posts:
            lines.append("(none)")
        for index, post in enumerate(posts, 1):
            if not isinstance(post, dict):
                continue
            days = post.get("days") or []
            day_text = ",".join(days) if isinstance(days, (list, tuple)) else days
            lines.append(
                f"{index}. {_mention_channel(post.get('channel_id'))} {day_text} {post.get('time')} — {_clip(post.get('message') or '')}")
        return lines
    if section == "translate":
        cfg = config.get("translate") or {}
        lines = ["**translate**", f"translate.backend: {_clip(cfg.get('backend') or 'libretranslate')}",
                 f"translate.url: {_clip(cfg.get('libretranslate_url') or '')}",
                 f"translate.default_target: {_clip(cfg.get('default_target') or 'en')}",
                 f"translate.max_chars: {cfg.get('max_chars', 1500)}", "translate.channels:"]
        channels = cfg.get("channels") or {}
        if not channels:
            lines.append("(none)")
        else:
            for cid, lang in channels.items():
                lines.append(f"{_mention_channel(cid)} → {_clip(lang)}")
        lines.append("API keys stay in the environment and are not shown.")
        return lines
    if section == "pricealerts":
        cfg = config.get("pricealerts") or {}
        return ["**pricealerts**",
                f"pricealerts.channel: {_mention_channel(cfg.get('channel_id'))}",
                f"pricealerts.interval_minutes: {cfg.get('interval_minutes', 5)}",
                f"pricealerts.market_hours_only: {bool(cfg.get('market_hours_only', True))}",
                f"pricealerts.delivery: {_clip(cfg.get('delivery') or 'channel')}",
                f"pricealerts.max_per_user: {cfg.get('max_alerts_per_user', 10)}"]
    return []


def render_settings(config, section=None) -> str:
    """Text for /settings show. Secret keys and token-shaped values are redacted."""
    if not isinstance(config, dict):
        return "Config is not a JSON object."
    wanted = None
    if section not in (None, ""):
        wanted = SECTION_ALIAS.get(str(section).strip().lower(), str(section).strip().lower())
        feature = FEATURE_OF.get(wanted)
        if feature is None:
            known = ", ".join(FEATURE_OF)
            return f"No settings section {section!r}. Choose from {known}."
        if not _enabled(config, feature):
            return f"{feature} is not enabled. Add it to features and restart before changing it here."
        body = _section_lines(config, wanted)
    else:
        body = ["Routine settings. Type a feature name into /settings set to filter the list.",
                "scheduler and translate appear when you type those names."]
        for name in ("admin", "welcome", "server_logs", "tickets", "rolemenu", "verify",
                     "modflag", "mcstatus", "scheduler", "translate", "pricealerts"):
            feature = FEATURE_OF[name]
            if _enabled(config, feature):
                body.append("")
                body.extend(_section_lines(config, name))
    features = [redact_text(name) for name in (config.get("features") or [])]
    notice = "The Discord token and .env are not shown and cannot be changed from Discord."
    footer = (
        "Read-only here (restart required): "
        f"features={', '.join(features) or 'none'}; "
        f"guild_id={_clip(config.get('guild_id') or 'unset', 32)}; "
        f"message_content_intent={bool(config.get('message_content_intent'))}; "
        f"members_intent={bool(config.get('members_intent'))}."
    )
    # Drop any secret-keyed lines a hand-edited config might have pushed into a section.
    safe = []
    for line in body:
        head = line.split(":", 1)[0]
        if is_secret_key(head):
            safe.append(f"{head}: [hidden]")
        else:
            safe.append(line)
    text = "\n".join([notice, ""] + safe + ["", footer])
    if len(text) <= 1900:
        return text
    room = 1900 - len(notice) - len(footer) - 80
    clipped = "\n".join(safe)[:max(room, 0)]
    return f"{notice}\n\n{clipped}\n… Use /settings show with a section name to see the rest.\n\n{footer}"


def config_path_for(bot) -> Path:
    path = getattr(bot, "config_path", None)
    if path:
        return Path(path)
    env = os.environ.get("BOTKIT_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "config.json"


def _refuse_path(path: Path):
    name = path.name.lower()
    if name == ".env" or name.endswith(".env") or "token" in name or name.endswith(".env.example"):
        raise SettingError("Refusing to write that file.")
    env_path = os.environ.get("BOTKIT_ENV")
    if env_path:
        try:
            if path.resolve() == Path(env_path).resolve():
                raise SettingError("Refusing to write the environment file.")
        except OSError:
            pass


def save_config(path, config):
    path = Path(path)
    _refuse_path(path)
    payload = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def replace_config(bot, new):
    current = getattr(bot, "config", None)
    if isinstance(current, dict):
        current.clear()
        current.update(new)
    else:
        bot.config = new


def push_live(bot, feature) -> bool:
    """Push one feature's config into its cog. Returns False when that cog is not loaded."""
    getter = getattr(bot, "get_cog", None)
    cog = getter(COG_BY_FEATURE.get(feature, "")) if callable(getter) else None
    if cog is None or not hasattr(cog, "apply_config"):
        return False
    cog.apply_config()
    if feature == "rolemenu" and hasattr(cog, "refresh_persistent_view"):
        cog.refresh_persistent_view()
    return True


def success_text(setting, loaded, config) -> str:
    parts = [f"Saved {setting}."]
    parts.append("It is in effect now." if loaded else "That feature is not loaded, so this takes effect after a restart.")
    note = NOTES.get(setting)
    if note:
        parts.append(note)
    if setting.startswith("translate.") and not (config or {}).get("message_content_intent"):
        parts.append(
            "Auto-translate still needs the Message Content intent, which takes effect after a restart, "
            "and the toggle has to be on in the Developer Portal.")
    return " ".join(parts)


async def complete_setting(interaction: discord.Interaction, current: str):
    config = getattr(interaction.client, "config", None) or {}
    if home_guild_error(config, interaction.guild) or not can_manage_settings(interaction.user, config):
        return []
    return setting_choices(config, current)


def _feature_of_key(setting) -> str:
    spec = CATALOG_BY_KEY.get(str(setting or "").strip().lower())
    return spec.feature if spec else ""


class Admin(commands.Cog):
    settings = app_commands.Group(name="settings", description="View or change routine bot settings")

    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self.apply_config()

    def apply_config(self):
        cfg = self.bot.config.get("admin") if isinstance(self.bot.config.get("admin"), dict) else {}
        self.staff_role_id = cfg.get("staff_role_id")

    def _deny(self, interaction) -> Optional[str]:
        config = self.bot.config if isinstance(getattr(self.bot, "config", None), dict) else {}
        home = home_guild_error(config, interaction.guild)
        if home:
            return home
        if not can_manage_settings(interaction.user, config):
            return ("You can't change bot settings. This is limited to the configured staff role, "
                    "administrators, and members who can manage channels or threads.")
        return None

    @settings.command(name="show", description="Show the settings staff can change")
    @app_commands.describe(section="One feature, such as welcome or tickets. Leave blank for all.")
    @app_commands.guild_only()
    async def show(self, interaction: discord.Interaction, section: Optional[str] = None):
        denial = self._deny(interaction)
        if denial:
            return await interaction.response.send_message(denial, ephemeral=True)
        await interaction.response.send_message(render_settings(self.bot.config, section), ephemeral=True)

    @settings.command(name="set", description="Change one setting. Saved to config.json and applied live when safe.")
    @app_commands.describe(
        setting="Setting name. Type welcome, tickets, scheduler, or translate to filter.",
        value="Text, true/false, days|HH:MM|message, or a language code.",
        channel="Text channel or category, when the setting needs one.",
        role="Role, when the setting needs one.",
    )
    @app_commands.autocomplete(setting=complete_setting)
    @app_commands.guild_only()
    async def set_setting(self, interaction: discord.Interaction, setting: str, value: Optional[str] = None,
                          channel: Optional[discord.abc.GuildChannel] = None, role: Optional[discord.Role] = None):
        denial = self._deny(interaction)
        if denial:
            return await interaction.response.send_message(denial, ephemeral=True)
        async with self._lock:
            try:
                new = change_settings(
                    self.bot.config, setting, value,
                    channel_id=getattr(channel, "id", None) if channel is not None else None,
                    channel_type=getattr(channel, "type", None) if channel is not None else None,
                    role_id=getattr(role, "id", None) if role is not None else None,
                    role_name=getattr(role, "name", None) if role is not None else None,
                    env=os.environ,
                )
                save_config(config_path_for(self.bot), new)
            except SettingError as exc:
                return await interaction.response.send_message(str(exc), ephemeral=True)
            except OSError as exc:
                log.warning("settings save failed for %s: %s", setting, exc)
                return await interaction.response.send_message(
                    "Couldn't save config.json. Nothing was changed.", ephemeral=True)
            replace_config(self.bot, new)
            feature = _feature_of_key(setting)
            try:
                loaded = push_live(self.bot, feature)
            except Exception:
                log.exception("settings apply failed for %s", setting)
                return await interaction.response.send_message(
                    f"Saved {setting.strip().lower()} to config.json, but it did not apply live. Restart the bot to pick it up.",
                    ephemeral=True)
        log.info("settings change by %s: %s", getattr(interaction.user, "id", "?"), setting.strip().lower())
        await interaction.response.send_message(success_text(setting.strip().lower(), loaded, self.bot.config), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admin(bot))
