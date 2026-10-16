import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from src.safety import watchdog


@contextmanager
def _without_env(name: str):
    old = os.environ.pop(name, None)
    try:
        yield
    finally:
        if old is not None:
            os.environ[name] = old


class SafetyWatchdogTests(unittest.TestCase):
    def test_explicit_empty_log_file_disables_log_check(self):
        with mock.patch.dict(os.environ, {"SAFETY_WATCHDOG_LOG_FILE": ""}, clear=False):
            self.assertIsNone(watchdog._resolve_log_path(Path("/bot")))

    def test_unset_log_file_uses_supervisor_default(self):
        with _without_env("SAFETY_WATCHDOG_LOG_FILE"):
            with mock.patch.object(watchdog.settings, "SAFETY_WATCHDOG_LOG_FILE", ""):
                with mock.patch.object(
                    watchdog.settings, "SUPERVISOR_BOT_LOGFILE", "logs/bot_process.log"
                ):
                    self.assertEqual(
                        watchdog._resolve_log_path(Path("/bot")),
                        Path("/bot/logs/bot_process.log"),
                    )

    def test_tail_log_returns_only_requested_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text("\n".join(f"line-{i}" for i in range(200)) + "\n", encoding="utf-8")

            self.assertEqual(
                watchdog._tail_log(path, 3),
                ["line-197", "line-198", "line-199"],
            )


if __name__ == "__main__":
    unittest.main()
