import unittest
from types import SimpleNamespace

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position


class FakeExchange:
    def fetch_ohlcv(self, symbol):
        return pd.DataFrame({"close": [94.0]})


class FakeRisk:
    def __init__(self):
        self.open_positions = {
            "BTC/USDT": Position(
                symbol="BTC/USDT",
                entry_price=100.0,
                amount=0.5,
                stop_loss=95.0,
                take_profit=110.0,
                side="long",
                highest_price=100.0,
                strategy_name="UnitStrategy",
            )
        }
        self.closed = 0

    def check_exit_conditions(self, symbol, current_price):
        return "stop_loss"

    def close_position(self, symbol, current_price):
        self.closed += 1
        self.open_positions.pop(symbol, None)
        return -3.0


class FakeExecEngine:
    def __init__(self, result):
        self.result = result
        self.exit_calls = 0
        self.entry_calls = 0

    def execute_exit(self, symbol, side, amount):
        self.exit_calls += 1
        return self.result

    def execute_entry(self, *args, **kwargs):
        self.entry_calls += 1
        return self.result


class BotSafetyTests(unittest.TestCase):
    def _bot_for_exit(self, exec_result):
        bot = object.__new__(MultiStrategyBot)
        bot.exchange = FakeExchange()
        bot.risk = FakeRisk()
        bot.exec_engine = FakeExecEngine(exec_result)
        bot.health = SimpleNamespace(update_data_freshness=lambda symbol: None)
        bot.repo = SimpleNamespace(close_trade=lambda *args, **kwargs: True)
        bot.perf_tracker = SimpleNamespace(refresh=lambda: None)
        bot.tg = SimpleNamespace(notify_trade_closed=lambda *args, **kwargs: None)
        bot.decision_repo = SimpleNamespace(available=False)
        bot._open_trade_ids = {"BTC/USDT": 123}
        bot._last_prices = {}
        bot._active_strategy_runtime = "UnitStrategy"
        bot._last_brain_snapshot = {}
        bot._recovery_blocked_symbols = set()
        return bot

    def test_failed_exit_order_keeps_local_position_open(self):
        failed = ExecutionResult.failed("fp", "exchange unavailable")
        bot = self._bot_for_exit(failed)

        bot._process_pair("BTC/USDT")

        self.assertIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot.risk.closed, 0)
        self.assertEqual(bot._open_trade_ids["BTC/USDT"], 123)

    def test_successful_exit_order_closes_local_position(self):
        ok = ExecutionResult(
            success=True,
            order={"id": "exit-1", "status": "closed"},
            fill_price=94.0,
            intended_price=0.0,
            deviation_pct=0.0,
            retries_used=0,
            fingerprint="fp",
            reason="",
        )
        bot = self._bot_for_exit(ok)

        bot._process_pair("BTC/USDT")

        self.assertNotIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot.risk.closed, 1)
        self.assertNotIn("BTC/USDT", bot._open_trade_ids)

    def test_live_futures_short_is_blocked_before_execution(self):
        original_mode = settings.TRADING_MODE
        original_futures = settings.FUTURES_MODE
        try:
            settings.TRADING_MODE = "live"
            settings.FUTURES_MODE = True
            bot = object.__new__(MultiStrategyBot)
            bot.exec_engine = FakeExecEngine(ExecutionResult.failed("fp", "should not call"))
            bot.decision_repo = SimpleNamespace(available=False)
            bot._last_brain_snapshot = {}
            bot._active_strategy_runtime = "UnitStrategy"
            signal = EnhancedSignal(
                strategy_name="UnitStrategy",
                symbol="BTC/USDT",
                timeframe="1h",
                side=Side.SHORT,
                confidence=90.0,
                entry=100.0,
                stop_loss=105.0,
                take_profit=90.0,
                rr=2.0,
                reason="unit",
            )

            bot._execute_short("BTC/USDT", signal, 0.5)

            self.assertEqual(bot.exec_engine.entry_calls, 0)
        finally:
            settings.TRADING_MODE = original_mode
            settings.FUTURES_MODE = original_futures

    def test_unhealthy_cycle_still_processes_exits_without_entries(self):
        bot = object.__new__(MultiStrategyBot)
        calls = []
        bot._startup_checks_ok = True
        bot.pairs = ["BTC/USDT", "ETH/USDT"]
        bot.exec_engine = SimpleNamespace(
            is_healthy=False,
            get_status=lambda: {
                "pause_reason": "circuit",
                "circuit_state": "open",
                "consecutive_errors": 3,
                "kill_switch": False,
            },
        )
        bot.health = SimpleNamespace(
            update_heartbeat=lambda: None,
            record_error=lambda *args, **kwargs: None,
        )
        bot._active_strategy_runtime = "UnitStrategy"
        bot._process_pair = lambda symbol, allow_entries=True: calls.append((symbol, allow_entries))
        bot._sync_runtime_state = lambda: None

        bot.run_cycle()

        self.assertEqual(calls, [("BTC/USDT", False), ("ETH/USDT", False)])


if __name__ == "__main__":
    unittest.main()
