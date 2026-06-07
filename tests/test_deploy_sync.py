import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT / "deploy" / "sync-from-github.sh"


def _run(cmd, *, cwd: Path, env=None):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(map(str, cmd))}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


class DeploySyncTests(unittest.TestCase):
    def test_sync_preserves_runtime_state_and_orders_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin_src = tmp_path / "origin-src"
            origin_bare = tmp_path / "origin.git"
            server = tmp_path / "server"
            updater = tmp_path / "updater"
            fake_bin = tmp_path / "bin"
            sudo_log = tmp_path / "sudo.log"

            origin_src.mkdir()
            _run(["git", "init", "-b", "main"], cwd=origin_src)
            _run(["git", "config", "user.email", "test@example.com"], cwd=origin_src)
            _run(["git", "config", "user.name", "Test User"], cwd=origin_src)
            (origin_src / "data").mkdir()
            (origin_src / "README.md").write_text("v1\n", encoding="utf-8")
            (origin_src / "data" / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False, "source": "origin-v1"}),
                encoding="utf-8",
            )
            (origin_src / "data" / "daily_summary.json").write_text(
                json.dumps({"source": "origin-v1"}),
                encoding="utf-8",
            )
            _run(["git", "add", "."], cwd=origin_src)
            _run(["git", "commit", "-m", "initial"], cwd=origin_src)
            _run(["git", "clone", "--bare", str(origin_src), str(origin_bare)], cwd=tmp_path)
            _run(["git", "clone", str(origin_bare), str(server)], cwd=tmp_path)

            local_recovery = {"paused": True, "risk_off": True, "source": "local-runtime"}
            local_daily = {"source": "local-runtime"}
            (server / "data" / "runtime_recovery.json").write_text(
                json.dumps(local_recovery),
                encoding="utf-8",
            )
            (server / "data" / "daily_summary.json").write_text(
                json.dumps(local_daily),
                encoding="utf-8",
            )

            _run(["git", "clone", str(origin_bare), str(updater)], cwd=tmp_path)
            _run(["git", "config", "user.email", "test@example.com"], cwd=updater)
            _run(["git", "config", "user.name", "Test User"], cwd=updater)
            (updater / "README.md").write_text("v2\n", encoding="utf-8")
            (updater / "data" / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False, "source": "origin-v2"}),
                encoding="utf-8",
            )
            _run(["git", "add", "."], cwd=updater)
            _run(["git", "commit", "-m", "update"], cwd=updater)
            _run(["git", "push", "origin", "main"], cwd=updater)

            fake_bin.mkdir()
            fake_sudo = fake_bin / "sudo"
            fake_sudo.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    echo "$*" >> "$SUDO_LOG"
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["SUDO_LOG"] = str(sudo_log)

            result = subprocess.run(
                [str(SYNC_SCRIPT), str(server)],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertEqual((server / "README.md").read_text(encoding="utf-8"), "v2\n")
            self.assertEqual(
                json.loads((server / "data" / "runtime_recovery.json").read_text(encoding="utf-8")),
                local_recovery,
            )
            self.assertEqual(
                json.loads((server / "data" / "daily_summary.json").read_text(encoding="utf-8")),
                local_daily,
            )

            service_calls = sudo_log.read_text(encoding="utf-8").splitlines()
            self.assertLess(
                service_calls.index("systemctl stop safety-watchdog"),
                service_calls.index("systemctl stop krypto-bot"),
            )
            self.assertLess(
                service_calls.index("systemctl start krypto-bot"),
                service_calls.index("systemctl start safety-watchdog"),
            )


if __name__ == "__main__":
    unittest.main()
