"""
Runtime Control State (Control Plane)

Zentrale, thread-sichere Laufzeitsteuerung für den Bot.
Diese Schicht ist absichtlich engine-nah (nicht telegram-nah), damit
verschiedene Interfaces (Telegram, CLI, später Web-UI) dieselben Flags
setzen können.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Dict


class RuntimeControlState:
    """Thread-sicherer Zustand für Pause/Risk-Off/Strategie-Präferenz."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paused: bool = False
        self._risk_off: bool = False
        self._preferred_strategy: str = ""
        self._mode_request: str = ""
        self._last_action: str = "init"
        self._updated_at: str = datetime.now(timezone.utc).isoformat()

    def _touch(self, action: str) -> None:
        self._last_action = action
        self._updated_at = datetime.now(timezone.utc).isoformat()

    def pause_entries(self) -> None:
        with self._lock:
            self._paused = True
            self._touch("pause_entries")

    def resume_entries(self) -> None:
        with self._lock:
            self._paused = False
            self._touch("resume_entries")

    def enable_risk_off(self) -> None:
        with self._lock:
            self._risk_off = True
            self._touch("enable_risk_off")

    def disable_risk_off(self) -> None:
        with self._lock:
            self._risk_off = False
            self._touch("disable_risk_off")

    def set_preferred_strategy(self, strategy_name: str) -> None:
        with self._lock:
            self._preferred_strategy = strategy_name.strip()
            self._touch(f"set_preferred_strategy:{strategy_name}")

    def clear_preferred_strategy(self) -> None:
        with self._lock:
            self._preferred_strategy = ""
            self._touch("clear_preferred_strategy")

    def request_mode(self, mode: str) -> None:
        with self._lock:
            self._mode_request = mode.strip().lower()
            self._touch(f"request_mode:{mode}")

    def get_snapshot(self) -> Dict:
        with self._lock:
            return {
                "paused": self._paused,
                "risk_off": self._risk_off,
                "preferred_strategy": self._preferred_strategy,
                "mode_request": self._mode_request,
                "last_action": self._last_action,
                "updated_at": self._updated_at,
            }


runtime_control = RuntimeControlState()

