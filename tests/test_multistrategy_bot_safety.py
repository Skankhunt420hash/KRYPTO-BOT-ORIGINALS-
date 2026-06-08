import unittest

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position, RiskManager


class _FailingExecEngine:
    def __init__(self):
        self.exit_calls = []
        self.entry_calls = []

    def execute_exit(self, symbol, order_side, amount):
        self.exit_calls.append((symbol, order_side, amount))
        return ExecutionResult.failed("exit-test", "exchange_timeout")

    def execute_entry(self, symbol, order_side, amount, signal=None):
        self.entry_calls.append((symbol, order_side, amount, signal))
        return ExecutionResult(
            success=True,
            order={"id": "unexpected"},
            fill_price=signal.entry if signal else 0.0,
            intended_price=signal.entry if signal else 0.0,
            deviation_pct=0.0,
            retries_used=0,
            fingerprint="unexpected",
            reason="",
        )


class _ExchangeWithClose:
    def __init__(self, close):
        self.close = close

    def fetch_ohlcv(self, symbol):
        return pd.DataFrame(
            {
                "open": [self.close],
                "high": [self.close],
                "low": [self.close],
                "close": [self.close],
                "volume": [1.0],
            }
        )


class _HealthStub:
    def update_data_freshness(self, symbol):
        pass

    def record_error(self, level, message):
        pass


class _TelegramStub:
    def __init__(self):
        self.blocked = []
        self.errors = []

    def notify_trade_blocked(self, **kwargs):
        self.blocked.append(kwargs)

    def notify_error(self, *args, **kwargs):
        self.errors.append((args, kwargs))


class MultiStrategyBotSafetyTests(unittest.TestCase):
    def _bot_shell(self):
        bot = object.__new__(MultiStrategyBot)
        bot.health = _HealthStub()
        bot.tg = _TelegramStub()
        bot._open_trade_ids = {}
        bot._last_prices = {}
        bot._last_brain_snapshot = {}
        bot._active_strategy_runtime = "TestStrategy"
        bot.decisions = []
        bot._market_context = lambda df: {}
        bot._record_last_decision = lambda **kwargs: bot.decisions.append(kwargs)
        bot._log_decision_cycle = lambda **kwargs: None
        bot._record_trade_event = lambda **kwargs: None
        return bot

    def test_failed_exit_keeps_local_position_and_trade_id_open(self):
        bot = self._bot_shell()
        bot.exchange = _ExchangeWithClose(close=90.0)
        bot.exec_engine = _FailingExecEngine()
        bot.risk = RiskManager(initial_balance=1000.0)
        bot.risk.open_positions["BTC/USDT"] = Position(
            symbol="BTC/USDT",
            entry_price=100.0,
            amount=1.0,
            stop_loss=95.0,
            take_profit=120.0,
            side="long",
            highest_price=100.0,
            strategy_name="TestStrategy",
        )
        bot._open_trade_ids["BTC/USDT"] = 42

        bot._process_pair("BTC/USDT")

        self.assertIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids["BTC/USDT"], 42)
        self.assertEqual(bot.risk.total_trades, 0)
        self.assertEqual(bot.exec_engine.exit_calls, [("BTC/USDT", "sell", 1.0)])
        self.assertEqual(bot.decisions[-1]["decision"], "exit_failed")

    def test_live_futures_short_is_blocked_before_entry_order(self):
        old_mode = settings.TRADING_MODE
        old_futures = settings.FUTURES_MODE
        try:
            settings.TRADING_MODE = "live"
            settings.FUTURES_MODE = True
            bot = self._bot_shell()
            bot.exec_engine = _FailingExecEngine()
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

            bot._execute_short("BTC/USDT", signal, 1.0)

            self.assertEqual(bot.exec_engine.entry_calls, [])
            self.assertEqual(bot.decisions[-1]["decision"], "short_blocked")
            self.assertEqual(bot.tg.blocked[-1]["reason"], "live_futures_short_not_implemented")
        finally:
            settings.TRADING_MODE = old_mode
            settings.FUTURES_MODE = old_futures


if __name__ == "__main__":
    unittest.main()
