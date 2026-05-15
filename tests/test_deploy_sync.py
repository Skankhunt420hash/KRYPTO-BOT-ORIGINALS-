import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeploySyncTests(unittest.TestCase):
    def _git(self, cwd: Path, *args: str) -> None:
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            }
        )
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)

    def test_sync_preserves_local_runtime_recovery_after_pull(self):
        script = Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "origin.git"
            seed = root / "seed"
            server = root / "server"
            bin_dir = root / "bin"

            self._git(root, "init", "--bare", str(remote))
            self._git(root, "init", str(seed))
            self._git(seed, "checkout", "-b", "main")
            (seed / "data").mkdir()
            (seed / "data" / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False}, indent=2),
                encoding="utf-8",
            )
            (seed / "data" / "daily_summary.json").write_text("{}\n", encoding="utf-8")
            (seed / "README.md").write_text("v1\n", encoding="utf-8")
            self._git(seed, "add", ".")
            self._git(seed, "commit", "-m", "initial")
            self._git(seed, "remote", "add", "origin", str(remote))
            self._git(seed, "push", "-u", "origin", "main")
            self._git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

            self._git(root, "clone", str(remote), str(server))
            local_recovery = {
                "paused": True,
                "risk_off": True,
                "operator_note": "must survive deploy",
            }
            (server / "data" / "runtime_recovery.json").write_text(
                json.dumps(local_recovery, indent=2),
                encoding="utf-8",
            )

            (seed / "data" / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False, "upstream": 2}, indent=2),
                encoding="utf-8",
            )
            (seed / "README.md").write_text("v2\n", encoding="utf-8")
            self._git(seed, "add", ".")
            self._git(seed, "commit", "-m", "update runtime template")
            self._git(seed, "push", "origin", "main")

            bin_dir.mkdir()
            fake_sudo = bin_dir / "sudo"
            fake_sudo.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_sudo.chmod(fake_sudo.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            subprocess.run(
                ["bash", str(script), str(server)],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            recovered = json.loads((server / "data" / "runtime_recovery.json").read_text())
            self.assertEqual(recovered, local_recovery)
            self.assertEqual((server / "README.md").read_text(encoding="utf-8"), "v2\n")


if __name__ == "__main__":
    unittest.main()
