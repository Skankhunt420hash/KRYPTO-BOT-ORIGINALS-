import unittest

from config.settings import settings
from src.engine.execution_engine import ExecutionEngine


class _EmptyOrderConnector:
    def __init__(self):
        self.buy_calls = 0

    def create_market_buy_order(self, symbol, amount):
        self.buy_calls += 1
        return {}

    def create_market_sell_order(self, symbol, amount):
        raise AssertionError("sell should not be called")


class ExecutionEngineTests(unittest.TestCase):
    def test_live_empty_order_result_is_not_retried(self):
        old_mode = settings.TRADING_MODE
        old_retries = settings.EXECUTION_MAX_RETRIES
        try:
            settings.TRADING_MODE = "live"
            settings.EXECUTION_MAX_RETRIES = 3
            connector = _EmptyOrderConnector()
            engine = ExecutionEngine(connector)

            with self.assertRaises(RuntimeError):
                engine._execute_with_retry("TEST/USDT", "buy", 1.0)

            self.assertEqual(connector.buy_calls, 1)
        finally:
            settings.TRADING_MODE = old_mode
            settings.EXECUTION_MAX_RETRIES = old_retries


if __name__ == "__main__":
    unittest.main()
