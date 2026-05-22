import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_reads_from_end_without_full_text_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "bot.log"
            log_path.write_bytes(
                b"old line\n" * 10_000
                + b"ERROR newest one\n"
                + b"Traceback newest two\n"
            )

            with patch.object(Path, "read_text", side_effect=AssertionError("full read")):
                lines = watchdog._tail_log(log_path, max_lines=2)

        self.assertEqual(lines, ["ERROR newest one", "Traceback newest two"])

    def test_explicit_empty_log_file_disables_log_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"SAFETY_WATCHDOG_LOG_FILE": ""}):
                self.assertIsNone(watchdog._resolve_log_path(root))

    def test_unset_log_file_falls_back_to_supervisor_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    watchdog._resolve_log_path(root),
                    root / "logs/bot_process.log",
                )


if __name__ == "__main__":
    unittest.main()
