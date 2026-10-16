import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.settings import settings
from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_reads_from_file_end_without_read_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text("\n".join(f"line-{i}" for i in range(1000)) + "\n", encoding="utf-8")

            with patch.object(Path, "read_text", side_effect=AssertionError("read_text not allowed")):
                lines = watchdog._tail_log(path, 5)

        self.assertEqual(lines, ["line-995", "line-996", "line-997", "line-998", "line-999"])

    def test_error_counter_ignores_status_tokens_and_regime_names(self):
        lines = [
            "Status: CB=closed Errors=3 KillSwitch=False",
            "reject_reason=REGIME_ERROR",
            "2026-01-01 INFO normal line",
            "2026-01-01 ERROR real failure",
            "Traceback (most recent call last):",
        ]

        self.assertEqual(watchdog._count_error_lines(lines), 2)

    def test_clear_stuck_recovery_is_opt_in_by_default(self):
        self.assertFalse(settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY)


if __name__ == "__main__":
    unittest.main()
