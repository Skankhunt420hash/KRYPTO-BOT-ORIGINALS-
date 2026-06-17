import unittest
from unittest.mock import patch

from src.engine.execution_engine import ExecutionEngine
from src.engine.runtime_control import runtime_control
from src.strategies.signal import EnhancedSignal, Side


def _signal() -> EnhancedSignal:
    return EnhancedSignal(
        strategy_name="TestStrategy",
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


class FakeConnector:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.buy_calls = 0

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def create_market_buy_order(self, symbol, amount):
        self.buy_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    def create_market_sell_order(self, symbol, amount):
        raise AssertionError("sell should not be called")


class ExecutionEngineSafetyTests(unittest.TestCase):
    def setUp(self):
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def tearDown(self):
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def test_entry_rejected_when_risk_off_before_order(self):
        connector = FakeConnector([{"id": "ord-1", "average": 100.0, "status": "closed"}])
        runtime_control.enable_risk_off()

        result = ExecutionEngine(connector).execute_entry("BTC/USDT", "buy", 1.0, _signal())

        self.assertFalse(result.success)
        self.assertIn("RISK OFF", result.reason)
        self.assertEqual(connector.buy_calls, 0)

    def test_entry_retry_stops_when_risk_off_enabled_after_first_failure(self):
        def enable_risk_off_and_return():
            runtime_control.enable_risk_off()
            raise TimeoutError("temporary network issue")

        connector = FakeConnector([enable_risk_off_and_return, {"id": "ord-2"}])

        with (
            patch("src.engine.execution_engine.settings.EXECUTION_MAX_RETRIES", 2),
            patch("src.engine.execution_engine.settings.EXECUTION_RETRY_BACKOFF_SEC", 0),
        ):
            result = ExecutionEngine(connector).execute_entry("BTC/USDT", "buy", 1.0, _signal())

        self.assertFalse(result.success)
        self.assertIn("EntryControlBlocked", result.reason)
        self.assertEqual(connector.buy_calls, 1)

    def test_empty_order_result_is_not_retried(self):
        connector = FakeConnector([{}, {"id": "duplicate-risk"}])

        with patch("src.engine.execution_engine.settings.EXECUTION_MAX_RETRIES", 3):
            result = ExecutionEngine(connector).execute_entry("BTC/USDT", "buy", 1.0, _signal())

        self.assertFalse(result.success)
        self.assertIn("OrderResultUnavailable", result.reason)
        self.assertEqual(connector.buy_calls, 1)


if __name__ == "__main__":
    unittest.main()
