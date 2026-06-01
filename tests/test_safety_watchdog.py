import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_returns_last_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "".join(f"line {idx}\n" for idx in range(200)),
                encoding="utf-8",
            )

            self.assertEqual(
                watchdog._tail_log(path, 3),
                ["line 197", "line 198", "line 199"],
            )

    def test_tail_log_keeps_recent_errors_after_large_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_bytes((b"x" * (2 * 1024 * 1024)) + b"\nERROR final\n")

            self.assertEqual(watchdog._tail_log(path, 1), ["ERROR final"])
            self.assertEqual(watchdog._count_error_lines(watchdog._tail_log(path, 1)), 1)

    def test_explicit_empty_log_file_disables_log_check(self):
        root = Path("/tmp/project-root")

        with mock.patch.dict(os.environ, {"SAFETY_WATCHDOG_LOG_FILE": ""}, clear=True):
            self.assertIsNone(watchdog._resolve_log_path(root))

    def test_unset_log_file_uses_supervisor_default(self):
        root = Path("/tmp/project-root")

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                watchdog.settings,
                "SUPERVISOR_BOT_LOGFILE",
                "logs/supervisor.log",
            ):
                self.assertEqual(
                    watchdog._resolve_log_path(root),
                    root / "logs/supervisor.log",
                )


if __name__ == "__main__":
    unittest.main()
