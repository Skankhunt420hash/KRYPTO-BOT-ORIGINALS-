"""Regression: lokale Entry-Position muss Exchange-Fill (Preis/Menge) nutzen."""

import unittest
from unittest.mock import Mock, patch

from config.settings import settings
from src.engine.execution_engine import (
    ExecutionEngine,
    ExecutionResult,
    _extract_fill_amount,
    apply_fill_to_signal,
    resolve_entry_fill,
)
from src.engine.risk_engine import RiskEngine
from src.strategies.signal import EnhancedSignal, Side


class EntryFillAccountingTests(unittest.TestCase):
    def _long_signal(self) -> EnhancedSignal:
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

    def test_extract_fill_amount_prefers_filled_over_requested(self):
        order = {"filled": 0.00123, "amount": 0.00123456}
        self.assertEqual(_extract_fill_amount(order, 0.00123456), 0.00123)

    def test_extract_fill_amount_falls_back_to_amount_then_request(self):
        self.assertEqual(_extract_fill_amount({"amount": 0.5}, 1.0), 0.5)
        self.assertEqual(_extract_fill_amount({}, 0.42), 0.42)

    def test_apply_fill_to_signal_preserves_long_distances(self):
        sig = self._long_signal()
        filled = apply_fill_to_signal(sig, 102.0)
        self.assertAlmostEqual(filled.entry, 102.0)
        self.assertAlmostEqual(filled.stop_loss, 97.0)  # 102 - 5
        self.assertAlmostEqual(filled.take_profit, 112.0)  # 102 + 10
        self.assertAlmostEqual(filled.rr, 2.0)

    def test_resolve_entry_fill_opens_risk_position_with_exchange_size(self):
        sig = self._long_signal()
        requested = 0.00123456
        result = ExecutionResult(
            success=True,
            order={"id": "x", "filled": 0.00123, "average": 101.5, "status": "closed"},
            fill_price=101.5,
            intended_price=100.0,
            deviation_pct=1.5,
            retries_used=0,
            fingerprint="fp",
            reason="",
            fill_amount=0.00123,
        )
        filled_signal, fill_amount, fill_price = resolve_entry_fill(
            sig, result, requested
        )
        self.assertAlmostEqual(fill_amount, 0.00123)
        self.assertAlmostEqual(fill_price, 101.5)
        self.assertAlmostEqual(filled_signal.entry, 101.5)

        engine = RiskEngine(initial_balance=10_000.0)
        pos = engine.open_with_signal(filled_signal, fill_amount)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.amount, 0.00123)
        self.assertAlmostEqual(pos.entry_price, 101.5)
        # Exit muss dieselbe Exchange-Menge verwenden (kein Oversized-Sell)
        self.assertAlmostEqual(engine.open_positions["BTC/USDT"].amount, 0.00123)

    def test_execute_entry_populates_fill_amount_from_order(self):
        connector = Mock()
        connector.create_market_buy_order.return_value = {
            "id": "live-1",
            "filled": 0.00123,
            "amount": 0.00123,
            "average": 101.25,
            "status": "closed",
        }
        connector.fetch_ticker.return_value = {"last": 100.0}

        with patch.object(settings, "TRADING_MODE", "live"), \
             patch.object(settings, "MAX_ENTRY_DEVIATION_PCT", 5.0), \
             patch.object(settings, "EXECUTION_MAX_RETRIES", 0):
            engine = ExecutionEngine(connector)
            engine.is_paper = False
            engine._fingerprints.clear()
            result = engine.execute_entry(
                "BTC/USDT", "buy", 0.00123456, self._long_signal()
            )

        self.assertTrue(result.success)
        self.assertAlmostEqual(result.fill_amount, 0.00123)
        self.assertAlmostEqual(result.fill_price, 101.25)


if __name__ == "__main__":
    unittest.main()
