import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class DeploySyncTests(unittest.TestCase):
    def test_sync_preserves_runtime_recovery_control_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            seed = base / "seed"
            origin = base / "origin.git"
            server = base / "server"
            fake_bin = base / "bin"
            seed.mkdir()
            fake_bin.mkdir()

            sudo = fake_bin / "sudo"
            sudo.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            sudo.chmod(sudo.stat().st_mode | stat.S_IXUSR)

            _git(seed, "init")
            _git(seed, "checkout", "-b", "main")
            _git(seed, "config", "user.email", "test@example.com")
            _git(seed, "config", "user.name", "Test User")
            (seed / "data").mkdir()
            (seed / "README.md").write_text("v1\n", encoding="utf-8")
            (seed / "data" / "daily_summary.json").write_text("{}\n", encoding="utf-8")
            (seed / "data" / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False}, indent=2),
                encoding="utf-8",
            )
            _git(seed, "add", ".")
            _git(seed, "commit", "-m", "initial")

            _git(base, "init", "--bare", str(origin))
            _git(seed, "remote", "add", "origin", str(origin))
            _git(seed, "push", "-u", "origin", "main")
            _git(base, f"--git-dir={origin}", "symbolic-ref", "HEAD", "refs/heads/main")
            _git(base, "clone", str(origin), str(server))

            runtime_path = server / "data" / "runtime_recovery.json"
            runtime_path.write_text(
                json.dumps({"paused": True, "risk_off": True, "preferred_strategy": "manual"}, indent=2),
                encoding="utf-8",
            )
            (server / "data" / "daily_summary.json").write_text(
                json.dumps({"local": True}, indent=2),
                encoding="utf-8",
            )

            (seed / "README.md").write_text("v2\n", encoding="utf-8")
            (seed / "data" / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False, "updated_at": "upstream"}, indent=2),
                encoding="utf-8",
            )
            _git(seed, "add", ".")
            _git(seed, "commit", "-m", "upstream update")
            _git(seed, "push", "origin", "main")

            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            subprocess.run(
                ["bash", str(ROOT / "deploy" / "sync-from-github.sh"), str(server)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )

            restored = json.loads(runtime_path.read_text(encoding="utf-8"))
            self.assertTrue(restored["paused"])
            self.assertTrue(restored["risk_off"])
            self.assertEqual(restored["preferred_strategy"], "manual")
            self.assertEqual((server / "README.md").read_text(encoding="utf-8"), "v2\n")


if __name__ == "__main__":
    unittest.main()
