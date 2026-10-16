import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config.settings import settings
from src.bot import MultiStrategyBot
from src.engine.runtime_control import runtime_control
from src.safety.watchdog import (
    _clear_stuck_recovery,
    _count_error_lines,
    _initial_log_cursor,
    _read_new_log_lines,
    _resolve_log_path,
    _tail_log,
)


class RecentSafetyRegressionTests(unittest.TestCase):
    def tearDown(self) -> None:
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()

    def test_paper_control_locks_are_not_cleared_without_opt_in(self):
        with (
            patch.object(settings, "TRADING_MODE", "paper"),
            patch.object(settings, "PAPER_CLEAR_CONTROL_LOCKS_EACH_CYCLE", False),
        ):
            runtime_control.pause_entries()
            runtime_control.enable_risk_off()

            MultiStrategyBot.__new__(
                MultiStrategyBot
            )._paper_undo_unwanted_control_locks()

            snapshot = runtime_control.get_snapshot()
            self.assertTrue(snapshot["paused"])
            self.assertTrue(snapshot["risk_off"])

    def test_watchdog_does_not_clear_recovery_without_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recovery = root / "runtime_recovery.json"
            recovery.write_text(
                json.dumps({"paused": True, "risk_off": True}),
                encoding="utf-8",
            )
            with (
                patch.object(settings, "TRADING_MODE", "paper"),
                patch.object(settings, "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY", False),
                patch.object(settings, "STATE_RECOVERY_FILE", str(recovery)),
            ):
                changed, _ = _clear_stuck_recovery(root)

            self.assertFalse(changed)
            self.assertEqual(
                json.loads(recovery.read_text(encoding="utf-8")),
                {"paused": True, "risk_off": True},
            )

    def test_watchdog_tail_is_bounded_and_reads_latest_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "bot.log"
            log.write_text(
                "\n".join(f"line-{index}" for index in range(10_000)),
                encoding="utf-8",
            )
            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("full-file read must not be used"),
            ):
                lines = _tail_log(log, 5)

            self.assertEqual(
                lines,
                ["line-9995", "line-9996", "line-9997", "line-9998", "line-9999"],
            )

    def test_watchdog_ignores_stale_errors_and_status_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "bot.log"
            log.write_text("ERROR stale\n" * 30, encoding="utf-8")
            identity, offset = _initial_log_cursor(log)
            with log.open("a", encoding="utf-8") as stream:
                stream.write("Status: Errors=3 REGIME_ERROR\n")
                stream.write("ERROR new exchange failure\n")

            lines, _, _ = _read_new_log_lines(log, identity, offset, max_lines=500)

            self.assertEqual(len(lines), 2)
            self.assertEqual(_count_error_lines(lines), 1)

    def test_watchdog_cursor_detects_copytruncate_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "bot.log"
            log.write_text("OLD " + ("x" * 100_000), encoding="utf-8")
            identity, offset = _initial_log_cursor(log)
            log.write_text(
                "NEW ERROR after rotation\n" + ("y" * 110_000),
                encoding="utf-8",
            )

            lines, _, _ = _read_new_log_lines(log, identity, offset, max_lines=500)

            self.assertTrue(lines[0].startswith("NEW ERROR after rotation"))

    def test_explicit_empty_watchdog_log_disables_log_checks(self):
        root = Path("/tmp/project")
        with patch.dict(os.environ, {"SAFETY_WATCHDOG_LOG_FILE": ""}):
            self.assertIsNone(_resolve_log_path(root))
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(settings, "SUPERVISOR_BOT_LOGFILE", "logs/bot.log"),
        ):
            self.assertEqual(_resolve_log_path(root), root / "logs/bot.log")

    def test_deploy_preserves_runtime_files_and_stops_watchdog_first(self):
        script = Path(__file__).parents[1] / "deploy" / "sync-from-github.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data").mkdir()
            daily = root / "data" / "daily_summary.json"
            recovery = root / "data" / "runtime_recovery.json"
            daily.write_text('{"days":["local"]}', encoding="utf-8")
            recovery.write_text('{"risk_off":true}', encoding="utf-8")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            call_log = root / "calls.log"
            fake_git = bin_dir / "git"
            fake_git.write_text(
                "#!/usr/bin/env bash\n"
                'echo "git $*" >> "$CALL_LOG"\n'
                'if [[ "$1" == "restore" ]]; then\n'
                '  echo \'{"days":["git"]}\' > data/daily_summary.json\n'
                '  echo \'{"risk_off":false}\' > data/runtime_recovery.json\n'
                "fi\n",
                encoding="utf-8",
            )
            fake_sudo = bin_dir / "sudo"
            fake_sudo.write_text(
                "#!/usr/bin/env bash\n"
                'echo "sudo $*" >> "$CALL_LOG"\n'
                'if [[ "$*" == "systemctl stop krypto-bot" ]]; then\n'
                '  echo \'{"risk_off":true,"state":"final"}\' > data/runtime_recovery.json\n'
                "fi\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            fake_sudo.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["CALL_LOG"] = str(call_log)

            result = subprocess.run(
                ["bash", str(script), str(root)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(daily.read_text(encoding="utf-8"), '{"days":["local"]}')
            self.assertEqual(
                recovery.read_text(encoding="utf-8"),
                '{"risk_off":true,"state":"final"}\n',
            )
            calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertLess(
                calls.index("sudo systemctl stop safety-watchdog"),
                calls.index("sudo systemctl stop krypto-bot"),
            )
            self.assertLess(
                calls.index("sudo systemctl start krypto-bot"),
                calls.index("sudo systemctl start safety-watchdog"),
            )


if __name__ == "__main__":
    unittest.main()
