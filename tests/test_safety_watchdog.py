import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_reads_only_bounded_tail_without_read_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "".join(f"line-{i}\n" for i in range(2000)),
                encoding="utf-8",
            )

            with patch.object(Path, "read_text", side_effect=AssertionError("full read")):
                lines = watchdog._tail_log(path, 3)

        self.assertEqual(lines, ["line-1997", "line-1998", "line-1999"])

    def test_error_counter_ignores_status_fields_and_regime_names(self):
        lines = [
            "Status: CB=open Errors=20 KillSwitch=False",
            "risk_decision=regime_error regime=REGIME_ERROR",
            "[ERROR] real failure",
            "CRITICAL: exchange unavailable",
            "Traceback (most recent call last):",
        ]

        self.assertEqual(watchdog._count_error_lines(lines), 3)

    def test_explicit_empty_log_file_disables_log_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"SAFETY_WATCHDOG_LOG_FILE": ""}):
                self.assertIsNone(watchdog._resolve_log_path(Path(tmp)))

    def test_clear_stuck_recovery_defaults_to_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "runtime_recovery.json"
            recovery.write_text('{"paused": true, "risk_off": true}\n', encoding="utf-8")

            with (
                patch.object(watchdog.settings, "TRADING_MODE", "paper"),
                patch.object(watchdog.settings, "STATE_RECOVERY_FILE", "runtime_recovery.json"),
                patch.object(watchdog.settings, "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY", False),
            ):
                changed, _ = watchdog._clear_stuck_recovery(root)

            self.assertFalse(changed)
            self.assertIn('"paused": true', recovery.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
