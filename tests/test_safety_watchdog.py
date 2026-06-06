import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.settings import settings
from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_values = {
            "TRADING_MODE": getattr(settings, "TRADING_MODE", None),
            "STATE_RECOVERY_FILE": getattr(settings, "STATE_RECOVERY_FILE", None),
            "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY": getattr(
                settings, "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY", None
            ),
            "SAFETY_WATCHDOG_LOG_FILE": getattr(settings, "SAFETY_WATCHDOG_LOG_FILE", None),
            "SUPERVISOR_BOT_LOGFILE": getattr(settings, "SUPERVISOR_BOT_LOGFILE", None),
        }

    def tearDown(self) -> None:
        for key, value in self._old_values.items():
            if value is not None:
                setattr(settings, key, value)

    def test_clear_stuck_recovery_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "data" / "runtime_recovery.json"
            recovery.parent.mkdir(parents=True)
            recovery.write_text('{"paused": true, "risk_off": true}', encoding="utf-8")

            settings.TRADING_MODE = "paper"
            settings.STATE_RECOVERY_FILE = "data/runtime_recovery.json"
            settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False

            changed, msg = watchdog._clear_stuck_recovery(root)

            self.assertFalse(changed)
            self.assertIn("aus", msg)
            self.assertEqual(
                recovery.read_text(encoding="utf-8"),
                '{"paused": true, "risk_off": true}',
            )

    def test_clear_stuck_recovery_can_be_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "data" / "runtime_recovery.json"
            recovery.parent.mkdir(parents=True)
            recovery.write_text('{"paused": true, "risk_off": true}', encoding="utf-8")

            settings.TRADING_MODE = "paper"
            settings.STATE_RECOVERY_FILE = "data/runtime_recovery.json"
            settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = True

            changed, msg = watchdog._clear_stuck_recovery(root)

            self.assertTrue(changed)
            self.assertIn("zur", msg)
            data = recovery.read_text(encoding="utf-8")
            self.assertIn('"paused": false', data)
            self.assertIn('"risk_off": false', data)

    def test_explicit_empty_log_file_disables_log_burst_check(self):
        settings.SAFETY_WATCHDOG_LOG_FILE = ""
        settings.SUPERVISOR_BOT_LOGFILE = "logs/bot_process.log"

        with patch.dict(os.environ, {"SAFETY_WATCHDOG_LOG_FILE": ""}, clear=False):
            self.assertIsNone(watchdog._resolve_log_path(Path("/tmp/project")))

    def test_unset_log_file_falls_back_to_supervisor_log(self):
        settings.SAFETY_WATCHDOG_LOG_FILE = ""
        settings.SUPERVISOR_BOT_LOGFILE = "logs/bot_process.log"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAFETY_WATCHDOG_LOG_FILE", None)
            self.assertEqual(
                watchdog._resolve_log_path(Path("/tmp/project")),
                Path("/tmp/project/logs/bot_process.log"),
            )

    def test_tail_log_returns_last_lines_from_large_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "".join(f"line {i}\n" for i in range(1000)) + "final error\n",
                encoding="utf-8",
            )

            lines = watchdog._tail_log(path, 3, chunk_size=64, max_bytes=512)

            self.assertEqual(lines, ["line 998", "line 999", "final error"])

    def test_error_counter_ignores_status_words(self):
        lines = [
            "Health snapshot: Errors=3 status=DEGRADED",
            "REGIME_ERROR seen as strategy label",
            "2026-01-01 ERROR real failure",
            "Traceback (most recent call last):",
        ]

        self.assertEqual(watchdog._count_error_lines(lines), 2)


if __name__ == "__main__":
    unittest.main()
