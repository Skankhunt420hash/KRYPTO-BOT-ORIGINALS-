"""Mini-Live Daily-Loss muss gegen Live-Equity rechnen, nicht gegen Paper-Balance."""

import unittest
from unittest.mock import patch

from src.engine.risk_engine import RiskEngine
from src.strategies.signal import EnhancedSignal, Side


def _signal() -> EnhancedSignal:
    return EnhancedSignal(
        strategy_name="MomentumPullback",
        symbol="BTC/USDT",
        timeframe="1h",
        side=Side.LONG,
        confidence=80.0,
        entry=100.0,
        stop_loss=98.0,
        take_profit=104.0,
        rr=2.0,
        reason="unittest",
    )


class LiveDailyLossBaseTests(unittest.TestCase):
    def test_mini_live_daily_loss_uses_account_equity_not_paper_balance(self):
        """
        Trigger: LIVE_TEST_MODE, PAPER_TRADING_BALANCE=10000, LIVE_TEST_DAILY_LOSS=1%,
        Live-Equity=100 USDT, Tagesverlust=1.5 USDT.
        Erwartung: Limit ~1 USDT (1% von 100), nicht 100 USDT (1% von 10000) → blockiert.
        """
        engine = RiskEngine(initial_balance=10_000.0)
        engine._daily_loss = -1.5
        amount = 0.1  # notional 10 USDT unter typischem Mini-Live-Cap

        with patch.multiple(
            "src.engine.risk_engine.settings",
            TRADING_MODE="live",
            LIVE_HARD_RISK_GATE_ENABLED=True,
            LIVE_TEST_MODE=True,
            LIVE_TEST_DAILY_LOSS_LIMIT_PCT=1.0,
            DAILY_LOSS_LIMIT_PCT=0.0,
            LIVE_MIN_ACCOUNT_EQUITY_USDT=50.0,
            LIVE_MIN_FREE_CAPITAL_USDT=10.0,
            LIVE_MAX_LOSING_STREAK=10,
            LIVE_ALLOWED_STRATEGIES="",
            LIVE_MAX_POSITION_SIZE=25.0,
            RISK_PER_TRADE_PCT=5.0,
            MAX_POSITION_NOTIONAL=10_000.0,
            MIN_POSITION_NOTIONAL=1.0,
        ):
            allowed, reason = engine.check_live_hard_gate(
                _signal(),
                amount,
                free_capital_usdt=80.0,
                # current equity 98.5 + abs(daily_loss 1.5) => base 100 → limit 1.0
                account_equity_usdt=98.5,
                allowed_symbols=["BTC/USDT"],
            )

        self.assertFalse(allowed)
        self.assertIn("DAILY LOSS LIMIT", reason)
        # Limit muss ~1 USDT sein, nicht ~100 USDT vom Paper-Kapital
        self.assertIn("/1.00 USDT", reason)

    def test_mini_live_daily_loss_allows_when_under_equity_based_limit(self):
        engine = RiskEngine(initial_balance=10_000.0)
        engine._daily_loss = -0.4
        amount = 0.1

        with patch.multiple(
            "src.engine.risk_engine.settings",
            TRADING_MODE="live",
            LIVE_HARD_RISK_GATE_ENABLED=True,
            LIVE_TEST_MODE=True,
            LIVE_TEST_DAILY_LOSS_LIMIT_PCT=1.0,
            DAILY_LOSS_LIMIT_PCT=0.0,
            LIVE_MIN_ACCOUNT_EQUITY_USDT=50.0,
            LIVE_MIN_FREE_CAPITAL_USDT=10.0,
            LIVE_MAX_LOSING_STREAK=10,
            LIVE_ALLOWED_STRATEGIES="",
            LIVE_MAX_POSITION_SIZE=25.0,
            RISK_PER_TRADE_PCT=5.0,
            MAX_POSITION_NOTIONAL=10_000.0,
            MIN_POSITION_NOTIONAL=1.0,
        ):
            allowed, reason = engine.check_live_hard_gate(
                _signal(),
                amount,
                free_capital_usdt=80.0,
                account_equity_usdt=99.6,  # base ≈ 100, limit 1.0, loss 0.4 → OK
                allowed_symbols=["BTC/USDT"],
            )

        self.assertTrue(allowed)
        self.assertEqual(reason, "LIVE_HARD_GATE_OK")


if __name__ == "__main__":
    unittest.main()
