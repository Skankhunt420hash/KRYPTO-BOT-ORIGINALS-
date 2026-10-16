import unittest
from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh").read_text(
    encoding="utf-8"
)
LINES = SCRIPT.splitlines()


def _first_line_containing(needle: str) -> int:
    for idx, line in enumerate(LINES):
        if needle in line:
            return idx
    raise AssertionError(f"missing line containing {needle!r}")


class DeploySyncScriptTests(unittest.TestCase):
    def test_stops_watchdog_before_bot(self):
        watchdog_stop = _first_line_containing('systemctl stop "$WATCHDOG_SERVICE"')
        bot_stop = _first_line_containing('systemctl stop "$BOT_SERVICE"')

        self.assertLess(watchdog_stop, bot_stop)

    def test_preserves_runtime_files_across_git_pull(self):
        self.assertIn("data/runtime_recovery.json", SCRIPT)
        self.assertIn("data/daily_summary.json", SCRIPT)

        backup = _first_line_containing("backup_runtime_files")
        git_restore = _first_line_containing("git restore data/daily_summary.json data/runtime_recovery.json")
        git_pull = _first_line_containing("git pull origin main --no-rebase")
        restore_after_pull = next(
            idx for idx, line in enumerate(LINES[git_pull + 1 :], start=git_pull + 1)
            if "restore_runtime_files" in line
        )
        pip_install = _first_line_containing(".venv/bin/pip install")

        self.assertLess(backup, git_restore)
        self.assertLess(git_restore, git_pull)
        self.assertLess(git_pull, restore_after_pull)
        self.assertLess(restore_after_pull, pip_install)

    def test_pull_failure_restores_runtime_files_before_exit(self):
        failure_echo = _first_line_containing("pull fehlgeschlagen")
        failure_restore = next(
            idx for idx, line in enumerate(LINES[failure_echo + 1 :], start=failure_echo + 1)
            if "restore_runtime_files" in line
        )
        failure_exit = next(
            idx for idx, line in enumerate(LINES[failure_restore + 1 :], start=failure_restore + 1)
            if "exit 1" in line
        )

        self.assertLess(failure_echo, failure_restore)
        self.assertLess(failure_restore, failure_exit)

    def test_start_services_restarts_watchdog_after_bot(self):
        bot_start = _first_line_containing('systemctl start "$BOT_SERVICE"')
        watchdog_start = _first_line_containing('systemctl start "$WATCHDOG_SERVICE"')

        self.assertLess(bot_start, watchdog_start)


if __name__ == "__main__":
    unittest.main()
