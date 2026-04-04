"""
Zentraler Runtime-State für Control-Interfaces (Telegram, später UI/API).

Ziele:
- Thread-sicherer Zugriff auf zentrale Bot-Laufzeitdaten
- Entkoppelte Datenquelle für Status-/Summary-Befehle
- Keine Abhängigkeit von Telegram-spezifischer Logik
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import threading
from typing import Deque, Dict, List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: bool = False
        self._mode: str = "paper"
        self._paused: bool = False
        self._risk_off: bool = False
        self._active_strategy: str = "n/a"
        self._enabled_strategies: List[str] = []
        self._balance: float = 0.0
        self._equity: float = 0.0
        self._available_capital: float = 0.0
        self._health_status: str = "n/a"
        self._total_trades: int = 0
        self._open_positions: List[Dict] = []
        self._last_signal: Dict = {}
        self._last_decision: Dict = {}
        self._brain: Dict = {}
        self._app_context: Dict = {}
        self._performance: Dict = {}
        self._recent_trades: Deque[Dict] = deque(maxlen=20)
        self._recent_logs: Deque[str] = deque(maxlen=50)
        self._updated_at: str = _utc_now()

    def update_engine(
        self,
        *,
        running: Optional[bool] = None,
        mode: Optional[str] = None,
        paused: Optional[bool] = None,
        risk_off: Optional[bool] = None,
        active_strategy: Optional[str] = None,
        enabled_strategies: Optional[List[str]] = None,
        balance: Optional[float] = None,
        equity: Optional[float] = None,
        available_capital: Optional[float] = None,
        health_status: Optional[str] = None,
        total_trades: Optional[int] = None,
        open_positions: Optional[List[Dict]] = None,
        last_signal: Optional[Dict] = None,
        last_decision: Optional[Dict] = None,
    ) -> None:
        with self._lock:
            if running is not None:
                self._running = bool(running)
            if mode is not None:
                self._mode = str(mode)
            if paused is not None:
                self._paused = bool(paused)
            if risk_off is not None:
                self._risk_off = bool(risk_off)
            if active_strategy is not None:
                self._active_strategy = str(active_strategy)
            if enabled_strategies is not None:
                self._enabled_strategies = list(enabled_strategies)
            if balance is not None:
                self._balance = float(balance)
            if equity is not None:
                self._equity = float(equity)
            if available_capital is not None:
                self._available_capital = float(available_capital)
            if health_status is not None:
                self._health_status = str(health_status)
            if total_trades is not None:
                self._total_trades = int(total_trades)
            if open_positions is not None:
                self._open_positions = list(open_positions)
            if last_signal is not None:
                self._last_signal = dict(last_signal)
            if last_decision is not None:
                self._last_decision = dict(last_decision)
            self._updated_at = _utc_now()

    def update_brain(self, brain: Dict) -> None:
        with self._lock:
            self._brain = dict(brain or {})
            self._updated_at = _utc_now()

    def update_app_context(self, app_context: Dict) -> None:
        with self._lock:
            self._app_context = dict(app_context or {})
            self._updated_at = _utc_now()

    def update_performance(self, performance: Dict) -> None:
        with self._lock:
            self._performance = dict(performance or {})
            self._updated_at = _utc_now()

    def set_last_signal(self, signal: Dict) -> None:
        with self._lock:
            self._last_signal = dict(signal or {})
            self._updated_at = _utc_now()

    def set_last_decision(self, decision: Dict) -> None:
        with self._lock:
            self._last_decision = dict(decision or {})
            self._updated_at = _utc_now()

    def append_trade(self, trade: Dict) -> None:
        with self._lock:
            self._recent_trades.appendleft(dict(trade))
            self._updated_at = _utc_now()

    def append_log(self, message: str) -> None:
        cleaned = str(message).strip()
        if not cleaned:
            return
        with self._lock:
            self._recent_logs.appendleft(cleaned)
            self._updated_at = _utc_now()

    def snapshot(self) -> Dict:
        with self._lock:
            return {
                "running": self._running,
                "mode": self._mode,
                "paused": self._paused,
                "risk_off": self._risk_off,
                "active_strategy": self._active_strategy,
                "enabled_strategies": deepcopy(self._enabled_strategies),
                "balance": self._balance,
                "equity": self._equity,
                "available_capital": self._available_capital,
                "health_status": self._health_status,
                "total_trades": self._total_trades,
                "open_positions": deepcopy(self._open_positions),
                "last_signal": deepcopy(self._last_signal),
                "last_decision": deepcopy(self._last_decision),
                "brain": deepcopy(self._brain),
                "app_context": deepcopy(self._app_context),
                "performance": deepcopy(self._performance),
                "recent_trades": deepcopy(list(self._recent_trades)),
                "recent_logs": deepcopy(list(self._recent_logs)),
                "updated_at": self._updated_at,
            }


runtime_state = RuntimeState()

