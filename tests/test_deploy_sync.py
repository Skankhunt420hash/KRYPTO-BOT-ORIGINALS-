import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "sync-from-github.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(0o755)


class DeploySyncTests(unittest.TestCase):
    def _make_workspace(self, *, pull_fails: bool):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        bot_dir = root / "bot"
        data_dir = bot_dir / "data"
        bin_dir = root / "bin"
        data_dir.mkdir(parents=True)
        bin_dir.mkdir()
        (data_dir / "runtime_recovery.json").write_text('{"paused": true}\n', encoding="utf-8")
        (data_dir / "daily_summary.json").write_text('{"pnl": 12}\n', encoding="utf-8")
        (bot_dir / ".env").write_text("", encoding="utf-8")

        git_body = f"""
        #!/usr/bin/env bash
        echo "git $*" >> "{root / 'calls.log'}"
        if [[ "$1" == "restore" ]]; then
          echo '{{"paused": false}}' > data/runtime_recovery.json
          echo '{{"pnl": 0}}' > data/daily_summary.json
          exit 0
        fi
        if [[ "$1" == "pull" && "{'1' if pull_fails else '0'}" == "1" ]]; then
          exit 1
        fi
        exit 0
        """
        sudo_body = f"""
        #!/usr/bin/env bash
        echo "sudo $*" >> "{root / 'calls.log'}"
        exit 0
        """
        _write_executable(bin_dir / "git", git_body)
        _write_executable(bin_dir / "sudo", sudo_body)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        return tmp, bot_dir, root / "calls.log", env

    def test_success_preserves_runtime_files_and_service_order(self):
        tmp, bot_dir, calls_log, env = self._make_workspace(pull_fails=False)
        with tmp:
            result = subprocess.run(
                ["bash", str(SCRIPT), str(bot_dir)],
                cwd=str(ROOT),
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual((bot_dir / "data" / "runtime_recovery.json").read_text(encoding="utf-8"), '{"paused": true}\n')
            self.assertEqual((bot_dir / "data" / "daily_summary.json").read_text(encoding="utf-8"), '{"pnl": 12}\n')
            calls = calls_log.read_text(encoding="utf-8").splitlines()
            self.assertLess(calls.index("sudo systemctl stop safety-watchdog"), calls.index("sudo systemctl stop krypto-bot"))
            self.assertLess(calls.index("sudo systemctl start krypto-bot"), calls.index("sudo systemctl start safety-watchdog"))

    def test_pull_failure_restores_runtime_files_and_restarts_services(self):
        tmp, bot_dir, calls_log, env = self._make_workspace(pull_fails=True)
        with tmp:
            result = subprocess.run(
                ["bash", str(SCRIPT), str(bot_dir)],
                cwd=str(ROOT),
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual((bot_dir / "data" / "runtime_recovery.json").read_text(encoding="utf-8"), '{"paused": true}\n')
            self.assertEqual((bot_dir / "data" / "daily_summary.json").read_text(encoding="utf-8"), '{"pnl": 12}\n')
            calls = calls_log.read_text(encoding="utf-8")
            self.assertIn("sudo systemctl start krypto-bot", calls)
            self.assertIn("sudo systemctl start safety-watchdog", calls)


if __name__ == "__main__":
    unittest.main()
