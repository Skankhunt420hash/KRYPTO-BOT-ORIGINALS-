"""Regression: open positions outside TRADING_PAIRS/universe must still get exits."""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from src.utils.risk_manager import Position


class OpenPositionOutsidePairsTests(unittest.TestCase):
    """
    Trigger: Live bot restores an open DB position for SOL/USDT, but current
    TRADING_PAIRS / TRADING_UNIVERSE_MAX_SYMBOLS no longer includes SOL.
    Before fix: run_cycle only iterated self.pairs → SL/TP never evaluated.
    """

    def _make_bot(self, pairs, open_symbol: str):
        from src.bot import MultiStrategyBot

        with patch.object(MultiStrategyBot, "__init__", lambda self: None):
            bot = MultiStrategyBot()
        bot.pairs = list(pairs)
        bot.risk = MagicMock()
        bot.risk.open_positions = {
            open_symbol: Position(
                symbol=open_symbol,
                entry_price=100.0,
                amount=1.0,
                stop_loss=90.0,
                take_profit=120.0,
                side="long",
                highest_price=100.0,
                strategy_name="TestStrategy",
            )
        }
        bot.risk.check_exit_conditions = MagicMock(return_value="stop_loss")
        bot.risk.close_position = MagicMock(return_value=-10.0)
        bot.risk.get_stats = MagicMock(
            return_value={
                "balance": 1000.0,
                "total_pnl": -10.0,
                "total_trades": 1,
                "winrate_pct": 0.0,
                "open_positions": 1,
                "daily_loss": 10.0,
                "portfolio_risk_pct": 0.0,
            }
        )
        bot.exchange = MagicMock()
        bot.exchange.fetch_ohlcv.return_value = pd.DataFrame(
            {"close": [85.0]},
            index=pd.to_datetime(["2026-01-01"]),
        )
        bot.exec_engine = MagicMock()
        bot.exec_engine.is_healthy = True
        bot.exec_engine.execute_exit.return_value = MagicMock(
            success=True,
            fill_price=85.0,
            reason="",
            order={"id": "exit-1"},
        )
        bot.repo = MagicMock()
        bot.tg = MagicMock()
        bot.health = MagicMock()
        bot.scorer = MagicMock()
        bot.perf_tracker = MagicMock()
        bot._open_trade_ids = {open_symbol: 42}
        bot._last_prices = {}
        bot._startup_checks_ok = True
        bot._startup_block_reason = ""
        bot._recovery_blocked_symbols = set()
        bot._active_strategy_runtime = "auto"
        bot._last_brain_snapshot = {}
        bot._paper_undo_unwanted_control_locks = MagicMock()
        bot._update_performance_tracking = MagicMock()
        bot._sync_runtime_state = MagicMock()
        bot._persist_recovery_state = MagicMock()
        bot._record_trade_event = MagicMock()
        bot._record_last_decision = MagicMock()
        bot._log_decision_cycle = MagicMock()
        return bot

    def test_symbols_for_cycle_includes_open_positions_outside_pairs(self):
        bot = self._make_bot(["BTC/USDT", "ETH/USDT"], "SOL/USDT")
        symbols = bot._symbols_for_cycle()
        self.assertEqual(symbols, ["BTC/USDT", "ETH/USDT", "SOL/USDT"])

    def test_run_cycle_evaluates_exit_for_outside_pairs_position(self):
        bot = self._make_bot(["BTC/USDT"], "SOL/USDT")
        # In-universe pair has no open position / empty data → skipped early
        bot.exchange.fetch_ohlcv.side_effect = lambda symbol, *a, **k: (
            pd.DataFrame({"close": [85.0]}, index=pd.to_datetime(["2026-01-01"]))
            if symbol == "SOL/USDT"
            else pd.DataFrame()
        )

        bot.run_cycle()

        bot.exec_engine.execute_exit.assert_called_once_with("SOL/USDT", "sell", 1.0)
        bot.risk.close_position.assert_called_once_with("SOL/USDT", 85.0)

    def test_outside_pairs_without_open_position_does_not_enter(self):
        """Entry path must stay gated to configured pairs."""
        from src.bot import MultiStrategyBot

        with patch.object(MultiStrategyBot, "__init__", lambda self: None):
            bot = MultiStrategyBot()
        bot.pairs = ["BTC/USDT"]
        bot.risk = MagicMock()
        bot.risk.open_positions = {}
        bot.risk.check_exit_conditions = MagicMock(return_value=None)
        bot.exchange = MagicMock()
        bot.exchange.fetch_ohlcv.return_value = pd.DataFrame(
            {"close": [100.0]},
            index=pd.to_datetime(["2026-01-01"]),
        )
        bot.health = MagicMock()
        bot._recovery_blocked_symbols = set()
        bot._active_strategy_runtime = "auto"
        bot._last_prices = {}
        bot._last_brain_snapshot = {}
        bot._record_last_decision = MagicMock()
        bot._log_decision_cycle = MagicMock()
        bot.regime_engine = MagicMock()

        bot._process_pair("SOL/USDT")

        bot.regime_engine.detect.assert_not_called()
        bot._record_last_decision.assert_called()
        self.assertEqual(
            bot._record_last_decision.call_args.kwargs.get("reason")
            or bot._record_last_decision.call_args[1].get("reason"),
            "outside_configured_pairs",
        )


if __name__ == "__main__":
    unittest.main()
