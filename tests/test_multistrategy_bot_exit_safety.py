import unittest

import pandas as pd

from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.utils.risk_manager import Position


class _ExchangeWithStopLossData:
    def fetch_ohlcv(self, symbol):
        return pd.DataFrame({"close": [100.0, 99.0, 98.0, 97.0, 96.0, 94.0]})


class _RiskWithOpenLong:
    def __init__(self):
        self.open_positions = {
            "BTC/USDT": Position(
                symbol="BTC/USDT",
                entry_price=100.0,
                amount=1.0,
                stop_loss=95.0,
                take_profit=120.0,
                side="long",
                strategy_name="TestStrategy",
            )
        }
        self.close_called = False

    def check_exit_conditions(self, symbol, current_price):
        return "stop_loss"

    def close_position(self, symbol, current_price):
        self.close_called = True
        self.open_positions.pop(symbol, None)
        return -6.0


class _FailingExecutionEngine:
    def __init__(self):
        self.calls = []

    def execute_exit(self, symbol, side, amount):
        self.calls.append((symbol, side, amount))
        return ExecutionResult.failed("exit_fp", "exchange unavailable")


class _DisabledDecisionRepo:
    available = False


class MultiStrategyBotExitSafetyTests(unittest.TestCase):
    def test_failed_exit_order_keeps_local_position_and_trade_id_open(self):
        bot = object.__new__(MultiStrategyBot)
        bot._recovery_blocked_symbols = set()
        bot.exchange = _ExchangeWithStopLossData()
        bot.risk = _RiskWithOpenLong()
        bot.exec_engine = _FailingExecutionEngine()
        bot.decision_repo = _DisabledDecisionRepo()
        bot._active_strategy_runtime = "TestStrategy"
        bot._open_trade_ids = {"BTC/USDT": 123}

        MultiStrategyBot._process_pair(bot, "BTC/USDT")

        self.assertEqual([("BTC/USDT", "sell", 1.0)], bot.exec_engine.calls)
        self.assertFalse(bot.risk.close_called)
        self.assertIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual({"BTC/USDT": 123}, bot._open_trade_ids)


if __name__ == "__main__":
    unittest.main()
