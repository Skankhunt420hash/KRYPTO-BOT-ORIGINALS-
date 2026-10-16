import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.strategies.signal import EnhancedSignal, Side


def _short_signal() -> EnhancedSignal:
    return EnhancedSignal(
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


class MultiStrategyBotSafetyTests(unittest.TestCase):
    def test_failed_exit_order_keeps_local_position_open(self):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        position = SimpleNamespace(
            entry_price=100.0,
            amount=0.5,
            side="long",
            strategy_name="ExitStrategy",
        )
        risk = SimpleNamespace(
            open_positions={"BTC/USDT": position},
            check_exit_conditions=MagicMock(return_value="stop_loss"),
            close_position=MagicMock(return_value=-5.0),
        )
        bot.risk = risk
        bot.exchange = SimpleNamespace(
            fetch_ohlcv=MagicMock(
                return_value=pd.DataFrame({"close": [100.0]}, index=pd.date_range("2024-01-01", periods=1))
            )
        )
        bot.health = SimpleNamespace(update_data_freshness=MagicMock())
        bot.exec_engine = SimpleNamespace(
            execute_exit=MagicMock(
                return_value=ExecutionResult.failed("fp", "exchange unavailable")
            )
        )
        bot._recovery_blocked_symbols = set()
        bot._last_prices = {}
        bot._open_trade_ids = {"BTC/USDT": 123}
        bot._active_strategy_runtime = "ExitStrategy"
        bot._market_context = MagicMock(return_value={})
        bot._record_last_decision = MagicMock()

        bot._process_pair("BTC/USDT")

        risk.close_position.assert_not_called()
        self.assertIn("BTC/USDT", risk.open_positions)
        self.assertEqual(bot._open_trade_ids["BTC/USDT"], 123)
        bot._record_last_decision.assert_called_once()

    def test_live_futures_short_is_blocked_before_connector_order(self):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exec_engine = SimpleNamespace(execute_entry=MagicMock())
        bot.risk = SimpleNamespace(open_with_signal=MagicMock())
        bot._last_brain_snapshot = {}
        bot._record_last_decision = MagicMock()
        bot._log_decision_cycle = MagicMock()
        bot._notify_mini_live_order = MagicMock()

        with (
            patch("src.bot.settings.TRADING_MODE", "live"),
            patch("src.bot.settings.FUTURES_MODE", True),
        ):
            bot._execute_short("BTC/USDT", _short_signal(), 1.0)

        bot.exec_engine.execute_entry.assert_not_called()
        bot.risk.open_with_signal.assert_not_called()
        bot._notify_mini_live_order.assert_not_called()
        bot._record_last_decision.assert_called_once()


if __name__ == "__main__":
    unittest.main()
