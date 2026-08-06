"""Regression: futures exits must send reduceOnly to avoid reverse exposure."""

import unittest
from unittest.mock import MagicMock, patch

from config.settings import settings


class FuturesExitReduceOnlyTests(unittest.TestCase):
    """
    Trigger: FUTURES_MODE live exit where local size exceeds exchange position
    (amount_to_precision drift, partial/manual close, liquidation, phantom DB restore).

    Without reduceOnly, a closing sell/buy can open reverse exposure in one-way mode.
    """

    def setUp(self):
        self._prev_futures = getattr(settings, "FUTURES_MODE", False)
        self._prev_mode = settings.TRADING_MODE
        self._prev_live = getattr(settings, "LIVE_TRADING_ENABLED", False)
        self._prev_test = getattr(settings, "LIVE_TEST_MODE", False)
        self._prev_key = getattr(settings, "API_KEY", "")
        self._prev_secret = getattr(settings, "API_SECRET", "")
        settings.FUTURES_MODE = True
        settings.TRADING_MODE = "live"
        settings.LIVE_TRADING_ENABLED = True
        settings.LIVE_TEST_MODE = False
        settings.API_KEY = "test-key"
        settings.API_SECRET = "test-secret"

    def tearDown(self):
        settings.FUTURES_MODE = self._prev_futures
        settings.TRADING_MODE = self._prev_mode
        settings.LIVE_TRADING_ENABLED = self._prev_live
        settings.LIVE_TEST_MODE = self._prev_test
        settings.API_KEY = self._prev_key
        settings.API_SECRET = self._prev_secret

    def _live_connector(self):
        from src.exchange.connector import ExchangeConnector

        with patch.object(ExchangeConnector, "_connect", lambda self: None):
            conn = ExchangeConnector()
        conn.exchange_id = "binance"
        conn.is_paper = False
        conn._exchange = MagicMock()
        conn._exchange.amount_to_precision.side_effect = lambda _s, a: str(a)
        conn._exchange.create_market_order.return_value = {
            "id": "ord-1",
            "status": "closed",
            "average": 100.0,
            "filled": 1.0,
        }
        conn._markets_cache = {
            "BTC/USDT": {
                "symbol": "BTC/USDT",
                "base": "BTC",
                "quote": "USDT",
                "active": True,
                "limits": {"amount": {"min": 0.0}, "cost": {"min": 0.0}},
                "precision": {"amount": 6},
                "info": {},
            }
        }
        conn.fetch_market_price = MagicMock(return_value=100.0)
        conn._has_sufficient_balance = MagicMock(return_value=True)
        conn._is_duplicate_order = MagicMock(return_value=False)
        conn._register_order_fingerprint = MagicMock()
        return conn

    def test_futures_exit_sell_sets_reduce_only(self):
        conn = self._live_connector()
        order = conn.create_market_sell_order("BTC/USDT", 1.0, is_exit=True)
        self.assertEqual(order.get("id"), "ord-1")
        kwargs = conn._exchange.create_market_order.call_args.kwargs
        self.assertTrue(kwargs["params"].get("reduceOnly"))

    def test_futures_exit_buy_sets_reduce_only(self):
        conn = self._live_connector()
        order = conn.create_market_buy_order("BTC/USDT", 1.0, is_exit=True)
        self.assertEqual(order.get("id"), "ord-1")
        kwargs = conn._exchange.create_market_order.call_args.kwargs
        self.assertTrue(kwargs["params"].get("reduceOnly"))

    def test_futures_entry_does_not_set_reduce_only(self):
        conn = self._live_connector()
        order = conn.create_market_sell_order("BTC/USDT", 1.0, is_exit=False)
        self.assertEqual(order.get("id"), "ord-1")
        kwargs = conn._exchange.create_market_order.call_args.kwargs
        self.assertFalse(kwargs["params"].get("reduceOnly", False))

    def test_spot_exit_does_not_set_reduce_only(self):
        settings.FUTURES_MODE = False
        conn = self._live_connector()
        order = conn.create_market_sell_order("BTC/USDT", 1.0, is_exit=True)
        self.assertEqual(order.get("id"), "ord-1")
        kwargs = conn._exchange.create_market_order.call_args.kwargs
        self.assertFalse(kwargs["params"].get("reduceOnly", False))

    def test_execute_exit_passes_is_exit_to_connector(self):
        from src.engine.execution_engine import ExecutionEngine

        connector = MagicMock()
        connector.create_market_sell_order.return_value = {
            "id": "exit-1",
            "status": "closed",
            "average": 99.0,
            "filled": 0.5,
        }
        engine = ExecutionEngine(connector, tg=None)
        engine.is_paper = False
        result = engine.execute_exit("BTC/USDT", "sell", 0.5)
        self.assertTrue(result.success)
        connector.create_market_sell_order.assert_called_once_with(
            "BTC/USDT", 0.5, is_exit=True
        )
        connector.create_market_buy_order.assert_not_called()

    def test_execute_entry_passes_is_exit_false(self):
        from src.engine.execution_engine import ExecutionEngine
        from src.strategies.signal import EnhancedSignal, Side

        connector = MagicMock()
        connector.create_market_buy_order.return_value = {
            "id": "entry-1",
            "status": "closed",
            "average": 100.0,
            "filled": 0.5,
        }
        connector.fetch_ticker.return_value = {"last": 100.0}
        engine = ExecutionEngine(connector, tg=None)
        engine.is_paper = False
        signal = EnhancedSignal(
            strategy_name="Test",
            symbol="BTC/USDT",
            timeframe="1h",
            side=Side.LONG,
            confidence=70.0,
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            rr=2.0,
            reason="test",
        )
        result = engine.execute_entry(
            "BTC/USDT", "buy", 0.5, signal=signal
        )
        self.assertTrue(result.success)
        connector.create_market_buy_order.assert_called_once_with(
            "BTC/USDT", 0.5, is_exit=False
        )


if __name__ == "__main__":
    unittest.main()
