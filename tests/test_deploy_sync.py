import unittest
from pathlib import Path


class DeploySyncScriptTests(unittest.TestCase):
    def test_sync_preserves_runtime_files_and_quiesces_watchdog(self):
        script = Path("deploy/sync-from-github.sh").read_text(encoding="utf-8")

        self.assertIn("backup_runtime_files", script)
        self.assertIn("restore_runtime_files", script)
        self.assertIn("data/runtime_recovery.json", script)
        self.assertIn("data/daily_summary.json", script)

        stop_watchdog = script.index("systemctl stop safety-watchdog")
        stop_bot = script.index("systemctl stop krypto-bot")
        git_pull = script.index("git pull origin main --no-rebase")
        restore = script.index("\nrestore_runtime_files\n", git_pull)
        restart_services = script.index("\nrestart_services\n", restore)
        restart_bot = script.index("systemctl start krypto-bot")
        restart_watchdog = script.index("systemctl start safety-watchdog")

        self.assertLess(stop_watchdog, stop_bot)
        self.assertLess(stop_bot, git_pull)
        self.assertLess(git_pull, restore)
        self.assertLess(restore, restart_services)
        self.assertLess(restart_bot, restart_watchdog)


if __name__ == "__main__":
    unittest.main()
