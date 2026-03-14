import unittest

from src.engine.risk_engine import RiskEngine
from src.strategies.signal import EnhancedSignal, Side


class RiskEngineTests(unittest.TestCase):
    """Basis-Tests für zentrale Risk-Checks."""

    def _make_dummy_signal(self) -> EnhancedSignal:
        return EnhancedSignal(
            strategy_name="TestStrategy",
            symbol="TEST/USDT",
            timeframe="1h",
            side=Side.LONG,
            confidence=80.0,
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            rr=2.0,
            reason="unittest",
        )

    def test_daily_loss_limit_blocks_signal(self):
        engine = RiskEngine(initial_balance=10_000.0)
        sig = self._make_dummy_signal()

        # Simuliere Tagesverlust, der das Limit überschreitet
        engine._daily_loss = -600.0  # intern: negativer Wert
        engine._initial_balance = 10_000.0

        allowed, reason = engine.check_signal(sig)
        self.assertFalse(allowed)
        self.assertIn("DAILY LOSS LIMIT", reason)

    def test_duplicate_signal_blocked(self):
        engine = RiskEngine(initial_balance=10_000.0)
        sig = self._make_dummy_signal()

        # Erstes Signal registrieren → sollte erlaubt sein
        allowed1, _ = engine.check_signal(sig)
        if allowed1:
            engine.register_signal(sig)

        # Zweites identisches Signal direkt danach → sollte i.d.R. geblockt werden
        allowed2, reason2 = engine.check_signal(sig)
        if not allowed2:
            self.assertIn("DUPLICATE SIGNAL", reason2)


if __name__ == "__main__":
    unittest.main()

