import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeploySyncScriptTests(unittest.TestCase):
    def test_sync_preserves_local_runtime_recovery_state(self):
        root = Path(tempfile.mkdtemp(prefix="sync-script-test-"))
        try:
            origin = root / "origin.git"
            seed = root / "seed"
            server = root / "server"
            fakebin = root / "fakebin"

            subprocess.run(
                ["git", "init", "-b", "main", str(seed)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test User"], check=True)

            data_dir = seed / "data"
            data_dir.mkdir()
            (data_dir / "runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False}),
                encoding="utf-8",
            )
            (data_dir / "daily_summary.json").write_text("{}", encoding="utf-8")
            subprocess.run(["git", "-C", str(seed), "add", "data"], check=True)
            subprocess.run(
                ["git", "-C", str(seed), "commit", "-m", "seed runtime files"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            subprocess.run(
                ["git", "clone", "--bare", str(seed), str(origin)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            subprocess.run(
                ["git", "clone", str(origin), str(server)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            local_state = {
                "paused": True,
                "risk_off": True,
                "marker": "local-server-state",
            }
            (server / "data" / "runtime_recovery.json").write_text(
                json.dumps(local_state),
                encoding="utf-8",
            )
            (server / "data" / "daily_summary.json").write_text(
                '{"local": true}',
                encoding="utf-8",
            )

            fakebin.mkdir()
            sudo = fakebin / "sudo"
            sudo.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            sudo.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{fakebin}:{env['PATH']}"
            script = Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh"
            result = subprocess.run(
                ["bash", str(script), str(server)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            restored = json.loads(
                (server / "data" / "runtime_recovery.json").read_text(encoding="utf-8")
            )
            self.assertEqual(restored, local_state)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
