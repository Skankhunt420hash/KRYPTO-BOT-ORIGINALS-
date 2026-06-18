import json
import tempfile
import unittest
from pathlib import Path

from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_is_bounded_for_huge_single_line_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_bytes(b"A" * (3 * 1024 * 1024))

            lines = watchdog._tail_log(path, 1)

        self.assertEqual(1, len(lines))
        self.assertLess(len(lines[0]), 1024 * 1024)

    def test_empty_configured_log_file_disables_log_check(self):
        root = Path("/tmp/project")
        old_log_file = getattr(watchdog.settings, "SAFETY_WATCHDOG_LOG_FILE", None)
        old_supervisor = getattr(watchdog.settings, "SUPERVISOR_BOT_LOGFILE", None)
        try:
            watchdog.settings.SAFETY_WATCHDOG_LOG_FILE = ""
            watchdog.settings.SUPERVISOR_BOT_LOGFILE = "logs/bot_process.log"

            self.assertIsNone(watchdog._resolve_watchdog_log_path(root))
        finally:
            watchdog.settings.SAFETY_WATCHDOG_LOG_FILE = old_log_file
            watchdog.settings.SUPERVISOR_BOT_LOGFILE = old_supervisor

    def test_unset_log_file_falls_back_to_supervisor_log(self):
        root = Path("/tmp/project")
        old_log_file = getattr(watchdog.settings, "SAFETY_WATCHDOG_LOG_FILE", None)
        old_supervisor = getattr(watchdog.settings, "SUPERVISOR_BOT_LOGFILE", None)
        try:
            watchdog.settings.SAFETY_WATCHDOG_LOG_FILE = None
            watchdog.settings.SUPERVISOR_BOT_LOGFILE = "logs/bot_process.log"

            self.assertEqual(
                root / "logs/bot_process.log",
                watchdog._resolve_watchdog_log_path(root),
            )
        finally:
            watchdog.settings.SAFETY_WATCHDOG_LOG_FILE = old_log_file
            watchdog.settings.SUPERVISOR_BOT_LOGFILE = old_supervisor

    def test_clear_stuck_recovery_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "data" / "runtime_recovery.json"
            recovery.parent.mkdir()
            recovery.write_text(
                json.dumps({"paused": True, "risk_off": True}),
                encoding="utf-8",
            )

            old_mode = getattr(watchdog.settings, "TRADING_MODE", None)
            old_clear = getattr(
                watchdog.settings, "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY", None
            )
            old_file = getattr(watchdog.settings, "STATE_RECOVERY_FILE", None)
            try:
                watchdog.settings.TRADING_MODE = "paper"
                watchdog.settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False
                watchdog.settings.STATE_RECOVERY_FILE = "data/runtime_recovery.json"

                cleared, msg = watchdog._clear_stuck_recovery(root)

                self.assertFalse(cleared)
                self.assertIn("clear recovery aus", msg)
                self.assertEqual(
                    {"paused": True, "risk_off": True},
                    json.loads(recovery.read_text(encoding="utf-8")),
                )
            finally:
                watchdog.settings.TRADING_MODE = old_mode
                watchdog.settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = old_clear
                watchdog.settings.STATE_RECOVERY_FILE = old_file


if __name__ == "__main__":
    unittest.main()
