"""Offene Positionen müssen bei leerem OHLCV weiterhin SL/TP über Ticker prüfen."""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from src.engine.execution_engine import ExecutionResult
from src.utils.risk_manager import Position


class ExitOnEmptyOhlcvTests(unittest.TestCase):
    """
    Trigger: Live/Paper mit offener Position; fetch_ohlcv liefert leeres DataFrame
    (Endpoint-Fehler/Ratelimit), Ticker liefert aber gültigen Preis unter SL.

    Vor dem Fix: early return → kein Exit → unmanaged Exposure.
    """

    def _make_multi_bot(self):
        from src.bot import MultiStrategyBot

        with patch.object(MultiStrategyBot, "__init__", lambda self: None):
            bot = MultiStrategyBot()
        bot.exchange = MagicMock()
        bot.risk = MagicMock()
        bot.exec_engine = MagicMock()
        bot.health = MagicMock()
        bot.repo = MagicMock()
        bot.tg = MagicMock()
        bot.perf_tracker = MagicMock()
        bot._open_trade_ids = {}
        bot._last_prices = {}
        bot._recovery_blocked_symbols = set()
        bot._active_strategy_runtime = "test"
        bot._record_last_decision = MagicMock()
        bot._log_decision_cycle = MagicMock()
        bot._record_trade_event = MagicMock()
        return bot

    def test_multi_exit_uses_ticker_when_ohlcv_empty(self):
        bot = self._make_multi_bot()
        symbol = "BTC/USDT"
        pos = Position(
            symbol=symbol,
            entry_price=100.0,
            amount=0.01,
            stop_loss=98.0,
            take_profit=110.0,
            side="long",
            highest_price=100.0,
            strategy_name="momentum_pullback",
        )
        bot.risk.open_positions = {symbol: pos}
        bot.risk.check_exit_conditions.return_value = "stop_loss"
        bot.risk.close_position.return_value = -2.0
        bot.exchange.fetch_ohlcv.return_value = pd.DataFrame()
        bot.exchange.fetch_market_price.return_value = 97.0
        bot.exec_engine.execute_exit.return_value = ExecutionResult(
            success=True,
            order={"id": "x1", "status": "closed"},
            fill_price=97.0,
            intended_price=0.0,
            deviation_pct=0.0,
            retries_used=0,
            fingerprint="exit_test",
            reason="",
        )

        bot._process_pair(symbol)

        bot.exchange.fetch_market_price.assert_called_once_with(symbol)
        bot.risk.check_exit_conditions.assert_called_once_with(symbol, 97.0)
        bot.exec_engine.execute_exit.assert_called_once_with(symbol, "sell", 0.01)
        bot.risk.close_position.assert_called_once_with(symbol, 97.0)

    def test_multi_skips_entries_when_ohlcv_empty_without_position(self):
        bot = self._make_multi_bot()
        symbol = "ETH/USDT"
        bot.risk.open_positions = {}
        bot.exchange.fetch_ohlcv.return_value = pd.DataFrame()

        bot._process_pair(symbol)

        bot.exchange.fetch_market_price.assert_not_called()
        bot.risk.check_exit_conditions.assert_not_called()
        bot.exec_engine.execute_exit.assert_not_called()
        bot._record_last_decision.assert_any_call(
            symbol=symbol, decision="skip", reason="no_data"
        )

    def test_legacy_bot_exit_uses_ticker_when_ohlcv_empty(self):
        from src.bot import TradingBot

        with patch.object(TradingBot, "__init__", lambda self: None):
            bot = TradingBot()
        bot.exchange = MagicMock()
        bot.risk = MagicMock()
        bot.repo = MagicMock()
        bot.tg = MagicMock()
        bot.perf_tracker = MagicMock()
        bot.strategy = MagicMock()
        bot.strategy.name = "legacy"
        bot._open_trade_ids = {}
        bot._last_prices = {}
        bot._active_strategy_runtime = "legacy"
        bot._record_last_decision = MagicMock()
        bot._record_last_signal = MagicMock()
        bot._record_trade_event = MagicMock()

        symbol = "BTC/USDT"
        pos = Position(
            symbol=symbol,
            entry_price=100.0,
            amount=0.02,
            stop_loss=99.0,
            take_profit=120.0,
            side="long",
            highest_price=100.0,
        )
        bot.risk.open_positions = {symbol: pos}
        bot.risk.check_exit_conditions.return_value = "stop_loss"
        bot.risk.close_position.return_value = -1.0
        bot.exchange.fetch_ohlcv.return_value = pd.DataFrame()
        bot.exchange.fetch_market_price.return_value = 98.5

        bot._process_pair(symbol)

        bot.exchange.fetch_market_price.assert_called_once_with(symbol)
        bot.risk.check_exit_conditions.assert_called_once_with(symbol, 98.5)
        bot.exchange.create_market_sell_order.assert_called_once_with(symbol, 0.02)
        bot.strategy.analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
