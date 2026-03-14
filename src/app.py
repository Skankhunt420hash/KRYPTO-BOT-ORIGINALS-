from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

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
        try:
            parent = os.path.dirname(lock_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            self._lock_acquired = True
            runtime_state.append_log(f"APP lock_acquired path={lock_path}")
            runtime_state.update_app_context(self._ctx_payload())
            return True
        except FileExistsError:
            logger.error(
                "Es läuft vermutlich bereits eine Instanz (Lockfile vorhanden: %s).",
                lock_path,
            )
            logger.error(
                "Stoppe die andere Instanz oder lösche das Lockfile, wenn der Prozess nicht mehr läuft."
            )
            runtime_state.append_log(f"APP lock_exists path={lock_path}")
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
