"""Regression: Controller /pause /riskoff müssen den Bot-Prozess erreichen."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.settings import settings
from src.engine.control_state_store import (
    apply_recovery_control_to_runtime,
    persist_runtime_control_to_recovery,
)
from src.engine.runtime_control import runtime_control
from src.telegram.control_panel import PanelCallbacks, TelegramControlPanel


class CrossProcessControlStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._recovery = Path(self._tmpdir.name) / "runtime_recovery.json"
        # Isolierte Flags: keine Latenz durch bestehende Datei/State
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        self._patches = [
            patch.object(settings, "STATE_RECOVERY_ENABLED", True),
            patch.object(settings, "STATE_RECOVERY_FILE", str(self._recovery)),
            patch.object(settings, "STATE_RECOVERY_RESTORE_PAUSED", True),
            patch.object(settings, "STATE_RECOVERY_RESTORE_RISK_OFF", True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        self._tmpdir.cleanup()

    def test_controller_pause_persists_without_bot_callback(self) -> None:
        """Controller-Prozess: Panel ohne Bot-Callback schreibt Recovery-Datei."""
        panel = TelegramControlPanel(callbacks=PanelCallbacks())
        runtime_control.pause_entries()
        panel._persist_control_state()

        self.assertTrue(self._recovery.is_file())
        data = json.loads(self._recovery.read_text(encoding="utf-8"))
        self.assertTrue(data.get("paused"))

    def test_bot_applies_controller_pause_from_recovery_file(self) -> None:
        """Bot-Prozess: nach Paper-Clear greift die Datei-Sperre wieder."""
        # Simuliere Controller-Schreiben
        runtime_control.pause_entries()
        runtime_control.enable_risk_off()
        self.assertTrue(persist_runtime_control_to_recovery())

        # Simuliere Bot-Prozess-Memory (frisch / durch Paper-Clear geleert)
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        snap = runtime_control.get_snapshot()
        self.assertFalse(snap["paused"])
        self.assertFalse(snap["risk_off"])

        self.assertTrue(apply_recovery_control_to_runtime())
        snap = runtime_control.get_snapshot()
        self.assertTrue(snap["paused"])
        self.assertTrue(snap["risk_off"])

    def test_persist_merges_without_wiping_bot_fields(self) -> None:
        self._recovery.write_text(
            json.dumps(
                {
                    "last_signal": {"symbol": "BTC/USDT"},
                    "brain": {"ok": True},
                    "paused": False,
                    "risk_off": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        runtime_control.pause_entries()
        self.assertTrue(persist_runtime_control_to_recovery())
        data = json.loads(self._recovery.read_text(encoding="utf-8"))
        self.assertTrue(data["paused"])
        self.assertEqual(data["last_signal"]["symbol"], "BTC/USDT")
        self.assertEqual(data["brain"]["ok"], True)

    def test_resume_clears_persisted_pause(self) -> None:
        runtime_control.pause_entries()
        persist_runtime_control_to_recovery()
        runtime_control.resume_entries()
        persist_runtime_control_to_recovery()
        data = json.loads(self._recovery.read_text(encoding="utf-8"))
        self.assertFalse(data.get("paused"))


if __name__ == "__main__":
    unittest.main()
