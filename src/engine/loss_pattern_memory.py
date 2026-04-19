"""
Verlust-/Erfolgs-Gedächtnis: wiederholte Verluste mit demselben Setup blockieren neue Entries;
Gewinne können ältere Verlust-Markierungen „vergeben“ (Lern-Feedback).

Persistiert in JSON, damit es über Neustarts hinweg gilt.

Schlüssel: Strategie|Symbol oder Strategie|Symbol|Regime (siehe LOSS_PATTERN_INCLUDE_REGIME).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Tuple

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("loss_pattern_memory")


def _memory_path() -> Path:
    raw = getattr(settings, "LOSS_PATTERN_MEMORY_FILE", "data/loss_pattern_memory.json")
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[2] / p
    return p


class LossPatternMemory:
    """
    Speichert Verlust-Zeitstempel pro Setup-Schlüssel.
    """

    def __init__(self) -> None:
        self._events: Dict[str, List[float]] = {}
        self._load()

    def _compose_key(self, strategy_name: str, symbol: str, regime: str) -> str:
        s = (strategy_name or "").strip()
        sym = (symbol or "").strip()
        if bool(getattr(settings, "LOSS_PATTERN_INCLUDE_REGIME", True)):
            r = (regime or "").strip() or "UNKNOWN"
            return f"{s}|{sym}|{r}"
        return f"{s}|{sym}"

    def _load(self) -> None:
        path = _memory_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out: Dict[str, List[float]] = {}
                for k, v in data.items():
                    if isinstance(v, list):
                        out[str(k)] = [float(x) for x in v]
                self._events = out
            self._migrate_legacy_keys()
        except Exception as e:
            logger.warning("LossPatternMemory: Laden fehlgeschlagen: %s", e)

    def _migrate_legacy_keys(self) -> None:
        """2-teilige Keys → 3-teilig mit UNKNOWN, wenn Regime aktiv."""
        if not bool(getattr(settings, "LOSS_PATTERN_INCLUDE_REGIME", True)):
            return
        merged: Dict[str, List[float]] = {}
        for k, v in list(self._events.items()):
            if k.count("|") == 1:
                nk = f"{k}|UNKNOWN"
                merged.setdefault(nk, []).extend(v)
            else:
                merged.setdefault(k, []).extend(v)
        self._events = merged

    def _save(self) -> None:
        path = _memory_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._events, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("LossPatternMemory: Speichern fehlgeschlagen: %s", e)

    def _window_sec(self) -> float:
        return float(getattr(settings, "LOSS_PATTERN_WINDOW_HOURS", 72) or 72) * 3600.0

    def _prune(self, now: float) -> None:
        w = self._window_sec()
        for key in list(self._events.keys()):
            self._events[key] = [t for t in self._events[key] if now - t <= w]
            if not self._events[key]:
                del self._events[key]

    def record_loss(self, strategy_name: str, symbol: str, regime: str = "") -> None:
        if not bool(getattr(settings, "LOSS_PATTERN_MEMORY_ENABLED", True)):
            return
        s = (strategy_name or "").strip()
        sym = (symbol or "").strip()
        if not s or not sym:
            return
        now = time.time()
        self._prune(now)
        key = self._compose_key(s, sym, regime)
        self._events.setdefault(key, []).append(now)
        logger.info(
            "LossPatternMemory: Verlust registriert | %s (%d im Fenster)",
            key,
            len(self._events[key]),
        )
        self._save()

    def record_win(self, strategy_name: str, symbol: str, regime: str = "") -> None:
        """
        Entfernt älteste Verlust-Markierungen (Lern-Feedback: Setup kann wieder ok sein).
        """
        if not bool(getattr(settings, "LOSS_PATTERN_MEMORY_ENABLED", True)):
            return
        forgive = int(getattr(settings, "LOSS_PATTERN_WIN_FORGIVENESS", 1) or 0)
        if forgive <= 0:
            return
        s = (strategy_name or "").strip()
        sym = (symbol or "").strip()
        if not s or not sym:
            return
        now = time.time()
        self._prune(now)
        key = self._compose_key(s, sym, regime)
        if key not in self._events or not self._events[key]:
            return
        removed = 0
        for _ in range(forgive):
            if not self._events.get(key):
                break
            self._events[key].pop(0)
            removed += 1
        if not self._events[key]:
            del self._events[key]
        logger.info(
            "LossPatternMemory: Gewinn-Feedback | %s (%d Verlust-Mark. entfernt)",
            key,
            removed,
        )
        self._save()

    def is_blocked(self, strategy_name: str, symbol: str, regime: str = "") -> Tuple[bool, str]:
        if not bool(getattr(settings, "LOSS_PATTERN_MEMORY_ENABLED", True)):
            return False, ""
        s = (strategy_name or "").strip()
        sym = (symbol or "").strip()
        if not s or not sym:
            return False, ""
        now = time.time()
        self._prune(now)
        key = self._compose_key(s, sym, regime)
        max_l = int(getattr(settings, "LOSS_PATTERN_MAX_LOSSES", 2) or 2)
        n = len(self._events.get(key, []))
        if n >= max_l:
            hrs = float(getattr(settings, "LOSS_PATTERN_WINDOW_HOURS", 72) or 72)
            return (
                True,
                f"LOSS PATTERN BLOCK: {n} Verluste in {hrs:.0f}h für {key} "
                f"(Setup nicht erneut)",
            )
        return False, ""
