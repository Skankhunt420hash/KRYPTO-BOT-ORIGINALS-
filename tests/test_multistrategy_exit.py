import unittest
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.utils.risk_manager import Position


class MultiStrategyExitTests(unittest.TestCase):
    def _make_bot(self, exit_result: ExecutionResult) -> MultiStrategyBot:
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot._recovery_blocked_symbols = set()
        bot._active_strategy_runtime = "AUTO"
        bot._last_prices = {}
        bot._open_trade_ids = {"BTC/USDT": 123}
        bot.decision_repo = SimpleNamespace(available=False)
        bot.exchange = SimpleNamespace(
            fetch_ohlcv=lambda symbol: pd.DataFrame({"close": [95.0]})
        )
        bot.exec_engine = mock.Mock()
        bot.exec_engine.execute_exit.return_value = exit_result
        bot.health = mock.Mock()
        bot.perf_tracker = mock.Mock()
        bot.repo = mock.Mock()
        bot.tg = mock.Mock()

        position = Position(
            symbol="BTC/USDT",
            entry_price=100.0,
            amount=0.5,
            stop_loss=96.0,
            take_profit=120.0,
            side="long",
            strategy_name="TestStrategy",
        )
        bot.risk = mock.Mock()
        bot.risk.open_positions = {"BTC/USDT": position}
        bot.risk.check_exit_conditions.return_value = "stop_loss"
        bot.risk.close_position.return_value = -2.5
        return bot

    def test_failed_exit_order_keeps_position_open_locally(self):
        bot = self._make_bot(ExecutionResult.failed("exit-fp", "network down"))

        bot._process_pair("BTC/USDT")

        bot.exec_engine.execute_exit.assert_called_once_with("BTC/USDT", "sell", 0.5)
        bot.risk.close_position.assert_not_called()
        bot.repo.close_trade.assert_not_called()
        self.assertEqual(bot._open_trade_ids, {"BTC/USDT": 123})
        self.assertIn("BTC/USDT", bot.risk.open_positions)
        bot.health.record_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
