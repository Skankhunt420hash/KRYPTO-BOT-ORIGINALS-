import unittest
from pathlib import Path


class DeploySyncScriptTests(unittest.TestCase):
    def test_sync_preserves_runtime_recovery_and_stops_watchdog_first(self):
        script = Path("deploy/sync-from-github.sh").read_text(encoding="utf-8")

        self.assertIn("cp -p data/runtime_recovery.json", script)
        self.assertIn("restore_runtime_state", script)
        self.assertIn("systemctl stop safety-watchdog", script)
        self.assertNotIn("git restore data/daily_summary.json data/runtime_recovery.json", script)
        self.assertLess(
            script.index("systemctl stop safety-watchdog"),
            script.index("systemctl stop krypto-bot"),
        )


if __name__ == "__main__":
    unittest.main()
