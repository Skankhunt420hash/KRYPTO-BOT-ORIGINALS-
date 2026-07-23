"""
Runtime Control State (Control Plane)

Zentrale, thread-sichere Laufzeitsteuerung für den Bot.
Diese Schicht ist absichtlich engine-nah (nicht telegram-nah), damit
verschiedene Interfaces (Telegram, CLI, später Web-UI) dieselben Flags
setzen können.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from config.settings import settings


logger = logging.getLogger(__name__)


class RuntimeControlState:
    """Thread-sicherer Zustand für Pause/Risk-Off/Strategie-Präferenz."""

    def __init__(
        self,
        state_file: Optional[Path] = None,
        persistence_enabled: Optional[bool] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._state_file_override = Path(state_file) if state_file is not None else None
        self._persistence_enabled_override = persistence_enabled
        self._last_file_signature: Optional[Tuple[int, int, int]] = None
        self._has_persisted_state = False
        self._paused: bool = False
        self._risk_off: bool = False
        self._preferred_strategy: str = ""
        self._mode_request: str = ""
        self._last_action: str = "init"
        self._updated_at: str = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._refresh_from_disk_locked()

    def _persistence_enabled(self) -> bool:
        if self._persistence_enabled_override is not None:
            return self._persistence_enabled_override
        return bool(getattr(settings, "RUNTIME_CONTROL_PERSIST_ENABLED", True))

    def _state_file(self) -> Path:
        if self._state_file_override is not None:
            return self._state_file_override
        return Path(
            getattr(settings, "RUNTIME_CONTROL_FILE", "data/runtime_control.json")
        )

    @staticmethod
    def _signature(path: Path) -> Tuple[int, int, int]:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size, getattr(stat, "st_ino", 0)

    def _refresh_from_disk_locked(self) -> None:
        if not self._persistence_enabled():
            return
        path = self._state_file()
        try:
            signature = self._signature(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.error("Runtime-Control-State kann nicht geprüft werden: %s", exc)
            return
        if signature == self._last_file_signature:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("JSON-Wurzel ist kein Objekt")
            if not isinstance(raw.get("paused"), bool) or not isinstance(
                raw.get("risk_off"), bool
            ):
                raise ValueError("paused/risk_off fehlen oder sind keine Booleans")
            self._paused = raw["paused"]
            self._risk_off = raw["risk_off"]
            self._preferred_strategy = str(raw.get("preferred_strategy") or "")
            self._mode_request = str(raw.get("mode_request") or "")
            self._last_action = str(raw.get("last_action") or "persisted_state_loaded")
            self._updated_at = str(raw.get("updated_at") or self._updated_at)
            self._has_persisted_state = True
        except Exception as exc:
            # Ein beschädigter Safety-State darf Entries nicht still freigeben.
            self._paused = True
            self._risk_off = True
            self._last_action = "invalid_persisted_state_fail_closed"
            self._updated_at = datetime.now(timezone.utc).isoformat()
            self._has_persisted_state = True
            logger.error(
                "Runtime-Control-State ist ungültig; Entries werden gesperrt: %s", exc
            )
        self._last_file_signature = signature

    def _persist_locked(self) -> bool:
        if not self._persistence_enabled():
            return True
        path = self._state_file()
        tmp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        payload = {
            "paused": self._paused,
            "risk_off": self._risk_off,
            "preferred_strategy": self._preferred_strategy,
            "mode_request": self._mode_request,
            "last_action": self._last_action,
            "updated_at": self._updated_at,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
            self._last_file_signature = self._signature(path)
            self._has_persisted_state = True
            return True
        except Exception as exc:
            logger.error("Runtime-Control-State konnte nicht gespeichert werden: %s", exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _touch(self, action: str) -> None:
        self._last_action = action
        self._updated_at = datetime.now(timezone.utc).isoformat()

    def pause_entries(self) -> bool:
        with self._lock:
            self._paused = True
            self._touch("pause_entries")
            return self._persist_locked()

    def resume_entries(self) -> bool:
        with self._lock:
            self._paused = False
            self._touch("resume_entries")
            return self._persist_locked()

    def enable_risk_off(self) -> bool:
        with self._lock:
            self._risk_off = True
            self._touch("enable_risk_off")
            return self._persist_locked()

    def disable_risk_off(self) -> bool:
        with self._lock:
            self._risk_off = False
            self._touch("disable_risk_off")
            return self._persist_locked()

    def set_preferred_strategy(self, strategy_name: str) -> bool:
        with self._lock:
            self._preferred_strategy = strategy_name.strip()
            self._touch(f"set_preferred_strategy:{strategy_name}")
            return self._persist_locked()

    def clear_preferred_strategy(self) -> bool:
        with self._lock:
            self._preferred_strategy = ""
            self._touch("clear_preferred_strategy")
            return self._persist_locked()

    def request_mode(self, mode: str) -> bool:
        with self._lock:
            self._mode_request = mode.strip().lower()
            self._touch(f"request_mode:{mode}")
            return self._persist_locked()

    def get_snapshot(self) -> Dict:
        with self._lock:
            self._refresh_from_disk_locked()
            return {
                "paused": self._paused,
                "risk_off": self._risk_off,
                "preferred_strategy": self._preferred_strategy,
                "mode_request": self._mode_request,
                "last_action": self._last_action,
                "updated_at": self._updated_at,
            }

    def has_persisted_state(self) -> bool:
        with self._lock:
            self._refresh_from_disk_locked()
            return self._has_persisted_state


runtime_control = RuntimeControlState()

