import unittest

from config.settings import settings
from src.utils.risk_manager import RiskManager


class RiskManagerSafetyTests(unittest.TestCase):
    def setUp(self):
        self._old_mode = settings.TRADING_MODE
        self._old_equity = settings.PAPER_EQUITY_ACCOUNT
        settings.TRADING_MODE = "paper"
        settings.PAPER_EQUITY_ACCOUNT = True

    def tearDown(self):
        settings.TRADING_MODE = self._old_mode
        settings.PAPER_EQUITY_ACCOUNT = self._old_equity

    def test_paper_equity_open_does_not_remove_notional_twice(self):
        risk = RiskManager(initial_balance=10_000.0)

        pos = risk.open_position("BTC/USDT", price=1000.0, amount=1.0)
        pnl = risk.close_position("BTC/USDT", current_price=1100.0)

        self.assertIsNotNone(pos)
        self.assertEqual(pnl, 100.0)
        self.assertEqual(risk.balance, 10_100.0)


if __name__ == "__main__":
    unittest.main()
