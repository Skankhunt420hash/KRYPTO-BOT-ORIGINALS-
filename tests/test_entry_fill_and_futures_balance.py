"""Regression tests for futures balance checks and live-short safety."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from config.settings import settings
from src.exchange.connector import ExchangeConnector
from src.strategies.signal import EnhancedSignal, Side


class FuturesBalanceCheckTests(unittest.TestCase):
    def setUp(self):
        self._prev_futures = settings.FUTURES_MODE

    def tearDown(self):
        settings.FUTURES_MODE = self._prev_futures

    def _connector_stub(self) -> ExchangeConnector:
        with patch.object(ExchangeConnector, "_connect", lambda self: None):
            conn = ExchangeConnector.__new__(ExchangeConnector)
            conn.exchange_id = "binance"
            conn.is_paper = False
            conn._exchange = MagicMock()
            conn._markets_cache = None
            conn._recent_order_fingerprints = {}
            conn._read_retry_max = 1
            conn._read_retry_backoff_sec = 0.0
            conn._duplicate_window_sec = 15
            return conn

    def test_futures_mode_skips_spot_base_inventory_check_for_sell(self):
        settings.FUTURES_MODE = True
        conn = self._connector_stub()
        # Spot-style wallet: no free base (typical for linear perps).
        conn.fetch_balance = MagicMock(
            return_value={"BTC": {"free": 0.0}, "USDT": {"free": 500.0}}
        )
        conn._get_market = MagicMock(
            return_value={"base": "BTC", "quote": "USDT", "active": True}
        )
        self.assertTrue(
            conn._has_sufficient_balance("BTC/USDT:USDT", "sell", 0.01, 60000.0)
        )

    def test_spot_mode_still_requires_free_base_for_sell(self):
        settings.FUTURES_MODE = False
        conn = self._connector_stub()
        conn.fetch_balance = MagicMock(
            return_value={"BTC": {"free": 0.0}, "USDT": {"free": 500.0}}
        )
        conn._get_market = MagicMock(
            return_value={"base": "BTC", "quote": "USDT", "active": True}
        )
        self.assertFalse(conn._has_sufficient_balance("BTC/USDT", "sell", 0.01, 60000.0))


class LiveShortGuardTests(unittest.TestCase):
    def setUp(self):
        self._prev = {
            "TRADING_MODE": settings.TRADING_MODE,
            "FUTURES_MODE": settings.FUTURES_MODE,
            "SHORT_ENABLED": settings.SHORT_ENABLED,
        }

    def tearDown(self):
        for key, value in self._prev.items():
            setattr(settings, key, value)

    def _signal(self) -> EnhancedSignal:
        return EnhancedSignal(
            strategy_name="TestStrategy",
            symbol="BTC/USDT:USDT",
            timeframe="1h",
            side=Side.SHORT,
            confidence=80.0,
            entry=100.0,
            stop_loss=105.0,
            take_profit=90.0,
            rr=2.0,
            reason="unittest",
        )

    def test_live_futures_short_does_not_call_execution_engine(self):
        settings.TRADING_MODE = "live"
        settings.FUTURES_MODE = True
        settings.SHORT_ENABLED = True

        from src.bot import MultiStrategyBot

        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exec_engine = MagicMock()
        bot.risk = MagicMock()
        bot.repo = MagicMock()
        bot.tg = MagicMock()
        bot.perf_tracker = MagicMock()
        bot._last_brain_snapshot = {}
        bot._open_trade_ids = {}
        bot._notify_mini_live_order = MagicMock()
        bot._record_trade_event = MagicMock()
        bot._record_last_decision = MagicMock()
        bot._log_decision_cycle = MagicMock()

        bot._execute_short("BTC/USDT:USDT", self._signal(), 0.01)

        bot.exec_engine.execute_entry.assert_not_called()
        bot.risk.open_with_signal.assert_not_called()
        bot._notify_mini_live_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
