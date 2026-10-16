"""Recovery muss Exchange-Side/Size gegen DB abgleichen und bei Drift fail-closed sein."""

import unittest
from unittest.mock import MagicMock, patch

from src.engine.runtime_control import runtime_control
from src.exchange.connector import (
    amounts_compatible,
    exchange_position_exposure,
    is_open_exchange_position,
    normalize_trading_symbol,
)
from src.utils.risk_manager import Position


def _reset_runtime_control() -> None:
    runtime_control.resume_entries()
    runtime_control.disable_risk_off()


class ExposureHelperTests(unittest.TestCase):
    def test_normalize_strips_futures_settle_suffix(self):
        self.assertEqual(normalize_trading_symbol("BTC/USDT:USDT"), "BTC/USDT")
        self.assertEqual(normalize_trading_symbol("ETH/USDT"), "ETH/USDT")

    def test_zero_contract_rows_are_not_open(self):
        self.assertFalse(
            is_open_exchange_position(
                {
                    "symbol": "BTC/USDT:USDT",
                    "contracts": 0,
                    "notional": 0,
                    "info": {"positionAmt": "0"},
                }
            )
        )

    def test_exposure_reads_signed_position_amt(self):
        symbol, side, amount = exchange_position_exposure(
            {
                "symbol": "ETH/USDT:USDT",
                "contracts": 0,
                "info": {"positionAmt": "-1.5"},
            }
        )
        self.assertEqual(symbol, "ETH/USDT")
        self.assertEqual(side, "short")
        self.assertEqual(amount, 1.5)

    def test_amounts_compatible_allows_tiny_precision_drift(self):
        self.assertTrue(amounts_compatible(0.01, 0.01))
        self.assertTrue(amounts_compatible(1.0, 1.004))
        self.assertFalse(amounts_compatible(0.01, 0.03))


class RecoveryPositionReconcileTests(unittest.TestCase):
    """
    Trigger: LIVE-Restart; DB hat BTC long 0.01, Exchange hat BTC long 0.03
    (Manual Top-up / Partial-Fill-Drift / vorherige Doppel-Order).
    Vor dem Fix: nur Symbol-Set-Vergleich → kein Orphan → Exit schließt 0.01 und
    markiert DB closed, Rest-Exposure bleibt untracked.
    """

    def setUp(self):
        _reset_runtime_control()

    def tearDown(self):
        _reset_runtime_control()

    def _make_bot(self):
        from src.bot import MultiStrategyBot

        with patch.object(MultiStrategyBot, "__init__", lambda self: None):
            bot = MultiStrategyBot()
        bot.exchange = MagicMock()
        bot.risk = MagicMock()
        bot.risk.open_positions = {}
        bot.repo = MagicMock()
        bot.repo.get_open_trades.return_value = []
        bot.tg = MagicMock()
        bot._recovery_blocked_symbols = set()
        bot._open_trade_ids = {}
        bot._last_prices = {}
        bot._startup_checks_ok = True
        bot._startup_block_reason = ""
        bot._restore_control_state_from_file = MagicMock()
        bot._startup_sanity_checks = MagicMock(return_value=[])
        return bot

    def _btc_local(self, amount: float = 0.01, side: str = "long") -> Position:
        return Position(
            symbol="BTC/USDT",
            entry_price=50000.0,
            amount=amount,
            stop_loss=49000.0,
            take_profit=52000.0,
            side=side,
            highest_price=50000.0,
        )

    def test_matching_size_and_side_does_not_block(self):
        bot = self._make_bot()
        bot.risk.open_positions = {"BTC/USDT": self._btc_local(0.01, "long")}
        bot.exchange.fetch_open_orders.return_value = []
        bot.exchange.fetch_open_positions.return_value = [
            {"symbol": "BTC/USDT:USDT", "contracts": 0.01, "side": "long", "notional": 500.0},
        ]

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertTrue(bot._startup_checks_ok)
        self.assertNotIn("BTC/USDT", bot._recovery_blocked_symbols)

    def test_size_mismatch_blocks_startup_and_symbol(self):
        bot = self._make_bot()
        bot.risk.open_positions = {"BTC/USDT": self._btc_local(0.01, "long")}
        bot.exchange.fetch_open_orders.return_value = []
        bot.exchange.fetch_open_positions.return_value = [
            {"symbol": "BTC/USDT:USDT", "contracts": 0.03, "side": "long", "notional": 1500.0},
        ]

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertFalse(bot._startup_checks_ok)
        self.assertIn("position_size_or_side_mismatch", bot._startup_block_reason)
        self.assertIn("BTC/USDT", bot._recovery_blocked_symbols)
        snap = runtime_control.get_snapshot()
        self.assertTrue(snap.get("paused"))
        self.assertTrue(snap.get("risk_off"))

    def test_side_mismatch_blocks_startup_and_symbol(self):
        bot = self._make_bot()
        bot.risk.open_positions = {"BTC/USDT": self._btc_local(0.01, "long")}
        bot.exchange.fetch_open_orders.return_value = []
        bot.exchange.fetch_open_positions.return_value = [
            {
                "symbol": "BTC/USDT:USDT",
                "contracts": 0.01,
                "side": "short",
                "info": {"positionAmt": "-0.01"},
            },
        ]

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertFalse(bot._startup_checks_ok)
        self.assertIn("position_size_or_side_mismatch", bot._startup_block_reason)
        self.assertIn("BTC/USDT", bot._recovery_blocked_symbols)

    def test_zero_size_futures_rows_do_not_startup_block(self):
        bot = self._make_bot()
        bot.exchange.fetch_open_orders.return_value = []
        bot.exchange.fetch_open_positions.return_value = [
            {"symbol": "BTC/USDT:USDT", "contracts": 0, "info": {"positionAmt": "0"}},
            {"symbol": "ETH/USDT:USDT", "contracts": 0.0, "notional": 0},
        ]

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertTrue(bot._startup_checks_ok)
        self.assertEqual(bot._recovery_blocked_symbols, set())


if __name__ == "__main__":
    unittest.main()
