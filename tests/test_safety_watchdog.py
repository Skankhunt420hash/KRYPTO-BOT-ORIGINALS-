import tempfile
import unittest
from pathlib import Path

from src.safety.watchdog import _tail_log


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_returns_only_requested_trailing_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text(
                "".join(f"line-{i}\n" for i in range(1000)),
                encoding="utf-8",
            )

            self.assertEqual(
                _tail_log(path, 3),
                ["line-997", "line-998", "line-999"],
            )

    def test_tail_log_handles_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_tail_log(Path(tmp) / "missing.log", 10), [])


if __name__ == "__main__":
    unittest.main()
