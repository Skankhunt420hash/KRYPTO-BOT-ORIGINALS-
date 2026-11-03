"""Regression: /killswitchoff darf Emergency-Pause nicht dauerhaft latched lassen."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from config.settings import settings
from src.engine.execution_engine import ExecutionEngine


class KillSwitchEmergencyPauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_ks = settings.KILL_SWITCH_FILE
        self._tmpdir = tempfile.TemporaryDirectory()
        self.ks_path = Path(self._tmpdir.name) / "KILL_SWITCH"
        settings.KILL_SWITCH_FILE = str(self.ks_path)

        connector = MagicMock()
        connector.is_paper = True
        self.engine = ExecutionEngine(connector, tg=None)

    def tearDown(self) -> None:
        settings.KILL_SWITCH_FILE = self._prev_ks
        self._tmpdir.cleanup()

    def test_killswitch_off_clears_emergency_pause_latch(self) -> None:
        self.assertTrue(self.engine.is_healthy)

        self.ks_path.write_text("KILL_SWITCH=1\n", encoding="utf-8")
        self.assertFalse(self.engine.is_healthy)
        self.assertTrue(self.engine._emergency_paused)
        self.assertTrue(self.engine._pause_reason.startswith("KILL SWITCH"))

        # /killswitchoff löscht nur die Datei – ohne Fix bliebe is_healthy False
        self.ks_path.unlink()
        self.assertTrue(self.engine.is_healthy)
        self.assertFalse(self.engine._emergency_paused)
        self.assertEqual(self.engine._pause_reason, "")

    def test_non_killswitch_emergency_pause_stays_latched(self) -> None:
        self.engine._trigger_pause("Emergency Pause: 5 Execution-Fehler")
        self.assertFalse(self.engine.is_healthy)
        self.assertTrue(self.engine._emergency_paused)

        # Kill-Switch-Datei war nie aktiv – Auto-Clear greift nicht
        self.assertFalse(self.ks_path.exists())
        self.assertFalse(self.engine.is_healthy)
        self.assertTrue(self.engine._pause_reason.startswith("Emergency Pause"))


if __name__ == "__main__":
    unittest.main()
