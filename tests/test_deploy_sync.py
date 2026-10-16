import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh"


class DeploySyncScriptTests(unittest.TestCase):
    def test_runtime_files_are_backed_up_and_restored_around_pull(self):
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("RUNTIME_BACKUP_DIR", script)
        self.assertIn('cp "$f" "$RUNTIME_BACKUP_DIR/$f"', script)
        self.assertIn('cp "$RUNTIME_BACKUP_DIR/$f" "$f"', script)

        restore_idx = script.index("git restore data/daily_summary.json data/runtime_recovery.json")
        pull_idx = script.index("git pull origin main --no-rebase")
        restore_runtime_idx = script.index("restore_runtime_files", pull_idx)
        self.assertLess(restore_idx, pull_idx)
        self.assertLess(pull_idx, restore_runtime_idx)

    def test_watchdog_is_stopped_before_bot_and_restarted_after_bot(self):
        script = SCRIPT.read_text(encoding="utf-8")

        stop_watchdog = script.index("systemctl stop safety-watchdog")
        stop_bot = script.index("systemctl stop krypto-bot")
        start_bot = script.rindex("systemctl start krypto-bot")
        start_watchdog = script.rindex("systemctl start safety-watchdog")

        self.assertLess(stop_watchdog, stop_bot)
        self.assertLess(start_bot, start_watchdog)
        self.assertIn("cleanup_on_error", script)


if __name__ == "__main__":
    unittest.main()
