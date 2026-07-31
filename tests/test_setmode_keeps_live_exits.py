"""
Regression: /setmode paper darf Live-Exit-Routing nicht zerstören.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from config.settings import settings
from src.engine.runtime_control import runtime_control
from src.exchange.connector import ExchangeConnector
from src.telegram.control_panel import TelegramControlPanel


class SetmodeKeepsLiveExitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_mode = settings.TRADING_MODE
        self._prev_live_enabled = settings.LIVE_TRADING_ENABLED
        self._prev_key = settings.API_KEY
        self._prev_secret = settings.API_SECRET
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        runtime_control.request_mode("")

    def tearDown(self) -> None:
        settings.TRADING_MODE = self._prev_mode
        settings.LIVE_TRADING_ENABLED = self._prev_live_enabled
        settings.API_KEY = self._prev_key
        settings.API_SECRET = self._prev_secret
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        runtime_control.request_mode("")

    def test_setmode_paper_does_not_flip_trading_mode(self) -> None:
        settings.TRADING_MODE = "live"
        settings.LIVE_TRADING_ENABLED = True
        settings.API_KEY = "test-key"
        settings.API_SECRET = "test-secret"

        panel = TelegramControlPanel(notifier=MagicMock())
        panel._send_text = MagicMock()
        panel._notifier = MagicMock()

        panel._handle_setmode("1", "/setmode paper")

        self.assertEqual(settings.TRADING_MODE, "live")
        snap = runtime_control.get_snapshot()
        self.assertTrue(snap.get("risk_off"))
        self.assertEqual(snap.get("mode_request"), "paper")
        panel._send_text.assert_called()
        sent = panel._send_text.call_args[0][1]
        self.assertIn("Order-Routing bleibt vorerst", sent)
        self.assertIn("live", sent)

    def test_setmode_paper_keeps_live_orders_enabled(self) -> None:
        """Concrete trigger: after /setmode paper, connector can still exit live."""
        settings.TRADING_MODE = "live"
        settings.LIVE_TRADING_ENABLED = True
        settings.API_KEY = "test-key"
        settings.API_SECRET = "test-secret"

        panel = TelegramControlPanel(notifier=MagicMock())
        panel._send_text = MagicMock()
        panel._notifier = MagicMock()

        with patch.object(ExchangeConnector, "_connect", lambda self: None):
            connector = ExchangeConnector.__new__(ExchangeConnector)
            connector.is_paper = False  # init-time value while live
            connector.exchange_id = "binance"
            connector._exchange = None

            self.assertTrue(connector._live_orders_enabled)

            panel._handle_setmode("1", "/setmode paper")

            # Bug previously: TRADING_MODE=paper → _live_orders_enabled False,
            # while is_paper stayed False → exits returned {}.
            self.assertEqual(settings.TRADING_MODE, "live")
            self.assertFalse(connector.is_paper)
            self.assertTrue(connector._live_orders_enabled)


if __name__ == "__main__":
    unittest.main()
