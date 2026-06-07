import unittest
from types import SimpleNamespace

import pandas as pd

from src.bot import MultiStrategyBot
from src.engine.risk_engine import RiskEngine
from src.utils.risk_manager import Position


def _ohlcv(close: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "volume": [1.0],
        }
    )


class _DummyExchange:
    def __init__(self, close: float):
        self.close = close
        self.fetches = []

    def fetch_ohlcv(self, symbol: str) -> pd.DataFrame:
        self.fetches.append(symbol)
        return _ohlcv(self.close)


class _DummyExecution:
    def __init__(self, *, exit_success: bool, healthy: bool = True):
        self.exit_success = exit_success
        self.healthy = healthy
        self.exit_calls = []

    @property
    def is_healthy(self) -> bool:
        return self.healthy

    def get_status(self) -> dict:
        return {
            "pause_reason": "test_pause",
            "circuit_state": "open",
            "consecutive_errors": 3,
            "kill_switch": False,
        }

    def execute_exit(self, symbol: str, side: str, amount: float):
        self.exit_calls.append((symbol, side, amount))
        return SimpleNamespace(
            success=self.exit_success,
            reason="" if self.exit_success else "exchange unavailable",
            order={"id": "exit-1"} if self.exit_success else {},
        )


class _DummyRepo:
    def __init__(self):
        self.closed = []

    def close_trade(self, *args):
        self.closed.append(args)
        return True


class _DummyPerfTracker:
    def refresh(self):
        return None


class _DummyTelegram:
    def __init__(self):
        self.errors = []
        self.closed = []

    def notify_error(self, *args):
        self.errors.append(args)

    def notify_trade_closed(self, **kwargs):
        self.closed.append(kwargs)


class _DummyHealth:
    def __init__(self):
        self.heartbeats = 0
        self.fresh = []
        self.errors = []

    def update_heartbeat(self):
        self.heartbeats += 1

    def update_data_freshness(self, symbol: str):
        self.fresh.append(symbol)

    def record_error(self, *args):
        self.errors.append(args)


def _make_bot(*, close: float, exit_success: bool, healthy: bool = True) -> MultiStrategyBot:
    bot = MultiStrategyBot.__new__(MultiStrategyBot)
    bot.exchange = _DummyExchange(close)
    bot.exec_engine = _DummyExecution(exit_success=exit_success, healthy=healthy)
    bot.repo = _DummyRepo()
    bot.perf_tracker = _DummyPerfTracker()
    bot.tg = _DummyTelegram()
    bot.health = _DummyHealth()
    bot.decision_repo = SimpleNamespace(available=False)
    bot.risk = RiskEngine(initial_balance=10_000.0)
    bot.pairs = ["TEST/USDT"]
    bot.strategies = []
    bot.running = True
    bot._open_trade_ids = {"TEST/USDT": 123}
    bot._last_prices = {}
    bot._recovery_blocked_symbols = set()
    bot._startup_checks_ok = True
    bot._startup_block_reason = ""
    bot._active_strategy_runtime = "test"
    bot._last_brain_snapshot = {}
    bot._sync_runtime_state = lambda: None
    bot.risk.open_positions["TEST/USDT"] = Position(
        symbol="TEST/USDT",
        entry_price=100.0,
        amount=1.5,
        stop_loss=95.0,
        take_profit=110.0,
        side="long",
        highest_price=100.0,
        strategy_name="SafetyTest",
    )
    return bot


class MultiStrategyBotExitSafetyTests(unittest.TestCase):
    def test_failed_exit_order_keeps_local_and_db_position_open(self):
        bot = _make_bot(close=111.0, exit_success=False)

        bot._process_pair("TEST/USDT")

        self.assertIn("TEST/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids["TEST/USDT"], 123)
        self.assertEqual(bot.repo.closed, [])
        self.assertEqual(bot.exec_engine.exit_calls, [("TEST/USDT", "sell", 1.5)])

    def test_startup_gate_still_processes_open_position_exit(self):
        bot = _make_bot(close=111.0, exit_success=True)
        bot._startup_checks_ok = False
        bot._startup_block_reason = "exchange_markets_unavailable"

        bot.run_cycle()

        self.assertNotIn("TEST/USDT", bot.risk.open_positions)
        self.assertNotIn("TEST/USDT", bot._open_trade_ids)
        self.assertEqual(bot.exec_engine.exit_calls, [("TEST/USDT", "sell", 1.5)])

    def test_execution_health_gate_still_processes_open_position_exit(self):
        bot = _make_bot(close=111.0, exit_success=True, healthy=False)

        bot.run_cycle()

        self.assertNotIn("TEST/USDT", bot.risk.open_positions)
        self.assertNotIn("TEST/USDT", bot._open_trade_ids)
        self.assertEqual(bot.exec_engine.exit_calls, [("TEST/USDT", "sell", 1.5)])


if __name__ == "__main__":
    unittest.main()
