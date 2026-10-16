import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.risk_engine import RiskEngine
from src.engine.runtime_control import runtime_control
from src.safety.watchdog import _clear_stuck_recovery, _tail_log
from src.strategies.signal import EnhancedSignal, Side
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
