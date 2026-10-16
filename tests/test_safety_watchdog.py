import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def test_explicit_empty_log_file_disables_log_check(self):
        root = Path("/tmp/project")

        with (
            patch.object(watchdog.settings, "SAFETY_WATCHDOG_LOG_FILE_CONFIGURED", True),
            patch.object(watchdog.settings, "SAFETY_WATCHDOG_LOG_FILE", ""),
            patch.object(watchdog.settings, "SUPERVISOR_BOT_LOGFILE", "logs/bot_process.log"),
        ):
            self.assertIsNone(watchdog._resolve_log_path(root))

    def test_unset_log_file_falls_back_to_supervisor_log(self):
        root = Path("/tmp/project")

        with (
            patch.object(watchdog.settings, "SAFETY_WATCHDOG_LOG_FILE_CONFIGURED", False),
            patch.object(watchdog.settings, "SAFETY_WATCHDOG_LOG_FILE", ""),
            patch.object(watchdog.settings, "SUPERVISOR_BOT_LOGFILE", "logs/bot_process.log"),
        ):
            self.assertEqual(
                watchdog._resolve_log_path(root),
                root / "logs/bot_process.log",
            )

    def test_error_counter_ignores_status_fields(self):
        lines = [
            "EXECUTION PAUSIERT | Status: CB=CLOSED Errors=3 KillSwitch=False",
            "decision=REGIME_ERROR_BLOCKED",
            "not an error count field",
            "2026-06-05 00:00:00 ERROR real failure",
            "CRITICAL: exchange unavailable",
        ]

        self.assertEqual(watchdog._count_error_lines(lines), 2)

    def test_recovery_clear_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "runtime_recovery.json"
            path.write_text(
                json.dumps({"paused": True, "risk_off": True}),
                encoding="utf-8",
            )

            with (
                patch.object(watchdog.settings, "TRADING_MODE", "paper"),
                patch.object(watchdog.settings, "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY", False),
                patch.object(watchdog.settings, "STATE_RECOVERY_FILE", "runtime_recovery.json"),
            ):
                changed, msg = watchdog._clear_stuck_recovery(root)

            self.assertFalse(changed)
            self.assertIn("clear recovery aus", msg)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"paused": True, "risk_off": True},
            )

    def test_recovery_clear_still_works_when_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "runtime_recovery.json"
            path.write_text(
                json.dumps({"paused": True, "risk_off": True}),
                encoding="utf-8",
            )

            with (
                patch.object(watchdog.settings, "TRADING_MODE", "paper"),
                patch.object(watchdog.settings, "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY", True),
                patch.object(watchdog.settings, "STATE_RECOVERY_FILE", "runtime_recovery.json"),
            ):
                changed, msg = watchdog._clear_stuck_recovery(root)

            self.assertTrue(changed)
            self.assertIn("zurückgesetzt", msg)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"paused": False, "risk_off": False},
            )


if __name__ == "__main__":
    unittest.main()
