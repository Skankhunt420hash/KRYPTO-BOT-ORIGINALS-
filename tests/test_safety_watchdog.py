import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.safety import watchdog
from src.safety.watchdog import _tail_log


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_reads_from_file_end_without_full_text_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "bot.log"
            lines = [f"INFO line {i}" for i in range(200)]
            lines.extend(["ERROR one", "Traceback two", "CRITICAL three"])
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with mock.patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("full file read is not allowed"),
            ):
                tail = _tail_log(log_path, 3)

        self.assertEqual(tail, ["ERROR one", "Traceback two", "CRITICAL three"])

    def test_clear_stuck_recovery_disabled_keeps_control_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "data" / "runtime_recovery.json"
            recovery.parent.mkdir()
            recovery.write_text(
                '{"paused": true, "risk_off": true}\n',
                encoding="utf-8",
            )

            with (
                mock.patch.object(watchdog.settings, "TRADING_MODE", "paper"),
                mock.patch.object(watchdog.settings, "STATE_RECOVERY_FILE", "data/runtime_recovery.json"),
                mock.patch.object(watchdog.settings, "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY", False),
            ):
                changed, msg = watchdog._clear_stuck_recovery(root)
            content = recovery.read_text(encoding="utf-8")

            self.assertFalse(changed)
            self.assertEqual(msg, "clear recovery aus")
            self.assertEqual(content, '{"paused": true, "risk_off": true}\n')


if __name__ == "__main__":
    unittest.main()
