import unittest
from unittest.mock import Mock, patch

from src.bot import MultiStrategyBot
from src.strategies.signal import EnhancedSignal, Side


class LiveShortSafetyTests(unittest.TestCase):
    def _make_short_signal(self) -> EnhancedSignal:
        return EnhancedSignal(
            strategy_name="TestStrategy",
            symbol="BTC/USDT",
            timeframe="1h",
            side=Side.SHORT,
            confidence=85.0,
            entry=100.0,
            stop_loss=102.0,
            take_profit=96.0,
            rr=2.0,
            reason="unittest short",
            regime="TEST",
        )

    def test_live_futures_short_is_blocked_before_execution(self):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.tg = Mock()
        bot.exec_engine = Mock()
        bot._last_brain_snapshot = {}
        bot._record_last_decision = Mock()
        bot._log_decision_cycle = Mock()

        signal = self._make_short_signal()

        with patch("src.bot.settings.TRADING_MODE", "live"), patch(
            "src.bot.settings.FUTURES_MODE", True
        ):
            bot._execute_short("BTC/USDT", signal, amount=0.5)

        bot.exec_engine.execute_entry.assert_not_called()
        bot.tg.notify_trade_blocked.assert_called_once_with(
            symbol="BTC/USDT",
            strategy="TestStrategy",
            side="short",
            reason="short_live_futures_not_implemented",
        )
        bot._record_last_decision.assert_called_once_with(
            symbol="BTC/USDT",
            decision="blocked_live_short",
            reason="short_live_futures_not_implemented",
            strategy="TestStrategy",
        )


if __name__ == "__main__":
    unittest.main()
