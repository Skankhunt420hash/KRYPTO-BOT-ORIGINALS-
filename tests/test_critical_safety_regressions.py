import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.safety.watchdog import _clear_stuck_recovery, _count_error_lines, _tail_log
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position, RiskManager


class _FakeExchange:
    def __init__(self, price: float) -> None:
        self.price = price

    def fetch_ohlcv(self, symbol: str):
        return pd.DataFrame(
            {
                "open": [self.price],
                "high": [self.price],
                "low": [self.price],
                "close": [self.price],
                "volume": [1.0],
            }
        )


class _FakeHealth:
    def update_data_freshness(self, symbol: str) -> None:
        return None


class _FailingExitEngine:
    def execute_exit(self, symbol: str, side: str, amount: float):
        return ExecutionResult.failed("exit-test", "exchange_timeout")


class _ExplodingEntryEngine:
    def execute_entry(self, *args, **kwargs):
        raise AssertionError("live futures short must not submit sell entry orders")


class CriticalSafetyRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = {
            name: getattr(settings, name, None)
            for name in (
                "TRADING_MODE",
                "FUTURES_MODE",
                "STATE_RECOVERY_FILE",
                "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY",
            )
        }

    def tearDown(self) -> None:
        for name, value in self._orig.items():
            setattr(settings, name, value)

    def test_failed_multistrategy_exit_keeps_position_open(self):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exchange = _FakeExchange(price=94.0)
        bot.health = _FakeHealth()
        bot.exec_engine = _FailingExitEngine()
        bot.risk = RiskManager(initial_balance=10_000.0)
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
        bot._open_trade_ids = {"BTC/USDT": 123}
        bot._last_prices = {}
        bot._recovery_blocked_symbols = set()
        bot._active_strategy_runtime = "TestStrategy"
        decisions = []
        bot._record_last_decision = lambda **kwargs: decisions.append(kwargs)

        bot._process_pair("BTC/USDT")

        self.assertIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids["BTC/USDT"], 123)
        self.assertEqual(bot.risk.total_trades, 0)
        self.assertEqual(decisions[-1]["decision"], "exit_failed_position_kept_open")

    def test_live_futures_short_is_blocked_before_sell_order(self):
        settings.TRADING_MODE = "live"
        settings.FUTURES_MODE = True
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exec_engine = _ExplodingEntryEngine()
        decisions = []
        bot._record_last_decision = lambda **kwargs: decisions.append(kwargs)

        signal = EnhancedSignal(
            strategy_name="ShortStrategy",
            symbol="ETH/USDT",
            timeframe="1h",
            side=Side.SHORT,
            confidence=80.0,
            entry=100.0,
            stop_loss=105.0,
            take_profit=90.0,
            rr=2.0,
            reason="unit-test",
        )

        bot._execute_short("ETH/USDT", signal, amount=1.0)

        self.assertEqual(decisions[-1]["decision"], "short_live_futures_blocked")

    def test_watchdog_does_not_clear_recovery_locks_by_default(self):
        settings.TRADING_MODE = "paper"
        settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "data" / "runtime_recovery.json"
            recovery.parent.mkdir(parents=True)
            recovery.write_text(
                json.dumps({"paused": True, "risk_off": True}),
                encoding="utf-8",
            )
            settings.STATE_RECOVERY_FILE = "data/runtime_recovery.json"

            cleared, msg = _clear_stuck_recovery(root)

            self.assertFalse(cleared)
            self.assertIn("aus", msg)
            data = json.loads(recovery.read_text(encoding="utf-8"))
            self.assertTrue(data["paused"])
            self.assertTrue(data["risk_off"])

    def test_tail_log_returns_only_recent_lines_from_large_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "\n".join(f"line-{i}" for i in range(2000)),
                encoding="utf-8",
            )

            self.assertEqual(
                _tail_log(path, max_lines=5),
                ["line-1995", "line-1996", "line-1997", "line-1998", "line-1999"],
            )

    def test_watchdog_error_counter_ignores_status_words(self):
        self.assertEqual(
            _count_error_lines(["Status: Errors=3", "regime=REGIME_ERROR", "ERROR real failure"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
