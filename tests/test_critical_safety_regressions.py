import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionEngine, ExecutionResult
from src.engine.runtime_control import runtime_control
from src.safety import watchdog
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


class _DummyHealth:
    def update_heartbeat(self):
        pass

    def update_data_freshness(self, symbol):
        pass

    def record_error(self, level, message):
        pass

    def check_and_react(self):
        pass


class _DummyExchange:
    def fetch_ohlcv(self, symbol):
        return pd.DataFrame({"close": [100.0, 94.0]})


class _FailingExitEngine:
    def execute_exit(self, symbol, side, amount):
        return ExecutionResult.failed("exit", "exchange unavailable")


class _RecordingEntryEngine:
    def __init__(self):
        self.calls = []
        self.is_healthy = True

    def execute_entry(self, **kwargs):
        self.calls.append(kwargs)
        return ExecutionResult(
            success=True,
            order={"id": "unexpected", "status": "closed"},
            fill_price=kwargs["signal"].entry,
            intended_price=kwargs["signal"].entry,
            deviation_pct=0.0,
            retries_used=0,
            fingerprint="entry",
            reason="",
        )

    def get_status(self):
        return {
            "pause_reason": "",
            "circuit_state": "closed",
            "consecutive_errors": 0,
            "kill_switch": False,
        }


class _UnhealthyExecutionEngine(_RecordingEntryEngine):
    def __init__(self):
        super().__init__()
        self.is_healthy = False

    def get_status(self):
        return {
            "pause_reason": "emergency pause",
            "circuit_state": "open",
            "consecutive_errors": 5,
            "kill_switch": True,
        }


class _DummyRepo:
    def __init__(self):
        self.closed = []
        self.opened = []

    def close_trade(self, *args):
        self.closed.append(args)

    def save_open_trade(self, **kwargs):
        self.opened.append(kwargs)
        return 123


class _DummyTelegram:
    def notify_trade_closed(self, **kwargs):
        pass

    def notify_trade_opened(self, **kwargs):
        pass

    def notify_error(self, *args, **kwargs):
        pass


class _DummyRisk:
    def __init__(self):
        self.open_positions = {
            "BTC/USDT": Position(
                symbol="BTC/USDT",
                entry_price=100.0,
                amount=0.5,
                stop_loss=95.0,
                take_profit=120.0,
                side="long",
                highest_price=100.0,
                strategy_name="UnitStrategy",
            )
        }
        self.closed = []

    def check_exit_conditions(self, symbol, current_price):
        return "stop_loss"

    def close_position(self, symbol, current_price):
        self.closed.append((symbol, current_price))
        self.open_positions.pop(symbol, None)
        return -3.0

    def get_stats(self):
        return {
            "balance": 10_000.0,
            "total_pnl": 0.0,
            "total_trades": 0,
            "winrate_pct": 0.0,
            "open_positions": len(self.open_positions),
            "daily_loss": 0.0,
            "portfolio_risk_pct": 0.0,
        }


def _make_bot_for_exit_test():
    bot = MultiStrategyBot.__new__(MultiStrategyBot)
    bot.exchange = _DummyExchange()
    bot.health = _DummyHealth()
    bot.risk = _DummyRisk()
    bot.exec_engine = _FailingExitEngine()
    bot.repo = _DummyRepo()
    bot.tg = _DummyTelegram()
    bot._open_trade_ids = {"BTC/USDT": 77}
    bot._last_prices = {}
    bot._recovery_blocked_symbols = set()
    bot._active_strategy_runtime = "UnitStrategy"
    bot._last_brain_snapshot = {}
    bot._recorded_decisions = []
    bot._logged_cycles = []
    bot._record_last_decision = lambda **kwargs: bot._recorded_decisions.append(kwargs)
    bot._log_decision_cycle = lambda **kwargs: bot._logged_cycles.append(kwargs)
    return bot


def _make_bot_for_cycle_test(exec_engine=None):
    bot = MultiStrategyBot.__new__(MultiStrategyBot)
    bot.pairs = ["BTC/USDT"]
    bot.health = _DummyHealth()
    bot.risk = _DummyRisk()
    bot.exec_engine = exec_engine or _RecordingEntryEngine()
    bot.scorer = type("Scorer", (), {"refresh": lambda self: None})()
    bot.tg = _DummyTelegram()
    bot._startup_checks_ok = True
    bot._startup_block_reason = ""
    bot._active_strategy_runtime = "UnitStrategy"
    bot._processed_pairs = []
    bot._process_pair = lambda symbol: bot._processed_pairs.append(symbol)
    bot._paper_undo_unwanted_control_locks = lambda: None
    bot._update_performance_tracking = lambda: None
    bot._sync_runtime_state = lambda: None
    bot._persist_recovery_state = lambda: None
    return bot


def _short_signal():
    return EnhancedSignal(
        strategy_name="UnitStrategy",
        symbol="BTC/USDT",
        timeframe="1m",
        side=Side.SHORT,
        confidence=80.0,
        entry=100.0,
        stop_loss=110.0,
        take_profit=90.0,
        rr=2.0,
        reason="unit",
    )


class CriticalSafetyRegressionTests(unittest.TestCase):
    def tearDown(self):
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def test_failed_multistrategy_exit_keeps_local_and_db_state_open(self):
        bot = _make_bot_for_exit_test()

        bot._process_pair("BTC/USDT")

        self.assertIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids, {"BTC/USDT": 77})
        self.assertEqual(bot.risk.closed, [])
        self.assertEqual(bot.repo.closed, [])
        self.assertEqual(bot._recorded_decisions[-1]["decision"], "exit_failed")

    def test_live_futures_short_is_blocked_before_sell_order(self):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exec_engine = _RecordingEntryEngine()
        bot.risk = _DummyRisk()
        bot.repo = _DummyRepo()
        bot.tg = _DummyTelegram()
        bot._open_trade_ids = {}
        bot._last_brain_snapshot = {}
        bot._recorded_decisions = []
        bot._record_last_decision = lambda **kwargs: bot._recorded_decisions.append(kwargs)
        bot._log_decision_cycle = lambda **kwargs: None
        bot._notify_mini_live_order = lambda **kwargs: None

        with patch.object(settings, "TRADING_MODE", "live"), patch.object(
            settings, "FUTURES_MODE", True
        ), patch.object(settings, "SHORT_ENABLED", True):
            bot._execute_short("BTC/USDT", _short_signal(), 0.5)

        self.assertEqual(bot.exec_engine.calls, [])
        self.assertEqual(bot._open_trade_ids, {})
        self.assertEqual(bot._recorded_decisions[-1]["decision"], "short_blocked")

    def test_execution_entry_respects_runtime_pause_at_last_moment(self):
        class Connector:
            def __init__(self):
                self.orders = []

            def fetch_ticker(self, symbol):
                return {"last": 100.0}

            def create_market_buy_order(self, symbol, amount):
                self.orders.append((symbol, amount))
                return {"id": "order", "status": "closed"}

        connector = Connector()
        engine = ExecutionEngine(connector)
        runtime_control.pause_entries()

        result = engine.execute_entry("BTC/USDT", "buy", 0.1, _short_signal())

        self.assertFalse(result.success)
        self.assertIn("CONTROL PAUSE", result.reason)
        self.assertEqual(connector.orders, [])

    def test_execution_pause_still_processes_pairs_for_exits(self):
        bot = _make_bot_for_cycle_test(exec_engine=_UnhealthyExecutionEngine())

        bot.run_cycle()

        self.assertEqual(bot._processed_pairs, ["BTC/USDT"])

    def test_startup_block_still_processes_pairs_for_exits(self):
        bot = _make_bot_for_cycle_test()
        bot._startup_checks_ok = False
        bot._startup_block_reason = "orphan_exchange_positions:ETH/USDT"

        bot.run_cycle()

        self.assertEqual(bot._processed_pairs, ["BTC/USDT"])

    def test_watchdog_defaults_preserve_manual_control_locks(self):
        self.assertFalse(settings.PAPER_CLEAR_CONTROL_LOCKS_EACH_CYCLE)
        self.assertFalse(settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY)

    def test_watchdog_tail_is_bounded_and_error_matching_is_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "INFO warmup\n"
                + "\n".join(f"INFO filler {i}" for i in range(2000))
                + "\nErrors=3\nREGIME_ERROR\n[ERROR] real failure\n",
                encoding="utf-8",
            )

            lines = watchdog._tail_log(path, 10)

        self.assertLessEqual(len(lines), 10)
        self.assertEqual(watchdog._count_error_lines(["Errors=3", "REGIME_ERROR"]), 0)
        self.assertEqual(watchdog._count_error_lines(lines), 1)

    def test_clear_stuck_recovery_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = "data/runtime_recovery.json"
            path = root / rel
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"paused": True, "risk_off": True}), encoding="utf-8")

            with patch.object(settings, "TRADING_MODE", "paper"), patch.object(
                settings, "STATE_RECOVERY_FILE", rel
            ), patch.object(settings, "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY", False), patch.object(
                os, "getcwd", return_value=str(root)
            ):
                changed, _ = watchdog._clear_stuck_recovery(root)

            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(changed)
        self.assertTrue(data["paused"])
        self.assertTrue(data["risk_off"])


if __name__ == "__main__":
    unittest.main()
