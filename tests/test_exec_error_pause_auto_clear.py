"""Regression: Exec-Error-/Rejection-Emergency-Pause muss nach Cooldown verfallen."""

import time
import unittest
from unittest.mock import MagicMock

from config.settings import settings
from src.engine.execution_engine import CircuitState, ExecutionEngine


class ExecErrorPauseAutoClearTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_cooldown = settings.CIRCUIT_BREAKER_COOLDOWN_SEC
        self._prev_exec_errors = settings.MAX_CONSECUTIVE_EXEC_ERRORS
        self._prev_rejections = settings.MAX_CONSECUTIVE_REJECTIONS
        self._prev_em_pause = settings.EMERGENCY_PAUSE_ON_EXEC_ERRORS
        settings.CIRCUIT_BREAKER_COOLDOWN_SEC = 60
        settings.MAX_CONSECUTIVE_EXEC_ERRORS = 3
        settings.MAX_CONSECUTIVE_REJECTIONS = 3
        settings.EMERGENCY_PAUSE_ON_EXEC_ERRORS = True

    def tearDown(self) -> None:
        settings.CIRCUIT_BREAKER_COOLDOWN_SEC = self._prev_cooldown
        settings.MAX_CONSECUTIVE_EXEC_ERRORS = self._prev_exec_errors
        settings.MAX_CONSECUTIVE_REJECTIONS = self._prev_rejections
        settings.EMERGENCY_PAUSE_ON_EXEC_ERRORS = self._prev_em_pause

    def _engine(self) -> ExecutionEngine:
        return ExecutionEngine(MagicMock(), tg=None)

    def test_exec_error_emergency_pause_auto_clears_after_cooldown(self) -> None:
        engine = self._engine()
        for _ in range(3):
            engine._on_failure("NetworkError: timeout")

        self.assertTrue(engine._emergency_paused)
        self.assertTrue(engine._pause_reason.startswith("Emergency Pause:"))
        self.assertEqual(engine._circuit_state, CircuitState.OPEN)
        self.assertFalse(engine.is_healthy)

        # Cooldown abgelaufen → Pause und CB lösen sich (HALF_OPEN erlaubt Health)
        aged = time.monotonic() - 120.0
        engine._pause_started_at = aged
        engine._circuit_opened_at = aged

        self.assertTrue(engine.is_healthy)
        self.assertFalse(engine._emergency_paused)
        self.assertEqual(engine._pause_reason, "")
        self.assertEqual(engine._circuit_state, CircuitState.HALF_OPEN)

    def test_rejection_emergency_pause_auto_clears_after_cooldown(self) -> None:
        engine = self._engine()
        engine._consecutive_rejections = 3
        engine._check_rejection_limit()

        self.assertTrue(engine._emergency_paused)
        self.assertTrue(
            engine._pause_reason.startswith("Zu viele aufeinanderfolgende Rejections")
        )
        self.assertFalse(engine.is_healthy)

        engine._pause_started_at = time.monotonic() - 120.0
        self.assertTrue(engine.is_healthy)
        self.assertFalse(engine._emergency_paused)
        self.assertEqual(engine._pause_reason, "")

    def test_kill_switch_pause_does_not_auto_clear(self) -> None:
        engine = self._engine()
        engine._trigger_pause("KILL SWITCH: Datei './KILL_SWITCH' gefunden")
        engine._pause_started_at = time.monotonic() - 120.0

        # Ohne echte Kill-Switch-Datei bleibt die Pause latched (kein Auto-Clear)
        self.assertFalse(engine._maybe_auto_clear_transient_emergency_pause())
        self.assertTrue(engine._emergency_paused)


if __name__ == "__main__":
    unittest.main()
