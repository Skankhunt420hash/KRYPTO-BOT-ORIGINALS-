import json
import os
import tempfile
import unittest
from pathlib import Path

from config.settings import settings
from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_reads_only_requested_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "bot.log"
            lines = [f"INFO line {i}\n" for i in range(1000)]
            lines.extend(["ERROR last one\n", "INFO done\n"])
            log_path.write_text("".join(lines), encoding="utf-8")

            tail = watchdog._tail_log(log_path, 3)

            self.assertEqual(tail, ["INFO line 999", "ERROR last one", "INFO done"])

    def test_error_counter_ignores_status_words(self):
        lines = [
            "Status: CB=open Errors=3 KillSwitch=False",
            "risk_decision=REGIME_ERROR",
            "ERROR real failure",
            "Traceback (most recent call last):",
        ]

        self.assertEqual(watchdog._count_error_lines(lines), 2)

    def test_clear_stuck_recovery_is_opt_in(self):
        old_mode = settings.TRADING_MODE
        old_clear = settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY
        old_file = settings.STATE_RECOVERY_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                recovery = root / "data" / "runtime_recovery.json"
                recovery.parent.mkdir()
                recovery.write_text(
                    json.dumps({"paused": True, "risk_off": True}),
                    encoding="utf-8",
                )
                settings.TRADING_MODE = "paper"
                settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False
                settings.STATE_RECOVERY_FILE = "data/runtime_recovery.json"

                changed, msg = watchdog._clear_stuck_recovery(root)
                data = json.loads(recovery.read_text(encoding="utf-8"))

                self.assertFalse(changed)
                self.assertIn("aus", msg)
                self.assertTrue(data["paused"])
                self.assertTrue(data["risk_off"])
        finally:
            settings.TRADING_MODE = old_mode
            settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = old_clear
            settings.STATE_RECOVERY_FILE = old_file

    def test_explicit_empty_log_file_disables_log_path(self):
        old_env = os.environ.get("SAFETY_WATCHDOG_LOG_FILE")
        old_setting = settings.SAFETY_WATCHDOG_LOG_FILE
        try:
            os.environ["SAFETY_WATCHDOG_LOG_FILE"] = ""
            settings.SAFETY_WATCHDOG_LOG_FILE = ""
            self.assertIsNone(watchdog._resolve_log_path(Path("/tmp/project")))
        finally:
            settings.SAFETY_WATCHDOG_LOG_FILE = old_setting
            if old_env is None:
                os.environ.pop("SAFETY_WATCHDOG_LOG_FILE", None)
            else:
                os.environ["SAFETY_WATCHDOG_LOG_FILE"] = old_env


if __name__ == "__main__":
    unittest.main()
