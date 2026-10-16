import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionEngine, ExecutionResult
from src.engine.risk_engine import RiskEngine
from src.engine.runtime_control import runtime_control
from src.safety.watchdog import (
    _clear_stuck_recovery,
    _count_error_lines,
    _read_new_log_lines,
    _restart_bot,
    _tail_log,
)
from src.strategies.signal import EnhancedSignal, Side
from src.telegram.control_panel import TelegramControlPanel
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

    def update_data_freshness(self, symbol):
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


class _EmptyOrderConnector(_ConnectorForExecutionEngine):
    def create_market_buy_order(self, symbol, amount):
        self.buy_calls.append((symbol, amount))
        return {}


class _PauseDuringRetryConnector(_ConnectorForExecutionEngine):
    def create_market_buy_order(self, symbol, amount):
        self.buy_calls.append((symbol, amount))
        runtime_control.pause_entries()
        raise TimeoutError("temporary exchange timeout")


class CriticalSafetyRegressionTests(unittest.TestCase):
    def setUp(self):
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        self._old_mode = settings.TRADING_MODE
        self._old_futures = settings.FUTURES_MODE
        self._old_short_enabled = settings.SHORT_ENABLED

    def tearDown(self):
        settings.TRADING_MODE = self._old_mode
        settings.FUTURES_MODE = self._old_futures
        settings.SHORT_ENABLED = self._old_short_enabled
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def _long_signal(self):
        return EnhancedSignal(
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

    def _short_signal(self):
        return EnhancedSignal(
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
        signal = self._short_signal()

        bot._execute_short("BTC/USDT", signal, 1.0)

        self.assertEqual(bot.exec_engine.entry_calls, [])
        self.assertEqual(bot.risk.open_positions, {})

    def test_execution_engine_runtime_pause_blocks_entry_connector_call(self):
        connector = _ConnectorForExecutionEngine()
        engine = ExecutionEngine(connector)
        signal = self._long_signal()
        runtime_control.pause_entries()

        result = engine.execute_entry("BTC/USDT", "buy", 1.0, signal)

        self.assertFalse(result.success)
        self.assertIn("CONTROL PAUSE", result.reason)
        self.assertEqual(connector.buy_calls, [])

    def test_short_enabled_blocks_native_short_signals(self):
        settings.SHORT_ENABLED = False
        engine = RiskEngine(initial_balance=10_000.0)

        allowed, reason = engine.check_signal(self._short_signal())

        self.assertFalse(allowed)
        self.assertIn("SHORT DISABLED", reason)

    def test_empty_order_result_is_not_retried(self):
        connector = _EmptyOrderConnector()
        engine = ExecutionEngine(connector)
        old_retries = settings.EXECUTION_MAX_RETRIES
        try:
            settings.EXECUTION_MAX_RETRIES = 3
            result = engine.execute_entry(
                "BTC/USDT", "buy", 1.0, self._long_signal()
            )
        finally:
            settings.EXECUTION_MAX_RETRIES = old_retries

        self.assertFalse(result.success)
        self.assertEqual(connector.buy_calls, [("BTC/USDT", 1.0)])

    def test_pause_during_retry_blocks_next_connector_call(self):
        connector = _PauseDuringRetryConnector()
        engine = ExecutionEngine(connector)
        old_retries = settings.EXECUTION_MAX_RETRIES
        old_backoff = settings.EXECUTION_RETRY_BACKOFF_SEC
        try:
            settings.EXECUTION_MAX_RETRIES = 3
            settings.EXECUTION_RETRY_BACKOFF_SEC = 0
            result = engine.execute_entry(
                "BTC/USDT", "buy", 1.0, self._long_signal()
            )
        finally:
            settings.EXECUTION_MAX_RETRIES = old_retries
            settings.EXECUTION_RETRY_BACKOFF_SEC = old_backoff

        self.assertFalse(result.success)
        self.assertIn("CONTROL PAUSE", result.reason)
        self.assertEqual(connector.buy_calls, [("BTC/USDT", 1.0)])

    def test_paper_control_locks_are_not_cleared_without_opt_in(self):
        old_clear = settings.PAPER_CLEAR_CONTROL_LOCKS_EACH_CYCLE
        try:
            settings.TRADING_MODE = "paper"
            settings.PAPER_CLEAR_CONTROL_LOCKS_EACH_CYCLE = False
            runtime_control.pause_entries()
            runtime_control.enable_risk_off()
            bot = MultiStrategyBot.__new__(MultiStrategyBot)

            bot._paper_undo_unwanted_control_locks()
        finally:
            settings.PAPER_CLEAR_CONTROL_LOCKS_EACH_CYCLE = old_clear

        snapshot = runtime_control.get_snapshot()
        self.assertTrue(snapshot["paused"])
        self.assertTrue(snapshot["risk_off"])

    def test_watchdog_does_not_clear_recovery_without_opt_in(self):
        old_clear = settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY
        old_file = settings.STATE_RECOVERY_FILE
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_recovery.json"
            path.write_text(
                json.dumps({"paused": True, "risk_off": True}), encoding="utf-8"
            )
            try:
                settings.TRADING_MODE = "paper"
                settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False
                settings.STATE_RECOVERY_FILE = str(path)

                changed, _ = _clear_stuck_recovery(Path(tmp))
            finally:
                settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = old_clear
                settings.STATE_RECOVERY_FILE = old_file

            self.assertFalse(changed)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"paused": True, "risk_off": True},
            )

    def test_watchdog_tail_and_delta_are_bounded_to_new_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "\n".join(f"old-{i}" for i in range(1000)) + "\n",
                encoding="utf-8",
            )
            stat = path.stat()
            identity = (int(stat.st_dev), int(stat.st_ino))
            offset = int(stat.st_size)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("INFO healthy\nERROR new failure\n")

            lines, new_identity, new_offset = _read_new_log_lines(
                path, identity, offset, 5
            )

            self.assertEqual(_tail_log(path, 2), ["INFO healthy", "ERROR new failure"])
            self.assertEqual(lines, ["INFO healthy", "ERROR new failure"])
            self.assertEqual(new_identity, identity)
            self.assertEqual(new_offset, path.stat().st_size)

    def test_watchdog_error_matcher_ignores_status_identifiers(self):
        self.assertEqual(
            _count_error_lines(["Errors=3", "REGIME_ERROR", "INFO healthy"]), 0
        )
        self.assertEqual(_count_error_lines(["ERROR exchange unavailable"]), 1)

    def test_failed_restart_does_not_start_cooldown(self):
        old_cmd = settings.SAFETY_WATCHDOG_RESTART_CMD
        try:
            settings.SAFETY_WATCHDOG_RESTART_CMD = "false"
            with patch(
                "src.safety.watchdog.subprocess.run",
                return_value=subprocess.CompletedProcess("false", 1),
            ) as run:
                last_restart = _restart_bot("bot_process_missing", None, None)
        finally:
            settings.SAFETY_WATCHDOG_RESTART_CMD = old_cmd

        self.assertIsNone(last_restart)
        run.assert_called_once()

    def test_telegram_panel_falls_back_to_single_configured_chat(self):
        fields = {
            "TELEGRAM_BOT_TOKEN": settings.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHAT_ID": settings.TELEGRAM_CHAT_ID,
            "TELEGRAM_ENABLED": settings.TELEGRAM_ENABLED,
            "TELEGRAM_PANEL_ENABLED": settings.TELEGRAM_PANEL_ENABLED,
            "TELEGRAM_PANEL_ALLOWED_IDS": settings.TELEGRAM_PANEL_ALLOWED_IDS,
        }
        try:
            settings.TELEGRAM_BOT_TOKEN = "token"
            settings.TELEGRAM_CHAT_ID = "123"
            settings.TELEGRAM_ENABLED = True
            settings.TELEGRAM_PANEL_ENABLED = True
            settings.TELEGRAM_PANEL_ALLOWED_IDS = ""
            with patch(
                "src.telegram.control_panel.TradeRepository", return_value=Mock()
            ):
                panel = TelegramControlPanel(notifier=SimpleNamespace())
            panel._dispatch_command = Mock()

            panel._handle_update({"message": {"chat": {"id": 999}, "text": "/pause"}})
            panel._handle_update({"message": {"chat": {"id": 123}, "text": "/pause"}})
        finally:
            for name, value in fields.items():
                setattr(settings, name, value)

        self.assertTrue(panel._enabled)
        panel._dispatch_command.assert_called_once_with("123", "/pause")

    def test_telegram_group_requires_allowed_sender_not_only_allowed_chat(self):
        fields = {
            "TELEGRAM_BOT_TOKEN": settings.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHAT_ID": settings.TELEGRAM_CHAT_ID,
            "TELEGRAM_ENABLED": settings.TELEGRAM_ENABLED,
            "TELEGRAM_PANEL_ENABLED": settings.TELEGRAM_PANEL_ENABLED,
            "TELEGRAM_PANEL_ALLOWED_IDS": settings.TELEGRAM_PANEL_ALLOWED_IDS,
        }
        try:
            settings.TELEGRAM_BOT_TOKEN = "token"
            settings.TELEGRAM_CHAT_ID = "-100"
            settings.TELEGRAM_ENABLED = True
            settings.TELEGRAM_PANEL_ENABLED = True
            settings.TELEGRAM_PANEL_ALLOWED_IDS = "-100,123"
            with patch(
                "src.telegram.control_panel.TradeRepository", return_value=Mock()
            ):
                panel = TelegramControlPanel(notifier=SimpleNamespace())
            panel._dispatch_command = Mock()
            base_chat = {"id": -100, "type": "supergroup"}

            panel._handle_update(
                {
                    "message": {
                        "chat": base_chat,
                        "from": {"id": 999},
                        "text": "/killswitchoff",
                    }
                }
            )
            panel._handle_update(
                {
                    "message": {
                        "chat": base_chat,
                        "from": {"id": 123},
                        "text": "/killswitchoff",
                    }
                }
            )
        finally:
            for name, value in fields.items():
                setattr(settings, name, value)

        panel._dispatch_command.assert_called_once_with("-100", "/killswitchoff")

    def test_telegram_panel_without_any_allowed_chat_is_disabled(self):
        fields = {
            "TELEGRAM_BOT_TOKEN": settings.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHAT_ID": settings.TELEGRAM_CHAT_ID,
            "TELEGRAM_ENABLED": settings.TELEGRAM_ENABLED,
            "TELEGRAM_PANEL_ENABLED": settings.TELEGRAM_PANEL_ENABLED,
            "TELEGRAM_PANEL_ALLOWED_IDS": settings.TELEGRAM_PANEL_ALLOWED_IDS,
        }
        try:
            settings.TELEGRAM_BOT_TOKEN = "token"
            settings.TELEGRAM_CHAT_ID = ""
            settings.TELEGRAM_ENABLED = True
            settings.TELEGRAM_PANEL_ENABLED = True
            settings.TELEGRAM_PANEL_ALLOWED_IDS = ""
            with patch(
                "src.telegram.control_panel.TradeRepository", return_value=Mock()
            ):
                panel = TelegramControlPanel(notifier=SimpleNamespace())
        finally:
            for name, value in fields.items():
                setattr(settings, name, value)

        self.assertFalse(panel._enabled)

    def test_deploy_sync_preserves_runtime_state_and_stops_watchdog(self):
        script = (
            Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("backup_runtime_files", script)
        self.assertIn("restore_runtime_files", script)
        self.assertIn("trap cleanup EXIT", script)
        self.assertLess(
            script.index("systemctl stop safety-watchdog"),
            script.index("systemctl stop krypto-bot"),
        )
        self.assertLess(
            script.index("git pull origin main"),
            script.rindex("restore_runtime_files"),
        )


if __name__ == "__main__":
    unittest.main()
