"""
Verlust-Muster-Gedächtnis: wiederholte Verluste mit derselben Strategie + Symbol
innerhalb eines Zeitfensters blockieren neue Entries (kein „blindes Wiederholen“).

Persistiert in JSON, damit es über Neustarts hinweg gilt.
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
    Speichert Verlust-Zeitstempel pro Schlüssel „Strategie|Symbol“.
    """

    def __init__(self) -> None:
        self._events: Dict[str, List[float]] = {}
        self._load()

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
        except Exception as e:
            logger.warning("LossPatternMemory: Laden fehlgeschlagen: %s", e)

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

    def record_loss(self, strategy_name: str, symbol: str) -> None:
        if not bool(getattr(settings, "LOSS_PATTERN_MEMORY_ENABLED", True)):
            return
        s = (strategy_name or "").strip()
        sym = (symbol or "").strip()
        if not s or not sym:
            return
        now = time.time()
        self._prune(now)
        key = f"{s}|{sym}"
        self._events.setdefault(key, []).append(now)
        logger.info(
            "LossPatternMemory: Verlust registriert | %s (%d in Fenster)",
            key,
            len(self._events[key]),
        )
        self._save()

    def is_blocked(self, strategy_name: str, symbol: str) -> Tuple[bool, str]:
        if not bool(getattr(settings, "LOSS_PATTERN_MEMORY_ENABLED", True)):
            return False, ""
        s = (strategy_name or "").strip()
        sym = (symbol or "").strip()
        if not s or not sym:
            return False, ""
        now = time.time()
        self._prune(now)
        key = f"{s}|{sym}"
        max_l = int(getattr(settings, "LOSS_PATTERN_MAX_LOSSES", 2) or 2)
        n = len(self._events.get(key, []))
        if n >= max_l:
            hrs = float(getattr(settings, "LOSS_PATTERN_WINDOW_HOURS", 72) or 72)
            return (
                True,
                f"LOSS PATTERN BLOCK: {n} Verluste in {hrs:.0f}h für {s} @ {sym} "
                f"(gleiches Setup nicht erneut)",
            )
        return False, ""
