import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh"


class DeploySyncTests(unittest.TestCase):
    def _make_fake_bin(self, tmp: Path, log: Path) -> Path:
        fake_bin = tmp / "fake-bin"
        fake_bin.mkdir()
        (fake_bin / "sudo").write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env sh
                echo "sudo $*" >> "{log}"
                exit 0
                """
            ),
            encoding="utf-8",
        )
        (fake_bin / "git").write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env sh
                echo "git $*" >> "{log}"
                case "$1" in
                  fetch)
                    exit 0
                    ;;
                  restore)
                    printf '{{"paused": false, "risk_off": false}}\\n' > data/runtime_recovery.json
                    printf '{{"daily": "from-repo"}}\\n' > data/daily_summary.json
                    exit 0
                    ;;
                  pull)
                    if [ "${{FAKE_GIT_PULL_FAIL:-0}}" = "1" ]; then
                      exit 2
                    fi
                    printf '{{"paused": false, "risk_off": false, "from_pull": true}}\\n' > data/runtime_recovery.json
                    exit 0
                    ;;
                esac
                exit 0
                """
            ),
            encoding="utf-8",
        )
        for exe in fake_bin.iterdir():
            exe.chmod(0o755)
        return fake_bin

    def _make_bot_dir(self, tmp: Path) -> Path:
        bot_dir = tmp / "bot"
        (bot_dir / "data").mkdir(parents=True)
        (bot_dir / "data" / "runtime_recovery.json").write_text(
            '{"paused": true, "risk_off": true, "local": true}\n',
            encoding="utf-8",
        )
        (bot_dir / "data" / "daily_summary.json").write_text(
            '{"daily": "local"}\n",
            encoding="utf-8",
        )
        return bot_dir

    def _run_script(self, bot_dir: Path, fake_bin: Path, env_extra=None):
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", str(SCRIPT), str(bot_dir)],
            cwd=str(bot_dir),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_sync_preserves_runtime_files_and_service_order(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            log = tmp / "commands.log"
            bot_dir = self._make_bot_dir(tmp)
            fake_bin = self._make_fake_bin(tmp, log)

            result = self._run_script(bot_dir, fake_bin)

            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertEqual(
                '{"paused": true, "risk_off": true, "local": true}\n',
                (bot_dir / "data" / "runtime_recovery.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                '{"daily": "local"}\n',
                (bot_dir / "data" / "daily_summary.json").read_text(encoding="utf-8"),
            )

            commands = log.read_text(encoding="utf-8").splitlines()
            self.assertLess(
                commands.index("sudo systemctl stop safety-watchdog"),
                commands.index("sudo systemctl stop krypto-bot"),
            )
            self.assertLess(
                commands.index("sudo systemctl start krypto-bot"),
                commands.index("sudo systemctl start safety-watchdog"),
            )

    def test_failed_pull_restores_runtime_files_and_restarts_bot(self):
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            log = tmp / "commands.log"
            bot_dir = self._make_bot_dir(tmp)
            fake_bin = self._make_fake_bin(tmp, log)

            result = self._run_script(
                bot_dir,
                fake_bin,
                env_extra={"FAKE_GIT_PULL_FAIL": "1"},
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(
                '{"paused": true, "risk_off": true, "local": true}\n',
                (bot_dir / "data" / "runtime_recovery.json").read_text(encoding="utf-8"),
            )
            commands = log.read_text(encoding="utf-8").splitlines()
            self.assertIn("sudo systemctl start krypto-bot", commands)


if __name__ == "__main__":
    unittest.main()
