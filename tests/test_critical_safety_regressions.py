import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionEngine
from src.engine.risk_engine import RiskEngine
from src.engine.runtime_control import runtime_control
from src.safety.watchdog import (
    _clear_stuck_recovery,
    _count_error_lines,
    _initial_log_cursor,
    _read_new_log_lines,
    _tail_log,
)
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


class CriticalSafetyRegressionTests(unittest.TestCase):
    def tearDown(self) -> None:
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    @staticmethod
    def _short_signal() -> EnhancedSignal:
        return EnhancedSignal(
            strategy_name="NativeShortStrategy",
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

    @staticmethod
    def _long_signal() -> EnhancedSignal:
        return EnhancedSignal(
            strategy_name="NativeLongStrategy",
            symbol="TEST/USDT",
            timeframe="1h",
            side=Side.LONG,
            confidence=80.0,
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            rr=2.0,
            reason="unit-test",
        )

    def test_paper_control_locks_are_not_cleared_by_default(self):
        old_mode = settings.TRADING_MODE
        old_clear = settings.PAPER_CLEAR_CONTROL_LOCKS_EACH_CYCLE
        try:
            settings.TRADING_MODE = "paper"
            settings.PAPER_CLEAR_CONTROL_LOCKS_EACH_CYCLE = False
            runtime_control.pause_entries()
            runtime_control.enable_risk_off()

            bot = MultiStrategyBot.__new__(MultiStrategyBot)
            bot._paper_undo_unwanted_control_locks()

            snap = runtime_control.get_snapshot()
            self.assertTrue(snap["paused"])
            self.assertTrue(snap["risk_off"])
        finally:
            settings.TRADING_MODE = old_mode
            settings.PAPER_CLEAR_CONTROL_LOCKS_EACH_CYCLE = old_clear

    def test_watchdog_does_not_clear_recovery_without_opt_in(self):
        old_mode = settings.TRADING_MODE
        old_clear = settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY
        old_file = settings.STATE_RECOVERY_FILE
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_recovery.json"
            path.write_text(
                json.dumps({"paused": True, "risk_off": True}),
                encoding="utf-8",
            )
            try:
                settings.TRADING_MODE = "paper"
                settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False
                settings.STATE_RECOVERY_FILE = str(path)

                changed, _ = _clear_stuck_recovery(Path(tmp))

                self.assertFalse(changed)
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    {"paused": True, "risk_off": True},
                )
            finally:
                settings.TRADING_MODE = old_mode
                settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = old_clear
                settings.STATE_RECOVERY_FILE = old_file

    def test_watchdog_tail_reads_latest_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "\n".join(f"line-{i}" for i in range(1000)),
                encoding="utf-8",
            )
            self.assertEqual(
                _tail_log(path, 5),
                ["line-995", "line-996", "line-997", "line-998", "line-999"],
            )

    def test_watchdog_error_matcher_ignores_status_names(self):
        lines = [
            "Status: Errors=3 REGIME_ERROR",
            "2026-01-01 | bot | ERROR | exchange failed",
            "2026-01-01 | bot | CRITICAL | state corrupt",
        ]
        self.assertEqual(_count_error_lines(lines), 2)

    def test_watchdog_log_cursor_ignores_stale_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "\n".join("ERROR stale failure" for _ in range(30)) + "\n",
                encoding="utf-8",
            )
            identity, offset = _initial_log_cursor(path)
            path.write_text(
                path.read_text(encoding="utf-8")
                + "INFO recovered\n"
                + "ERROR new failure\n",
                encoding="utf-8",
            )

            lines, identity, offset = _read_new_log_lines(
                path, identity, offset, max_lines=500
            )

            self.assertEqual(lines, ["INFO recovered", "ERROR new failure"])
            self.assertEqual(_count_error_lines(lines), 1)

    def test_watchdog_log_cursor_detects_copytruncate_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text("OLD " + ("x" * 100_000), encoding="utf-8")
            identity, offset = _initial_log_cursor(path)
            path.write_text(
                "NEW ERROR after rotation\n" + ("y" * 110_000),
                encoding="utf-8",
            )

            lines, _, _ = _read_new_log_lines(
                path, identity, offset, max_lines=500
            )

            self.assertTrue(lines[0].startswith("NEW ERROR after rotation"))

    def test_short_enabled_blocks_native_short_signals(self):
        old_short_enabled = settings.SHORT_ENABLED
        try:
            settings.SHORT_ENABLED = False
            engine = RiskEngine(initial_balance=10_000.0)
            allowed, reason = engine.check_signal(self._short_signal())
            self.assertFalse(allowed)
            self.assertIn("SHORT DISABLED", reason)
        finally:
            settings.SHORT_ENABLED = old_short_enabled

    def test_failed_exit_keeps_position_and_database_trade_open(self):
        symbol = "BTC/USDT"
        position = Position(
            symbol=symbol,
            entry_price=100.0,
            amount=1.0,
            stop_loss=95.0,
            take_profit=110.0,
            side="long",
            strategy_name="TestStrategy",
        )

        class FakeRisk:
            def __init__(self):
                self.open_positions = {symbol: position}
                self.close_called = False

            def check_exit_conditions(self, checked_symbol, current_price):
                return "stop_loss"

            def close_position(self, checked_symbol, current_price):
                self.close_called = True
                self.open_positions.pop(checked_symbol, None)
                return -6.0

        risk = FakeRisk()
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exchange = SimpleNamespace(
            fetch_ohlcv=Mock(return_value=pd.DataFrame({"close": [94.0]}))
        )
        bot.health = SimpleNamespace(update_data_freshness=Mock(), record_error=Mock())
        bot.risk = risk
        bot.exec_engine = SimpleNamespace(
            execute_exit=Mock(
                return_value=SimpleNamespace(success=False, reason="exchange_down")
            )
        )
        bot.repo = Mock()
        bot.tg = Mock()
        bot._open_trade_ids = {symbol: 123}
        bot._last_prices = {}
        bot._recovery_blocked_symbols = {symbol}
        bot._market_context = lambda df: {}
        bot._record_last_decision = Mock()
        bot._log_decision_cycle = Mock()

        bot._process_pair(symbol)

        self.assertFalse(risk.close_called)
        self.assertIn(symbol, risk.open_positions)
        self.assertEqual(bot._open_trade_ids[symbol], 123)
        bot.repo.close_trade.assert_not_called()
        bot._record_last_decision.assert_called_with(
            symbol=symbol,
            decision="exit_failed",
            reason="exchange_down",
            strategy="TestStrategy",
        )

    def test_unhealthy_execution_gate_still_checks_open_position_exits(self):
        symbol = "BTC/USDT"
        position = Position(
            symbol=symbol,
            entry_price=100.0,
            amount=1.0,
            stop_loss=95.0,
            take_profit=110.0,
            side="long",
            strategy_name="TestStrategy",
        )
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot._startup_checks_ok = True
        bot._active_strategy_runtime = "test"
        bot.risk = SimpleNamespace(
            open_positions={symbol: position},
            check_exit_conditions=Mock(return_value="stop_loss"),
        )
        bot.exchange = SimpleNamespace(
            fetch_ohlcv=Mock(return_value=pd.DataFrame({"close": [94.0]}))
        )
        bot.exec_engine = SimpleNamespace(
            is_healthy=False,
            get_status=Mock(
                return_value={
                    "pause_reason": "circuit open",
                    "circuit_state": "open",
                    "consecutive_errors": 3,
                    "kill_switch": False,
                }
            ),
        )
        bot.health = SimpleNamespace(update_heartbeat=Mock(), record_error=Mock())
        bot._last_prices = {}
        bot._attempt_position_exit = Mock()

        bot.run_cycle()

        bot._attempt_position_exit.assert_called_once_with(symbol, 94.0, "stop_loss")

    def test_live_futures_short_is_blocked_before_sell_order(self):
        old_mode = settings.TRADING_MODE
        old_futures = settings.FUTURES_MODE
        old_short = settings.SHORT_ENABLED
        try:
            settings.TRADING_MODE = "live"
            settings.FUTURES_MODE = True
            settings.SHORT_ENABLED = True
            bot = MultiStrategyBot.__new__(MultiStrategyBot)
            bot.exec_engine = SimpleNamespace(execute_entry=Mock())
            bot._record_last_decision = Mock()

            bot._execute_short("TEST/USDT", self._short_signal(), 1.0)

            bot.exec_engine.execute_entry.assert_not_called()
            bot._record_last_decision.assert_called_with(
                symbol="TEST/USDT",
                decision="short_blocked",
                reason="live_futures_short_not_implemented",
                strategy="NativeShortStrategy",
            )
        finally:
            settings.TRADING_MODE = old_mode
            settings.FUTURES_MODE = old_futures
            settings.SHORT_ENABLED = old_short

    def test_execution_engine_enforces_runtime_pause_before_entry(self):
        connector = SimpleNamespace(
            fetch_ticker=Mock(return_value={"last": 100.0}),
            create_market_buy_order=Mock(
                return_value={"id": "should-not-run", "status": "closed"}
            ),
        )
        engine = ExecutionEngine(connector)
        runtime_control.pause_entries()

        result = engine.execute_entry(
            "TEST/USDT", "buy", 1.0, self._long_signal()
        )

        self.assertFalse(result.success)
        self.assertIn("CONTROL PAUSE", result.reason)
        connector.create_market_buy_order.assert_not_called()

    def test_empty_connector_result_is_not_retried(self):
        old_retries = settings.EXECUTION_MAX_RETRIES
        settings.EXECUTION_MAX_RETRIES = 3
        try:
            connector = SimpleNamespace(
                fetch_ticker=Mock(return_value={"last": 100.0}),
                create_market_buy_order=Mock(return_value={}),
            )
            engine = ExecutionEngine(connector)

            result = engine.execute_entry(
                "TEST/USDT", "buy", 1.0, self._long_signal()
            )

            self.assertFalse(result.success)
            connector.create_market_buy_order.assert_called_once()
        finally:
            settings.EXECUTION_MAX_RETRIES = old_retries

    def test_unconfirmed_exit_is_not_closed_or_sent_twice(self):
        connector = SimpleNamespace(
            create_market_sell_order=Mock(
                return_value={
                    "id": "pending-exit",
                    "status": "open",
                    "remaining": 1.0,
                }
            )
        )
        engine = ExecutionEngine(connector)

        first = engine.execute_exit("TEST/USDT", "sell", 1.0)
        second = engine.execute_exit("TEST/USDT", "sell", 1.0)

        self.assertFalse(first.success)
        self.assertIn("OrderConfirmationPending", first.reason)
        self.assertFalse(second.success)
        connector.create_market_sell_order.assert_called_once()

    def test_deploy_sync_preserves_runtime_files_and_service_order(self):
        script = Path(__file__).parents[1] / "deploy" / "sync-from-github.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            daily = root / "data" / "daily_summary.json"
            recovery = root / "data" / "runtime_recovery.json"
            daily.write_text('{"days":["local"]}', encoding="utf-8")
            recovery.write_text('{"risk_off":true}', encoding="utf-8")

            bin_dir = root / "bin"
            bin_dir.mkdir()
            call_log = root / "calls.log"
            fake_git = bin_dir / "git"
            fake_git.write_text(
                "#!/usr/bin/env bash\n"
                'echo "git $*" >> "$CALL_LOG"\n'
                'if [[ "$1" == "restore" ]]; then\n'
                '  echo \'{"days":["git"]}\' > data/daily_summary.json\n'
                '  echo \'{"risk_off":false}\' > data/runtime_recovery.json\n'
                "fi\n",
                encoding="utf-8",
            )
            fake_sudo = bin_dir / "sudo"
            fake_sudo.write_text(
                "#!/usr/bin/env bash\n"
                'echo "sudo $*" >> "$CALL_LOG"\n'
                'if [[ "$*" == "systemctl stop krypto-bot" ]]; then\n'
                '  echo \'{"risk_off":true,"state":"final"}\' > data/runtime_recovery.json\n'
                "fi\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            fake_sudo.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["CALL_LOG"] = str(call_log)

            result = subprocess.run(
                ["bash", str(script), str(root)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(daily.read_text(encoding="utf-8"), '{"days":["local"]}')
            self.assertEqual(
                recovery.read_text(encoding="utf-8"),
                '{"risk_off":true,"state":"final"}\n',
            )
            calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertLess(
                calls.index("sudo systemctl stop safety-watchdog"),
                calls.index("sudo systemctl stop krypto-bot"),
            )
            self.assertLess(
                calls.index("sudo systemctl start krypto-bot"),
                calls.index("sudo systemctl start safety-watchdog"),
            )

    def test_deploy_restore_failure_keeps_services_stopped(self):
        script = Path(__file__).parents[1] / "deploy" / "sync-from-github.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            (root / "data" / "daily_summary.json").write_text(
                '{"days":["local"]}', encoding="utf-8"
            )
            (root / "data" / "runtime_recovery.json").write_text(
                '{"risk_off":true}', encoding="utf-8"
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            call_log = root / "calls.log"
            (bin_dir / "git").write_text(
                "#!/usr/bin/env bash\n"
                'echo "git $*" >> "$CALL_LOG"\n'
                'if [[ "$1" == "restore" ]]; then\n'
                '  echo \'{"risk_off":false}\' > data/runtime_recovery.json\n'
                "fi\n",
                encoding="utf-8",
            )
            (bin_dir / "sudo").write_text(
                "#!/usr/bin/env bash\n"
                'echo "sudo $*" >> "$CALL_LOG"\n',
                encoding="utf-8",
            )
            (bin_dir / "cp").write_text(
                "#!/usr/bin/env bash\n"
                'if [[ "$1" == /tmp/* ]]; then exit 1; fi\n'
                'exec /bin/cp "$@"\n',
                encoding="utf-8",
            )
            for executable in ("git", "sudo", "cp"):
                (bin_dir / executable).chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["CALL_LOG"] = str(call_log)

            result = subprocess.run(
                ["bash", str(script), str(root)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Dienste bleiben gestoppt", result.stderr)
            calls = call_log.read_text(encoding="utf-8")
            self.assertNotIn("sudo systemctl start krypto-bot", calls)
            self.assertNotIn("sudo systemctl start safety-watchdog", calls)


if __name__ == "__main__":
    unittest.main()
