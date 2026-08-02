"""Regression: Daily-Loss / Losing-Streak müssen Neustarts überleben."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from config.settings import settings
from src.engine.risk_engine import RiskEngine
from src.strategies.signal import EnhancedSignal, Side
from src.storage import database as database_module
from src.storage import trade_repository as trade_repository_module
from src.storage.trade_repository import TradeRepository


class DailyLossPersistenceTests(unittest.TestCase):
    def _make_signal(self) -> EnhancedSignal:
        return EnhancedSignal(
            strategy_name="TestStrategy",
            symbol="BTC/USDT",
            timeframe="1h",
            side=Side.LONG,
            confidence=80.0,
            entry=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            rr=2.0,
            reason="unittest",
        )

    def _temp_repo(self):
        fd, path = tempfile.mkstemp(prefix="krypto-bot-test-", suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        url = f"sqlite:///{path}"
        return path, url

    def test_db_reconstructs_daily_loss_and_streak(self):
        _path, url = self._temp_repo()
        today = datetime.now(timezone.utc).date()
        day_start = f"{today.isoformat()}T10:00:00"
        earlier = f"{today.isoformat()}T08:00:00"
        yesterday = f"{(today - timedelta(days=1)).isoformat()}T20:00:00"

        with patch.object(settings, "DATABASE_URL", url), patch.object(
            trade_repository_module, "_IS_PAPER", True
        ):
            self.assertTrue(database_module.init_db())
            repo = TradeRepository()
            self.assertTrue(repo.available)
            conn = database_module.get_connection()
            self.assertIsNotNone(conn)
            try:
                rows = [
                    # gestern: Verlust zählt nicht in daily_loss, aber in Streak-Kette
                    (yesterday, -1.0),
                    (earlier, -2.5),
                    (day_start, -1.5),
                ]
                for i, (ts, pnl) in enumerate(rows, start=1):
                    conn.execute(
                        """
                        INSERT INTO trades (
                            timestamp_open, timestamp_close, symbol, timeframe,
                            strategy_name, side, entry_price, stop_loss, take_profit,
                            exit_price, position_size, risk_amount, rr_planned,
                            pnl_abs, pnl_pct, status, paper_mode, created_at, updated_at
                        ) VALUES (
                            ?, ?, 'BTC/USDT', '1h',
                            'TestStrategy', 'long', 100, 95, 110,
                            90, 1.0, 5.0, 2.0,
                            ?, -5.0, 'closed', 1, ?, ?
                        )
                        """,
                        (ts, ts, pnl, ts, ts),
                    )
                # Gewinn unterbricht die Streak nicht rückwirkend für ältere Trades:
                # neuester Close ist Verlust → Streak läuft weiter bis Gewinn.
                conn.commit()
            finally:
                conn.close()

            daily_loss, streak, day_iso = repo.get_session_risk_counters(day=today)
            self.assertEqual(day_iso, today.isoformat())
            self.assertAlmostEqual(daily_loss, -4.0)  # -2.5 + -1.5 (ohne gestern)
            self.assertEqual(streak, 3)  # alle drei letzten Closes sind Verluste

    def test_restore_blocks_entries_after_restart(self):
        engine = RiskEngine(initial_balance=10_000.0)
        sig = self._make_signal()

        with patch.object(settings, "DAILY_LOSS_LIMIT_PCT", 2.0):
            engine.restore_session_risk_counters(
                daily_loss=-250.0,
                losing_streak=3,
                day=engine._utc_today(),
            )

            allowed, reason = engine.check_signal(sig)
            self.assertFalse(allowed)
            self.assertIn("DAILY LOSS LIMIT", reason)
            self.assertEqual(engine._global_losing_streak, 3)

    def test_new_utc_day_resets_restored_daily_loss_only(self):
        engine = RiskEngine(initial_balance=10_000.0)
        yesterday = engine._utc_today() - timedelta(days=1)
        engine.restore_session_risk_counters(
            daily_loss=-500.0,
            losing_streak=2,
            day=yesterday,
        )
        engine._reset_daily_loss_if_new_day()
        self.assertEqual(engine._daily_loss, 0.0)
        self.assertEqual(engine._daily_loss_date, engine._utc_today())
        # Losing-Streak ist bewusst session-übergreifend und bleibt erhalten
        self.assertEqual(engine._global_losing_streak, 2)


if __name__ == "__main__":
    unittest.main()
