"""Regression: Mini-Live Notional-Cap darf Exits nicht blockieren."""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

from config.settings import settings
from src.engine.execution_engine import ExecutionEngine
from src.exchange.connector import ExchangeConnector


class _RecordingConnector:
    """Minimaler Connector-Stub für ExecutionEngine-Pfade."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, float, bool]] = []

    def create_market_buy_order(
        self, symbol: str, amount: float, *, is_exit: bool = False
    ) -> Dict[str, Any]:
        self.calls.append(("buy", amount, is_exit))
        return {
            "id": "stub-buy",
            "symbol": symbol,
            "side": "buy",
            "amount": amount,
            "average": 1.0,
            "status": "closed",
        }

    def create_market_sell_order(
        self, symbol: str, amount: float, *, is_exit: bool = False
    ) -> Dict[str, Any]:
        self.calls.append(("sell", amount, is_exit))
        return {
            "id": "stub-sell",
            "symbol": symbol,
            "side": "sell",
            "amount": amount,
            "average": 1.0,
            "status": "closed",
        }

    def fetch_market_price(self, symbol: str) -> float:
        return 1.0


class MiniLiveExitCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "TRADING_MODE": settings.TRADING_MODE,
            "LIVE_TRADING_ENABLED": settings.LIVE_TRADING_ENABLED,
            "LIVE_TEST_MODE": settings.LIVE_TEST_MODE,
            "LIVE_MAX_POSITION_SIZE": settings.LIVE_MAX_POSITION_SIZE,
            "API_KEY": settings.API_KEY,
            "API_SECRET": settings.API_SECRET,
            "EXCHANGE_DUPLICATE_WINDOW_SEC": settings.EXCHANGE_DUPLICATE_WINDOW_SEC,
        }
        settings.TRADING_MODE = "live"
        settings.LIVE_TRADING_ENABLED = True
        settings.LIVE_TEST_MODE = True
        settings.LIVE_MAX_POSITION_SIZE = 25.0
        settings.API_KEY = "test-key"
        settings.API_SECRET = "test-secret"
        settings.EXCHANGE_DUPLICATE_WINDOW_SEC = 0

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            setattr(settings, key, value)

    def _make_live_connector(self, market_price: float) -> ExchangeConnector:
        with patch.object(ExchangeConnector, "_connect", lambda self: None):
            connector = ExchangeConnector()
        connector.is_paper = False
        connector.exchange_id = "binance"
        connector._exchange = MagicMock()
        connector._exchange.create_market_order = MagicMock(
            return_value={
                "id": "live-order-1",
                "symbol": "BTC/USDT",
                "side": "sell",
                "amount": 1.0,
                "average": market_price,
                "status": "closed",
            }
        )
        connector._normalize_and_validate_order = (  # type: ignore[method-assign]
            lambda symbol, side, amount: (amount, "ok")
        )
        connector.fetch_market_price = lambda symbol: market_price  # type: ignore[method-assign]
        connector._is_duplicate_order = lambda fp: False  # type: ignore[method-assign]
        connector._register_order_fingerprint = lambda fp: None  # type: ignore[method-assign]
        connector._order_fingerprint = lambda *args, **kwargs: "fp"  # type: ignore[method-assign]
        return connector

    def test_mini_live_cap_blocks_entry_above_limit(self):
        connector = self._make_live_connector(market_price=26.0)
        order = connector.create_market_buy_order("BTC/USDT", 1.0)
        self.assertEqual(order, {})
        connector._exchange.create_market_order.assert_not_called()

    def test_mini_live_cap_allows_exit_above_limit(self):
        connector = self._make_live_connector(market_price=26.0)
        order = connector.create_market_sell_order("BTC/USDT", 1.0, is_exit=True)
        self.assertTrue(order)
        self.assertEqual(order.get("id"), "live-order-1")
        connector._exchange.create_market_order.assert_called_once()

    def test_execution_engine_exit_marks_is_exit(self):
        stub = _RecordingConnector()
        engine = ExecutionEngine(connector=stub)
        result = engine.execute_exit("BTC/USDT", "sell", 1.0)
        self.assertTrue(result.success)
        self.assertEqual(stub.calls, [("sell", 1.0, True)])

    def test_execution_engine_entry_keeps_is_exit_false(self):
        stub = _RecordingConnector()
        engine = ExecutionEngine(connector=stub)
        # Direkt den Retry-Pfad testen (ohne Slippage-/Duplicate-Gates)
        order, retries = engine._execute_with_retry("BTC/USDT", "buy", 1.0)
        self.assertEqual(order.get("id"), "stub-buy")
        self.assertEqual(retries, 0)
        self.assertEqual(stub.calls, [("buy", 1.0, False)])


if __name__ == "__main__":
    unittest.main()
