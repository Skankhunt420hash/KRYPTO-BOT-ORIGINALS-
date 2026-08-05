"""Regression: Slippage-Emergency-Pause muss zeitlich verfallen."""

import time
import unittest
from collections import deque
from unittest.mock import MagicMock

from config.settings import settings
from src.engine.execution_engine import ExecutionEngine
from src.strategies.signal import EnhancedSignal, Side


def _signal(entry: float = 100.0) -> EnhancedSignal:
    return EnhancedSignal(
        strategy_name="TestStrategy",
        symbol="BTC/USDT",
        timeframe="1h",
        side=Side.LONG,
        confidence=80.0,
        entry=entry,
        stop_loss=95.0,
        take_profit=110.0,
        rr=2.0,
        reason="unittest",
    )


class SlippagePauseWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_max = settings.MAX_SLIPPAGE_EVENTS_WINDOW
        self._prev_win = getattr(settings, "SLIPPAGE_EVENTS_WINDOW_SEC", 300.0)
        self._prev_dev = settings.MAX_ENTRY_DEVIATION_PCT
        settings.MAX_SLIPPAGE_EVENTS_WINDOW = 3
        settings.SLIPPAGE_EVENTS_WINDOW_SEC = 60.0
        settings.MAX_ENTRY_DEVIATION_PCT = 0.5

    def tearDown(self) -> None:
        settings.MAX_SLIPPAGE_EVENTS_WINDOW = self._prev_max
        settings.SLIPPAGE_EVENTS_WINDOW_SEC = self._prev_win
        settings.MAX_ENTRY_DEVIATION_PCT = self._prev_dev

    def _engine_with_ticker(self, last: float) -> ExecutionEngine:
        connector = MagicMock()
        connector.fetch_ticker.return_value = {"last": last, "close": last}
        return ExecutionEngine(connector, tg=None)

    def test_slippage_events_expire_and_do_not_latch_forever(self):
        engine = self._engine_with_ticker(last=102.0)  # 2% > 0.5%
        sig = _signal(entry=100.0)

        for _ in range(3):
            result = engine.execute_entry("BTC/USDT", "buy", 0.01, sig)
            self.assertFalse(result.success)
            self.assertIn("SLIPPAGE", result.reason)

        self.assertTrue(engine._emergency_paused)
        self.assertTrue(engine._pause_reason.startswith("Zu viele Slippage-Events"))
        self.assertFalse(engine.is_healthy)

        # Events in die Vergangenheit schieben → Fenster leer → Pause löst sich
        aged = time.monotonic() - 120.0
        engine._slippage_events = deque([aged, aged, aged])
        self.assertTrue(engine.is_healthy)
        self.assertFalse(engine._emergency_paused)
        self.assertEqual(engine._pause_reason, "")

    def test_old_slippage_events_do_not_count_toward_pause(self):
        engine = self._engine_with_ticker(last=102.0)
        sig = _signal(entry=100.0)

        # Zwei veraltete Events + ein frisches → unter Schwelle 3
        aged = time.monotonic() - 120.0
        engine._slippage_events.append(aged)
        engine._slippage_events.append(aged)

        result = engine.execute_entry("BTC/USDT", "buy", 0.01, sig)
        self.assertFalse(result.success)
        self.assertFalse(engine._emergency_paused)
        self.assertEqual(len(engine._slippage_events), 1)


if __name__ == "__main__":
    unittest.main()
