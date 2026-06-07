import json
import tempfile
import unittest
from pathlib import Path

from config.settings import settings
from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def test_tail_log_returns_only_requested_tail_from_large_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            lines = [f"line-{i}" for i in range(5000)]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            self.assertEqual(
                watchdog._tail_log(path, 3),
                ["line-4997", "line-4998", "line-4999"],
            )

    def test_error_count_ignores_status_words_and_regime_names(self):
        lines = [
            "Status: CB=open Errors=3 KillSwitch=False",
            "risk_decision=REGIME_ERROR",
            "[ERROR] real exchange failure",
            "CRITICAL unrecoverable",
        ]

        self.assertEqual(watchdog._count_error_lines(lines), 2)

    def test_clear_stuck_recovery_is_disabled_by_default(self):
        old_mode = settings.TRADING_MODE
        old_file = settings.STATE_RECOVERY_FILE
        old_clear = settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY
        self.addCleanup(lambda: setattr(settings, "TRADING_MODE", old_mode))
        self.addCleanup(lambda: setattr(settings, "STATE_RECOVERY_FILE", old_file))
        self.addCleanup(
            lambda: setattr(
                settings, "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY", old_clear
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "data" / "runtime_recovery.json"
            recovery.parent.mkdir()
            payload = {"paused": True, "risk_off": True}
            recovery.write_text(json.dumps(payload), encoding="utf-8")

            settings.TRADING_MODE = "paper"
            settings.STATE_RECOVERY_FILE = "data/runtime_recovery.json"
            settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False

            cleared, msg = watchdog._clear_stuck_recovery(root)

            self.assertFalse(cleared)
            self.assertEqual(msg, "clear recovery aus")
            self.assertEqual(
                json.loads(recovery.read_text(encoding="utf-8")),
                payload,
            )


if __name__ == "__main__":
    unittest.main()
