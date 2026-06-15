import json
import tempfile
import unittest
from pathlib import Path

from config.settings import settings
from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_returns_last_lines_without_full_file_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "".join(f"line-{i}\n" for i in range(5000)),
                encoding="utf-8",
            )

            lines = watchdog._tail_log(path, 3)

        self.assertEqual(lines, ["line-4997", "line-4998", "line-4999"])

    def test_clear_stuck_recovery_is_opt_in(self):
        old_mode = settings.TRADING_MODE
        old_clear = settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY
        old_recovery_file = settings.STATE_RECOVERY_FILE
        try:
            settings.TRADING_MODE = "paper"
            settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                recovery = root / "runtime_recovery.json"
                recovery.write_text(
                    json.dumps({"paused": True, "risk_off": True}),
                    encoding="utf-8",
                )
                settings.STATE_RECOVERY_FILE = "runtime_recovery.json"

                changed, msg = watchdog._clear_stuck_recovery(root)
                data = json.loads(recovery.read_text(encoding="utf-8"))

            self.assertFalse(changed)
            self.assertEqual(msg, "clear recovery aus")
            self.assertTrue(data["paused"])
            self.assertTrue(data["risk_off"])
        finally:
            settings.TRADING_MODE = old_mode
            settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = old_clear
            settings.STATE_RECOVERY_FILE = old_recovery_file


if __name__ == "__main__":
    unittest.main()
