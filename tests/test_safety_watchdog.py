import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.settings import settings
from src.safety.watchdog import _resolve_log_path, _tail_log


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_reads_recent_lines_without_full_text_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "bot.log"
            log_path.write_text(
                "".join(f"line-{i}\n" for i in range(2000)),
                encoding="utf-8",
            )

            with patch.object(Path, "read_text", side_effect=AssertionError("full read")):
                lines = _tail_log(log_path, 3)

        self.assertEqual(lines, ["line-1997", "line-1998", "line-1999"])

    def test_explicit_empty_log_file_disables_log_burst_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SAFETY_WATCHDOG_LOG_FILE": ""}):
                self.assertIsNone(_resolve_log_path(Path(tmp)))

    def test_unset_log_file_falls_back_to_supervisor_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SAFETY_WATCHDOG_LOG_FILE", None)
                resolved = _resolve_log_path(root)

        self.assertEqual(resolved, root / settings.SUPERVISOR_BOT_LOGFILE)


if __name__ == "__main__":
    unittest.main()
