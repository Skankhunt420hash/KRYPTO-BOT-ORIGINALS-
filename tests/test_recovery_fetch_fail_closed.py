"""
Live-Recovery muss fail-closed sein, wenn private Exchange-Daten nicht geladen
werden können — sonst wirken API-Fehler wie „keine offenen Positionen/Orders“.
"""

import unittest
from unittest.mock import MagicMock, patch

from src.engine.runtime_control import runtime_control
from src.exchange.connector import ExchangeConnector, ExchangePrivateDataError


def _reset_runtime_control() -> None:
    runtime_control.resume_entries()
    runtime_control.disable_risk_off()


class ConnectorPrivateFetchFailClosedTests(unittest.TestCase):
    def test_fetch_open_positions_raises_instead_of_empty_list(self):
        connector = ExchangeConnector.__new__(ExchangeConnector)
        connector.is_paper = False
        connector._exchange = MagicMock()
        connector._exchange.fetch_positions = MagicMock(
            side_effect=RuntimeError("network down")
        )
        connector._read_retry_max = 0
        connector._read_retry_backoff_sec = 0.01
        connector._call_with_retry = ExchangeConnector._call_with_retry.__get__(
            connector, ExchangeConnector
        )

        with self.assertRaises(ExchangePrivateDataError):
            connector.fetch_open_positions()

    def test_fetch_open_orders_raises_instead_of_empty_list(self):
        connector = ExchangeConnector.__new__(ExchangeConnector)
        connector.is_paper = False
        connector._exchange = MagicMock()
        connector._exchange.fetch_open_orders = MagicMock(
            side_effect=RuntimeError("auth failed")
        )
        connector._read_retry_max = 0
        connector._read_retry_backoff_sec = 0.01
        connector._call_with_retry = ExchangeConnector._call_with_retry.__get__(
            connector, ExchangeConnector
        )

        with self.assertRaises(ExchangePrivateDataError):
            connector.fetch_open_orders()

    def test_paper_mode_still_returns_empty_without_api(self):
        connector = ExchangeConnector.__new__(ExchangeConnector)
        connector.is_paper = True
        connector._exchange = MagicMock()
        self.assertEqual(connector.fetch_open_positions(), [])
        self.assertEqual(connector.fetch_open_orders(), [])


class RecoveryFetchFailClosedTests(unittest.TestCase):
    """
    Trigger: TRADING_MODE=live; Neustart; Exchange-Position existiert (z.B. nach
    Failed-Exit-Local-Clear oder Order-OK/DB-OPEN-Fail), aber fetch_positions
    schlägt fehl (Netzwerk/Auth/Ratelimit).

    Vor dem Fix: Connector lieferte [], Recovery sah keine Orphans →
    startup_checks_ok=True → Live-Exposure ohne SL/TP.
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

    def test_positions_fetch_failure_blocks_startup(self):
        bot = self._make_bot()
        bot.exchange.fetch_open_orders.return_value = []
        bot.exchange.fetch_open_positions.side_effect = ExchangePrivateDataError(
            "fetch_open_positions failed: network down"
        )

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertFalse(bot._startup_checks_ok)
        self.assertIn("exchange_open_positions_unavailable", bot._startup_block_reason)
        self.assertTrue(runtime_control.get_snapshot().get("risk_off"))
        self.assertTrue(runtime_control.get_snapshot().get("paused"))
        bot.tg.notify_error.assert_called()

    def test_orders_fetch_failure_blocks_startup(self):
        bot = self._make_bot()
        bot.exchange.fetch_open_orders.side_effect = ExchangePrivateDataError(
            "fetch_open_orders failed: auth"
        )
        bot.exchange.fetch_open_positions.return_value = []

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertFalse(bot._startup_checks_ok)
        self.assertIn("exchange_open_orders_unavailable", bot._startup_block_reason)

    def test_successful_empty_fetch_does_not_block(self):
        bot = self._make_bot()
        bot.exchange.fetch_open_orders.return_value = []
        bot.exchange.fetch_open_positions.return_value = []

        with patch("src.bot.settings") as settings:
            settings.TRADING_MODE = "live"
            settings.STATE_RECOVERY_ENABLED = True
            settings.RECOVERY_MAX_OPEN_TRADES_RESTORE = 100
            bot._recover_after_restart()

        self.assertTrue(bot._startup_checks_ok)
        self.assertEqual(bot._startup_block_reason, "")


if __name__ == "__main__":
    unittest.main()
