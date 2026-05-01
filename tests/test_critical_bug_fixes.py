import unittest
from unittest.mock import MagicMock

import pandas as pd

from config.settings import settings
from src.bot import MultiStrategyBot, TradingBot
from src.engine.execution_engine import ExecutionResult
from src.strategies.base_strategy import Signal, TradeSignal
from src.telegram.control_panel import TelegramControlPanel
from src.utils.risk_manager import Position, RiskManager


def _single_close_df(price: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [price],
            "high": [price],
            "low": [price],
            "close": [price],
            "volume": [1.0],
        }
    )


class TelegramPanelAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = {
            "TELEGRAM_ENABLED": settings.TELEGRAM_ENABLED,
            "TELEGRAM_PANEL_ENABLED": settings.TELEGRAM_PANEL_ENABLED,
            "TELEGRAM_BOT_TOKEN": settings.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHAT_ID": settings.TELEGRAM_CHAT_ID,
            "TELEGRAM_PANEL_ALLOWED_IDS": settings.TELEGRAM_PANEL_ALLOWED_IDS,
        }

    def tearDown(self) -> None:
        for key, value in self._orig.items():
            setattr(settings, key, value)

    def test_panel_falls_back_to_chat_id_and_rejects_other_chats(self):
        settings.TELEGRAM_ENABLED = True
        settings.TELEGRAM_PANEL_ENABLED = True
        settings.TELEGRAM_BOT_TOKEN = "token"
        settings.TELEGRAM_CHAT_ID = "123"
        settings.TELEGRAM_PANEL_ALLOWED_IDS = ""

        panel = TelegramControlPanel(notifier=MagicMock())
        panel._dispatch_command = MagicMock()

        panel._handle_update(
            {
                "message": {
                    "chat": {"id": "999"},
                    "from": {"id": "999"},
                    "text": "/killswitch",
                }
            }
        )
        panel._dispatch_command.assert_not_called()

        panel._handle_update(
            {
                "message": {
                    "chat": {"id": "123"},
                    "from": {"id": "123"},
                    "text": "/status",
                }
            }
        )
        panel._dispatch_command.assert_called_once_with("123", "/status")

    def test_panel_disabled_without_any_allowed_id(self):
        settings.TELEGRAM_ENABLED = True
        settings.TELEGRAM_PANEL_ENABLED = True
        settings.TELEGRAM_BOT_TOKEN = "token"
        settings.TELEGRAM_CHAT_ID = ""
        settings.TELEGRAM_PANEL_ALLOWED_IDS = ""

        panel = TelegramControlPanel(notifier=MagicMock())

        self.assertFalse(panel.enabled)


class ExitOrderFailureTests(unittest.TestCase):
    def test_legacy_bot_keeps_position_open_when_stop_exit_order_fails(self):
        bot = object.__new__(TradingBot)
        bot.exchange = MagicMock()
        bot.exchange.fetch_ohlcv.return_value = _single_close_df(90.0)
        bot.exchange.create_market_sell_order.return_value = {}
        bot.strategy = MagicMock(
            name="test",
            analyze=MagicMock(
                return_value=TradeSignal(
                    signal=Signal.HOLD,
                    symbol="BTC/USDT",
                    price=90.0,
                    confidence=0.0,
                    reason="hold",
                )
            ),
        )
        bot.risk = RiskManager(initial_balance=1000.0)
        bot.risk.open_positions["BTC/USDT"] = Position(
            symbol="BTC/USDT",
            entry_price=100.0,
            amount=1.0,
            stop_loss=95.0,
            take_profit=120.0,
            highest_price=100.0,
        )
        bot._active_strategy_runtime = "test"
        bot._last_prices = {}
        bot._record_last_signal = MagicMock()
        bot._record_last_decision = MagicMock()
        bot.tg = MagicMock()
        bot.repo = MagicMock()
        bot.perf_tracker = MagicMock()
        bot._open_trade_ids = {"BTC/USDT": 42}

        bot._process_pair("BTC/USDT")

        self.assertIn("BTC/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids["BTC/USDT"], 42)
        bot.repo.close_trade.assert_not_called()
        bot._record_last_decision.assert_called_with(
            symbol="BTC/USDT",
            decision="exit_failed",
            reason="exchange_order_failed",
            strategy="test",
        )

    def test_multi_bot_keeps_position_open_when_exit_engine_fails(self):
        bot = object.__new__(MultiStrategyBot)
        bot._recovery_blocked_symbols = set()
        bot._last_prices = {}
        bot.exchange = MagicMock()
        bot.exchange.fetch_ohlcv.return_value = _single_close_df(90.0)
        bot.health = MagicMock()
        bot.risk = RiskManager(initial_balance=1000.0)
        bot.risk.open_positions["ETH/USDT"] = Position(
            symbol="ETH/USDT",
            entry_price=100.0,
            amount=1.0,
            stop_loss=95.0,
            take_profit=120.0,
            side="long",
            highest_price=100.0,
            strategy_name="test_strategy",
        )
        bot.exec_engine = MagicMock()
        bot.exec_engine.execute_exit.return_value = ExecutionResult.failed(
            "exit", "exchange_order_failed"
        )
        bot._market_context = MagicMock(return_value={})
        bot._record_last_decision = MagicMock()
        bot._log_decision_cycle = MagicMock()
        bot.repo = MagicMock()
        bot._open_trade_ids = {"ETH/USDT": 99}

        bot._process_pair("ETH/USDT")

        self.assertIn("ETH/USDT", bot.risk.open_positions)
        self.assertEqual(bot._open_trade_ids["ETH/USDT"], 99)
        bot.repo.close_trade.assert_not_called()
        bot._record_last_decision.assert_called_with(
            symbol="ETH/USDT",
            decision="exit_failed",
            reason="exchange_order_failed",
            strategy="test_strategy",
        )


if __name__ == "__main__":
    unittest.main()
