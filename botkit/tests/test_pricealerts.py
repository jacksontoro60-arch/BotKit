"""Offline tests for price alerts. No token, no network, no yfinance calls.

Run from the kit folder:  python -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord

from cogs import admin
from cogs.admin import Admin, SettingError, change_settings, render_settings, setting_choices
from cogs.pricealerts import (
    DISCLAIMER,
    AlertError,
    AlertStore,
    PriceAlerts,
    PriceError,
    YFinanceProvider,
    data_dir_for,
    fetch_yahoo,
    normalize_direction,
    normalize_price,
    normalize_symbol,
    prices_from_frame,
    should_trigger,
    us_market_open,
)

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 11, 12, 15, 0, tzinfo=ET)  # Thursday 20:00 UTC, session open
CLOSED = datetime(2026, 11, 14, 12, 0, tzinfo=ET)  # Saturday


def run(coro):
    return asyncio.run(coro)


class FakeProvider:
    def __init__(self, prices=None, error=None):
        self.prices = dict(prices or {})
        self.error = error
        self.calls = []

    async def quotes(self, symbols):
        self.calls.append(list(symbols))
        if self.error:
            raise self.error
        return {symbol: self.prices.get(symbol) for symbol in symbols}


def perms(**flags):
    return SimpleNamespace(
        administrator=flags.get("administrator", False),
        manage_channels=flags.get("manage_channels", False),
        manage_threads=flags.get("manage_threads", False),
    )


def member(role_ids=(), **flags):
    return SimpleNamespace(
        id=42, roles=[SimpleNamespace(id=rid) for rid in role_ids], guild_permissions=perms(**flags))


def config(**overrides):
    section = {
        "channel_id": "55",
        "interval_minutes": 5,
        "market_hours_only": True,
        "delivery": "channel",
        "max_alerts_per_user": 10,
    }
    section.update(overrides)
    return {
        "guild_id": "900",
        "features": ["admin", "pricealerts"],
        "admin": {"staff_role_id": "8"},
        "pricealerts": section,
    }


def make_bot(cfg, channel=None, user=None):
    channel = channel or MagicMock()
    channel.send = AsyncMock()
    user = user or MagicMock()
    user.send = AsyncMock()
    bot = SimpleNamespace(
        config=cfg,
        config_path=None,
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=channel),
        get_user=MagicMock(return_value=user),
        fetch_user=AsyncMock(return_value=user),
        wait_until_ready=AsyncMock(),
        get_cog=lambda name: None,
    )
    return bot, channel, user


def interaction(user_id=7, guild_id=900, user=None):
    item = MagicMock()
    item.user = user if user is not None else SimpleNamespace(id=user_id)
    item.guild = SimpleNamespace(id=guild_id)
    item.response.send_message = AsyncMock()
    item.response.defer = AsyncMock()
    item.followup.send = AsyncMock()
    item.client = None
    return item


def sent(item):
    return item.response.send_message.await_args.args[0]


def cog_in(tmp, cfg=None, provider=None, clock=None):
    cfg = config() if cfg is None else cfg
    bot, channel, user = make_bot(cfg)
    store = AlertStore(Path(tmp) / "pricealerts.json")
    cog = PriceAlerts(
        bot,
        provider=provider or FakeProvider(),
        store=store,
        clock=clock or (lambda: OPEN),
    )
    return cog, bot, channel, user


class Rules(unittest.TestCase):
    def test_symbol_and_price_validation(self):
        self.assertEqual(normalize_symbol(" aapl "), "AAPL")
        self.assertEqual(normalize_symbol("brk.b"), "BRK.B")
        self.assertEqual(normalize_symbol("btc-usd"), "BTC-USD")
        self.assertEqual(normalize_symbol("^gspc"), "^GSPC")
        self.assertEqual(normalize_symbol("0700.hk"), "0700.HK")
        for bad in ("", "   ", "AA PL", "AAPL;DROP", "../AAPL", "AAPL.", "-AAPL", "A" * 13, "$$$"):
            with self.assertRaises(AlertError):
                normalize_symbol(bad)
        self.assertEqual(normalize_price("150.5"), 150.5)
        self.assertEqual(normalize_price(1), 1.0)
        for bad in (0, -1, "nope", None, True, float("nan"), float("inf"), "0"):
            with self.assertRaises(AlertError):
                normalize_price(bad)
        self.assertEqual(normalize_direction("Above"), "above")
        with self.assertRaises(AlertError):
            normalize_direction("sideways")

    def test_trigger_above_and_below(self):
        self.assertTrue(should_trigger("above", 100, 100))
        self.assertTrue(should_trigger("above", 100, 100.01))
        self.assertFalse(should_trigger("above", 100, 99.99))
        self.assertTrue(should_trigger("below", 50, 50))
        self.assertTrue(should_trigger("below", 50, 49.5))
        self.assertFalse(should_trigger("below", 50, 50.01))
        self.assertFalse(should_trigger("above", 100, None))
        self.assertFalse(should_trigger("sideways", 100, 1000))
        self.assertFalse(should_trigger("above", 100, float("nan")))

    def test_market_hours(self):
        self.assertTrue(us_market_open(datetime(2026, 11, 12, 9, 30, tzinfo=ET)))
        self.assertFalse(us_market_open(datetime(2026, 11, 12, 9, 29, tzinfo=ET)))
        self.assertTrue(us_market_open(datetime(2026, 11, 12, 15, 59, tzinfo=ET)))
        self.assertFalse(us_market_open(datetime(2026, 11, 12, 16, 0, tzinfo=ET)))
        self.assertFalse(us_market_open(CLOSED))
        self.assertFalse(us_market_open(datetime(2026, 11, 15, 12, 0, tzinfo=ET)))
        self.assertTrue(us_market_open(datetime(2026, 11, 16, 9, 30, tzinfo=ET)))
        # November is EST (UTC-5); July is EDT (UTC-4).
        self.assertTrue(us_market_open(datetime(2026, 11, 12, 14, 30, tzinfo=timezone.utc)))
        self.assertFalse(us_market_open(datetime(2026, 11, 12, 14, 29, tzinfo=timezone.utc)))
        self.assertTrue(us_market_open(datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)))
        self.assertFalse(us_market_open(datetime(2026, 7, 13, 13, 29, tzinfo=timezone.utc)))
        self.assertTrue(us_market_open(datetime(2026, 11, 12, 14, 30)))  # naive means UTC


class Persistence(unittest.TestCase):
    def test_round_trip_and_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "pricealerts.json"
            store = AlertStore(path)
            store.load()
            first = store.add(user_id=7, symbol="AAPL", direction="above", price=150, guild_id=900, now=OPEN)
            second = store.add(user_id=7, symbol="MSFT", direction="below", price=400, now=OPEN)
            self.assertEqual((first["id"], second["id"]), (1, 2))
            self.assertFalse(path.with_name(path.name + ".tmp").exists())
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("DISCORD_TOKEN", text)
            loaded = AlertStore(path)
            loaded.load()
            self.assertEqual(loaded.alerts, store.alerts)
            self.assertEqual(loaded.next_id, 3)
            loaded.remove_for_user(7, 1)
            again = AlertStore(path)
            again.load()
            self.assertEqual([item["id"] for item in again.alerts], [2])
            added = again.add(user_id=7, symbol="GOOG", direction="above", price=10, now=OPEN)
            self.assertEqual(added["id"], 3)

    def test_per_user_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AlertStore(Path(tmp) / "pricealerts.json")
            store.load()
            for _ in range(10):
                store.add(user_id=7, symbol="AAPL", direction="above", price=1, cap=10)
            with self.assertRaises(AlertError) as caught:
                store.add(user_id=7, symbol="AAPL", direction="below", price=1, cap=10)
            self.assertIn("10", str(caught.exception))
            self.assertEqual(len(store.for_user(7)), 10)
            other = store.add(user_id=8, symbol="MSFT", direction="above", price=2, cap=10)
            self.assertEqual(other["user_id"], "8")
            self.assertEqual(len(store.alerts), 11)

    def test_corrupt_file_is_not_wiped_and_env_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pricealerts.json"
            path.write_text("{", encoding="utf-8")
            store = AlertStore(path)
            with self.assertRaises(AlertError):
                store.load()
            self.assertEqual(path.read_text(encoding="utf-8"), "{")
            env = Path(tmp) / ".env"
            env.write_text("DISCORD_TOKEN=keep-this\n", encoding="utf-8")
            secret_store = AlertStore(env)
            secret_store.alerts = [{"id": 1, "user_id": "1"}]
            with self.assertRaises(AlertError):
                secret_store.save()
            self.assertEqual(env.read_text(encoding="utf-8"), "DISCORD_TOKEN=keep-this\n")

    def test_data_dir_follows_the_config_file(self):
        bot = SimpleNamespace(config={}, config_path=Path("/tmp/clients/acme/config.json"))
        self.assertEqual(data_dir_for(bot), Path("/tmp/clients/acme/data"))
        bot.config = {"pricealerts": {"data_dir": "/var/botkit/alerts"}}
        self.assertEqual(data_dir_for(bot), Path("/var/botkit/alerts"))


class Poll(unittest.TestCase):
    def test_loaded_cog_polls_on_the_configured_interval(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                cog, bot, *_rest = cog_in(tmp)
                self.assertFalse(cog.poll.is_running())
                self.assertEqual(cog.poll._sleep, 5 * 60)
                await cog.cog_load()
                self.assertTrue(cog.poll.is_running())
                task = cog.poll.get_task()
                cog.cog_unload()
                if task is not None:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                self.assertFalse(cog.poll.is_running())

        run(body())

    def _seed(self, cog):
        cog.store.add(user_id=7, symbol="AAPL", direction="above", price=100, guild_id=900, now=OPEN)
        cog.store.add(user_id=7, symbol="MSFT", direction="above", price=100, now=OPEN)
        cog.store.add(user_id=8, symbol="GOOG", direction="below", price=50, now=OPEN)
        cog.store.add(user_id=8, symbol="TSLA", direction="below", price=50, now=OPEN)
        cog.store.add(user_id=7, symbol="IBM", direction="above", price=10, now=OPEN)

    def test_triggers_above_and_below_then_removes_them(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                provider = FakeProvider({
                    "AAPL": 101.25, "MSFT": 99, "GOOG": 49.5, "TSLA": 51, "IBM": 10,
                })
                cog, _bot, channel, user = cog_in(tmp, provider=provider)
                self._seed(cog)
                triggered = await cog.run_poll()
                self.assertEqual(provider.calls, [["AAPL", "MSFT", "GOOG", "TSLA", "IBM"]])
                self.assertEqual(sorted(item["symbol"] for item in triggered), ["AAPL", "GOOG", "IBM"])
                self.assertEqual(sorted(item["symbol"] for item in cog.store.alerts), ["MSFT", "TSLA"])
                texts = [call.args[0] for call in channel.send.await_args_list]
                self.assertEqual(len(texts), 3)
                apple = next(text for text in texts if "AAPL" in text)
                self.assertIn("<@7>", apple)
                self.assertIn("above", apple)
                self.assertIn("100.00", apple)
                self.assertIn("101.25", apple)
                self.assertIn("2026-11-12 20:00 UTC", apple)
                self.assertIn(DISCLAIMER, apple)
                google = next(text for text in texts if "GOOG" in text)
                self.assertIn("<@8>", google)
                self.assertIn("below", google)
                self.assertIn("49.50", google)
                user.send.assert_not_awaited()
                reloaded = AlertStore(cog.store.path)
                reloaded.load()
                self.assertEqual(sorted(item["symbol"] for item in reloaded.alerts), ["MSFT", "TSLA"])
                await cog.run_poll()
                self.assertEqual(channel.send.await_count, 3)
                self.assertEqual(provider.calls[-1], ["MSFT", "TSLA"])

        run(body())

    def test_market_hours_gate_and_around_the_clock(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                provider = FakeProvider({"AAPL": 200})
                cog, _bot, channel, _user = cog_in(tmp, provider=provider, clock=lambda: CLOSED)
                cog.store.add(user_id=7, symbol="AAPL", direction="above", price=100, now=CLOSED)
                self.assertEqual(await cog.run_poll(), [])
                self.assertEqual(provider.calls, [])
                channel.send.assert_not_awaited()
                self.assertEqual(len(cog.store.alerts), 1)

                cog.bot.config["pricealerts"]["market_hours_only"] = False
                cog.apply_config()
                triggered = await cog.run_poll()
                self.assertEqual([item["symbol"] for item in triggered], ["AAPL"])
                self.assertEqual(provider.calls, [["AAPL"]])
                channel.send.assert_awaited()
                self.assertEqual(cog.store.alerts, [])

                quiet = FakeProvider({"AAPL": 200})
                empty, _bot, _channel, _user = cog_in(
                    Path(tmp) / "empty", provider=quiet, clock=lambda: OPEN)
                self.assertEqual(await empty.run_poll(), [])
                self.assertEqual(quiet.calls, [])

        run(body())

    def test_failed_send_keeps_the_alert_and_dm_delivery(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                provider = FakeProvider({"AAPL": 20})
                cog, _bot, channel, user = cog_in(tmp, provider=provider)
                cog.store.add(user_id=7, symbol="AAPL", direction="below", price=25, now=OPEN)
                channel.send = AsyncMock(side_effect=RuntimeError("discord down"))
                self.assertEqual(await cog.run_poll(), [])
                self.assertEqual(len(cog.store.alerts), 1)
                reloaded = AlertStore(cog.store.path)
                reloaded.load()
                self.assertEqual(len(reloaded.alerts), 1)

                channel.send = AsyncMock()
                cog.bot.config["pricealerts"]["delivery"] = "dm"
                cog.apply_config()
                await cog.run_poll()
                user.send.assert_awaited()
                self.assertIn("<@7>", user.send.await_args.args[0])
                self.assertIn("below", user.send.await_args.args[0])
                channel.send.assert_not_awaited()
                self.assertEqual(cog.store.alerts, [])

        run(body())


class Commands(unittest.TestCase):
    def test_commands_are_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            cog, *_ = cog_in(tmp)
            names = sorted(command.name for command in cog.get_app_commands())
            self.assertEqual(names, ["alert", "price"])
            group = next(command for command in cog.get_app_commands() if command.name == "alert")
            self.assertEqual(sorted(command.name for command in group.commands), ["add", "list", "remove"])
            add = group.get_command("add")
            self.assertEqual([param.name for param in add.parameters], ["symbol", "direction", "price"])
            self.assertEqual([param.name for param in group.get_command("remove").parameters], ["alert_id"])
            self.assertEqual([param.name for param in cog.price.parameters], ["symbol"])

    def test_add_list_remove_and_validation(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                cfg = config(max_alerts_per_user=2)
                provider = FakeProvider({"AAPL": 10})
                cog, _bot, _channel, _user = cog_in(tmp, cfg=cfg, provider=provider)
                bad_symbol = interaction()
                await PriceAlerts.add.callback(cog, bad_symbol, "not a ticker!!", "above", 10)
                self.assertIn("ticker", sent(bad_symbol).lower())
                self.assertTrue(bad_symbol.response.send_message.await_args.kwargs["ephemeral"])
                self.assertEqual(cog.store.alerts, [])

                bad_price = interaction()
                await PriceAlerts.add.callback(cog, bad_price, "AAPL", "above", 0)
                self.assertIn("greater than 0", sent(bad_price))

                negative = interaction()
                await PriceAlerts.add.callback(cog, negative, "AAPL", "below", -5)
                self.assertIn("greater than 0", sent(negative))

                bad_way = interaction()
                await PriceAlerts.add.callback(cog, bad_way, "AAPL", "sideways", 10)
                self.assertIn("above or below", sent(bad_way))
                self.assertEqual(provider.calls, [])

                ok = interaction(user_id=7)
                await PriceAlerts.add.callback(cog, ok, "brk.b", "above", 150)
                self.assertIn("Alert 1", sent(ok))
                self.assertIn("BRK.B", sent(ok))
                self.assertIn(DISCLAIMER, sent(ok))
                self.assertTrue(ok.response.send_message.await_args.kwargs["ephemeral"])
                self.assertEqual(cog.store.alerts[0]["symbol"], "BRK.B")
                self.assertEqual(cog.store.alerts[0]["user_id"], "7")

                await PriceAlerts.add.callback(cog, interaction(user_id=7), "AAPL", "below", 20)
                capped = interaction(user_id=7)
                await PriceAlerts.add.callback(cog, capped, "MSFT", "above", 30)
                self.assertIn("at most 2", sent(capped))
                self.assertEqual(len(cog.store.for_user(7)), 2)
                await PriceAlerts.add.callback(cog, interaction(user_id=9), "MSFT", "above", 30)
                self.assertEqual(len(cog.store.for_user(9)), 1)

                listing = interaction(user_id=7)
                await PriceAlerts.list_alerts.callback(cog, listing)
                shown = sent(listing)
                self.assertIn("BRK.B", shown)
                self.assertIn("AAPL", shown)
                self.assertNotIn("MSFT", shown)
                self.assertTrue(listing.response.send_message.await_args.kwargs["ephemeral"])

                stolen = interaction(user_id=9)
                await PriceAlerts.remove.callback(cog, stolen, 1)
                self.assertIn("don't have", sent(stolen))
                self.assertEqual(len(cog.store.alerts), 3)

                gone = interaction(user_id=7)
                await PriceAlerts.remove.callback(cog, gone, 1)
                self.assertIn("Removed alert 1", sent(gone))
                self.assertEqual([item["id"] for item in cog.store.for_user(7)], [2])
                reloaded = AlertStore(cog.store.path)
                reloaded.load()
                self.assertEqual([item["id"] for item in reloaded.for_user(7)], [2])

        run(body())

    def test_price_quote(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                provider = FakeProvider({"AAPL": 188.5})
                cog, *_ = cog_in(tmp, provider=provider)
                bad = interaction()
                await PriceAlerts.price.callback(cog, bad, "nope nope")
                self.assertIn("ticker", sent(bad).lower())
                bad.response.defer.assert_not_awaited()
                self.assertEqual(provider.calls, [])

                quote = interaction()
                await PriceAlerts.price.callback(cog, quote, "aapl")
                quote.response.defer.assert_awaited()
                text = quote.followup.send.await_args.args[0]
                self.assertIn("AAPL", text)
                self.assertIn("188.50", text)
                self.assertIn(DISCLAIMER, text)
                self.assertEqual(provider.calls, [["AAPL"]])

                missing = FakeProvider({})
                cog.provider = missing
                none = interaction()
                await PriceAlerts.price.callback(cog, none, "ZZZZ")
                self.assertIn("No price", none.followup.send.await_args.args[0])

                cog.provider = FakeProvider(error=PriceError("down"))
                failed = interaction()
                await PriceAlerts.price.callback(cog, failed, "AAPL")
                self.assertIn("Couldn't fetch", failed.followup.send.await_args.args[0])

        run(body())


class Provider(unittest.TestCase):
    def test_batches_and_dedupes_without_network(self):
        seen = []

        def fetch(chunk):
            seen.append(list(chunk))
            if chunk == ["AAPL", "MSFT"]:
                raise RuntimeError("down")
            return {symbol: 3 if symbol != "BAD" else 0 for symbol in chunk}

        async def body():
            provider = YFinanceProvider(batch_size=2, fetch=fetch)
            self.assertEqual(await provider.quotes([]), {})
            prices = await provider.quotes(["aapl", "AAPL", "msft", "goog", "bad"])
            self.assertEqual(prices["GOOG"], 3)
            self.assertIsNone(prices["AAPL"])
            self.assertIsNone(prices["BAD"])
            self.assertEqual(seen, [["AAPL", "MSFT"], ["GOOG", "BAD"]])
            with self.assertRaises(PriceError):
                await YFinanceProvider(batch_size=2, fetch=lambda chunk: (_ for _ in ()).throw(RuntimeError("down"))).quotes(["AAPL"])

        run(body())

    def test_frame_parser_and_yahoo_batch_stay_offline(self):
        import pandas as pd

        def ticker_frame(columns):
            data = {symbol: values for symbol, values in columns.items()}
            frame = pd.DataFrame(data)
            frame.columns = pd.MultiIndex.from_tuples(
                [(symbol, "Close") for symbol in columns], names=["Ticker", "Price"])
            return frame

        parsed = prices_from_frame(ticker_frame({"AAPL": [100.0, 101.5], "MSFT": [5.0, float("nan")]}), ["AAPL", "MSFT"])
        self.assertEqual(parsed["AAPL"], 101.5)
        self.assertEqual(parsed["MSFT"], 5.0)
        flat = pd.DataFrame({"Close": [10.0, float("nan")]})
        self.assertEqual(prices_from_frame(flat, ["IBM"]), {"IBM": 10.0})
        self.assertEqual(prices_from_frame(None, ["IBM"]), {"IBM": None})
        column_grouped = pd.DataFrame({("Close", "AAPL"): [42.0]})
        column_grouped.columns = pd.MultiIndex.from_tuples(column_grouped.columns, names=["Price", "Ticker"])
        self.assertEqual(prices_from_frame(column_grouped, ["AAPL"]), {"AAPL": 42.0})

        intraday = ticker_frame({"AAPL": [101.5], "MSFT": [float("nan")]})
        daily = ticker_frame({"MSFT": [200.25]})
        with patch("yfinance.download", side_effect=[intraday, daily]) as download:
            prices = fetch_yahoo(["AAPL", "MSFT"])
        self.assertEqual(download.call_count, 2)
        first = download.call_args_list[0].kwargs
        self.assertEqual(list(first["tickers"]), ["AAPL", "MSFT"])
        self.assertEqual((first["period"], first["interval"], first["progress"], first["threads"]), ("1d", "1m", False, False))
        second = download.call_args_list[1].kwargs
        self.assertEqual(list(second["tickers"]), ["MSFT"])
        self.assertEqual(second["period"], "5d")
        self.assertEqual(prices, {"AAPL": 101.5, "MSFT": 200.25})

        with patch("yfinance.download", return_value=ticker_frame({"AAPL": [8.0]})) as download:
            self.assertEqual(fetch_yahoo(["AAPL"]), {"AAPL": 8.0})
        self.assertEqual(download.call_count, 1)


class Settings(unittest.TestCase):
    def test_catalog_validation_and_show(self):
        cfg = config()
        updated = change_settings(cfg, "pricealerts.interval_minutes", "2")
        self.assertEqual(updated["pricealerts"]["interval_minutes"], 2)
        self.assertEqual(cfg["pricealerts"]["interval_minutes"], 5)
        for bad in ("0", "1", "1441", "soon"):
            if bad == "1":
                self.assertEqual(change_settings(cfg, "pricealerts.interval_minutes", bad)["pricealerts"]["interval_minutes"], 1)
                continue
            with self.assertRaises(SettingError):
                change_settings(cfg, "pricealerts.interval_minutes", bad)
        hours = change_settings(cfg, "pricealerts.market_hours_only", "off")
        self.assertFalse(hours["pricealerts"]["market_hours_only"])
        dm = change_settings(cfg, "pricealerts.delivery", "dm")
        self.assertEqual(dm["pricealerts"]["delivery"], "dm")
        capped = change_settings(cfg, "pricealerts.max_per_user", "3")
        self.assertEqual(capped["pricealerts"]["max_alerts_per_user"], 3)
        with self.assertRaises(SettingError):
            change_settings(cfg, "pricealerts.max_per_user", "51")
        shown = render_settings(cfg, "prices")
        self.assertIn("<#55>", shown)
        self.assertIn("pricealerts.interval_minutes: 5", shown)
        self.assertIn("market_hours_only: True", shown)
        choices = setting_choices(cfg, "price")
        self.assertIn("pricealerts.interval_minutes", [choice.value for choice in choices])
        disabled = config()
        disabled["features"] = ["admin"]
        with self.assertRaises(SettingError):
            change_settings(disabled, "pricealerts.interval_minutes", "5")
        dm_only = config(delivery="dm")
        dm_only["pricealerts"].pop("channel_id")
        with self.assertRaises(SettingError):
            change_settings(dm_only, "pricealerts.delivery", "channel")

    def test_settings_apply_live(self):
        async def body():
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                cfg = config()
                path.write_text(json.dumps(cfg), encoding="utf-8")
                cog, bot, _channel, _user = cog_in(tmp, cfg=cfg)
                bot.config_path = path
                cog.store.add(user_id=7, symbol="AAPL", direction="above", price=10, now=OPEN)
                admin_cog = Admin(bot)
                cogs = {"PriceAlerts": cog, "Admin": admin_cog}
                bot.get_cog = lambda name: cogs.get(name)
                staff = interaction(user=member((8,)))
                staff.client = bot
                await Admin.set_setting.callback(admin_cog, staff, "pricealerts.interval_minutes", "2")
                reply = sent(staff)
                self.assertIn("Saved pricealerts.interval_minutes", reply)
                self.assertIn("in effect", reply)
                self.assertIn("next price check", reply.lower())
                self.assertTrue(staff.response.send_message.await_args.kwargs["ephemeral"])
                self.assertEqual(cog.interval_minutes, 2)
                self.assertEqual(cog.poll._sleep, 120)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pricealerts"]["interval_minutes"], 2)
                self.assertEqual(len(cog.store.alerts), 1)

                channel = SimpleNamespace(id=77, type=discord.ChannelType.text)
                await Admin.set_setting.callback(admin_cog, staff, "pricealerts.channel", None, channel)
                self.assertEqual(cog.channel_id, 77)

                await Admin.set_setting.callback(admin_cog, staff, "pricealerts.market_hours_only", "false")
                self.assertFalse(cog.market_hours_only)
                await Admin.set_setting.callback(admin_cog, staff, "pricealerts.delivery", "dm")
                self.assertEqual(cog.delivery, "dm")
                await Admin.set_setting.callback(admin_cog, staff, "pricealerts.max_per_user", "4")
                self.assertEqual(cog.max_alerts, 4)
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["pricealerts"]["channel_id"], "77")
                self.assertEqual(saved["pricealerts"]["delivery"], "dm")
                self.assertFalse(saved["pricealerts"]["market_hours_only"])
                self.assertEqual(saved["pricealerts"]["max_alerts_per_user"], 4)
                self.assertEqual(cog.store.alerts[0]["symbol"], "AAPL")
                self.assertFalse((path.parent / (path.name + ".tmp")).exists())

                rejected = interaction(user=member((8,)))
                await Admin.set_setting.callback(admin_cog, rejected, "pricealerts.interval_minutes", "0")
                self.assertIn("between 1 minute", sent(rejected))
                self.assertEqual(cog.interval_minutes, 2)

        run(body())


if __name__ == "__main__":
    unittest.main()
