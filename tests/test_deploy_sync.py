import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "sync-from-github.sh"


class DeploySyncScriptTests(unittest.TestCase):
    def _write_executable(self, path: Path, content: str) -> None:
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
        path.chmod(0o755)

    def _make_bot_dir(self, root: Path) -> Path:
        bot_dir = root / "bot"
        (bot_dir / "data").mkdir(parents=True)
        (bot_dir / "data" / "runtime_recovery.json").write_text(
            '{"paused": true, "risk_off": true}\n',
            encoding="utf-8",
        )
        (bot_dir / "data" / "daily_summary.json").write_text(
            '{"days": [{"day": "today", "pnl_abs": -123.45}]}\n',
            encoding="utf-8",
        )
        return bot_dir

    def _make_fake_bin(self, root: Path, *, pull_fails: bool) -> Path:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        command_log = root / "commands.log"

        self._write_executable(
            fake_bin / "sudo",
            f"""
            #!/usr/bin/env bash
            echo "sudo $*" >> "{command_log}"
            exec "$@"
            """,
        )
        self._write_executable(
            fake_bin / "systemctl",
            f"""
            #!/usr/bin/env bash
            echo "systemctl $*" >> "{command_log}"
            exit 0
            """,
        )
        pull_exit = 1 if pull_fails else 0
        self._write_executable(
            fake_bin / "git",
            f"""
            #!/usr/bin/env bash
            echo "git $*" >> "{command_log}"
            case "$1" in
              fetch)
                exit 0
                ;;
              restore)
                printf '%s\\n' '{{"paused": false, "risk_off": false}}' > data/runtime_recovery.json
                printf '%s\\n' '{{"days": []}}' > data/daily_summary.json
                exit 0
                ;;
              pull)
                exit {pull_exit}
                ;;
            esac
            exit 99
            """,
        )
        return fake_bin

    def _run_script(self, bot_dir: Path, fake_bin: Path) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        return subprocess.run(
            ["bash", str(SCRIPT), str(bot_dir)],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_failed_pull_restarts_services_and_preserves_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bot_dir = self._make_bot_dir(root)
            fake_bin = self._make_fake_bin(root, pull_fails=True)

            result = self._run_script(bot_dir, fake_bin)

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                '{"paused": true, "risk_off": true}\n',
                (bot_dir / "data" / "runtime_recovery.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                '{"days": [{"day": "today", "pnl_abs": -123.45}]}\n',
                (bot_dir / "data" / "daily_summary.json").read_text(encoding="utf-8"),
            )
            commands = (root / "commands.log").read_text(encoding="utf-8")
            self.assertIn("systemctl stop safety-watchdog", commands)
            self.assertIn("systemctl stop krypto-bot", commands)
            self.assertIn("systemctl start krypto-bot", commands)
            self.assertIn("systemctl start safety-watchdog", commands)

    def test_successful_pull_preserves_runtime_files_before_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bot_dir = self._make_bot_dir(root)
            fake_bin = self._make_fake_bin(root, pull_fails=False)

            result = self._run_script(bot_dir, fake_bin)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                '{"paused": true, "risk_off": true}\n',
                (bot_dir / "data" / "runtime_recovery.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                '{"days": [{"day": "today", "pnl_abs": -123.45}]}\n',
                (bot_dir / "data" / "daily_summary.json").read_text(encoding="utf-8"),
            )
            commands = (root / "commands.log").read_text(encoding="utf-8").splitlines()
            bot_start_idx = commands.index("systemctl start krypto-bot")
            watchdog_start_idx = commands.index("systemctl start safety-watchdog")
            status_idx = commands.index("systemctl status krypto-bot --no-pager")
            self.assertLess(bot_start_idx, watchdog_start_idx)
            self.assertLess(watchdog_start_idx, status_idx)


if __name__ == "__main__":
    unittest.main()
