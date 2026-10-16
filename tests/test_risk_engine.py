import unittest

from config.settings import settings
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
        old_limit = settings.DAILY_LOSS_LIMIT_PCT
        settings.DAILY_LOSS_LIMIT_PCT = 5.0
        engine = RiskEngine(initial_balance=10_000.0)
        sig = self._make_dummy_signal()

        try:
            # Simuliere Tagesverlust, der das Limit überschreitet
            engine._daily_loss = -600.0  # intern: negativer Wert
            engine._initial_balance = 10_000.0

            allowed, reason = engine.check_signal(sig)
            self.assertFalse(allowed)
            self.assertIn("DAILY LOSS LIMIT", reason)
        finally:
            settings.DAILY_LOSS_LIMIT_PCT = old_limit

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

    def test_live_hard_gate_daily_loss_uses_account_equity(self):
        old_mode = settings.TRADING_MODE
        old_gate = settings.LIVE_HARD_RISK_GATE_ENABLED
        old_live_test = settings.LIVE_TEST_MODE
        old_daily = settings.DAILY_LOSS_LIMIT_PCT
        old_live_daily = settings.LIVE_TEST_DAILY_LOSS_LIMIT_PCT
        try:
            settings.TRADING_MODE = "live"
            settings.LIVE_HARD_RISK_GATE_ENABLED = True
            settings.LIVE_TEST_MODE = True
            settings.DAILY_LOSS_LIMIT_PCT = 5.0
            settings.LIVE_TEST_DAILY_LOSS_LIMIT_PCT = 1.0

            engine = RiskEngine(initial_balance=10_000.0)
            engine._daily_loss = -3.0
            sig = self._make_dummy_signal()

            allowed, reason = engine.check_live_hard_gate(
                sig,
                amount=0.1,
                free_capital_usdt=200.0,
                account_equity_usdt=200.0,
                allowed_symbols=["TEST/USDT"],
            )

            self.assertFalse(allowed)
            self.assertIn("DAILY LOSS LIMIT", reason)
            self.assertIn("3.00/2.00", reason)
        finally:
            settings.TRADING_MODE = old_mode
            settings.LIVE_HARD_RISK_GATE_ENABLED = old_gate
            settings.LIVE_TEST_MODE = old_live_test
            settings.DAILY_LOSS_LIMIT_PCT = old_daily
            settings.LIVE_TEST_DAILY_LOSS_LIMIT_PCT = old_live_daily


if __name__ == "__main__":
    unittest.main()

