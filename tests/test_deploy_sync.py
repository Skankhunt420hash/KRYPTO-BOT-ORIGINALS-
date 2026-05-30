import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT / "deploy" / "sync-from-github.sh"


def _run(cmd, cwd, env=None):
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


class DeploySyncTests(unittest.TestCase):
    def test_sync_preserves_runtime_jsons_and_stops_watchdog_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin_work = base / "origin-work"
            origin_bare = base / "origin.git"
            bot_dir = base / "bot"

            origin_work.mkdir()
            _run(["git", "init", "-b", "main"], origin_work)
            _run(["git", "config", "user.email", "test@example.invalid"], origin_work)
            _run(["git", "config", "user.name", "Test User"], origin_work)
            (origin_work / "data").mkdir()
            (origin_work / "data" / "runtime_recovery.json").write_text(
                '{"paused": false, "source": "committed"}\n',
                encoding="utf-8",
            )
            (origin_work / "data" / "daily_summary.json").write_text(
                '{"source": "committed"}\n',
                encoding="utf-8",
            )
            (origin_work / "README.md").write_text("v1\n", encoding="utf-8")
            _run(["git", "add", "."], origin_work)
            _run(["git", "commit", "-m", "initial"], origin_work)
            _run(["git", "clone", "--bare", str(origin_work), str(origin_bare)], base)
            _run(["git", "clone", str(origin_bare), str(bot_dir)], base)

            local_runtime = '{"paused": true, "source": "local"}\n'
            local_daily = '{"source": "local"}\n'
            (bot_dir / "data" / "runtime_recovery.json").write_text(
                local_runtime,
                encoding="utf-8",
            )
            (bot_dir / "data" / "daily_summary.json").write_text(
                local_daily,
                encoding="utf-8",
            )

            remote_update = base / "remote-update"
            _run(["git", "clone", str(origin_bare), str(remote_update)], base)
            _run(["git", "config", "user.email", "test@example.invalid"], remote_update)
            _run(["git", "config", "user.name", "Test User"], remote_update)
            (remote_update / "README.md").write_text("v2\n", encoding="utf-8")
            _run(["git", "add", "README.md"], remote_update)
            _run(["git", "commit", "-m", "remote update"], remote_update)
            _run(["git", "push", "origin", "main"], remote_update)

            calls_log = base / "systemctl.log"
            fake_bin = base / "bin"
            fake_bin.mkdir()
            fake_sudo = fake_bin / "sudo"
            fake_sudo.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    echo "$*" >> "$CALL_LOG"
                    if [[ "${1:-}" == "systemctl" ]]; then
                      shift
                      if [[ "${1:-}" == "is-active" ]]; then
                        exit 0
                      fi
                      exit 0
                    fi
                    exec "$@"
                    """
                ),
                encoding="utf-8",
            )
            fake_sudo.chmod(fake_sudo.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["CALL_LOG"] = str(calls_log)

            shutil.copy2(SYNC_SCRIPT, bot_dir / "sync-from-github.sh")
            result = subprocess.run(
                ["bash", "sync-from-github.sh", str(bot_dir)],
                cwd=str(bot_dir),
                env=env,
                text=True,
                capture_output=True,
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertEqual(
                (bot_dir / "data" / "runtime_recovery.json").read_text(encoding="utf-8"),
                local_runtime,
            )
            self.assertEqual(
                (bot_dir / "data" / "daily_summary.json").read_text(encoding="utf-8"),
                local_daily,
            )
            self.assertEqual(
                (bot_dir / "README.md").read_text(encoding="utf-8"),
                "v2\n",
            )

            calls = calls_log.read_text(encoding="utf-8").splitlines()
            self.assertLess(
                calls.index("systemctl stop safety-watchdog"),
                calls.index("systemctl stop krypto-bot"),
            )
            self.assertLess(
                calls.index("systemctl start krypto-bot"),
                calls.index("systemctl start safety-watchdog"),
            )


if __name__ == "__main__":
    unittest.main()
