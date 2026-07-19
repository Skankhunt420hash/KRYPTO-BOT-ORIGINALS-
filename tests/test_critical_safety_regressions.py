import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.risk_engine import RiskEngine
from src.engine.runtime_control import runtime_control
from src.safety.watchdog import (
    _clear_stuck_recovery,
    _count_error_lines,
    _read_new_log_lines,
    _tail_log,
)
from src.strategies.signal import EnhancedSignal, Side
from src.telegram.control_panel import TelegramControlPanel
from src.utils.risk_manager import Position


class CriticalSafetyRegressionTests(unittest.TestCase):
    def tearDown(self) -> None:
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def _short_signal(self) -> EnhancedSignal:
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

    def test_watchdog_does_not_recount_static_log_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text("ERROR one\nERROR two\n", encoding="utf-8")

            lines, offset = _read_new_log_lines(path, 0, 20)
            self.assertEqual(_count_error_lines(lines), 2)

            lines, offset = _read_new_log_lines(path, offset, 20)
            self.assertEqual(lines, [])

            with path.open("a", encoding="utf-8") as fh:
                fh.write("ERROR three\n")
            lines, offset = _read_new_log_lines(path, offset, 20)
            self.assertEqual(lines, ["ERROR three"])

    def test_watchdog_error_matcher_ignores_status_words(self):
        lines = [
            "Status: CB=open Errors=3 KillSwitch=False",
            "decision=REGIME_ERROR reject_reason=regime_detection_failed",
            "[ERROR] real failure",
            "Traceback (most recent call last):",
        ]

        self.assertEqual(_count_error_lines(lines), 2)

    def test_deploy_sync_preserves_runtime_state_and_stops_watchdog_first(self):
        script = (
            Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)",
            script,
        )
        self.assertIn('trap cleanup EXIT', script)
        self.assertIn('cp "$BACKUP_DIR/$f" "$f"', script)
        self.assertLess(
            script.index("sudo systemctl stop safety-watchdog"),
            script.index("sudo systemctl stop krypto-bot"),
        )
        self.assertIn("sudo systemctl start krypto-bot 2>/dev/null || true", script)
        self.assertIn("sudo systemctl start safety-watchdog 2>/dev/null || true", script)

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

    def test_empty_telegram_allowlist_falls_back_to_main_chat(self):
        old_values = (
            settings.TELEGRAM_ENABLED,
            settings.TELEGRAM_PANEL_ENABLED,
            settings.TELEGRAM_BOT_TOKEN,
            settings.TELEGRAM_CHAT_ID,
            settings.TELEGRAM_PANEL_ALLOWED_IDS,
        )
        try:
            settings.TELEGRAM_ENABLED = True
            settings.TELEGRAM_PANEL_ENABLED = True
            settings.TELEGRAM_BOT_TOKEN = "token"
            settings.TELEGRAM_CHAT_ID = "123"
            settings.TELEGRAM_PANEL_ALLOWED_IDS = ""
            with patch("src.telegram.control_panel.TradeRepository"):
                panel = TelegramControlPanel()
            panel._dispatch_command = Mock()

            panel._handle_update({"message": {"chat": {"id": 999}, "text": "/riskon"}})
            panel._handle_update({"message": {"chat": {"id": 123}, "text": "/status"}})

            self.assertTrue(panel.enabled)
            self.assertEqual(panel._allowed_ids, {"123"})
            panel._dispatch_command.assert_called_once_with("123", "/status")
        finally:
            (
                settings.TELEGRAM_ENABLED,
                settings.TELEGRAM_PANEL_ENABLED,
                settings.TELEGRAM_BOT_TOKEN,
                settings.TELEGRAM_CHAT_ID,
                settings.TELEGRAM_PANEL_ALLOWED_IDS,
            ) = old_values

    def test_telegram_panel_without_any_allowed_chat_is_disabled(self):
        old_values = (
            settings.TELEGRAM_ENABLED,
            settings.TELEGRAM_PANEL_ENABLED,
            settings.TELEGRAM_BOT_TOKEN,
            settings.TELEGRAM_CHAT_ID,
            settings.TELEGRAM_PANEL_ALLOWED_IDS,
        )
        try:
            settings.TELEGRAM_ENABLED = True
            settings.TELEGRAM_PANEL_ENABLED = True
            settings.TELEGRAM_BOT_TOKEN = "token"
            settings.TELEGRAM_CHAT_ID = ""
            settings.TELEGRAM_PANEL_ALLOWED_IDS = ""
            with patch("src.telegram.control_panel.TradeRepository"):
                panel = TelegramControlPanel()

            self.assertFalse(panel.enabled)
            self.assertEqual(panel._allowed_ids, set())
        finally:
            (
                settings.TELEGRAM_ENABLED,
                settings.TELEGRAM_PANEL_ENABLED,
                settings.TELEGRAM_BOT_TOKEN,
                settings.TELEGRAM_CHAT_ID,
                settings.TELEGRAM_PANEL_ALLOWED_IDS,
            ) = old_values

    def test_failed_multistrategy_exit_keeps_position_open(self):
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

        test_case = self

        class FakeRisk:
            def __init__(self) -> None:
                self.open_positions = {symbol: position}
                self.close_called = False

            def check_exit_conditions(self, checked_symbol, current_price):
                test_case.assertEqual(checked_symbol, symbol)
                test_case.assertEqual(current_price, 94.0)
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
        bot._recovery_blocked_symbols = set()
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


if __name__ == "__main__":
    unittest.main()
