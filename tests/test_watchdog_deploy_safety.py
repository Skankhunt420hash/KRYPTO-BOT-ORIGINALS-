import json
import tempfile
import unittest
from pathlib import Path

from config.settings import settings
from src.safety import watchdog


class WatchdogDeploySafetyTests(unittest.TestCase):
    def setUp(self):
        self._old_mode = settings.TRADING_MODE
        self._old_clear = settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY
        self._old_recovery = settings.STATE_RECOVERY_FILE
        settings.TRADING_MODE = "paper"

    def tearDown(self):
        settings.TRADING_MODE = self._old_mode
        settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = self._old_clear
        settings.STATE_RECOVERY_FILE = self._old_recovery

    def test_watchdog_does_not_clear_recovery_state_by_default(self):
        settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "data" / "runtime_recovery.json"
            recovery.parent.mkdir()
            recovery.write_text(
                json.dumps({"paused": True, "risk_off": True}),
                encoding="utf-8",
            )
            settings.STATE_RECOVERY_FILE = "data/runtime_recovery.json"

            changed, msg = watchdog._clear_stuck_recovery(root)

            self.assertFalse(changed)
            self.assertIn("clear recovery aus", msg)
            self.assertEqual(
                json.loads(recovery.read_text(encoding="utf-8")),
                {"paused": True, "risk_off": True},
            )

    def test_tail_log_returns_only_requested_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "".join(f"line-{idx}\n" for idx in range(1000)),
                encoding="utf-8",
            )

            lines = watchdog._tail_log(path, 5)

            self.assertEqual(lines, [f"line-{idx}" for idx in range(995, 1000)])

    def test_deploy_sync_preserves_runtime_files_and_stops_watchdog(self):
        script = Path("deploy/sync-from-github.sh").read_text(encoding="utf-8")

        self.assertIn("stop safety-watchdog", script)
        self.assertIn("backup_runtime_files", script)
        self.assertIn("restore_runtime_files", script)
        self.assertNotIn("git restore data/daily_summary.json data/runtime_recovery.json", script)


if __name__ == "__main__":
    unittest.main()
