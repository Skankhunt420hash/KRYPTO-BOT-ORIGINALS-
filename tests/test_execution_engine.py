import unittest

from config.settings import settings
from src.engine.execution_engine import ExecutionEngine
from src.engine.runtime_control import runtime_control
from src.strategies.signal import EnhancedSignal, Side


class _FakeConnector:
    def __init__(self) -> None:
        self.order_attempts = 0

    def fetch_ticker(self, symbol):
        return {"last": 100.0}

    def create_market_buy_order(self, symbol, amount):
        self.order_attempts += 1
        return {
            "id": "fake-buy",
            "symbol": symbol,
            "side": "buy",
            "amount": amount,
            "price": 100.0,
            "status": "closed",
        }

    def create_market_sell_order(self, symbol, amount):
        self.order_attempts += 1
        return {
            "id": "fake-sell",
            "symbol": symbol,
            "side": "sell",
            "amount": amount,
            "price": 100.0,
            "status": "closed",
        }


class _PausingRetryConnector(_FakeConnector):
    def create_market_buy_order(self, symbol, amount):
        self.order_attempts += 1
        runtime_control.pause_entries()
        raise TimeoutError("temporary network failure")


class ExecutionEngineRuntimeControlTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        self._old_retries = settings.EXECUTION_MAX_RETRIES
        self._old_backoff = settings.EXECUTION_RETRY_BACKOFF_SEC

    def tearDown(self) -> None:
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        settings.EXECUTION_MAX_RETRIES = self._old_retries
        settings.EXECUTION_RETRY_BACKOFF_SEC = self._old_backoff

    def _signal(self) -> EnhancedSignal:
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
            reason="unittest",
        )

    def test_pause_blocks_entry_before_connector_order(self):
        connector = _FakeConnector()
        engine = ExecutionEngine(connector)
        runtime_control.pause_entries()

        result = engine.execute_entry("BTC/USDT", "buy", 0.1, self._signal())

        self.assertFalse(result.success)
        self.assertIn("CONTROL PAUSE", result.reason)
        self.assertEqual(connector.order_attempts, 0)

    def test_risk_off_blocks_entry_before_connector_order(self):
        connector = _FakeConnector()
        engine = ExecutionEngine(connector)
        runtime_control.enable_risk_off()

        result = engine.execute_entry("BTC/USDT", "buy", 0.1, self._signal())

        self.assertFalse(result.success)
        self.assertIn("RISK OFF", result.reason)
        self.assertEqual(connector.order_attempts, 0)

    def test_pause_stops_entry_retries(self):
        settings.EXECUTION_MAX_RETRIES = 1
        settings.EXECUTION_RETRY_BACKOFF_SEC = 0
        connector = _PausingRetryConnector()
        engine = ExecutionEngine(connector)

        result = engine.execute_entry("BTC/USDT", "buy", 0.1, self._signal())

        self.assertFalse(result.success)
        self.assertIn("CONTROL PAUSE", result.reason)
        self.assertEqual(connector.order_attempts, 1)


if __name__ == "__main__":
    unittest.main()
