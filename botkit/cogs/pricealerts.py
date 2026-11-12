"""Stock price alerts. Optional: add "pricealerts" to config features.

Members set one-shot alerts with /alert. A background task polls prices and posts
in a channel or by DM when a target is hit, then removes that alert. /price is a
quick quote. Quotes come from the free yfinance library behind PriceProvider, so
tests can substitute a fake. No API key is required.

No privileged intents. Send Messages covers channel delivery. DM delivery only
works when that member allows DMs.

Alerts are stored in data/pricealerts.json next to this bot's config.json
(BOTKIT_CONFIG when the supervisor sets it). Writes are atomic.

US market hours mean the regular equity session: weekdays 09:30 inclusive to
16:00 exclusive, America/New_York. Holidays and early closes are not excluded.
market_hours_only false checks around the clock.

Price alerts are educational and are not financial advice.
"""
import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 24 * 60
DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_MAX_ALERTS = 10
MAX_ALERTS_CAP = 50
DEFAULT_BATCH_SIZE = 25
ALERTS_FILENAME = "pricealerts.json"
MARKET_TZ = ZoneInfo("America/New_York")
OPEN_MINUTE = 9 * 60 + 30
CLOSE_MINUTE = 16 * 60
DISCLAIMER = "Educational only, not financial advice."

_SYMBOL_RE = re.compile(r"\^?[A-Z0-9][A-Z0-9.\-=]{0,11}")
_SEPARATORS = ".-="


class AlertError(Exception):
    """A rejected alert or setting. The message is safe to show in Discord."""


class PriceError(Exception):
    """The price source failed for a whole batch."""


def normalize_symbol(raw) -> str:
    symbol = "" if raw is None else str(raw).strip().upper()
    if not symbol or len(symbol) > 12 or not _SYMBOL_RE.fullmatch(symbol):
        raise AlertError("Use a ticker like AAPL, BRK.B, or BTC-USD.")
    body = symbol[1:] if symbol.startswith("^") else symbol
    if not body or body[0] in _SEPARATORS or body[-1] in _SEPARATORS:
        raise AlertError("Use a ticker like AAPL, BRK.B, or BTC-USD.")
    if re.search(r"[.\-=]{2,}", body):
        raise AlertError("Use a ticker like AAPL, BRK.B, or BTC-USD.")
    return symbol


def normalize_direction(raw) -> str:
    if not isinstance(raw, str) and hasattr(raw, "value"):
        raw = raw.value
    text = str(raw or "").strip().lower()
    if text not in ("above", "below"):
        raise AlertError("Direction must be above or below.")
    return text


def normalize_price(raw) -> float:
    if isinstance(raw, bool) or raw is None or isinstance(raw, str) and not raw.strip():
        raise AlertError("Price must be a number greater than 0.")
    try:
        price = float(raw)
    except (TypeError, ValueError):
        raise AlertError("Price must be a number greater than 0.") from None
    if not math.isfinite(price) or price <= 0:
        raise AlertError("Price must be greater than 0.")
    price = round(price, 8)
    if price <= 0:
        raise AlertError("Price must be greater than 0.")
    return price


def normalize_interval_minutes(value) -> int:
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        raise AlertError("Interval must be a whole number of minutes.") from None
    if minutes < MIN_INTERVAL_MINUTES or minutes > MAX_INTERVAL_MINUTES:
        raise AlertError("Interval must be between 1 minute and 24 hours.")
    return minutes


def interval_from_config(value) -> int:
    """Config value used by the running cog. Missing or junk values fall back to 5.

    A number outside 1–1440 is clamped so a hand-edited file cannot poll faster
    than once a minute. /settings rejects those values instead of clamping.
    """
    if value in (None, ""):
        return DEFAULT_INTERVAL_MINUTES
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MINUTES
    return min(MAX_INTERVAL_MINUTES, max(MIN_INTERVAL_MINUTES, minutes))


def normalize_delivery(value) -> str:
    text = str(value or "channel").strip().lower()
    if text not in ("channel", "dm"):
        raise AlertError("Delivery must be channel or dm.")
    return text


def normalize_max_alerts(value) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise AlertError("The per-user alert cap must be a whole number.") from None
    if number < 1 or number > MAX_ALERTS_CAP:
        raise AlertError(f"The per-user alert cap must be between 1 and {MAX_ALERTS_CAP}.")
    return number


def as_bool(value, default=True) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    return default


def us_market_open(now: datetime) -> bool:
    """True during the regular US equity session.

    Weekdays, 09:30 inclusive through 16:00 exclusive, America/New_York.
    A naive datetime is treated as UTC. Holidays and early closes are not excluded.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(MARKET_TZ)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return OPEN_MINUTE <= minutes < CLOSE_MINUTE


def should_trigger(direction, target, current) -> bool:
    """Above fires at or above the target. Below fires at or below it."""
    if current is None:
        return False
    try:
        price = float(current)
        goal = float(target)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(price) or not math.isfinite(goal):
        return False
    if direction == "above":
        return price >= goal
    if direction == "below":
        return price <= goal
    return False


def format_price(price) -> str:
    value = float(price)
    if abs(value) >= 1:
        return f"{value:.2f}"
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def format_alert_message(alert, current, when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"<@{alert['user_id']}> **{alert['symbol']}** is {alert['direction']} "
        f"{format_price(alert['price'])}. Current price: {format_price(current)} at {stamp}.\n"
        f"{DISCLAIMER}"
    )


def _column_key(columns, symbol):
    wanted = symbol.upper()
    for column in columns:
        if str(column).upper() == wanted:
            return column
    return None


def _last_price(series):
    try:
        if series is None:
            return None
        if hasattr(series, "dropna"):
            series = series.dropna()
        if getattr(series, "empty", False):
            return None
        value = series.iloc[-1] if hasattr(series, "iloc") else series
        if hasattr(value, "iloc"):
            value = value.iloc[0]
        price = float(value)
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def prices_from_frame(frame, symbols):
    """Last Close from a yfinance download frame. Missing symbols map to None.

    Accepts group-by-ticker columns (symbol, field), group-by-column (field, symbol),
    and a flat Close column for a single symbol. Does not touch the network.
    """
    wanted = [str(symbol).upper() for symbol in symbols]
    out = {symbol: None for symbol in wanted}
    if frame is None:
        return out
    try:
        import pandas as pd
    except ImportError:
        return out
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return out
    columns = frame.columns
    if isinstance(columns, pd.MultiIndex):
        level0 = {str(value) for value in columns.get_level_values(0)}
        if "Close" in level0:
            closes = frame["Close"]
            if isinstance(closes, pd.Series):
                if len(wanted) == 1:
                    out[wanted[0]] = _last_price(closes)
                else:
                    name = str(getattr(closes, "name", "") or "").upper()
                    if name in out:
                        out[name] = _last_price(closes)
            else:
                for symbol in wanted:
                    key = _column_key(closes.columns, symbol)
                    if key is not None:
                        out[symbol] = _last_price(closes[key])
            return out
        for symbol in wanted:
            key = _column_key(columns.get_level_values(0), symbol)
            if key is None:
                continue
            sub = frame[key]
            # One field under the ticker comes back as a Series named Close.
            if isinstance(sub, pd.Series):
                if str(getattr(sub, "name", "") or "") in ("", "Close"):
                    out[symbol] = _last_price(sub)
                continue
            if "Close" in getattr(sub, "columns", []):
                out[symbol] = _last_price(sub["Close"])
        return out
    if "Close" in columns and len(wanted) == 1:
        out[wanted[0]] = _last_price(frame["Close"])
    return out


def _download(symbols, period, interval):
    import yfinance as yf
    return yf.download(
        tickers=list(symbols),
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=False,
        timeout=10,
    )


def fetch_yahoo(symbols):
    """Last prices for a batch of symbols. One download per chunk, then a daily fallback.

    Intraday bars are empty for some symbols outside the session, so a missing
    price is filled from the latest daily close when that second call works.
    """
    if not symbols:
        return {}
    try:
        import yfinance  # noqa: F401  (import error is the useful failure mode)
    except ImportError as exc:
        raise PriceError("yfinance is not installed.") from exc
    frame = _download(symbols, "1d", "1m")
    prices = prices_from_frame(frame, symbols)
    missing = [symbol for symbol in prices if prices[symbol] is None]
    if not missing:
        return prices
    try:
        daily = _download(missing, "5d", "1d")
    except Exception:
        log.warning("daily price fallback failed for %s", ",".join(missing), exc_info=True)
        return prices
    for symbol, price in prices_from_frame(daily, missing).items():
        if price is not None and prices.get(symbol) is None:
            prices[symbol] = price
    return prices


class PriceProvider:
    """Last traded price for many symbols at once. Implementations must not require an API key."""

    async def quotes(self, symbols):
        raise NotImplementedError


class YFinanceProvider(PriceProvider):
    """Yahoo Finance via yfinance. `fetch` is injectable so tests stay offline."""

    def __init__(self, *, batch_size=DEFAULT_BATCH_SIZE, fetch=None):
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.batch_size = int(batch_size)
        self._fetch = fetch or fetch_yahoo

    async def quotes(self, symbols):
        unique = []
        seen = set()
        for raw in symbols:
            symbol = str(raw).upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                unique.append(symbol)
        out = {symbol: None for symbol in unique}
        if not unique:
            return out
        failures = 0
        batches = 0
        for start in range(0, len(unique), self.batch_size):
            chunk = unique[start:start + self.batch_size]
            batches += 1
            try:
                got = await asyncio.to_thread(self._fetch, chunk)
            except Exception:
                failures += 1
                log.warning("price batch failed for %s", ",".join(chunk), exc_info=True)
                continue
            if not isinstance(got, dict):
                continue
            for symbol in chunk:
                try:
                    price = float(got.get(symbol))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(price) and price > 0:
                    out[symbol] = price
        if batches and failures == batches:
            raise PriceError("price source failed")
        return out


def _refuse_store_path(path: Path):
    name = path.name.lower()
    if name == ".env" or name.endswith(".env") or "token" in name:
        raise AlertError("Refusing to store alerts in that file.")
    env_path = os.environ.get("BOTKIT_ENV")
    if env_path:
        try:
            if path.resolve() == Path(env_path).resolve():
                raise AlertError("Refusing to store alerts in the environment file.")
        except OSError:
            pass


class AlertStore:
    """JSON list of alerts. save() writes a temp file and replaces the destination."""

    def __init__(self, path):
        self.path = Path(path)
        self.alerts = []
        self.next_id = 1

    def load(self):
        if not self.path.exists():
            self.alerts = []
            self.next_id = 1
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AlertError(f"Couldn't read saved alerts from {self.path.name}.") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("alerts"), list):
            raise AlertError(f"Saved alerts in {self.path.name} are not in the expected format.")
        alerts = []
        for item in raw["alerts"]:
            if isinstance(item, dict) and "id" in item and "user_id" in item:
                alerts.append(dict(item))
        self.alerts = alerts
        try:
            self.next_id = max(1, int(raw.get("next_id") or 1))
        except (TypeError, ValueError):
            self.next_id = 1
        if self.alerts:
            highest = max(int(item["id"]) for item in self.alerts)
            self.next_id = max(self.next_id, highest + 1)

    def save(self):
        _refuse_store_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"next_id": self.next_id, "alerts": self.alerts},
            indent=2,
            ensure_ascii=False,
        ) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    def for_user(self, user_id):
        uid = str(user_id)
        return [dict(alert) for alert in self.alerts if str(alert.get("user_id")) == uid]

    def add(self, *, user_id, symbol, direction, price, guild_id=None, cap=DEFAULT_MAX_ALERTS, now=None):
        if len(self.for_user(user_id)) >= cap:
            raise AlertError(f"You can have at most {cap} alerts. Remove one with /alert remove.")
        stamp = now or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        alert = {
            "id": self.next_id,
            "user_id": str(user_id),
            "guild_id": None if guild_id in (None, "") else str(guild_id),
            "symbol": symbol,
            "direction": direction,
            "price": price,
            "created_at": stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.next_id += 1
        self.alerts.append(alert)
        self.save()
        return dict(alert)

    def remove_for_user(self, user_id, alert_id):
        uid = str(user_id)
        try:
            number = int(alert_id)
        except (TypeError, ValueError):
            raise AlertError("That alert id is not a number.") from None
        for index, alert in enumerate(self.alerts):
            if int(alert["id"]) == number and str(alert.get("user_id")) == uid:
                removed = self.alerts.pop(index)
                self.save()
                return removed
        raise AlertError("You don't have an alert with that id.")

    def remove_id(self, alert_id):
        number = int(alert_id)
        self.alerts = [alert for alert in self.alerts if int(alert["id"]) != number]


def data_dir_for(bot) -> Path:
    """Per-bot data directory: pricealerts.data_dir, else a data folder beside config.json."""
    config = getattr(bot, "config", None)
    section = {}
    if isinstance(config, dict) and isinstance(config.get("pricealerts"), dict):
        section = config["pricealerts"]
    custom = section.get("data_dir")
    if custom not in (None, ""):
        return Path(str(custom))
    config_path = getattr(bot, "config_path", None)
    if not config_path:
        env = os.environ.get("BOTKIT_CONFIG")
        if env:
            config_path = env
    if config_path:
        return Path(config_path).resolve().parent / "data"
    return Path(__file__).resolve().parent.parent / "data"


def alerts_path_for(bot) -> Path:
    return data_dir_for(bot) / ALERTS_FILENAME


def _section(bot):
    config = getattr(bot, "config", None)
    if not isinstance(config, dict):
        return {}
    section = config.get("pricealerts")
    return section if isinstance(section, dict) else {}


class PriceAlerts(commands.Cog):
    """Slash commands and the price poll. The poll starts in cog_load, once a loop is running."""

    alert = app_commands.Group(name="alert", description="Set or manage your stock price alerts")

    def __init__(self, bot, *, provider=None, store=None, clock=None):
        self.bot = bot
        self.provider = provider or YFinanceProvider()
        self.store = store if store is not None else AlertStore(alerts_path_for(bot))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self.channel_id = None
        self.interval_minutes = DEFAULT_INTERVAL_MINUTES
        self.market_hours_only = True
        self.delivery = "channel"
        self.max_alerts = DEFAULT_MAX_ALERTS
        self.store.load()
        self.apply_config()

    def apply_config(self):
        """Re-read channel, interval, market hours, delivery, and the per-user cap."""
        cfg = _section(self.bot)
        channel = cfg.get("channel_id")
        try:
            self.channel_id = int(channel) if channel not in (None, "") else None
        except (TypeError, ValueError):
            self.channel_id = None
        self.interval_minutes = interval_from_config(cfg.get("interval_minutes"))
        self.market_hours_only = as_bool(cfg.get("market_hours_only"), True)
        try:
            self.delivery = normalize_delivery(cfg.get("delivery", "channel"))
        except AlertError:
            self.delivery = "channel"
        try:
            raw_cap = cfg.get("max_alerts_per_user", DEFAULT_MAX_ALERTS)
            self.max_alerts = normalize_max_alerts(
                DEFAULT_MAX_ALERTS if raw_cap in (None, "") else raw_cap)
        except AlertError:
            self.max_alerts = DEFAULT_MAX_ALERTS
        try:
            batch_size = int(cfg.get("batch_size", DEFAULT_BATCH_SIZE))
        except (TypeError, ValueError):
            batch_size = DEFAULT_BATCH_SIZE
        if batch_size < 1:
            batch_size = DEFAULT_BATCH_SIZE
        if isinstance(self.provider, YFinanceProvider):
            self.provider.batch_size = batch_size
        # Seconds, not minutes: discord.py stores only the unit you pass, and the
        # sleep is their sum. One unit keeps the running interval obvious.
        self.poll.change_interval(seconds=self.interval_minutes * 60)

    async def cog_load(self):
        if not self.poll.is_running():
            self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

    def _now(self):
        now = self.clock()
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now

    async def _deliver(self, alert, text):
        if self.delivery == "dm":
            user = None
            getter = getattr(self.bot, "get_user", None)
            if callable(getter):
                user = getter(int(alert["user_id"]))
            if user is None:
                user = await self.bot.fetch_user(int(alert["user_id"]))
            await user.send(text)
            return
        if not self.channel_id:
            raise AlertError("No alerts channel is configured.")
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(self.channel_id)
        await channel.send(text)

    async def run_poll(self, *, now=None):
        """Check every saved alert once. Returns the alerts that were delivered and removed."""
        async with self._lock:
            when = now or self._now()
            if self.market_hours_only and not us_market_open(when):
                return []
            alerts = [dict(alert) for alert in self.store.alerts]
            if not alerts:
                return []
            symbols = []
            seen = set()
            for alert in alerts:
                symbol = alert.get("symbol")
                if symbol and symbol not in seen:
                    seen.add(symbol)
                    symbols.append(symbol)
            try:
                quotes = await self.provider.quotes(symbols)
            except Exception:
                log.exception("price poll failed")
                return []
            if not isinstance(quotes, dict):
                return []
            triggered = []
            for alert in alerts:
                current = quotes.get(alert.get("symbol"))
                if not should_trigger(alert.get("direction"), alert.get("price"), current):
                    continue
                try:
                    await self._deliver(alert, format_alert_message(alert, current, when))
                except Exception:
                    log.exception("price alert %s failed to send", alert.get("id"))
                    continue
                self.store.remove_id(alert["id"])
                triggered.append(alert)
            if triggered:
                try:
                    self.store.save()
                except OSError:
                    log.exception("couldn't save price alerts after a trigger")
            return triggered

    @tasks.loop(seconds=DEFAULT_INTERVAL_MINUTES * 60)
    async def poll(self):
        try:
            await self.run_poll()
        except Exception:
            log.exception("price alert poll failed")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    def _user_id(self, interaction):
        user = getattr(interaction, "user", None)
        user_id = getattr(user, "id", None)
        if user_id is None:
            raise AlertError("Couldn't tell who you are.")
        return user_id

    @alert.command(name="add", description="Alert when a symbol crosses a price")
    @app_commands.describe(
        symbol="Ticker, such as AAPL or BTC-USD",
        direction="Alert when the price is above or below the target",
        price="Target price, greater than 0",
    )
    @app_commands.choices(direction=[
        app_commands.Choice(name="above", value="above"),
        app_commands.Choice(name="below", value="below"),
    ])
    async def add(self, interaction: discord.Interaction, symbol: str, direction: str,
                  price: app_commands.Range[float, 0.00000001, 1_000_000_000.0]):
        try:
            chosen = normalize_symbol(symbol)
            way = normalize_direction(direction)
            target = normalize_price(price)
            user_id = self._user_id(interaction)
        except AlertError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        guild = getattr(interaction, "guild", None)
        async with self._lock:
            try:
                alert = self.store.add(
                    user_id=user_id,
                    symbol=chosen,
                    direction=way,
                    price=target,
                    guild_id=getattr(guild, "id", None),
                    cap=self.max_alerts,
                    now=self._now(),
                )
            except AlertError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Alert {alert['id']} set: **{chosen}** {way} {format_price(target)}. "
            f"It is removed after it triggers.\n{DISCLAIMER}",
            ephemeral=True,
        )

    @alert.command(name="list", description="List your price alerts")
    async def list_alerts(self, interaction: discord.Interaction):
        try:
            user_id = self._user_id(interaction)
        except AlertError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        alerts = self.store.for_user(user_id)
        if not alerts:
            await interaction.response.send_message("You have no price alerts.", ephemeral=True)
            return
        lines = [f"Your price alerts ({len(alerts)}/{self.max_alerts}):"]
        for alert in alerts:
            lines.append(
                f"`{alert['id']}` **{alert['symbol']}** {alert['direction']} {format_price(alert['price'])}")
        lines.append(DISCLAIMER)
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @alert.command(name="remove", description="Remove one of your price alerts")
    @app_commands.describe(alert_id="Id from /alert list")
    async def remove(self, interaction: discord.Interaction, alert_id: app_commands.Range[int, 1]):
        try:
            user_id = self._user_id(interaction)
        except AlertError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        async with self._lock:
            try:
                removed = self.store.remove_for_user(user_id, alert_id)
            except AlertError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"Removed alert {removed['id']} ({removed['symbol']} {removed['direction']} "
            f"{format_price(removed['price'])}).",
            ephemeral=True,
        )

    @app_commands.command(name="price", description="Show the latest price for a symbol")
    @app_commands.describe(symbol="Ticker, such as AAPL or BTC-USD")
    async def price(self, interaction: discord.Interaction, symbol: str):
        try:
            chosen = normalize_symbol(symbol)
        except AlertError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer()
        try:
            quotes = await self.provider.quotes([chosen])
        except Exception:
            log.exception("quote failed for %s", chosen)
            await interaction.followup.send(f"Couldn't fetch a price right now.\n{DISCLAIMER}")
            return
        current = quotes.get(chosen) if isinstance(quotes, dict) else None
        if current is None:
            await interaction.followup.send(f"No price for **{chosen}**.\n{DISCLAIMER}")
            return
        await interaction.followup.send(f"**{chosen}** is {format_price(current)}.\n{DISCLAIMER}")


async def setup(bot):
    await bot.add_cog(PriceAlerts(bot))
