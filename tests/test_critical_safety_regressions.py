import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.safety import watchdog
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


class _FakeHealth:
    def update_data_freshness(self, symbol):
        self.last_symbol = symbol


class _FakeExchange:
    def __init__(self, price=100.0):
        self.price = price

    def fetch_ohlcv(self, symbol):
        idx = pd.date_range("2026-01-01", periods=2, freq="h")
        return pd.DataFrame(
            {
                "open": [self.price, self.price],
                "high": [self.price, self.price],
                "low": [self.price, self.price],
                "close": [self.price, self.price],
                "volume": [1.0, 1.0],
            },
            index=idx,
        )


class _ExitFailingExecutionEngine:
    def __init__(self):
        self.calls = []

    def execute_exit(self, symbol, side, amount):
        self.calls.append((symbol, side, amount))
        return ExecutionResult.failed("exit-test", "exchange unavailable")


class _EntryRecordingExecutionEngine:
    def __init__(self):
        self.entry_calls = []

    def execute_entry(self, **kwargs):
        self.entry_calls.append(kwargs)
        return ExecutionResult(
            success=True,
            order={"id": "unexpected"},
            fill_price=kwargs["signal"].entry,
            intended_price=kwargs["signal"].entry,
            deviation_pct=0.0,
            retries_used=0,
            fingerprint="unexpected",
            reason="",
        )


class _RiskWithOpenPosition:
    def __init__(self, position):
        self.open_positions = {position.symbol: position}
        self.closed = False

    def check_exit_conditions(self, symbol, current_price):
        return "stop_loss"

    def close_position(self, symbol, current_price):
        self.closed = True
        self.open_positions.pop(symbol, None)
        return -1.0


class _ShortRisk:
    def __init__(self):
        self.opened = False

    def open_with_signal(self, signal, amount):
        self.opened = True


def _bare_bot():
    bot = object.__new__(MultiStrategyBot)
    bot._recovery_blocked_symbols = set()
    bot._last_prices = {}
    bot._last_brain_snapshot = {}
    bot._active_strategy_runtime = "test"
    bot.health = _FakeHealth()
    bot._record_last_decision = lambda **kwargs: setattr(bot, "last_decision", kwargs)
    bot._log_decision_cycle = lambda **kwargs: setattr(bot, "last_cycle", kwargs)
    bot._notify_mini_live_order = lambda **kwargs: setattr(bot, "mini_live_warn", kwargs)
    return bot


class CriticalSafetyRegressionTests(unittest.TestCase):
    def test_failed_multistrategy_exit_keeps_position_and_trade_id_open(self):
        bot = _bare_bot()
        position = Position(
            symbol="BTC/USDT",
            entry_price=110.0,
            amount=0.2,
            stop_loss=105.0,
            take_profit=120.0,
            side="long",
            strategy_name="TestStrategy",
        )
        bot.exchange = _FakeExchange(price=100.0)
        bot.risk = _RiskWithOpenPosition(position)
        bot.exec_engine = _ExitFailingExecutionEngine()
        bot._open_trade_ids = {"BTC/USDT": 42}

        bot._process_pair("BTC/USDT")

        self.assertFalse(bot.risk.closed)
        self.assertIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids["BTC/USDT"], 42)
        self.assertEqual(bot.last_decision["decision"], "exit_failed")
        self.assertEqual(bot.last_cycle["risk_decision"], "exit_failed_keep_open")

    def test_live_futures_short_is_blocked_before_sell_order(self):
        old_mode = settings.TRADING_MODE
        old_futures = settings.FUTURES_MODE
        old_short = settings.SHORT_ENABLED
        try:
            settings.TRADING_MODE = "live"
            settings.FUTURES_MODE = True
            settings.SHORT_ENABLED = True
            bot = _bare_bot()
            bot.exec_engine = _EntryRecordingExecutionEngine()
            bot.risk = _ShortRisk()
            signal = EnhancedSignal(
                strategy_name="ShortStrategy",
                symbol="ETH/USDT",
                timeframe="1h",
                side=Side.SHORT,
                confidence=90.0,
                entry=100.0,
                stop_loss=105.0,
                take_profit=90.0,
                rr=2.0,
                reason="unit-test",
            )

            bot._execute_short("ETH/USDT", signal, 0.5)

            self.assertEqual(bot.exec_engine.entry_calls, [])
            self.assertFalse(bot.risk.opened)
            self.assertEqual(bot.last_decision["reason"], "live_futures_short_not_implemented")
        finally:
            settings.TRADING_MODE = old_mode
            settings.FUTURES_MODE = old_futures
            settings.SHORT_ENABLED = old_short

    def test_short_enabled_false_blocks_paper_short_before_order(self):
        old_mode = settings.TRADING_MODE
        old_futures = settings.FUTURES_MODE
        old_short = settings.SHORT_ENABLED
        try:
            settings.TRADING_MODE = "paper"
            settings.FUTURES_MODE = False
            settings.SHORT_ENABLED = False
            bot = _bare_bot()
            bot.exec_engine = _EntryRecordingExecutionEngine()
            bot.risk = _ShortRisk()
            signal = EnhancedSignal(
                strategy_name="ShortStrategy",
                symbol="ETH/USDT",
                timeframe="1h",
                side=Side.SHORT,
                confidence=90.0,
                entry=100.0,
                stop_loss=105.0,
                take_profit=90.0,
                rr=2.0,
                reason="unit-test",
            )

            bot._execute_short("ETH/USDT", signal, 0.5)

            self.assertEqual(bot.exec_engine.entry_calls, [])
            self.assertFalse(bot.risk.opened)
            self.assertIn("SHORT_ENABLED=false", bot.last_decision["reason"])
        finally:
            settings.TRADING_MODE = old_mode
            settings.FUTURES_MODE = old_futures
            settings.SHORT_ENABLED = old_short

    def test_watchdog_error_matcher_ignores_status_words(self):
        lines = [
            "Status: CB=closed Errors=3 KillSwitch=False",
            "decision=REGIME_ERROR reject_reason=regime_detection_failed",
            "[ERROR] real failure",
            "Traceback (most recent call last):",
        ]

        self.assertEqual(watchdog._count_error_lines(lines), 2)

    def test_watchdog_explicit_empty_log_file_disables_log_checks(self):
        old_env = os.environ.get("SAFETY_WATCHDOG_LOG_FILE")
        old_setting = settings.SAFETY_WATCHDOG_LOG_FILE
        try:
            os.environ["SAFETY_WATCHDOG_LOG_FILE"] = ""
            settings.SAFETY_WATCHDOG_LOG_FILE = ""
            self.assertIsNone(watchdog._resolve_log_path(Path("/tmp/project")))
        finally:
            if old_env is None:
                os.environ.pop("SAFETY_WATCHDOG_LOG_FILE", None)
            else:
                os.environ["SAFETY_WATCHDOG_LOG_FILE"] = old_env
            settings.SAFETY_WATCHDOG_LOG_FILE = old_setting

    def test_watchdog_tail_reads_recent_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            old_lines = [f"old line {idx}" for idx in range(2000)]
            recent_lines = ["recent one", "recent two"]
            path.write_text("\n".join(old_lines + recent_lines), encoding="utf-8")

            self.assertEqual(watchdog._tail_log(path, 2), recent_lines)


if __name__ == "__main__":
    unittest.main()
