from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh"


class DeploySyncScriptTests(unittest.TestCase):
    def test_runtime_files_are_backed_up_before_git_restore(self):
        text = SCRIPT.read_text(encoding="utf-8")

        backup_pos = text.index("\nbackup_runtime_files\n")
        restore_pos = text.index("git restore data/daily_summary.json data/runtime_recovery.json")

        self.assertLess(backup_pos, restore_pos)
        self.assertIn("restore_runtime_files", text)
        self.assertIn("fail_sync", text)

    def test_watchdog_stops_before_bot_and_starts_after_bot(self):
        text = SCRIPT.read_text(encoding="utf-8")

        stop_watchdog = text.index('sudo systemctl stop "$SAFETY_WATCHDOG_SERVICE"')
        stop_bot = text.index('sudo systemctl stop "$BOT_SERVICE"')
        start_bot = text.index('sudo systemctl start "$BOT_SERVICE"')
        start_watchdog = text.index('sudo systemctl start "$SAFETY_WATCHDOG_SERVICE"')

        self.assertLess(stop_watchdog, stop_bot)
        self.assertLess(start_bot, start_watchdog)


if __name__ == "__main__":
    unittest.main()
