import unittest

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionEngine, ExecutionResult
from src.engine.risk_engine import RiskEngine
from src.engine.runtime_control import runtime_control
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


def _ohlcv(close: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "volume": [100.0],
        }
    )


class _FakeExchange:
    def __init__(self, close: float = 100.0):
        self.close = close

    def fetch_ohlcv(self, symbol):
        return _ohlcv(self.close)


class _FakeExecution:
    def __init__(self, *, exit_success=True, healthy=True):
        self.exit_success = exit_success
        self._healthy = healthy
        self.exit_calls = []
        self.entry_calls = []

    @property
    def is_healthy(self):
        return self._healthy

    def get_status(self):
        return {
            "pause_reason": "test pause",
            "circuit_state": "open",
            "consecutive_errors": 1,
            "kill_switch": False,
        }

    def execute_exit(self, symbol, order_side, amount):
        self.exit_calls.append((symbol, order_side, amount))
        if not self.exit_success:
            return ExecutionResult.failed("exit-fp", "exchange_down")
        return ExecutionResult(
            success=True,
            order={"id": "exit-1", "status": "closed"},
            fill_price=0.0,
            intended_price=0.0,
            deviation_pct=0.0,
            retries_used=0,
            fingerprint="exit-fp",
            reason="",
        )

    def execute_entry(self, symbol, order_side, amount, signal):
        self.entry_calls.append((symbol, order_side, amount, signal))
        return ExecutionResult(
            success=True,
            order={"id": "entry-1", "status": "closed"},
            fill_price=signal.entry,
            intended_price=signal.entry,
            deviation_pct=0.0,
            retries_used=0,
            fingerprint="entry-fp",
            reason="",
        )


class _FakeRepo:
    available = True

    def __init__(self):
        self.closed = []

    def close_trade(self, *args):
        self.closed.append(args)


class _FakePerfTracker:
    available = False

    def refresh(self):
        pass


class _FakePerfRepo:
    available = False


class _FakeDecisionRepo:
    available = False


class _FakeNotifier:
    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None

        return _noop


class _FakeHealth:
    status = type("Status", (), {"value": "ok"})()

    def __init__(self):
        self.errors = []

    def record_error(self, *args):
        self.errors.append(args)

    def update_heartbeat(self):
        pass

    def check_and_react(self):
        pass


class _ConnectorForExecutionEngine:
    def __init__(self):
        self.buy_calls = []

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def create_market_buy_order(self, symbol, amount):
        self.buy_calls.append((symbol, amount))
        return {"id": "buy-1", "status": "closed", "price": 100.0}

    def create_market_sell_order(self, symbol, amount):
        raise AssertionError("sell order should not be used in this test")


class CriticalSafetyRegressionTests(unittest.TestCase):
    def setUp(self):
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        self._old_mode = settings.TRADING_MODE
        self._old_futures = settings.FUTURES_MODE

    def tearDown(self):
        settings.TRADING_MODE = self._old_mode
        settings.FUTURES_MODE = self._old_futures
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def _bot_with_open_long(self, *, close=94.0, exit_success=True, healthy=True):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exchange = _FakeExchange(close=close)
        bot.risk = RiskEngine(initial_balance=10_000.0)
        bot.risk.open_positions["BTC/USDT"] = Position(
            symbol="BTC/USDT",
            entry_price=100.0,
            amount=1.0,
            stop_loss=95.0,
            take_profit=110.0,
            side="long",
            highest_price=100.0,
            strategy_name="TestStrategy",
        )
        bot.exec_engine = _FakeExecution(exit_success=exit_success, healthy=healthy)
        bot.repo = _FakeRepo()
        bot.perf_repo = _FakePerfRepo()
        bot.decision_repo = _FakeDecisionRepo()
        bot.perf_tracker = _FakePerfTracker()
        bot.tg = _FakeNotifier()
        bot.health = _FakeHealth()
        bot.pairs = ["BTC/USDT"]
        bot._open_trade_ids = {"BTC/USDT": 42}
        bot._last_prices = {}
        bot._active_strategy_runtime = "Multi (Meta-Selector)"
        bot._last_selector_snapshot = {}
        bot._last_brain_snapshot = {}
        bot._recovery_blocked_symbols = set()
        bot._startup_checks_ok = True
        bot._startup_block_reason = ""
        return bot

    def test_failed_exit_order_keeps_position_and_db_trade_open(self):
        bot = self._bot_with_open_long(exit_success=False)

        bot._process_pair("BTC/USDT")

        self.assertIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids["BTC/USDT"], 42)
        self.assertEqual(bot.repo.closed, [])
        self.assertEqual(bot.risk.total_trades, 0)
        self.assertEqual(bot.exec_engine.exit_calls, [("BTC/USDT", "sell", 1.0)])

    def test_startup_gate_still_processes_risk_reducing_exits(self):
        bot = self._bot_with_open_long(exit_success=True)
        bot._startup_checks_ok = False
        bot._startup_block_reason = "exchange_markets_unavailable"

        bot.run_cycle()

        self.assertNotIn("BTC/USDT", bot.risk.open_positions)
        self.assertNotIn("BTC/USDT", bot._open_trade_ids)
        self.assertEqual(len(bot.repo.closed), 1)
        self.assertEqual(bot.exec_engine.exit_calls, [("BTC/USDT", "sell", 1.0)])

    def test_unhealthy_execution_gate_still_processes_exits(self):
        bot = self._bot_with_open_long(exit_success=True, healthy=False)

        bot.run_cycle()

        self.assertNotIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot.exec_engine.exit_calls, [("BTC/USDT", "sell", 1.0)])

    def test_live_futures_short_is_blocked_before_entry_order(self):
        settings.TRADING_MODE = "live"
        settings.FUTURES_MODE = True
        bot = self._bot_with_open_long()
        bot.risk.open_positions.clear()
        bot._open_trade_ids.clear()
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
        self.assertEqual(bot.risk.open_positions, {})

    def test_execution_engine_runtime_pause_blocks_entry_connector_call(self):
        connector = _ConnectorForExecutionEngine()
        engine = ExecutionEngine(connector)
        signal = EnhancedSignal(
            strategy_name="LongStrategy",
            symbol="BTC/USDT",
            timeframe="1h",
            side=Side.LONG,
            confidence=80.0,
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            rr=2.0,
            reason="unit-test",
        )
        runtime_control.pause_entries()

        result = engine.execute_entry("BTC/USDT", "buy", 1.0, signal)

        self.assertFalse(result.success)
        self.assertIn("CONTROL PAUSE", result.reason)
        self.assertEqual(connector.buy_calls, [])


if __name__ == "__main__":
    unittest.main()
