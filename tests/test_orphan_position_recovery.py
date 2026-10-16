"""Recovery darf Null-Futures-Rows und Symbol-Suffixe nicht als Orphans werten."""

import unittest
from unittest.mock import MagicMock, patch

from src.engine.runtime_control import runtime_control
from src.exchange.connector import is_open_exchange_position, normalize_trading_symbol
from src.utils.risk_manager import Position


def _reset_runtime_control() -> None:
    runtime_control.resume_entries()
    runtime_control.disable_risk_off()


class SymbolAndPositionHelpersTests(unittest.TestCase):
    def test_normalize_strips_futures_settle_suffix(self):
        self.assertEqual(normalize_trading_symbol("BTC/USDT:USDT"), "BTC/USDT")
        self.assertEqual(normalize_trading_symbol("ETH/USDT"), "ETH/USDT")
        self.assertEqual(normalize_trading_symbol(""), "")

    def test_zero_contract_rows_are_not_open(self):
        self.assertFalse(
            is_open_exchange_position(
                {"symbol": "BTC/USDT:USDT", "contracts": 0, "notional": 0, "info": {"positionAmt": "0"}}
            )
        )
        self.assertFalse(
            is_open_exchange_position({"symbol": "ETH/USDT:USDT", "contracts": 0.0})
        )

    def test_nonzero_contracts_count_as_open(self):
        self.assertTrue(
            is_open_exchange_position(
                {"symbol": "BTC/USDT:USDT", "contracts": 0.01, "notional": 500}
            )
        )
        self.assertTrue(
            is_open_exchange_position(
                {
                    "symbol": "ETH/USDT:USDT",
                    "contracts": 0,
                    "info": {"positionAmt": "-1.5"},
                }
            )
        )


class OrphanRecoveryGateTests(unittest.TestCase):
    """
    Trigger: LIVE + Futures; ccxt liefert viele positionAmt=0 Rows und/oder
    Symbole als BASE/QUOTE:SETTLE, während die DB BASE/QUOTE speichert.
    Vor dem Fix: orphan_exchange_positions → startup_checks_ok=False →
    run_cycle übersprungen (inkl. Exits).
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

    def test_zero_size_futures_rows_do_not_startup_block(self):
        bot = self._make_bot()
        bot.exchange.fetch_open_orders.return_value = []
        bot.exchange.fetch_open_positions.return_value = [
            {"symbol": "BTC/USDT:USDT", "contracts": 0, "info": {"positionAmt": "0"}},
            {"symbol": "ETH/USDT:USDT", "contracts": 0.0, "notional": 0},
            {"symbol": "SOL/USDT:USDT", "contracts": 0, "amount": 0},
        ]

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertTrue(bot._startup_checks_ok)
        self.assertEqual(bot._startup_block_reason, "")
        self.assertEqual(bot._recovery_blocked_symbols, set())

    def test_matching_futures_symbol_suffix_is_not_orphan(self):
        bot = self._make_bot()
        bot.risk.open_positions = {
            "BTC/USDT": Position(
                symbol="BTC/USDT",
                entry_price=50000.0,
                amount=0.01,
                stop_loss=49000.0,
                take_profit=52000.0,
                side="long",
                highest_price=50000.0,
            )
        }
        bot.exchange.fetch_open_orders.return_value = []
        bot.exchange.fetch_open_positions.return_value = [
            {"symbol": "BTC/USDT:USDT", "contracts": 0.01, "notional": 500.0},
        ]

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertTrue(bot._startup_checks_ok)
        self.assertNotIn("BTC/USDT", bot._recovery_blocked_symbols)
        self.assertNotIn("BTC/USDT:USDT", bot._recovery_blocked_symbols)

    def test_real_orphan_nonzero_position_still_blocks_startup(self):
        bot = self._make_bot()
        bot.exchange.fetch_open_orders.return_value = []
        bot.exchange.fetch_open_positions.return_value = [
            {"symbol": "DOGE/USDT:USDT", "contracts": 100.0, "notional": 20.0},
        ]

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertFalse(bot._startup_checks_ok)
        self.assertIn("orphan_exchange_positions", bot._startup_block_reason)
        self.assertIn("DOGE/USDT", bot._recovery_blocked_symbols)


if __name__ == "__main__":
    unittest.main()
