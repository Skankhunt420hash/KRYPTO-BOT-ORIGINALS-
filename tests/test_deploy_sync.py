import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT / "deploy" / "sync-from-github.sh"


def _run(cmd, cwd, env=None):
    subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class DeploySyncTests(unittest.TestCase):
    def test_sync_preserves_local_runtime_jsons_across_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin = tmp_path / "origin.git"
            seed = tmp_path / "seed"
            work = tmp_path / "work"

            git_env = os.environ.copy()
            git_env.update(
                {
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.invalid",
                }
            )

            _run(["git", "init", "--bare", str(origin)], cwd=tmp_path, env=git_env)
            _run(["git", "init", "-b", "main", str(seed)], cwd=tmp_path, env=git_env)
            (seed / "data").mkdir()
            (seed / "data/runtime_recovery.json").write_text(
                json.dumps({"paused": False, "risk_off": False}),
                encoding="utf-8",
            )
            (seed / "data/daily_summary.json").write_text(
                json.dumps({"days": [{"day": "committed"}]}),
                encoding="utf-8",
            )
            (seed / "README.md").write_text("v1\n", encoding="utf-8")
            _run(["git", "add", "."], cwd=seed, env=git_env)
            _run(["git", "commit", "-m", "initial"], cwd=seed, env=git_env)
            _run(["git", "remote", "add", "origin", str(origin)], cwd=seed, env=git_env)
            _run(["git", "push", "-u", "origin", "main"], cwd=seed, env=git_env)
            _run(["git", "clone", "-b", "main", str(origin), str(work)], cwd=tmp_path, env=git_env)

            (seed / "README.md").write_text("v2\n", encoding="utf-8")
            _run(["git", "add", "README.md"], cwd=seed, env=git_env)
            _run(["git", "commit", "-m", "remote update"], cwd=seed, env=git_env)
            _run(["git", "push", "origin", "main"], cwd=seed, env=git_env)

            local_recovery = {"paused": True, "risk_off": True}
            local_daily = {"days": [{"day": "local"}]}
            (work / "data/runtime_recovery.json").write_text(
                json.dumps(local_recovery),
                encoding="utf-8",
            )
            (work / "data/daily_summary.json").write_text(
                json.dumps(local_daily),
                encoding="utf-8",
            )

            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_sudo = fake_bin / "sudo"
            fake_sudo.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    if [[ "$1" == "systemctl" ]]; then
                      exit 0
                    fi
                    exec "$@"
                    """
                ),
                encoding="utf-8",
            )
            fake_sudo.chmod(0o755)

            run_env = git_env.copy()
            run_env["PATH"] = f"{fake_bin}{os.pathsep}{run_env.get('PATH', '')}"
            _run(["bash", str(SYNC_SCRIPT), str(work)], cwd=ROOT, env=run_env)

            self.assertEqual(
                json.loads((work / "data/runtime_recovery.json").read_text(encoding="utf-8")),
                local_recovery,
            )
            self.assertEqual(
                json.loads((work / "data/daily_summary.json").read_text(encoding="utf-8")),
                local_daily,
            )
            self.assertEqual((work / "README.md").read_text(encoding="utf-8"), "v2\n")


if __name__ == "__main__":
    unittest.main()
