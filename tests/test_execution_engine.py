import unittest

from src.engine.execution_engine import ExecutionEngine
from src.engine.runtime_control import runtime_control
from src.strategies.signal import EnhancedSignal, Side


class FakeConnector:
    def __init__(self):
        self.buy_orders = 0

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def create_market_buy_order(self, symbol, amount):
        self.buy_orders += 1
        return {"id": "order-1", "status": "closed", "average": 100.0}


def _signal():
    return EnhancedSignal(
        strategy_name="UnitStrategy",
        symbol="BTC/USDT",
        timeframe="1h",
        side=Side.LONG,
        confidence=90.0,
        entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        rr=2.0,
        reason="unit",
    )


class ExecutionEngineRuntimeControlTests(unittest.TestCase):
    def tearDown(self):
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def test_execute_entry_blocks_paused_runtime_control_before_order(self):
        connector = FakeConnector()
        engine = ExecutionEngine(connector)

        runtime_control.pause_entries()
        result = engine.execute_entry("BTC/USDT", "buy", 1.0, _signal())

        self.assertFalse(result.success)
        self.assertIn("CONTROL PAUSE", result.reason)
        self.assertEqual(connector.buy_orders, 0)

    def test_execute_entry_blocks_risk_off_runtime_control_before_order(self):
        connector = FakeConnector()
        engine = ExecutionEngine(connector)

        runtime_control.enable_risk_off()
        result = engine.execute_entry("BTC/USDT", "buy", 1.0, _signal())

        self.assertFalse(result.success)
        self.assertIn("RISK OFF", result.reason)
        self.assertEqual(connector.buy_orders, 0)


if __name__ == "__main__":
    unittest.main()
