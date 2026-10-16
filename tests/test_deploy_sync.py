import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = REPO_ROOT / "deploy" / "sync-from-github.sh"


class DeploySyncTests(unittest.TestCase):
    def _make_fake_tools(self, tmp: Path) -> tuple[Path, Path]:
        fakebin = tmp / "fakebin"
        fakebin.mkdir()
        call_log = tmp / "calls.log"

        git_script = textwrap.dedent(
            """\
            #!/usr/bin/env bash
            echo "git $*" >> "__CALL_LOG__"
            if [[ "$1" == "restore" ]]; then
              printf '%s\n' '{"days":[]}' > data/daily_summary.json
              printf '%s\n' '{"paused":false,"risk_off":false}' > data/runtime_recovery.json
            fi
            if [[ "$1" == "pull" && "${FAKE_GIT_PULL_FAIL:-0}" == "1" ]]; then
              exit 1
            fi
            exit 0
            """
        ).replace("__CALL_LOG__", str(call_log))
        sudo_script = textwrap.dedent(
            """\
            #!/usr/bin/env bash
            echo "sudo $*" >> "__CALL_LOG__"
            exit 0
            """
        ).replace("__CALL_LOG__", str(call_log))

        for name, content in {"git": git_script, "sudo": sudo_script}.items():
            path = fakebin / name
            path.write_text(content, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

        return fakebin, call_log

    def _make_bot_dir(self, tmp: Path) -> Path:
        bot_dir = tmp / "bot"
        data_dir = bot_dir / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "runtime_recovery.json").write_text(
            json.dumps({"paused": True, "risk_off": True}),
            encoding="utf-8",
        )
        (data_dir / "daily_summary.json").write_text(
            json.dumps({"days": [{"day": "live"}]}),
            encoding="utf-8",
        )
        return bot_dir

    def _run_sync(self, bot_dir: Path, fakebin: Path, pull_fail: bool = False) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PATH"] = f"{fakebin}{os.pathsep}{env['PATH']}"
        if pull_fail:
            env["FAKE_GIT_PULL_FAIL"] = "1"
        return subprocess.run(
            ["bash", str(SYNC_SCRIPT), str(bot_dir)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_sync_preserves_runtime_jsons_and_orders_services(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            fakebin, call_log = self._make_fake_tools(tmp)
            bot_dir = self._make_bot_dir(tmp)

            result = self._run_sync(bot_dir, fakebin)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads((bot_dir / "data/runtime_recovery.json").read_text(encoding="utf-8")),
                {"paused": True, "risk_off": True},
            )
            self.assertEqual(
                json.loads((bot_dir / "data/daily_summary.json").read_text(encoding="utf-8")),
                {"days": [{"day": "live"}]},
            )
            calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertLess(
                calls.index("sudo systemctl stop safety-watchdog"),
                calls.index("sudo systemctl stop krypto-bot"),
            )
            self.assertIn("git fetch origin main", calls)
            self.assertLess(
                calls.index("sudo systemctl start krypto-bot"),
                calls.index("sudo systemctl start safety-watchdog"),
            )

    def test_sync_restores_runtime_jsons_and_restarts_services_after_pull_failure(self):
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            fakebin, call_log = self._make_fake_tools(tmp)
            bot_dir = self._make_bot_dir(tmp)

            result = self._run_sync(bot_dir, fakebin, pull_fail=True)

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                json.loads((bot_dir / "data/runtime_recovery.json").read_text(encoding="utf-8")),
                {"paused": True, "risk_off": True},
            )
            calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("sudo systemctl start krypto-bot", calls)
            self.assertIn("sudo systemctl start safety-watchdog", calls)


if __name__ == "__main__":
    unittest.main()
