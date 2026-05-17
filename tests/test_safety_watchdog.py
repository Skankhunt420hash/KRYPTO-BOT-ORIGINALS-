import json
import tempfile
import unittest
from pathlib import Path

from config.settings import settings
from src.safety import watchdog


class SafetyWatchdogTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "TRADING_MODE": settings.TRADING_MODE,
            "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY": settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY,
            "STATE_RECOVERY_FILE": settings.STATE_RECOVERY_FILE,
            "SAFETY_WATCHDOG_RESTART_CMD": settings.SAFETY_WATCHDOG_RESTART_CMD,
            "SAFETY_WATCHDOG_RESTART_COOLDOWN_SEC": settings.SAFETY_WATCHDOG_RESTART_COOLDOWN_SEC,
        }

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(settings, name, value)

    def test_clear_stuck_recovery_preserves_file_while_bot_runs(self):
        settings.TRADING_MODE = "paper"
        settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = True
        settings.STATE_RECOVERY_FILE = "runtime_recovery.json"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / settings.STATE_RECOVERY_FILE
            payload = {"paused": True, "risk_off": True}
            path.write_text(json.dumps(payload), encoding="utf-8")

            changed, msg = watchdog._clear_stuck_recovery(root, bot_running=True)

            self.assertFalse(changed)
            self.assertIn("bot laeuft", msg)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)

    def test_clear_stuck_recovery_requires_opt_in_and_stopped_bot(self):
        settings.TRADING_MODE = "paper"
        settings.SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY = True
        settings.STATE_RECOVERY_FILE = "runtime_recovery.json"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / settings.STATE_RECOVERY_FILE
            path.write_text(json.dumps({"paused": True, "risk_off": True}), encoding="utf-8")

            changed, msg = watchdog._clear_stuck_recovery(root, bot_running=False)

            self.assertTrue(changed, msg)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"paused": False, "risk_off": False},
            )

    def test_tail_log_returns_only_requested_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            path.write_text("\n".join(f"line-{i}" for i in range(1000)), encoding="utf-8")

            self.assertEqual(
                watchdog._tail_log(path, 3),
                ["line-997", "line-998", "line-999"],
            )

    def test_restart_bot_executes_parsed_argv_without_shell(self):
        settings.SAFETY_WATCHDOG_RESTART_CMD = "systemctl restart krypto-bot"
        settings.SAFETY_WATCHDOG_RESTART_COOLDOWN_SEC = 0
        calls = []
        original_run = watchdog.subprocess.run

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))

        try:
            watchdog.subprocess.run = fake_run
            watchdog._restart_bot("unit-test", tg=None, last_mono=0.0)
        finally:
            watchdog.subprocess.run = original_run

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], ["systemctl", "restart", "krypto-bot"])
        self.assertNotIn("shell", calls[0][1])


if __name__ == "__main__":
    unittest.main()
