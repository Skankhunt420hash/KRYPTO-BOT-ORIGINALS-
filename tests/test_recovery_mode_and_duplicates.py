"""Recovery fail-closed: Live-Opens im Paper-Modus + doppelte Open-DB-Rows."""

import unittest
from unittest.mock import MagicMock, patch

from src.engine.runtime_control import runtime_control
from src.utils.risk_manager import Position


def _reset_runtime_control() -> None:
    runtime_control.resume_entries()
    runtime_control.disable_risk_off()


class RecoveryModeAndDuplicateTests(unittest.TestCase):
    def setUp(self):
        _reset_runtime_control()

    def tearDown(self):
        _reset_runtime_control()

    def _make_bot(self):
        from src.bot import MultiStrategyBot

        with patch.object(MultiStrategyBot, "__init__", lambda self: None):
            bot = MultiStrategyBot()
        bot.exchange = MagicMock()
        bot.exchange.fetch_open_orders.return_value = []
        bot.exchange.fetch_open_positions.return_value = []
        bot.risk = MagicMock()
        bot.risk.open_positions = {}
        bot.risk.balance = 10_000.0
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

    def _open_row(
        self,
        *,
        trade_id: int,
        symbol: str = "BTC/USDT",
        amount: float = 0.01,
        side: str = "long",
    ) -> dict:
        return {
            "id": trade_id,
            "symbol": symbol,
            "entry_price": 50000.0,
            "position_size": amount,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "side": side,
            "strategy_name": "unit",
        }

    def test_paper_start_with_live_open_db_trades_fail_closed(self):
        """
        Trigger: Live-Position in DB (paper_mode=0); .env auf paper umgestellt; Restart.
        Vorher: get_open_trades filtert nur paper → keine Restore, kein Exchange-Fetch
        → Startup OK trotz untracked Live-Exposure.
        """
        bot = self._make_bot()

        def _get_open_trades(limit=200, *, paper_mode=None):
            if paper_mode is False:
                return [self._open_row(trade_id=7, symbol="BTC/USDT")]
            return []

        bot.repo.get_open_trades.side_effect = _get_open_trades

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "paper"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertFalse(bot._startup_checks_ok)
        self.assertIn("live_open_trades_while_paper", bot._startup_block_reason)
        self.assertIn("BTC/USDT", bot._recovery_blocked_symbols)
        snap = runtime_control.get_snapshot()
        self.assertTrue(snap.get("paused"))
        self.assertTrue(snap.get("risk_off"))
        bot.tg.notify_error.assert_called()

    def test_paper_start_without_live_opens_stays_ok(self):
        bot = self._make_bot()

        def _get_open_trades(limit=200, *, paper_mode=None):
            return []

        bot.repo.get_open_trades.side_effect = _get_open_trades

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "paper"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertTrue(bot._startup_checks_ok)
        self.assertEqual(bot._startup_block_reason, "")
        self.assertEqual(bot._recovery_blocked_symbols, set())

    def test_duplicate_open_db_rows_fail_closed_startup(self):
        """
        Trigger: zwei status=open Rows für BTC/USDT (z. B. nach Failed-Exit-Clear + Re-Entry).
        Vorher: neueste Row restauriert, Symbol in _recovery_blocked_symbols →
        _process_pair skipped Exits dauerhaft, Startup blieb OK.
        """
        bot = self._make_bot()
        bot.repo.get_open_trades.return_value = [
            self._open_row(trade_id=2, symbol="BTC/USDT", amount=0.02),
            self._open_row(trade_id=1, symbol="BTC/USDT", amount=0.01),
        ]

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            with patch("src.bot.paper_equity_ledger_enabled", return_value=True):
                bot._recover_after_restart()

        self.assertFalse(bot._startup_checks_ok)
        self.assertIn("duplicate_open_db_trades", bot._startup_block_reason)
        self.assertIn("BTC/USDT", bot._recovery_blocked_symbols)
        self.assertIn("BTC/USDT", bot.risk.open_positions)
        pos = bot.risk.open_positions["BTC/USDT"]
        self.assertIsInstance(pos, Position)
        self.assertEqual(pos.amount, 0.02)
        snap = runtime_control.get_snapshot()
        self.assertTrue(snap.get("paused"))
        self.assertTrue(snap.get("risk_off"))

    def test_single_open_db_row_does_not_duplicate_block(self):
        bot = self._make_bot()
        bot.repo.get_open_trades.return_value = [
            self._open_row(trade_id=3, symbol="ETH/USDT", amount=0.5),
        ]

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            with patch("src.bot.paper_equity_ledger_enabled", return_value=True):
                bot._recover_after_restart()

        self.assertTrue(bot._startup_checks_ok)
        self.assertNotIn("duplicate_open_db_trades", bot._startup_block_reason)
        self.assertIn("ETH/USDT", bot.risk.open_positions)
        self.assertNotIn("ETH/USDT", bot._recovery_blocked_symbols)


if __name__ == "__main__":
    unittest.main()
