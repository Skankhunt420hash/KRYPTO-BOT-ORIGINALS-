"""Regression: --once / run_once muss den Single-Instance-Lock halten."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from config.settings import settings
from src.app import TradingApplication
from src.engine.runtime_control import runtime_control


class RunOnceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_enforce = settings.ENFORCE_SINGLE_INSTANCE
        self._prev_lock = settings.APP_INSTANCE_LOCKFILE
        self._tmpdir = tempfile.TemporaryDirectory()
        self._lock_path = str(Path(self._tmpdir.name) / "app.lock")
        settings.ENFORCE_SINGLE_INSTANCE = True
        settings.APP_INSTANCE_LOCKFILE = self._lock_path
        # Isoliere Runtime-Control von Side-Effects anderer Tests / Bot-Init
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def tearDown(self) -> None:
        settings.ENFORCE_SINGLE_INSTANCE = self._prev_enforce
        settings.APP_INSTANCE_LOCKFILE = self._prev_lock
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        self._tmpdir.cleanup()

    def test_run_once_fails_when_lock_held(self) -> None:
        # Fremde, noch laufende Instanz simulieren (andere PID + main.py cmdline)
        Path(self._lock_path).write_text(
            "pid=1\n"
            f"created_at=1\n"
            f"cwd={os.getcwd()}\n",
            encoding="utf-8",
        )
        other = TradingApplication(use_multi=True, interval_seconds=None)
        with patch("src.app.psutil.Process") as proc_cls:
            proc = proc_cls.return_value
            proc.is_running.return_value = True
            proc.status.return_value = "running"
            proc.cmdline.return_value = ["python", "main.py", "--multi"]
            proc.cwd.return_value = os.getcwd()
            proc.name.return_value = "python3"
            with self.assertRaises(RuntimeError) as ctx:
                other.run_once(autostart_services=False)
            self.assertEqual(str(ctx.exception), "single_instance_lock_failed")

    def test_run_once_acquires_and_releases_lock(self) -> None:
        app = TradingApplication(use_multi=True, interval_seconds=None)
        bot = MagicMock()
        with patch.object(app, "create_bot", return_value=bot) as create_bot:
            app.run_once(autostart_services=False)
            create_bot.assert_called_once_with(autostart_services=False)
            bot.run_cycle.assert_called_once()
        self.assertFalse(app._lock_acquired)
        self.assertFalse(Path(self._lock_path).exists())


if __name__ == "__main__":
    unittest.main()
