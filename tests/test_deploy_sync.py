import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh"


def run(cmd, cwd, env=None):
    subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class DeploySyncTests(unittest.TestCase):
    def test_sync_preserves_runtime_files_and_orders_services_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin.git"
            seed = root / "seed"
            bot_dir = root / "bot"
            fake_bin = root / "bin"
            sudo_log = root / "sudo.log"

            run(["git", "init", "--bare", str(origin)], cwd=root)
            run(["git", "init", str(seed)], cwd=root)
            run(["git", "checkout", "-b", "main"], cwd=seed)
            run(["git", "config", "user.email", "test@example.com"], cwd=seed)
            run(["git", "config", "user.name", "Test User"], cwd=seed)
            (seed / "data").mkdir()
            (seed / "data" / "runtime_recovery.json").write_text(
                '{"paused": false, "risk_off": false}\n',
                encoding="utf-8",
            )
            (seed / "data" / "daily_summary.json").write_text(
                '{"days": []}\n',
                encoding="utf-8",
            )
            run(["git", "add", "data"], cwd=seed)
            run(["git", "commit", "-m", "seed runtime files"], cwd=seed)
            run(["git", "remote", "add", "origin", str(origin)], cwd=seed)
            run(["git", "push", "-u", "origin", "main"], cwd=seed)
            run(["git", "clone", "-b", "main", str(origin), str(bot_dir)], cwd=root)

            (bot_dir / "data" / "runtime_recovery.json").write_text(
                '{"paused": true, "risk_off": true}\n',
                encoding="utf-8",
            )
            (bot_dir / "data" / "daily_summary.json").write_text(
                '{"days": [{"day": "local"}]}\n',
                encoding="utf-8",
            )

            fake_bin.mkdir()
            fake_sudo = fake_bin / "sudo"
            fake_sudo.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$SUDO_LOG"\nexit 0\n',
                encoding="utf-8",
            )
            fake_sudo.chmod(fake_sudo.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["SUDO_LOG"] = str(sudo_log)

            run(["bash", str(SCRIPT), str(bot_dir)], cwd=root, env=env)

            self.assertIn(
                '"paused": true',
                (bot_dir / "data" / "runtime_recovery.json").read_text(encoding="utf-8"),
            )
            self.assertIn(
                '"day": "local"',
                (bot_dir / "data" / "daily_summary.json").read_text(encoding="utf-8"),
            )

            service_commands = sudo_log.read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(service_commands), 4)
            self.assertEqual(service_commands[0], "systemctl stop safety-watchdog")
            self.assertEqual(service_commands[1], "systemctl stop krypto-bot")
            self.assertLess(
                service_commands.index("systemctl start krypto-bot"),
                service_commands.index("systemctl start safety-watchdog"),
            )


if __name__ == "__main__":
    unittest.main()
