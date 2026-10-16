import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.safety.watchdog import _tail_log


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_does_not_read_entire_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "bot.log"
            log_path.write_text(
                "".join(f"line-{idx}\n" for idx in range(200)),
                encoding="utf-8",
            )

            with patch.object(Path, "read_text", side_effect=AssertionError("full read")):
                lines = _tail_log(log_path, 5)

        self.assertEqual(lines, ["line-195", "line-196", "line-197", "line-198", "line-199"])


if __name__ == "__main__":
    unittest.main()
