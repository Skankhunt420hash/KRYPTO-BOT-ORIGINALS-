from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from typing import Dict, Optional, Tuple

import psutil

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("controller.supervisor")


class BotProcessSupervisor:
    """
    Startet/stoppt den Hauptbot als separaten Prozess.
    Hält einen PID-File und räumt stale Einträge automatisch auf.
    """

    def __init__(self) -> None:
        self._project_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        self._pidfile = os.path.abspath(settings.SUPERVISOR_PIDFILE)
        self._bot_logfile = os.path.abspath(settings.SUPERVISOR_BOT_LOGFILE)

    def start(self) -> Tuple[bool, str]:
        running, info = self._get_running_process()
        if running:
            pid = info.get("pid")
            return False, f"Bot läuft bereits (PID {pid})."

        self._cleanup_pidfile_if_present()

        try:
            os.makedirs(os.path.dirname(self._pidfile), exist_ok=True)
            os.makedirs(os.path.dirname(self._bot_logfile), exist_ok=True)

            child_env = os.environ.copy()
            # WICHTIG: Nur der Controller pollt Telegram-Befehle.
            # Der Bot selbst sendet weiterhin Notifications, pollt aber nicht.
            child_env["TELEGRAM_PANEL_ENABLED"] = "false"

            cmd = [sys.executable, "main.py"] + self._parse_bot_args()
            log_handle = open(self._bot_logfile, "a", encoding="utf-8")
            proc = subprocess.Popen(
                cmd,
                cwd=self._project_root,
                env=child_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            self._write_pidfile(proc.pid, cmd)

            # Kurz prüfen, ob Prozess sofort stirbt.
            time.sleep(1.2)
            exit_code = proc.poll()
            if exit_code is not None:
                self._cleanup_pidfile_if_present()
                return (
                    False,
                    f"Bot konnte nicht starten (Exit-Code {exit_code}). "
                    f"Siehe {self._bot_logfile}.",
                )
            return True, f"Bot gestartet (PID {proc.pid})."
        except Exception as e:
            self._cleanup_pidfile_if_present()
            logger.error("Bot-Start fehlgeschlagen: %s", e)
            return False, f"Bot-Start fehlgeschlagen: {e}"

    def stop(self) -> Tuple[bool, str]:
        running, info = self._get_running_process()
        if not running:
            self._cleanup_pidfile_if_present()
            return False, "Bot läuft nicht."

        pid = int(info["pid"])
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            self._cleanup_pidfile_if_present()
            return True, f"Bot gestoppt (PID {pid})."
        except psutil.NoSuchProcess:
            self._cleanup_pidfile_if_present()
            return True, f"Bot-Prozess war bereits beendet (PID {pid})."
        except Exception as e:
            logger.error("Bot-Stop fehlgeschlagen (PID %s): %s", pid, e)
            return False, f"Bot-Stop fehlgeschlagen: {e}"

    def restart(self) -> Tuple[bool, str]:
        _, _ = self.stop()
        ok, msg = self.start()
        if ok:
            return True, f"Bot neu gestartet. {msg}"
        return False, f"Bot-Neustart fehlgeschlagen. {msg}"

    def status(self) -> Dict:
        running, info = self._get_running_process()
        if running:
            pid = int(info["pid"])
            started_at = info.get("started_at")
            uptime_s = None
            if started_at:
                try:
                    uptime_s = max(0, int(time.time()) - int(started_at))
                except Exception:
                    uptime_s = None
            return {
                "running": True,
                "pid": pid,
                "uptime_sec": uptime_s,
                "pidfile": self._pidfile,
                "bot_logfile": self._bot_logfile,
            }
        return {
            "running": False,
            "pid": None,
            "uptime_sec": None,
            "pidfile": self._pidfile,
            "bot_logfile": self._bot_logfile,
        }

    def _parse_bot_args(self) -> list[str]:
        raw = (settings.SUPERVISOR_BOT_ARGS or "").strip()
        if not raw:
            return ["--multi", "--interval", "60"]
        return shlex.split(raw, posix=(os.name != "nt"))

    def _read_pidfile(self) -> Dict:
        if not os.path.exists(self._pidfile):
            return {}
        try:
            with open(self._pidfile, "r", encoding="utf-8", errors="ignore") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]
            parsed: Dict[str, str] = {}
            for ln in lines:
                if "=" in ln:
                    k, v = ln.split("=", 1)
                    parsed[k.strip()] = v.strip()
            # Rückwärtskompatibel: alte Datei nur mit PID
            if "pid" not in parsed and lines:
                try:
                    int(lines[0])
                    parsed["pid"] = lines[0]
                except Exception:
                    pass
            return parsed
        except Exception:
            return {}

    def _write_pidfile(self, pid: int, cmd: list[str]) -> None:
        with open(self._pidfile, "w", encoding="utf-8") as f:
            f.write(f"pid={pid}\n")
            f.write(f"started_at={int(time.time())}\n")
            f.write(f"cmd={' '.join(cmd)}\n")
            f.write(f"cwd={self._project_root}\n")

    def _cleanup_pidfile_if_present(self) -> None:
        try:
            if os.path.exists(self._pidfile):
                os.remove(self._pidfile)
        except Exception as e:
            logger.warning("Konnte PID-File nicht entfernen (%s): %s", self._pidfile, e)

    def _get_running_process(self) -> Tuple[bool, Dict]:
        info = self._read_pidfile()
        pid_raw = info.get("pid")
        if not pid_raw:
            return False, {}
        try:
            pid = int(pid_raw)
        except Exception:
            self._cleanup_pidfile_if_present()
            return False, {}

        if pid == os.getpid():
            self._cleanup_pidfile_if_present()
            return False, {}

        try:
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                self._cleanup_pidfile_if_present()
                return False, {}
            # Plausibilitätscheck: sollte python + main.py im cmdline enthalten
            try:
                cmdline = " ".join(proc.cmdline()).lower()
            except Exception:
                cmdline = ""
            if "python" not in (proc.name() or "").lower() and "python" not in cmdline:
                self._cleanup_pidfile_if_present()
                return False, {}
            if "main.py" not in cmdline:
                self._cleanup_pidfile_if_present()
                return False, {}
            info["pid"] = pid
            return True, info
        except psutil.NoSuchProcess:
            self._cleanup_pidfile_if_present()
            return False, {}
        except Exception:
            # Konservativ: wenn PID-Prüfung fehlschlägt -> stale cleanup erlauben
            self._cleanup_pidfile_if_present()
            return False, {}
