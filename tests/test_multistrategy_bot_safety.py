import unittest
from unittest.mock import Mock

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.engine.risk_engine import RiskEngine
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


def _ohlcv_with_last_close(last_close: float) -> pd.DataFrame:
    closes = [100.0] * 59 + [last_close]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [price + 1.0 for price in closes],
            "low": [price - 1.0 for price in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )


class MultiStrategyBotSafetyTests(unittest.TestCase):
    def _bot_shell(self) -> MultiStrategyBot:
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exchange = Mock()
        bot.health = Mock()
        bot.exec_engine = Mock()
        bot.repo = Mock()
        bot.perf_tracker = Mock()
        bot.tg = Mock()
        bot._open_trade_ids = {}
        bot._last_prices = {}
        bot._recovery_blocked_symbols = set()
        bot._last_brain_snapshot = {}
        bot._record_last_decision = Mock()
        bot._record_trade_event = Mock()
        bot._log_decision_cycle = Mock()
        bot._notify_mini_live_order = Mock()
        return bot

    def test_failed_exit_keeps_position_and_db_trade_open(self):
        bot = self._bot_shell()
        symbol = "BTC/USDT"
        bot.exchange.fetch_ohlcv.return_value = _ohlcv_with_last_close(90.0)
        bot.risk = RiskEngine(initial_balance=10_000.0)
        bot.risk.open_positions[symbol] = Position(
            symbol=symbol,
            entry_price=100.0,
            amount=0.5,
            stop_loss=95.0,
            take_profit=120.0,
            side="long",
            highest_price=100.0,
            strategy_name="SafetyStrategy",
        )
        bot._open_trade_ids[symbol] = 42
        bot.exec_engine.execute_exit.return_value = ExecutionResult.failed(
            "exit_fp", "simulated exchange timeout"
        )

        MultiStrategyBot._process_pair(bot, symbol)

        bot.exec_engine.execute_exit.assert_called_once_with(symbol, "sell", 0.5)
        self.assertIn(symbol, bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids[symbol], 42)
        self.assertEqual(bot.risk.total_trades, 0)
        bot.repo.close_trade.assert_not_called()
        bot.tg.notify_trade_closed.assert_not_called()
        bot._record_trade_event.assert_not_called()
        bot._record_last_decision.assert_called_once()
        self.assertEqual(
            bot._record_last_decision.call_args.kwargs["decision"], "exit_failed"
        )

    def test_live_futures_short_is_blocked_before_real_order(self):
        old_trading_mode = settings.TRADING_MODE
        old_futures_mode = settings.FUTURES_MODE
        settings.TRADING_MODE = "live"
        settings.FUTURES_MODE = True
        try:
            bot = self._bot_shell()
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
                reason="unit test short",
            )

            MultiStrategyBot._execute_short(bot, signal.symbol, signal, 0.5)

            bot._notify_mini_live_order.assert_not_called()
            bot.exec_engine.execute_entry.assert_not_called()
            bot._record_last_decision.assert_called_once()
            self.assertEqual(
                bot._record_last_decision.call_args.kwargs["reason"],
                "live_futures_short_not_implemented",
            )
        finally:
            settings.TRADING_MODE = old_trading_mode
            settings.FUTURES_MODE = old_futures_mode


if __name__ == "__main__":
    unittest.main()
