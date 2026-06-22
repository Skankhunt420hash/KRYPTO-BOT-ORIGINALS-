import tempfile
import unittest
from pathlib import Path

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.safety.watchdog import _clear_stuck_recovery, _tail_log
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


class DummyExchange:
    def __init__(self, close: float):
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


class DummyRepo:
    available = False

    def __init__(self):
        self.closed = []

    def close_trade(self, *args):
        self.closed.append(args)


class DummyPerfTracker:
    def refresh(self):
        pass


class DummyTelegram:
    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None

        return _noop


class DummyHealth:
    def __init__(self):
        self.heartbeats = 0
        self.checks = 0

    def update_heartbeat(self):
        self.heartbeats += 1

    def check_and_react(self):
        self.checks += 1


class DummyRisk:
    def __init__(self, position):
        self.open_positions = {position.symbol: position}
        self.closed = []

    def check_exit_conditions(self, symbol, current_price):
        position = self.open_positions.get(symbol)
        if position and current_price <= position.stop_loss:
            return "stop_loss"
        return None

    def close_position(self, symbol, current_price):
        position = self.open_positions.pop(symbol, None)
        if not position:
            return None
        pnl = (current_price - position.entry_price) * position.amount
        self.closed.append((symbol, current_price, pnl))
        return pnl

    def get_stats(self):
        return {
            "balance": 10_000.0,
            "total_pnl": 0.0,
            "total_trades": len(self.closed),
            "winning_trades": 0,
            "winrate_pct": 0.0,
            "open_positions": len(self.open_positions),
            "daily_loss": 0.0,
            "portfolio_risk_pct": 0.0,
        }


class FailingExitEngine:
    def __init__(self):
        self.exit_calls = 0

    def execute_exit(self, symbol, order_side, amount):
        self.exit_calls += 1
        return ExecutionResult.failed("fp", "exchange_rejected")


class PausedExitEngine:
    def __init__(self):
        self.exit_calls = 0

    @property
    def is_healthy(self):
        return False

    def get_status(self):
        return {
            "pause_reason": "Circuit Breaker: open",
            "circuit_state": "open",
            "consecutive_errors": 3,
            "kill_switch": False,
        }

    def execute_exit(self, symbol, order_side, amount):
        self.exit_calls += 1
        return ExecutionResult(
            success=True,
            order={"id": "exit-1", "status": "closed"},
            fill_price=94.0,
            intended_price=0.0,
            deviation_pct=0.0,
            retries_used=0,
            fingerprint="fp",
            reason="",
        )


class RaisingEntryEngine:
    def execute_entry(self, *args, **kwargs):
        raise AssertionError("Live-Futures-Short darf keine Entry-Order senden")


def make_bot(close: float, position: Position):
    bot = MultiStrategyBot.__new__(MultiStrategyBot)
    bot.exchange = DummyExchange(close)
    bot.risk = DummyRisk(position)
    bot.repo = DummyRepo()
    bot.perf_tracker = DummyPerfTracker()
    bot.tg = DummyTelegram()
    bot.decision_repo = DummyRepo()
    bot._open_trade_ids = {position.symbol: 123}
    bot._recovery_blocked_symbols = set()
    bot._active_strategy_runtime = "Test"
    bot._last_brain_snapshot = {}
    bot._last_selector_snapshot = {}
    return bot


class MultiStrategySafetyTests(unittest.TestCase):
    def test_failed_exit_order_keeps_local_and_db_state_open(self):
        position = Position(
            symbol="TEST/USDT",
            entry_price=100.0,
            amount=1.0,
            stop_loss=95.0,
            take_profit=120.0,
            side="long",
            strategy_name="SafetyTest",
        )
        bot = make_bot(close=94.0, position=position)
        bot.exec_engine = FailingExitEngine()

        bot._process_pair("TEST/USDT")

        self.assertEqual(bot.exec_engine.exit_calls, 1)
        self.assertIn("TEST/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids["TEST/USDT"], 123)
        self.assertEqual(bot.repo.closed, [])

    def test_unhealthy_execution_still_processes_position_exits(self):
        position = Position(
            symbol="TEST/USDT",
            entry_price=100.0,
            amount=1.0,
            stop_loss=95.0,
            take_profit=120.0,
            side="long",
            strategy_name="SafetyTest",
        )
        bot = make_bot(close=94.0, position=position)
        bot.exec_engine = PausedExitEngine()
        bot.health = DummyHealth()
        bot.pairs = ["TEST/USDT"]
        bot.scorer = DummyPerfTracker()
        bot._startup_checks_ok = True
        bot._startup_block_reason = ""
        bot._paper_undo_unwanted_control_locks = lambda: None
        bot._update_performance_tracking = lambda: None
        bot._sync_runtime_state = lambda: None
        bot._persist_recovery_state = lambda: None

        bot.run_cycle()

        self.assertEqual(bot.exec_engine.exit_calls, 1)
        self.assertNotIn("TEST/USDT", bot.risk.open_positions)
        self.assertEqual(bot.repo.closed[0][0], 123)

    def test_live_futures_short_is_blocked_before_entry_order(self):
        old_mode = settings.TRADING_MODE
        old_futures = settings.FUTURES_MODE
        try:
            settings.TRADING_MODE = "live"
            settings.FUTURES_MODE = True
            bot = MultiStrategyBot.__new__(MultiStrategyBot)
            bot.exec_engine = RaisingEntryEngine()
            bot.decision_repo = DummyRepo()
            bot._last_brain_snapshot = {}
            bot._active_strategy_runtime = "Test"
            signal = EnhancedSignal(
                strategy_name="SafetyTest",
                symbol="TEST/USDT",
                timeframe="1h",
                side=Side.SHORT,
                confidence=80.0,
                entry=100.0,
                stop_loss=105.0,
                take_profit=90.0,
                rr=2.0,
                reason="short",
            )

            bot._execute_short("TEST/USDT", signal, 1.0)
        finally:
            settings.TRADING_MODE = old_mode
            settings.FUTURES_MODE = old_futures


class WatchdogSafetyTests(unittest.TestCase):
    def test_recovery_lock_clearing_is_opt_in(self):
        old_mode = settings.TRADING_MODE
        old_clear = settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY
        old_file = settings.STATE_RECOVERY_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                recovery = root / "runtime_recovery.json"
                recovery.write_text(
                    '{"paused": true, "risk_off": true}\n',
                    encoding="utf-8",
                )
                settings.TRADING_MODE = "paper"
                settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False
                settings.STATE_RECOVERY_FILE = "runtime_recovery.json"

                cleared, reason = _clear_stuck_recovery(root)

                self.assertFalse(cleared)
                self.assertEqual(reason, "clear recovery aus")
                self.assertIn('"paused": true', recovery.read_text(encoding="utf-8"))
        finally:
            settings.TRADING_MODE = old_mode
            settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = old_clear
            settings.STATE_RECOVERY_FILE = old_file

    def test_tail_log_returns_bounded_last_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "".join(f"line-{idx}\n" for idx in range(1000)),
                encoding="utf-8",
            )

            lines = _tail_log(path, 5)

            self.assertEqual(lines, ["line-995", "line-996", "line-997", "line-998", "line-999"])


if __name__ == "__main__":
    unittest.main()
