import unittest

from config.settings import settings
from src.engine.execution_engine import ExecutionEngine
from src.engine.runtime_control import runtime_control
from src.strategies.signal import EnhancedSignal, Side


class DummyConnector:
    def __init__(self, order_result):
        self.order_result = order_result
        self.buy_calls = 0
        self.sell_calls = 0

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def create_market_buy_order(self, symbol, amount):
        self.buy_calls += 1
        return self.order_result

    def create_market_sell_order(self, symbol, amount):
        self.sell_calls += 1
        return self.order_result


def _signal():
    return EnhancedSignal(
        strategy_name="UnitStrategy",
        symbol="BTC/USDT",
        timeframe="1m",
        side=Side.LONG,
        confidence=80.0,
        entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        rr=2.0,
        reason="unit",
    )


class ExecutionEngineSafetyTests(unittest.TestCase):
    def setUp(self):
        self._old = {
            "EXECUTION_MAX_RETRIES": settings.EXECUTION_MAX_RETRIES,
            "EXECUTION_RETRY_BACKOFF_SEC": settings.EXECUTION_RETRY_BACKOFF_SEC,
            "MAX_ENTRY_DEVIATION_PCT": settings.MAX_ENTRY_DEVIATION_PCT,
        }
        settings.EXECUTION_MAX_RETRIES = 3
        settings.EXECUTION_RETRY_BACKOFF_SEC = 0
        settings.MAX_ENTRY_DEVIATION_PCT = 0
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def tearDown(self):
        for key, value in self._old.items():
            setattr(settings, key, value)
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def test_empty_connector_order_result_is_not_retried(self):
        connector = DummyConnector(order_result={})
        engine = ExecutionEngine(connector)

        result = engine.execute_entry("BTC/USDT", "buy", 1.0, _signal())

        self.assertFalse(result.success)
        self.assertIn("OrderResultUnavailable", result.reason)
        self.assertEqual(connector.buy_calls, 1)

    def test_runtime_pause_blocks_entry_at_execution_sink(self):
        connector = DummyConnector(order_result={"id": "ok", "status": "closed", "price": 100})
        engine = ExecutionEngine(connector)
        runtime_control.pause_entries()

        result = engine.execute_entry("BTC/USDT", "buy", 1.0, _signal())

        self.assertFalse(result.success)
        self.assertIn("CONTROL PAUSE", result.reason)
        self.assertEqual(connector.buy_calls, 0)


if __name__ == "__main__":
    unittest.main()
