import unittest
from pathlib import Path


class DeploySyncScriptTests(unittest.TestCase):
    def test_sync_preserves_runtime_recovery_and_coordinates_watchdog(self):
        script = Path("deploy/sync-from-github.sh").read_text(encoding="utf-8")

        self.assertIn("RECOVERY_BACKUP", script)
        self.assertIn("cp data/runtime_recovery.json", script)
        self.assertIn("restore_runtime_recovery", script)
        self.assertNotIn("git restore data/daily_summary.json data/runtime_recovery.json", script)
        self.assertIn("systemctl stop safety-watchdog", script)
        self.assertIn(".deploy-sync-in-progress", script)


if __name__ == "__main__":
    unittest.main()
