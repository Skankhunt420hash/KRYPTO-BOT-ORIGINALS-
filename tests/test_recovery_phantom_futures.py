"""
Regression: Live-Futures-Recovery darf DB-open ohne Exchange-Exposure
nicht als gemanagte Position weiterlaufen lassen.

Ohne Fail-Closed feuert SL/TP auf Phantom-Rows und kann ohne reduceOnly
eine neue Gegenposition eröffnen.
"""

import unittest
from unittest.mock import MagicMock

from config.settings import settings
from src.bot import (
    MultiStrategyBot,
    _exchange_position_abs_size,
    _normalize_ccxt_symbol,
    _real_futures_position_symbols,
)
from src.engine.risk_engine import RiskEngine
from src.engine.runtime_control import runtime_control


class _FakeExchange:
    def __init__(self, positions=None, positions_error=None):
        self._positions = list(positions or [])
        self._positions_error = positions_error

    def fetch_open_orders(self):
        return []

    def fetch_open_positions(self):
        if self._positions_error is not None:
            raise self._positions_error
        return list(self._positions)

    def get_markets(self):
        return ["BTC/USDT", "ETH/USDT"]

    def fetch_market_price(self, _symbol):
        return 50000.0


class _FakeRepo:
    def __init__(self, rows):
        self._rows = rows
        self.available = True

    def get_open_trades(self, limit=200):
        return list(self._rows)[:limit]


class _FakeTg:
    def __init__(self):
        self.errors = []
        self.recovery = []

    def notify_error(self, context, message):
        self.errors.append((context, message))

    def notify_recovery_status(self, **kwargs):
        self.recovery.append(kwargs)


def _open_row(symbol="BTC/USDT", amount=0.01):
    return {
        "id": 1,
        "symbol": symbol,
        "entry_price": 50000.0,
        "position_size": amount,
        "stop_loss": 49000.0,
        "take_profit": 52000.0,
        "side": "long",
        "strategy_name": "Test",
    }


class SymbolNormalizeTests(unittest.TestCase):
    def test_strips_settle_suffix(self):
        self.assertEqual(_normalize_ccxt_symbol("BTC/USDT:USDT"), "BTC/USDT")
        self.assertEqual(_normalize_ccxt_symbol("BTC/USDT"), "BTC/USDT")
        self.assertEqual(_normalize_ccxt_symbol(""), "")

    def test_zero_size_rows_are_flat(self):
        self.assertEqual(
            _exchange_position_abs_size(
                {"symbol": "BTC/USDT:USDT", "contracts": 0, "notional": 0}
            ),
            0.0,
        )
        self.assertGreater(
            _exchange_position_abs_size(
                {"symbol": "BTC/USDT:USDT", "contracts": 0.01, "notional": 500}
            ),
            0.0,
        )
        self.assertGreater(
            _exchange_position_abs_size(
                {"symbol": "BTC/USDT", "info": {"positionAmt": "-0.02"}}
            ),
            0.0,
        )

    def test_real_symbols_ignore_zero_rows(self):
        rows = [
            {"symbol": "BTC/USDT:USDT", "contracts": 0, "notional": 0},
            {"symbol": "ETH/USDT:USDT", "contracts": 0.5, "notional": 1000},
        ]
        self.assertEqual(_real_futures_position_symbols(rows), {"ETH/USDT"})


class PhantomFuturesRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._mode = settings.TRADING_MODE
        self._futures = settings.FUTURES_MODE
        self._recovery = settings.STATE_RECOVERY_ENABLED
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def tearDown(self):
        settings.TRADING_MODE = self._mode
        settings.FUTURES_MODE = self._futures
        settings.STATE_RECOVERY_ENABLED = self._recovery
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def _harness(self, *, mode, futures, rows, positions, positions_error=None):
        settings.TRADING_MODE = mode
        settings.FUTURES_MODE = futures
        settings.STATE_RECOVERY_ENABLED = False
        bot = MultiStrategyBot.__new__(MultiStrategyBot)
        bot.exchange = _FakeExchange(positions=positions, positions_error=positions_error)
        bot.repo = _FakeRepo(rows)
        bot.risk = RiskEngine(initial_balance=10_000.0)
        bot.tg = _FakeTg()
        bot.pairs = ["BTC/USDT"]
        bot._open_trade_ids = {}
        bot._last_prices = {}
        bot._recovery_blocked_symbols = set()
        bot._startup_checks_ok = True
        bot._startup_block_reason = ""
        bot._restore_control_state_from_file = MagicMock()
        MultiStrategyBot._recover_after_restart(bot)
        return bot

    def test_futures_empty_exchange_blocks_phantom_db_row(self):
        bot = self._harness(
            mode="live",
            futures=True,
            rows=[_open_row()],
            positions=[],
        )
        self.assertFalse(bot._startup_checks_ok)
        self.assertIn("phantom_db_positions:BTC/USDT", bot._startup_block_reason)
        self.assertIn("BTC/USDT", bot._recovery_blocked_symbols)
        self.assertTrue(runtime_control.get_snapshot()["risk_off"])
        self.assertTrue(runtime_control.get_snapshot()["paused"])
        self.assertEqual(bot.tg.errors[0][0], "RECOVERY_STARTUP_BLOCK")

    def test_futures_zero_size_row_is_treated_as_flat(self):
        bot = self._harness(
            mode="live",
            futures=True,
            rows=[_open_row()],
            positions=[
                {
                    "symbol": "BTC/USDT",
                    "contracts": 0,
                    "notional": 0,
                    "info": {"positionAmt": "0"},
                }
            ],
        )
        self.assertFalse(bot._startup_checks_ok)
        self.assertIn("phantom_db_positions:BTC/USDT", bot._startup_block_reason)

    def test_matching_futures_position_is_not_phantom(self):
        bot = self._harness(
            mode="live",
            futures=True,
            rows=[_open_row()],
            positions=[{"symbol": "BTC/USDT", "contracts": 0.01, "notional": 500}],
        )
        self.assertTrue(bot._startup_checks_ok)
        self.assertEqual(bot._startup_block_reason, "")
        self.assertNotIn("BTC/USDT", bot._recovery_blocked_symbols)

    def test_settle_suffix_with_size_is_not_phantom(self):
        bot = self._harness(
            mode="live",
            futures=True,
            rows=[_open_row()],
            positions=[{"symbol": "BTC/USDT:USDT", "contracts": 0.01, "notional": 500}],
        )
        self.assertNotIn("phantom_db_positions", bot._startup_block_reason)

    def test_spot_empty_positions_are_not_phantom(self):
        bot = self._harness(
            mode="live",
            futures=False,
            rows=[_open_row()],
            positions=[],
        )
        self.assertTrue(bot._startup_checks_ok)
        self.assertNotIn("phantom_db_positions", bot._startup_block_reason)

    def test_positions_fetch_failure_is_not_treated_as_phantom(self):
        bot = self._harness(
            mode="live",
            futures=True,
            rows=[_open_row()],
            positions=[],
            positions_error=RuntimeError("network down"),
        )
        self.assertTrue(bot._startup_checks_ok)
        self.assertNotIn("phantom_db_positions", bot._startup_block_reason)


if __name__ == "__main__":
    unittest.main()
