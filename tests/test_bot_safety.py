import unittest

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.engine.runtime_control import runtime_control
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


class FakeHealth:
    def update_data_freshness(self, symbol):
        self.last_symbol = symbol


class FakeExchange:
    def __init__(self, df):
        self.df = df

    def fetch_ohlcv(self, symbol):
        return self.df


class FakeRisk:
    def __init__(self, position):
        self.open_positions = {position.symbol: position}
        self.close_calls = 0

    def check_exit_conditions(self, symbol, current_price):
        return "stop_loss"

    def close_position(self, symbol, current_price):
        self.close_calls += 1
        self.open_positions.pop(symbol, None)
        return -1.0


class FakeExecution:
    def __init__(self, result):
        self.result = result
        self.exit_calls = 0
        self.entry_calls = 0

    def execute_exit(self, symbol, side, amount):
        self.exit_calls += 1
        return self.result

    def execute_entry(self, **kwargs):
        self.entry_calls += 1
        return self.result


def _price_frame(close=90.0):
    return pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [89.0],
            "close": [close],
            "volume": [1.0],
        }
    )


def _short_signal():
    return EnhancedSignal(
        strategy_name="ShortStrategy",
        symbol="BTC/USDT",
        timeframe="1m",
        side=Side.SHORT,
        confidence=80.0,
        entry=100.0,
        stop_loss=105.0,
        take_profit=90.0,
        rr=2.0,
        reason="unit",
    )


class MultiStrategyBotSafetyTests(unittest.TestCase):
    def setUp(self):
        self._old_mode = settings.TRADING_MODE
        self._old_futures = settings.FUTURES_MODE
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def tearDown(self):
        settings.TRADING_MODE = self._old_mode
        settings.FUTURES_MODE = self._old_futures
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def test_failed_multi_exit_keeps_position_and_trade_id_open(self):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        position = Position(
            symbol="BTC/USDT",
            entry_price=100.0,
            amount=1.0,
            stop_loss=95.0,
            take_profit=110.0,
            side="long",
            strategy_name="UnitStrategy",
        )
        bot._recovery_blocked_symbols = set()
        bot.exchange = FakeExchange(_price_frame())
        bot.health = FakeHealth()
        bot.risk = FakeRisk(position)
        bot.exec_engine = FakeExecution(ExecutionResult.failed("fp", "exchange_down"))
        bot._open_trade_ids = {"BTC/USDT": 7}
        bot._last_prices = {}
        bot._last_decision = None
        bot._market_context = lambda df: {}
        bot._record_last_decision = lambda **kwargs: setattr(bot, "_last_decision", kwargs)

        bot._process_pair("BTC/USDT")

        self.assertIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids["BTC/USDT"], 7)
        self.assertEqual(bot.risk.close_calls, 0)
        self.assertEqual(bot._last_decision["decision"], "exit_failed")
        self.assertTrue(runtime_control.get_snapshot()["risk_off"])

    def test_live_futures_short_is_blocked_before_execution(self):
        settings.TRADING_MODE = "live"
        settings.FUTURES_MODE = True
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exec_engine = FakeExecution(ExecutionResult.failed("fp", "should_not_call"))
        bot._last_brain_snapshot = {}
        bot._last_decision = None
        bot._last_cycle = None
        bot._record_last_decision = lambda **kwargs: setattr(bot, "_last_decision", kwargs)
        bot._log_decision_cycle = lambda **kwargs: setattr(bot, "_last_cycle", kwargs)
        bot._notify_mini_live_order = lambda **kwargs: setattr(bot, "_notified", kwargs)

        bot._execute_short("BTC/USDT", _short_signal(), 1.0)

        self.assertEqual(bot.exec_engine.entry_calls, 0)
        self.assertEqual(bot._last_decision["decision"], "execution_blocked")
        self.assertEqual(bot._last_cycle["reject_reason"], "live_futures_short_not_implemented")


if __name__ == "__main__":
    unittest.main()
