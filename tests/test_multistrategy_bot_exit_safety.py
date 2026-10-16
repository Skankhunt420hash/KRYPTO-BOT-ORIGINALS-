import unittest
from types import SimpleNamespace

import pandas as pd

from src.bot import MultiStrategyBot
from src.engine.execution_engine import ExecutionResult
from src.utils.risk_manager import Position


class FakeExchange:
    def __init__(self, price=111.0):
        self.price = price

    def fetch_ohlcv(self, symbol):
        return pd.DataFrame(
            {
                "open": [self.price],
                "high": [self.price],
                "low": [self.price],
                "close": [self.price],
                "volume": [100.0],
            }
        )


class FakeRisk:
    def __init__(self):
        self.open_positions = {
            "TEST/USDT": Position(
                symbol="TEST/USDT",
                entry_price=100.0,
                amount=2.0,
                stop_loss=90.0,
                take_profit=110.0,
                side="long",
                highest_price=100.0,
                strategy_name="UnitStrategy",
            )
        }
        self.close_calls = []

    def check_exit_conditions(self, symbol, current_price):
        return "take_profit"

    def close_position(self, symbol, current_price):
        self.close_calls.append((symbol, current_price))
        self.open_positions.pop(symbol, None)
        return (current_price - 100.0) * 2.0

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


class FakeExecEngine:
    def __init__(self, *, healthy=True, exit_success=True):
        self.is_healthy = healthy
        self.exit_success = exit_success
        self.exit_calls = []

    def get_status(self):
        return {
            "pause_reason": "test pause",
            "circuit_state": "open",
            "consecutive_errors": 3,
            "kill_switch": False,
        }

    def execute_exit(self, symbol, side, amount):
        self.exit_calls.append((symbol, side, amount))
        if self.exit_success:
            return ExecutionResult(
                success=True,
                order={"id": "exit-1", "status": "closed"},
                fill_price=111.0,
                intended_price=0.0,
                deviation_pct=0.0,
                retries_used=0,
                fingerprint="exit-test",
                reason="",
            )
        return ExecutionResult.failed("exit-test", "exchange unavailable")


class FakeRepo:
    def __init__(self):
        self.closed_trades = []

    def close_trade(self, *args):
        self.closed_trades.append(args)


class FakeNotifier:
    def __init__(self):
        self.closed_notifications = []
        self.errors = []

    def notify_trade_closed(self, **kwargs):
        self.closed_notifications.append(kwargs)

    def notify_error(self, *args):
        self.errors.append(args)


class MultiStrategyBotExitSafetyTests(unittest.TestCase):
    def _make_bot(self, *, startup_ok=True, exec_healthy=True, exit_success=True):
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exchange = FakeExchange()
        bot.pairs = ["TEST/USDT"]
        bot.risk = FakeRisk()
        bot.exec_engine = FakeExecEngine(
            healthy=exec_healthy,
            exit_success=exit_success,
        )
        bot.repo = FakeRepo()
        bot.tg = FakeNotifier()
        bot.perf_tracker = SimpleNamespace(refresh=lambda: None)
        bot.scorer = SimpleNamespace(refresh=lambda: None)
        bot.health = SimpleNamespace(
            update_heartbeat=lambda: None,
            update_data_freshness=lambda symbol: None,
            record_error=lambda *args: None,
        )
        bot._open_trade_ids = {"TEST/USDT": 42}
        bot._last_prices = {}
        bot._recovery_blocked_symbols = set()
        bot._startup_checks_ok = startup_ok
        bot._startup_block_reason = "startup failed" if not startup_ok else ""
        bot._active_strategy_runtime = "Multi (Meta-Selector)"
        bot._last_selector_snapshot = {}
        bot._last_brain_snapshot = {}
        bot._paper_undo_unwanted_control_locks = lambda: None
        bot._update_performance_tracking = lambda: None
        bot._sync_runtime_state = lambda: None
        bot._persist_recovery_state = lambda: None
        bot._record_trade_event = lambda **kwargs: None
        bot._record_last_decision = lambda **kwargs: None
        bot._log_decision_cycle = lambda **kwargs: None
        return bot

    def test_startup_gate_still_processes_open_position_exit(self):
        bot = self._make_bot(startup_ok=False)

        bot.run_cycle()

        self.assertEqual(bot.exec_engine.exit_calls, [("TEST/USDT", "sell", 2.0)])
        self.assertNotIn("TEST/USDT", bot.risk.open_positions)
        self.assertEqual(bot.repo.closed_trades[0][0], 42)

    def test_unhealthy_execution_engine_still_processes_open_position_exit(self):
        bot = self._make_bot(exec_healthy=False)

        bot.run_cycle()

        self.assertEqual(bot.exec_engine.exit_calls, [("TEST/USDT", "sell", 2.0)])
        self.assertNotIn("TEST/USDT", bot.risk.open_positions)
        self.assertEqual(bot.repo.closed_trades[0][0], 42)

    def test_failed_exit_order_keeps_local_and_db_position_open(self):
        bot = self._make_bot(exit_success=False)

        bot._process_pair("TEST/USDT")

        self.assertEqual(bot.exec_engine.exit_calls, [("TEST/USDT", "sell", 2.0)])
        self.assertIn("TEST/USDT", bot.risk.open_positions)
        self.assertEqual(bot.risk.close_calls, [])
        self.assertEqual(bot._open_trade_ids, {"TEST/USDT": 42})
        self.assertEqual(bot.repo.closed_trades, [])
        self.assertEqual(bot.tg.closed_notifications, [])


if __name__ == "__main__":
    unittest.main()
