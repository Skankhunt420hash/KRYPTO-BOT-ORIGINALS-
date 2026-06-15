import unittest
from types import SimpleNamespace

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


def _ohlcv(close: float = 94.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, close],
            "high": [101.0, close + 1],
            "low": [99.0, close - 1],
            "close": [100.0, close],
            "volume": [10.0, 11.0],
        }
    )


class _DummyExchange:
    def fetch_ohlcv(self, symbol):
        return _ohlcv()


class _DummyHealth:
    def update_data_freshness(self, symbol):
        pass


class _UnavailableDecisionRepo:
    available = False


class _DummyRepo:
    def __init__(self):
        self.closed = False

    def close_trade(self, *args, **kwargs):
        self.closed = True
        return True


class _DummyRisk:
    def __init__(self):
        self.closed = False
        self.open_positions = {
            "TEST/USDT": Position(
                symbol="TEST/USDT",
                entry_price=100.0,
                amount=1.0,
                stop_loss=95.0,
                take_profit=110.0,
                side="long",
                strategy_name="TestStrategy",
            )
        }

    def check_exit_conditions(self, symbol, current_price):
        return "stop_loss"

    def close_position(self, symbol, current_price):
        self.closed = True
        self.open_positions.pop(symbol, None)
        return -5.0


class _FailingExec:
    def execute_exit(self, symbol, side, amount):
        return SimpleNamespace(success=False, reason="exchange rejected")


class _DummyExec:
    def __init__(self):
        self.entry_calls = []

    def execute_entry(self, *args, **kwargs):
        self.entry_calls.append((args, kwargs))
        return SimpleNamespace(success=True, fill_price=100.0, deviation_pct=0.0, order={})


class MultiStrategyBotSafetyTests(unittest.TestCase):
    def _make_bot_shell(self):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exchange = _DummyExchange()
        bot.health = _DummyHealth()
        bot.decision_repo = _UnavailableDecisionRepo()
        bot.repo = _DummyRepo()
        bot._open_trade_ids = {"TEST/USDT": 123}
        bot._active_strategy_runtime = "test"
        bot._last_prices = {}
        bot._last_brain_snapshot = {}
        return bot

    def test_failed_exit_keeps_position_and_db_trade_open(self):
        bot = self._make_bot_shell()
        bot.risk = _DummyRisk()
        bot.exec_engine = _FailingExec()

        bot._process_pair("TEST/USDT")

        self.assertFalse(bot.risk.closed)
        self.assertIn("TEST/USDT", bot.risk.open_positions)
        self.assertFalse(bot.repo.closed)
        self.assertEqual(bot._open_trade_ids["TEST/USDT"], 123)

    def test_live_futures_short_is_blocked_before_order(self):
        old_mode = settings.TRADING_MODE
        old_futures = settings.FUTURES_MODE
        try:
            settings.TRADING_MODE = "live"
            settings.FUTURES_MODE = True
            bot = self._make_bot_shell()
            bot.exec_engine = _DummyExec()
            signal = EnhancedSignal(
                strategy_name="TestStrategy",
                symbol="TEST/USDT",
                timeframe="1h",
                side=Side.SHORT,
                confidence=80.0,
                entry=100.0,
                stop_loss=105.0,
                take_profit=90.0,
                rr=2.0,
                reason="unit test",
            )

            bot._execute_short("TEST/USDT", signal, 1.0)

            self.assertEqual(bot.exec_engine.entry_calls, [])
        finally:
            settings.TRADING_MODE = old_mode
            settings.FUTURES_MODE = old_futures


if __name__ == "__main__":
    unittest.main()
