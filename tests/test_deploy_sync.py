import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeploySyncScriptTests(unittest.TestCase):
    def _run(self, args, cwd, env=None):
        return subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _commit_all(self, repo: Path, message: str) -> None:
        self._run(["git", "add", "."], repo)
        self._run(["git", "commit", "-m", message], repo)

    def test_sync_preserves_local_runtime_json_files(self):
        script = Path(__file__).resolve().parents[1] / "deploy" / "sync-from-github.sh"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin = root / "origin.git"
            source = root / "source"
            server = root / "server"
            fake_bin = root / "bin"

            self._run(["git", "init", "--bare", "--initial-branch=main", str(origin)], root)
            self._run(["git", "init", "--initial-branch=main", str(source)], root)
            self._run(["git", "config", "user.name", "Test"], source)
            self._run(["git", "config", "user.email", "test@example.com"], source)
            self._write(source / "README.md", "initial\n")
            self._write(source / "data" / "runtime_recovery.json", '{"paused": false}\n')
            self._write(source / "data" / "daily_summary.json", '{"days": []}\n')
            self._commit_all(source, "initial")
            self._run(["git", "remote", "add", "origin", str(origin)], source)
            self._run(["git", "push", "-u", "origin", "main"], source)

            self._run(["git", "clone", str(origin), str(server)], root)
            self._run(["git", "config", "user.name", "Server"], server)
            self._run(["git", "config", "user.email", "server@example.com"], server)

            self._write(source / "README.md", "updated upstream\n")
            self._write(source / "data" / "runtime_recovery.json", '{"paused": false, "upstream": true}\n')
            self._write(source / "data" / "daily_summary.json", '{"days": [{"upstream": true}]}\n')
            self._commit_all(source, "upstream update")
            self._run(["git", "push"], source)

            local_recovery = '{"paused": true, "risk_off": true}\n'
            local_summary = '{"days": [{"local": true}]}\n'
            self._write(server / "data" / "runtime_recovery.json", local_recovery)
            self._write(server / "data" / "daily_summary.json", local_summary)

            fake_bin.mkdir()
            sudo = fake_bin / "sudo"
            sudo.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            sudo.chmod(sudo.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

            self._run(["bash", str(script), str(server)], root, env=env)

            self.assertEqual(
                (server / "data" / "runtime_recovery.json").read_text(encoding="utf-8"),
                local_recovery,
            )
            self.assertEqual(
                (server / "data" / "daily_summary.json").read_text(encoding="utf-8"),
                local_summary,
            )
            self.assertEqual(
                (server / "README.md").read_text(encoding="utf-8"),
                "updated upstream\n",
            )


if __name__ == "__main__":
    unittest.main()
