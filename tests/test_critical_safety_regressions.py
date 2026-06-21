import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.safety import watchdog
from src.strategies.signal import EnhancedSignal, Side
from src.telegram.control_panel import TelegramControlPanel
from src.utils.risk_manager import Position


class _FakeExchange:
    def __init__(self, df):
        self._df = df

    def fetch_ohlcv(self, symbol):
        return self._df


class _FakeHealth:
    def update_data_freshness(self, symbol):
        return None

    def record_error(self, level, message):
        return None


class _FakeExecEngine:
    def __init__(self, result=None):
        self.result = result
        self.entry_calls = []

    def execute_exit(self, symbol, side, amount):
        return self.result

    def execute_entry(self, **kwargs):
        self.entry_calls.append(kwargs)
        raise AssertionError("execute_entry must not be called")


class _FakeRisk:
    def __init__(self, position):
        self.open_positions = {position.symbol: position}
        self.close_called = False

    def check_exit_conditions(self, symbol, current_price):
        return "stop_loss"

    def close_position(self, symbol, current_price):
        self.close_called = True
        self.open_positions.pop(symbol, None)
        return -1.0


class _FakeDecisionRepo:
    available = False


class CriticalTradingSafetyTests(unittest.TestCase):
    def test_failed_multistrategy_exit_keeps_position_and_trade_id_open(self):
        symbol = "BTC/USDT"
        df = pd.DataFrame({"close": [90.0]})
        position = Position(
            symbol=symbol,
            entry_price=100.0,
            amount=0.5,
            stop_loss=95.0,
            take_profit=110.0,
            side="long",
            strategy_name="SafetyTest",
        )
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exchange = _FakeExchange(df)
        bot.health = _FakeHealth()
        bot.risk = _FakeRisk(position)
        bot.exec_engine = _FakeExecEngine(
            ExecutionResult.failed("fp", "simulated exchange failure")
        )
        bot.decision_repo = _FakeDecisionRepo()
        bot._open_trade_ids = {symbol: 123}
        bot._last_prices = {}
        bot._active_strategy_runtime = "SafetyTest"

        bot._process_pair(symbol)

        self.assertIn(symbol, bot.risk.open_positions)
        self.assertFalse(bot.risk.close_called)
        self.assertEqual(bot._open_trade_ids[symbol], 123)

    def test_live_futures_short_is_blocked_before_execution_engine(self):
        old_mode = settings.TRADING_MODE
        old_futures = settings.FUTURES_MODE
        try:
            settings.TRADING_MODE = "live"
            settings.FUTURES_MODE = True
            bot = MultiStrategyBot.__new__(MultiStrategyBot)
            bot.exec_engine = _FakeExecEngine()
            bot._active_strategy_runtime = "SafetyTest"
            signal = EnhancedSignal(
                strategy_name="SafetyTest",
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

            bot._execute_short("BTC/USDT", signal, 0.5)

            self.assertEqual(bot.exec_engine.entry_calls, [])
        finally:
            settings.TRADING_MODE = old_mode
            settings.FUTURES_MODE = old_futures


class TelegramPanelSafetyTests(unittest.TestCase):
    def test_empty_panel_allowlist_falls_back_to_main_chat_only(self):
        old_enabled = settings.TELEGRAM_ENABLED
        old_panel = settings.TELEGRAM_PANEL_ENABLED
        old_token = settings.TELEGRAM_BOT_TOKEN
        old_chat = settings.TELEGRAM_CHAT_ID
        old_allowed = settings.TELEGRAM_PANEL_ALLOWED_IDS
        try:
            settings.TELEGRAM_ENABLED = True
            settings.TELEGRAM_PANEL_ENABLED = True
            settings.TELEGRAM_BOT_TOKEN = "token"
            settings.TELEGRAM_CHAT_ID = "123"
            settings.TELEGRAM_PANEL_ALLOWED_IDS = ""
            with patch("src.telegram.control_panel.TradeRepository"):
                panel = TelegramControlPanel()
            dispatched = []
            panel._dispatch_command = lambda chat_id, text: dispatched.append((chat_id, text))

            panel._handle_update({"message": {"chat": {"id": 999}, "text": "/riskon"}})
            panel._handle_update({"message": {"chat": {"id": 123}, "text": "/status"}})

            self.assertEqual(panel._allowed_ids, {"123"})
            self.assertEqual(dispatched, [("123", "/status")])
        finally:
            settings.TELEGRAM_ENABLED = old_enabled
            settings.TELEGRAM_PANEL_ENABLED = old_panel
            settings.TELEGRAM_BOT_TOKEN = old_token
            settings.TELEGRAM_CHAT_ID = old_chat
            settings.TELEGRAM_PANEL_ALLOWED_IDS = old_allowed


class WatchdogSafetyTests(unittest.TestCase):
    def test_tail_log_reads_only_requested_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bot.log")
            with open(path, "w", encoding="utf-8") as fh:
                for i in range(200):
                    fh.write(f"line-{i}\n")

            self.assertEqual(watchdog._tail_log(path=watchdog.Path(path), max_lines=3), [
                "line-197",
                "line-198",
                "line-199",
            ])

    def test_new_log_reader_does_not_recount_static_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = watchdog.Path(tmp) / "bot.log"
            path.write_text("ERROR one\nERROR two\nERROR three\n", encoding="utf-8")

            lines, offset = watchdog._read_new_log_lines(path, 0, 20)
            self.assertEqual(watchdog._count_error_lines(lines), 3)

            lines, offset = watchdog._read_new_log_lines(path, offset, 20)
            self.assertEqual(lines, [])

            with path.open("a", encoding="utf-8") as fh:
                fh.write("ERROR four\n")
            lines, offset = watchdog._read_new_log_lines(path, offset, 20)
            self.assertEqual(lines, ["ERROR four"])


class DeploySyncSafetyTests(unittest.TestCase):
    def test_deploy_sync_preserves_runtime_files_and_restarts_on_failure(self):
        script = (Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("trap finish EXIT", script)
        self.assertIn("RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)", script)
        self.assertIn('cp -a "$TMP_DIR/$file" "$file"', script)
        self.assertLess(
            script.index("sudo systemctl stop safety-watchdog"),
            script.index("sudo systemctl stop krypto-bot"),
        )
        self.assertIn("Bot/Watchdog werden mit lokalem Runtime-State wieder gestartet", script)


if __name__ == "__main__":
    unittest.main()
