import unittest
from types import SimpleNamespace

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionEngine
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


class FakeRisk:
    def __init__(self):
        self.close_called = False
        self.open_called = False
        self.open_positions = {
            "BTC/USDT": Position(
                symbol="BTC/USDT",
                entry_price=100.0,
                amount=1.0,
                stop_loss=95.0,
                take_profit=105.0,
                side="long",
                strategy_name="TestStrategy",
            )
        }

    def check_exit_conditions(self, symbol, current_price):
        return "take_profit"

    def close_position(self, symbol, current_price):
        self.close_called = True
        self.open_positions.pop(symbol, None)
        return 5.0

    def open_with_signal(self, signal, amount):
        self.open_called = True
        raise AssertionError("live futures short must not open a local position")


class FakeFailedExitEngine:
    def __init__(self):
        self.calls = 0

    def execute_exit(self, symbol, side, amount):
        self.calls += 1
        return SimpleNamespace(success=False, reason="exchange timeout")


class FakeRepo:
    def __init__(self):
        self.closed = []
        self.opened = []

    def close_trade(self, *args, **kwargs):
        self.closed.append((args, kwargs))

    def save_open_trade(self, *args, **kwargs):
        self.opened.append((args, kwargs))
        return 1


class FakeNoEntryEngine:
    def __init__(self):
        self.calls = 0

    def execute_entry(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("live futures short must not call execute_entry")


class MultiStrategyBotSafetyTests(unittest.TestCase):
    def _make_bot_shell(self):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot._active_strategy_runtime = "TestStrategy"
        bot._last_brain_snapshot = {}
        bot._last_prices = {}
        bot._open_trade_ids = {"BTC/USDT": 42}
        bot._recovery_blocked_symbols = set()
        bot.health = SimpleNamespace(
            update_data_freshness=lambda symbol: None,
            record_error=lambda *args, **kwargs: None,
        )
        bot._record_last_decision_calls = []
        bot._log_decision_cycle_calls = []
        bot._record_last_decision = lambda **kwargs: bot._record_last_decision_calls.append(kwargs)
        bot._log_decision_cycle = lambda **kwargs: bot._log_decision_cycle_calls.append(kwargs)
        return bot

    def test_failed_exit_order_keeps_position_and_db_trade_open(self):
        bot = self._make_bot_shell()
        bot.risk = FakeRisk()
        bot.exec_engine = FakeFailedExitEngine()
        bot.repo = FakeRepo()
        bot.exchange = SimpleNamespace(
            fetch_ohlcv=lambda symbol: pd.DataFrame(
                {
                    "open": [100.0, 102.0],
                    "high": [103.0, 106.0],
                    "low": [99.0, 101.0],
                    "close": [102.0, 106.0],
                    "volume": [10.0, 11.0],
                }
            )
        )

        MultiStrategyBot._process_pair(bot, "BTC/USDT")

        self.assertEqual(bot.exec_engine.calls, 1)
        self.assertFalse(bot.risk.close_called)
        self.assertIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids["BTC/USDT"], 42)
        self.assertEqual(bot.repo.closed, [])
        self.assertEqual(bot._record_last_decision_calls[-1]["decision"], "exit_failed")

    def test_live_futures_short_is_blocked_before_order_execution(self):
        old_mode = settings.TRADING_MODE
        old_futures = settings.FUTURES_MODE
        try:
            settings.TRADING_MODE = "live"
            settings.FUTURES_MODE = True
            bot = self._make_bot_shell()
            bot.exec_engine = FakeNoEntryEngine()
            bot.risk = FakeRisk()
            bot.repo = FakeRepo()
            bot._notify_mini_live_order = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("live futures short must not notify/order as mini-live")
            )
            signal = EnhancedSignal(
                strategy_name="ShortStrategy",
                symbol="BTC/USDT",
                timeframe="1h",
                side=Side.SHORT,
                confidence=80.0,
                entry=100.0,
                stop_loss=105.0,
                take_profit=90.0,
                rr=2.0,
                reason="unit-test",
            )

            MultiStrategyBot._execute_short(bot, "BTC/USDT", signal, 1.0)

            self.assertEqual(bot.exec_engine.calls, 0)
            self.assertFalse(bot.risk.open_called)
            self.assertEqual(bot.repo.opened, [])
            self.assertEqual(
                bot._record_last_decision_calls[-1]["reason"],
                "live_futures_short_not_implemented",
            )
        finally:
            settings.TRADING_MODE = old_mode
            settings.FUTURES_MODE = old_futures


class ExecutionEngineOrderSafetyTests(unittest.TestCase):
    def test_empty_order_result_is_not_retried(self):
        old_retries = settings.EXECUTION_MAX_RETRIES
        old_backoff = settings.EXECUTION_RETRY_BACKOFF_SEC
        try:
            settings.EXECUTION_MAX_RETRIES = 3
            settings.EXECUTION_RETRY_BACKOFF_SEC = 0

            class EmptyOrderConnector:
                def __init__(self):
                    self.calls = 0

                def create_market_sell_order(self, symbol, amount):
                    self.calls += 1
                    return {}

            connector = EmptyOrderConnector()
            engine = ExecutionEngine(connector)

            result = engine.execute_exit("BTC/USDT", "sell", 1.0)

            self.assertFalse(result.success)
            self.assertEqual(connector.calls, 1)
            self.assertIn("OrderResultUnavailable", result.reason)
        finally:
            settings.EXECUTION_MAX_RETRIES = old_retries
            settings.EXECUTION_RETRY_BACKOFF_SEC = old_backoff


if __name__ == "__main__":
    unittest.main()
