from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional
import psutil

from config.settings import settings
from src.bot import MultiStrategyBot, TradingBot
from src.engine.runtime_state import runtime_state
from src.utils.logger import setup_logger

logger = setup_logger("app")


@dataclass
class AppContext:
    mode: str
    use_multi: bool
    interval_seconds: Optional[int]
    running: bool = False
    bot_kind: str = "single"
    lockfile_path: str = ""


class TradingApplication:
    """
    Dünne App-Orchestrierung für lokalen Betrieb:
    - Einheitlicher Start/Stop-Pfad
    - Optionaler Single-Instance-Lock (vermeidet Telegram getUpdates 409)
    - RuntimeState-Spiegelung auf App-Ebene
    """

    def __init__(self, *, use_multi: bool, interval_seconds: Optional[int]) -> None:
        self.ctx = AppContext(
            mode=settings.TRADING_MODE,
            use_multi=use_multi,
            interval_seconds=interval_seconds,
            bot_kind="multi" if use_multi else "single",
            lockfile_path=settings.APP_INSTANCE_LOCKFILE,
        )
        self._bot: Optional[object] = None
        self._lock_acquired: bool = False
        runtime_state.update_app_context(self._ctx_payload())

    @property
    def bot(self):
        return self._bot

    def create_bot(self, *, autostart_services: bool = True):
        if self._bot is not None:
            return self._bot
        self._bot = MultiStrategyBot(autostart_services=autostart_services) if self.ctx.use_multi else TradingBot(
            autostart_services=autostart_services
        )
        runtime_state.append_log(
            f"APP bot_created kind={self.ctx.bot_kind} mode={self.ctx.mode}"
        )
        runtime_state.update_app_context(self._ctx_payload())
        return self._bot

    def acquire_lock(self) -> bool:
        if not settings.ENFORCE_SINGLE_INSTANCE:
            return True
        lock_path = self.ctx.lockfile_path
        parent = os.path.dirname(lock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # 1) Optimistischer Versuch: Lock atomar erstellen
        try:
            self._write_new_lockfile(lock_path)
            self._lock_acquired = True
            runtime_state.append_log(f"APP lock_acquired path={lock_path}")
            runtime_state.update_app_context(self._ctx_payload())
            return True
        except FileExistsError:
            # 2) Es gibt bereits ein Lockfile: prüfen, ob stale
            can_cleanup, reason = self._is_stale_lock(lock_path)
            if can_cleanup:
                try:
                    os.remove(lock_path)
                    runtime_state.append_log(
                        f"APP stale_lock_removed path={lock_path} reason={reason}"
                    )
                    logger.warning(
                        "Stale Lock erkannt und entfernt (%s): %s",
                        reason,
                        lock_path,
                    )
                except Exception as e:
                    logger.error(
                        "Konnte stale Lock nicht entfernen (%s): %s",
                        lock_path,
                        e,
                    )
                    return False

                # 3) Nach Cleanup exakt einmal erneut atomar versuchen
                try:
                    self._write_new_lockfile(lock_path)
                    self._lock_acquired = True
                    runtime_state.append_log(
                        f"APP lock_reacquired_after_cleanup path={lock_path}"
                    )
                    runtime_state.update_app_context(self._ctx_payload())
                    return True
                except FileExistsError:
                    logger.error(
                        "Start blockiert: Lock wurde parallel neu belegt (%s).",
                        lock_path,
                    )
                    runtime_state.append_log(f"APP lock_race path={lock_path}")
                    runtime_state.update_app_context(self._ctx_payload())
                    return False

            holder = self._read_lock_pid(lock_path)
            holder_msg = f"PID={holder}" if holder else "PID=unbekannt"
            logger.error(
                "Es läuft bereits eine Instanz (%s, Lockfile: %s).",
                holder_msg,
                lock_path,
            )
            logger.error(
                "Bitte laufende Instanz beenden. Falls die PID nicht existiert, wird "
                "das Lock beim nächsten Start automatisch bereinigt."
            )
            runtime_state.append_log(f"APP lock_exists path={lock_path} holder={holder_msg}")
            runtime_state.update_app_context(self._ctx_payload())
            return False
        except Exception as e:
            logger.error("Konnte Lockfile nicht erstellen: %s", e)
            return False

    def release_lock(self) -> None:
        if not self._lock_acquired:
            return
        try:
            os.remove(self.ctx.lockfile_path)
            self._lock_acquired = False
            runtime_state.append_log("APP lock_released")
            runtime_state.update_app_context(self._ctx_payload())
        except FileNotFoundError:
            self._lock_acquired = False
        except Exception as e:
            logger.warning("Lockfile konnte nicht entfernt werden: %s", e)

    def run_forever(self) -> None:
        if not self.acquire_lock():
            raise RuntimeError("single_instance_lock_failed")
        bot = self.create_bot(autostart_services=True)
        self.ctx.running = True
        runtime_state.update_app_context(self._ctx_payload())
        runtime_state.append_log(
            f"APP start loop kind={self.ctx.bot_kind} interval={self.ctx.interval_seconds}"
        )
        try:
            bot.run(interval_seconds=self.ctx.interval_seconds)
        finally:
            self.ctx.running = False
            runtime_state.update_app_context(self._ctx_payload())
            self.release_lock()

    def run_once(self, *, autostart_services: bool = False) -> None:
        bot = self.create_bot(autostart_services=autostart_services)
        bot.run_cycle()

    def stop(self) -> None:
        if self._bot is None:
            self.release_lock()
            return
        try:
            self._bot.stop()
        finally:
            self.ctx.running = False
            runtime_state.update_app_context(self._ctx_payload())
            self.release_lock()

    def _ctx_payload(self) -> dict:
        return {
            "mode": self.ctx.mode,
            "use_multi": self.ctx.use_multi,
            "interval_seconds": self.ctx.interval_seconds,
            "running": self.ctx.running,
            "bot_kind": self.ctx.bot_kind,
            "lockfile_path": self.ctx.lockfile_path,
            "lock_acquired": self._lock_acquired,
        }

    def _write_new_lockfile(self, lock_path: str) -> None:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                f"pid={os.getpid()}\n"
                f"created_at={int(time.time())}\n"
                f"cwd={os.getcwd()}\n"
            )

    @staticmethod
    def _read_lock_pid(lock_path: str) -> Optional[int]:
        try:
            with open(lock_path, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read().strip()
            if not raw:
                return None
            first_line = raw.splitlines()[0].strip()
            if first_line.startswith("pid="):
                first_line = first_line.split("=", 1)[1].strip()
            return int(first_line)
        except Exception:
            return None

    def _is_stale_lock(self, lock_path: str) -> tuple[bool, str]:
        pid = self._read_lock_pid(lock_path)
        if pid is None:
            return True, "invalid_or_empty_lockfile"
        if pid == os.getpid():
            return True, "same_pid_reentrant_lock"
        try:
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return True, "pid_not_running"
            # PID lebt: prüfen, ob es plausibel dieselbe App ist.
            try:
                cmd = " ".join(proc.cmdline()).lower()
            except Exception:
                cmd = ""
            try:
                proc_cwd = str(proc.cwd()).lower()
            except Exception:
                proc_cwd = ""
            here = os.getcwd().lower()
            is_python = "python" in (proc.name() or "").lower()
            same_project = (here and proc_cwd and here == proc_cwd) or ("main.py" in cmd)
            if is_python and same_project:
                return False, "active_instance_detected"
            # PID-Reuse oder fremder Prozess -> stale Lock
            return True, "pid_reused_or_foreign_process"
        except psutil.NoSuchProcess:
            return True, "pid_not_found"
        except Exception:
            # Konservativ: bei unerwarteten Fehlern lieber nicht blockieren
            return True, "pid_check_failed"
