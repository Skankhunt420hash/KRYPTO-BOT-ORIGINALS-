import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeploySyncScriptTests(unittest.TestCase):
    def _run(self, args, *, cwd, env=None):
        result = subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=(
                f"Command failed: {' '.join(str(a) for a in args)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            ),
        )
        return result

    def test_sync_preserves_local_runtime_recovery_state(self):
        repo_root = Path(__file__).resolve().parents[1]
        sync_script = repo_root / "deploy" / "sync-from-github.sh"

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            origin = tmp / "origin.git"
            upstream = tmp / "upstream"
            server = tmp / "server"

            self._run(["git", "init", "--bare", "--initial-branch=main", str(origin)], cwd=tmp)
            self._run(["git", "init", "--initial-branch=main", str(upstream)], cwd=tmp)
            self._run(["git", "config", "user.email", "test@example.com"], cwd=upstream)
            self._run(["git", "config", "user.name", "Test User"], cwd=upstream)

            (upstream / "data").mkdir()
            (upstream / "data" / "daily_summary.json").write_text("{}", encoding="utf-8")
            (upstream / "data" / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False}, indent=2),
                encoding="utf-8",
            )
            self._run(["git", "add", "data"], cwd=upstream)
            self._run(["git", "commit", "-m", "initial"], cwd=upstream)
            self._run(["git", "remote", "add", "origin", str(origin)], cwd=upstream)
            self._run(["git", "push", "-u", "origin", "main"], cwd=upstream)

            self._run(["git", "clone", str(origin), str(server)], cwd=tmp)

            local_recovery = {
                "mode": "paper",
                "paused": True,
                "risk_off": True,
                "preferred_strategy": "RangeReversion",
            }
            (server / "data" / "runtime_recovery.json").write_text(
                json.dumps(local_recovery, indent=2),
                encoding="utf-8",
            )

            (upstream / "data" / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False, "schema": "new"}, indent=2),
                encoding="utf-8",
            )
            self._run(["git", "add", "data/runtime_recovery.json"], cwd=upstream)
            self._run(["git", "commit", "-m", "update recovery template"], cwd=upstream)
            self._run(["git", "push", "origin", "main"], cwd=upstream)

            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            fake_sudo = fake_bin / "sudo"
            fake_sudo.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_sudo.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

            self._run(["bash", str(sync_script), str(server)], cwd=tmp, env=env)

            restored = json.loads((server / "data" / "runtime_recovery.json").read_text(encoding="utf-8"))
            self.assertEqual(restored, local_recovery)

            upstream_head = self._run(["git", "rev-parse", "HEAD"], cwd=upstream).stdout.strip()
            server_head = self._run(["git", "rev-parse", "HEAD"], cwd=server).stdout.strip()
            self.assertEqual(server_head, upstream_head)


if __name__ == "__main__":
    unittest.main()
