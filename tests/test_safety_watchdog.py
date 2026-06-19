import json
import tempfile
import unittest
from pathlib import Path

from config.settings import settings
from src.safety import watchdog


class SafetyWatchdogRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._orig_mode = settings.TRADING_MODE
        self._orig_recovery_file = settings.STATE_RECOVERY_FILE
        self._orig_clear = settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY

    def tearDown(self):
        settings.TRADING_MODE = self._orig_mode
        settings.STATE_RECOVERY_FILE = self._orig_recovery_file
        settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = self._orig_clear

    def test_recovery_clear_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "data" / "runtime_recovery.json"
            recovery.parent.mkdir()
            recovery.write_text(
                json.dumps({"paused": True, "risk_off": True}),
                encoding="utf-8",
            )

            settings.TRADING_MODE = "paper"
            settings.STATE_RECOVERY_FILE = str(recovery)
            settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = False

            changed, reason = watchdog._clear_stuck_recovery(root)

            self.assertFalse(changed)
            self.assertIn("aus", reason)
            self.assertEqual(
                {"paused": True, "risk_off": True},
                json.loads(recovery.read_text(encoding="utf-8")),
            )

    def test_recovery_clear_when_explicitly_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "data" / "runtime_recovery.json"
            recovery.parent.mkdir()
            recovery.write_text(
                json.dumps({"paused": True, "risk_off": True}),
                encoding="utf-8",
            )

            settings.TRADING_MODE = "paper"
            settings.STATE_RECOVERY_FILE = str(recovery)
            settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = True

            changed, _ = watchdog._clear_stuck_recovery(root)

            self.assertTrue(changed)
            self.assertEqual(
                {"paused": False, "risk_off": False},
                json.loads(recovery.read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()
