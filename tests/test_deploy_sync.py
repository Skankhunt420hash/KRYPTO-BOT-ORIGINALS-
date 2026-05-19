import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeploySyncTests(unittest.TestCase):
    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_sync_preserves_runtime_recovery_and_daily_summary(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "deploy" / "sync-from-github.sh"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin = tmp_path / "origin.git"
            source = tmp_path / "source"
            server = tmp_path / "server"
            fake_bin = tmp_path / "bin"

            self._git(tmp_path, "init", "--bare", str(origin))
            self._git(tmp_path, "clone", str(origin), str(source))
            self._git(source, "checkout", "-b", "main")
            self._git(source, "config", "user.name", "Test User")
            self._git(source, "config", "user.email", "test@example.com")

            (source / "data").mkdir()
            (source / "data" / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False}),
                encoding="utf-8",
            )
            (source / "data" / "daily_summary.json").write_text(
                json.dumps({"days": [{"day": "repo"}]}),
                encoding="utf-8",
            )
            (source / "README.md").write_text("v1\n", encoding="utf-8")
            self._git(source, "add", ".")
            self._git(source, "commit", "-m", "initial")
            self._git(source, "push", "-u", "origin", "main")

            self._git(tmp_path, "clone", str(origin), str(server))
            self._git(server, "checkout", "main")

            local_recovery = {"paused": True, "risk_off": True, "mode": "live"}
            local_daily = {"days": [{"day": "local", "pnl_abs": -123.45}]}
            (server / "data" / "runtime_recovery.json").write_text(
                json.dumps(local_recovery),
                encoding="utf-8",
            )
            (server / "data" / "daily_summary.json").write_text(
                json.dumps(local_daily),
                encoding="utf-8",
            )

            (source / "README.md").write_text("v2\n", encoding="utf-8")
            (source / "data" / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False, "mode": "paper"}),
                encoding="utf-8",
            )
            (source / "data" / "daily_summary.json").write_text(
                json.dumps({"days": [{"day": "origin"}]}),
                encoding="utf-8",
            )
            self._git(source, "add", ".")
            self._git(source, "commit", "-m", "update origin")
            self._git(source, "push", "origin", "main")

            fake_bin.mkdir()
            sudo = fake_bin / "sudo"
            sudo.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            sudo.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            result = subprocess.run(
                ["bash", str(script), str(server)],
                cwd=str(server),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(
                json.loads((server / "data" / "runtime_recovery.json").read_text(encoding="utf-8")),
                local_recovery,
            )
            self.assertEqual(
                json.loads((server / "data" / "daily_summary.json").read_text(encoding="utf-8")),
                local_daily,
            )
            self.assertEqual((server / "README.md").read_text(encoding="utf-8"), "v2\n")


if __name__ == "__main__":
    unittest.main()
