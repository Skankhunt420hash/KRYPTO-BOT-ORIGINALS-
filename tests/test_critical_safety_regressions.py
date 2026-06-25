import tempfile
import unittest
from pathlib import Path

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.engine.risk_engine import RiskEngine
from src.safety import watchdog
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


class SettingsPatch:
    def __init__(self, **values):
        self.values = values
        self.old_values = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.old_values[key] = getattr(settings, key)
            setattr(settings, key, value)
        return self

    def __exit__(self, exc_type, exc, tb):
        for key, value in self.old_values.items():
            setattr(settings, key, value)


class DummyDecisionRepo:
    available = False


class DummyExchange:
    def __init__(self, close_price=94.0):
        self.close_price = close_price

    def fetch_ohlcv(self, symbol):
        idx = pd.date_range("2026-01-01", periods=60, freq="h")
        closes = [100.0] * 59 + [self.close_price]
        return pd.DataFrame(
            {
                "open": closes,
                "high": [max(100.0, self.close_price)] * 60,
                "low": [min(100.0, self.close_price)] * 60,
                "close": closes,
                "volume": [1000.0] * 60,
            },
            index=idx,
        )


class DummyHealth:
    def update_data_freshness(self, symbol):
        pass


class FailingExitEngine:
    def execute_exit(self, symbol, order_side, amount):
        return ExecutionResult.failed("exit-fp", "exchange_down")


class RecordingEntryEngine:
    def __init__(self):
        self.calls = []

    def execute_entry(self, **kwargs):
        self.calls.append(kwargs)
        return ExecutionResult(
            success=True,
            order={"id": "unexpected", "status": "closed"},
            fill_price=kwargs["signal"].entry,
            intended_price=kwargs["signal"].entry,
            deviation_pct=0.0,
            retries_used=0,
            fingerprint="entry-fp",
            reason="",
        )


class DummyRepo:
    def __init__(self):
        self.closed_trade_ids = []

    def close_trade(self, trade_id, exit_price, pnl_abs, pnl_pct, reason_close):
        self.closed_trade_ids.append(trade_id)
        return True


def make_short_signal():
    return EnhancedSignal(
        strategy_name="TestShort",
        symbol="TEST/USDT",
        timeframe="1h",
        side=Side.SHORT,
        confidence=80.0,
        entry=100.0,
        stop_loss=105.0,
        take_profit=90.0,
        rr=2.0,
        reason="unit-test",
    )


class CriticalSafetyRegressionTests(unittest.TestCase):
    def _bare_bot(self):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot._active_strategy_runtime = "TestStrategy"
        bot._last_brain_snapshot = {}
        bot._recovery_blocked_symbols = set()
        bot.decision_repo = DummyDecisionRepo()
        return bot

    def test_failed_exit_order_keeps_position_and_db_trade_open(self):
        bot = self._bare_bot()
        bot.exchange = DummyExchange(close_price=94.0)
        bot.health = DummyHealth()
        bot.exec_engine = FailingExitEngine()
        bot.repo = DummyRepo()
        bot.risk = RiskEngine(initial_balance=10_000.0)
        bot.risk.open_positions["TEST/USDT"] = Position(
            symbol="TEST/USDT",
            entry_price=100.0,
            amount=1.0,
            stop_loss=95.0,
            take_profit=110.0,
            side="long",
            strategy_name="TestStrategy",
        )
        bot._open_trade_ids = {"TEST/USDT": 42}

        bot._process_pair("TEST/USDT")

        self.assertIn("TEST/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids, {"TEST/USDT": 42})
        self.assertEqual(bot.repo.closed_trade_ids, [])
        self.assertEqual(bot.risk.total_trades, 0)

    def test_live_futures_short_is_blocked_before_order_execution(self):
        bot = self._bare_bot()
        bot.exec_engine = RecordingEntryEngine()
        bot.risk = RiskEngine(initial_balance=10_000.0)
        bot._open_trade_ids = {}

        with SettingsPatch(TRADING_MODE="live", FUTURES_MODE=True, SHORT_ENABLED=True):
            bot._execute_short("TEST/USDT", make_short_signal(), amount=1.0)

        self.assertEqual(bot.exec_engine.calls, [])
        self.assertEqual(bot.risk.open_positions, {})

    def test_short_enabled_false_blocks_native_short_before_order_execution(self):
        bot = self._bare_bot()
        bot.exec_engine = RecordingEntryEngine()
        bot.risk = RiskEngine(initial_balance=10_000.0)
        bot._open_trade_ids = {}

        with SettingsPatch(TRADING_MODE="paper", FUTURES_MODE=False, SHORT_ENABLED=False):
            bot._execute_short("TEST/USDT", make_short_signal(), amount=1.0)

        self.assertEqual(bot.exec_engine.calls, [])
        self.assertEqual(bot.risk.open_positions, {})

    def test_watchdog_does_not_clear_recovery_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "runtime_recovery.json"
            recovery.write_text(
                '{"paused": true, "risk_off": true, "mode": "paper"}',
                encoding="utf-8",
            )

            with SettingsPatch(
                TRADING_MODE="paper",
                STATE_RECOVERY_FILE=str(recovery),
                SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY=False,
            ):
                changed, msg = watchdog._clear_stuck_recovery(root)

            self.assertFalse(changed, msg)
            self.assertIn('"paused": true', recovery.read_text(encoding="utf-8"))
            self.assertIn('"risk_off": true', recovery.read_text(encoding="utf-8"))

    def test_watchdog_error_matcher_ignores_status_counters(self):
        lines = [
            "Status: CB=closed Errors=3 KillSwitch=False",
            "regime=REGIME_ERROR risk_decision=regime_error",
            "2026-06-25 00:00:00 ERROR real failure",
            "Traceback (most recent call last):",
        ]

        self.assertEqual(watchdog._count_error_lines(lines), 2)

    def test_watchdog_tail_reads_recent_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.log"
            path.write_text(
                "".join(f"line-{i:05d} {'x' * 200}\n" for i in range(2000)),
                encoding="utf-8",
            )

            lines = watchdog._tail_log(path, max_lines=5)

        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[-1].startswith("line-01999 "))

    def test_deploy_sync_preserves_runtime_files_and_restarts_watchdog(self):
        script = Path("deploy/sync-from-github.sh").read_text(encoding="utf-8")

        self.assertIn("restore_runtime_files", script)
        self.assertIn("data/runtime_recovery.json", script)
        self.assertLess(
            script.index("sudo systemctl stop safety-watchdog"),
            script.index("sudo systemctl stop krypto-bot"),
        )
        self.assertIn("sudo systemctl start safety-watchdog", script)


if __name__ == "__main__":
    unittest.main()
