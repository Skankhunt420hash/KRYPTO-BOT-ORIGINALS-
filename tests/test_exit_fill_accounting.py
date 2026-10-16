import unittest

from src.engine.execution_engine import ExecutionResult, accounting_exit_price
from src.engine.risk_engine import RiskEngine
from src.strategies.signal import EnhancedSignal, Side


class ExitFillAccountingTests(unittest.TestCase):
    """Exit-PnL / Daily-Loss müssen den Exchange-Fill nutzen, nicht nur Candle-Close."""

    def test_accounting_exit_price_prefers_successful_fill(self):
        result = ExecutionResult(
            success=True,
            order={"id": "x", "average": 96.5},
            fill_price=96.5,
            intended_price=98.0,
            deviation_pct=1.53,
            retries_used=0,
            fingerprint="fp",
            reason="",
        )
        self.assertEqual(accounting_exit_price(result, 98.0), 96.5)

    def test_accounting_exit_price_falls_back_on_failure(self):
        result = ExecutionResult.failed("fp", "Leeres Order-Ergebnis vom Connector")
        self.assertEqual(accounting_exit_price(result, 98.0), 98.0)

    def test_accounting_exit_price_falls_back_on_zero_fill(self):
        result = ExecutionResult(
            success=True,
            order={"id": "x"},
            fill_price=0.0,
            intended_price=98.0,
            deviation_pct=0.0,
            retries_used=0,
            fingerprint="fp",
            reason="",
        )
        self.assertEqual(accounting_exit_price(result, 98.0), 98.0)

    def test_daily_loss_uses_fill_not_candle(self):
        """
        Trigger: Candle-Close triggert SL bei 98, Exchange füllt bei 96.5.
        Ohne Fix würde Daily-Loss nur -2 statt -3.5 zählen.
        """
        engine = RiskEngine(initial_balance=10_000.0)
        signal = EnhancedSignal(
            strategy_name="TestStrategy",
            symbol="BTC/USDT",
            timeframe="1h",
            side=Side.LONG,
            confidence=80.0,
            entry=100.0,
            stop_loss=98.0,
            take_profit=110.0,
            rr=2.0,
            reason="unittest",
        )
        engine.open_with_signal(signal, amount=1.0)

        candle_price = 98.0
        fill_price = 96.5
        exit_price = accounting_exit_price(
            ExecutionResult(
                success=True,
                order={"id": "exit-1", "average": fill_price},
                fill_price=fill_price,
                intended_price=candle_price,
                deviation_pct=abs(fill_price - candle_price) / candle_price * 100,
                retries_used=0,
                fingerprint="exit-fp",
                reason="",
            ),
            candle_price,
        )

        pnl = engine.close_position("BTC/USDT", exit_price)
        self.assertAlmostEqual(pnl, -3.5)
        self.assertAlmostEqual(engine._daily_loss, -3.5)

        # Candle-Preis hätte Daily-Loss unterschätzt (−2.0 statt −3.5)
        candle_only_pnl = (candle_price - 100.0) * 1.0
        self.assertAlmostEqual(candle_only_pnl, -2.0)
        self.assertGreater(abs(engine._daily_loss), abs(candle_only_pnl))


if __name__ == "__main__":
    unittest.main()
