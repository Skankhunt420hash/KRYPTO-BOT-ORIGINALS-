"""
Win-Rate-Tracker mit Auto-Schutz

Überwacht die echte Win-Rate in Echtzeit und schützt das Kapital
wenn der Bot eine Verlust-Strähne erlebt.

Schutz-Mechanismen:
  1. Rolling Win-Rate: Letzte N Trades (kein Durchschnitts-Blending)
  2. Auto-Pause: Wenn Win-Rate unter PAUSE_THRESHOLD → Stop-Trading
  3. Cooldown: Mindestpause nach zu vielen Verlusten
  4. Recovery: Langsam wieder hochfahren wenn Win-Rate steigt

Warum das wichtig ist:
  - Ein Bot mit 40% Win-Rate und 2:1 RR macht Gewinn ABER
  - Wenn er 10 Trades in Folge verliert: -10% Balance
  - Auto-Pause verhindert Ruinierung durch schlechte Marktphasen
"""

import time
from collections import deque
from typing import Optional

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("winrate_tracker")


class WinRateTracker:
    """
    Echtzeit-Win-Rate-Überwachung mit kapitalschützenden Reaktionen.

    Verwendung:
        tracker = WinRateTracker()
        tracker.record(pnl=15.0)     # Nach jedem Trade
        if tracker.should_pause():
            return  # Kein neuer Trade
    """

    def __init__(self) -> None:
        self._window_size: int = getattr(settings, "WINRATE_WINDOW", 20)
        self._pause_threshold: float = getattr(settings, "WINRATE_PAUSE_THRESHOLD", 0.40)
        self._resume_threshold: float = getattr(settings, "WINRATE_RESUME_THRESHOLD", 0.50)
        self._max_consecutive_losses: int = getattr(settings, "MAX_CONSECUTIVE_LOSSES", 5)
        self._cooldown_sec: int = getattr(settings, "LOSS_STREAK_COOLDOWN_SEC", 1800)  # 30min

        self._results: deque = deque(maxlen=self._window_size)  # True=Win, False=Loss
        self._consecutive_losses: int = 0
        self._paused: bool = False
        self._pause_reason: str = ""
        self._paused_at: float = 0.0
        self._total_trades: int = 0
        self._total_wins: int = 0

        logger.info(
            f"[cyan]WinRate-Tracker[/cyan] | "
            f"Fenster={self._window_size} | "
            f"Pause bei <{self._pause_threshold:.0%} | "
            f"Max-Verlust-Serie={self._max_consecutive_losses}"
        )

    def record(self, pnl: float) -> None:
        """Registriert das Ergebnis eines abgeschlossenen Trades."""
        is_win = pnl > 0
        self._results.append(is_win)
        self._total_trades += 1

        if is_win:
            self._total_wins += 1
            self._consecutive_losses = 0
            # Auto-Resume wenn Win-Rate sich erholt hat
            if self._paused and self._rolling_winrate >= self._resume_threshold:
                self._paused = False
                self._pause_reason = ""
                logger.info(
                    f"[green]WinRate-Tracker: TRADING FORTGESETZT[/green] | "
                    f"Rolling Win-Rate: {self._rolling_winrate:.1%}"
                )
        else:
            self._consecutive_losses += 1

        # Prüfe ob Pause nötig
        self._check_pause()

        # Logging alle 5 Trades
        if self._total_trades % 5 == 0:
            self._log_status()

    def should_pause(self) -> tuple:
        """
        Gibt (should_pause: bool, reason: str) zurück.
        Prüft auch Cooldown-Ablauf.
        """
        # Cooldown-Ablauf prüfen
        if self._paused and self._paused_at > 0:
            elapsed = time.time() - self._paused_at
            if elapsed >= self._cooldown_sec:
                self._paused = False
                self._consecutive_losses = 0
                logger.info(
                    f"[green]WinRate-Tracker: Cooldown abgelaufen "
                    f"({elapsed/60:.0f}min) – Trading fortgesetzt[/green]"
                )
                return False, ""

        if self._paused:
            remaining = max(0, self._cooldown_sec - (time.time() - self._paused_at))
            return True, f"{self._pause_reason} | Cooldown: noch {remaining/60:.0f}min"

        return False, ""

    @property
    def _rolling_winrate(self) -> float:
        if not self._results:
            return 1.0
        return sum(self._results) / len(self._results)

    @property
    def rolling_winrate(self) -> float:
        return self._rolling_winrate

    @property
    def global_winrate(self) -> float:
        return (self._total_wins / self._total_trades) if self._total_trades > 0 else 1.0

    def get_stats(self) -> dict:
        return {
            "total_trades": self._total_trades,
            "global_winrate_pct": round(self.global_winrate * 100, 1),
            "rolling_winrate_pct": round(self._rolling_winrate * 100, 1),
            "consecutive_losses": self._consecutive_losses,
            "paused": self._paused,
            "pause_reason": self._pause_reason,
        }

    def _check_pause(self) -> None:
        """Entscheidet ob Trading pausiert werden soll."""
        if self._paused:
            return

        # 1. Zu viele Verluste in Folge
        if self._consecutive_losses >= self._max_consecutive_losses:
            self._trigger_pause(
                f"VERLUST-SERIE: {self._consecutive_losses}× in Folge verloren"
            )
            return

        # 2. Rolling Win-Rate zu niedrig (erst ab Mindest-Daten)
        if len(self._results) >= 10 and self._rolling_winrate < self._pause_threshold:
            self._trigger_pause(
                f"WIN-RATE ZU NIEDRIG: {self._rolling_winrate:.1%} < {self._pause_threshold:.0%} "
                f"(letzte {len(self._results)} Trades)"
            )

    def _trigger_pause(self, reason: str) -> None:
        self._paused = True
        self._pause_reason = reason
        self._paused_at = time.time()
        logger.warning(
            f"[red]⛔ TRADING PAUSIERT[/red] | {reason} | "
            f"Cooldown: {self._cooldown_sec//60}min"
        )

    def _log_status(self) -> None:
        rwr = self._rolling_winrate
        color = "green" if rwr >= 0.55 else "yellow" if rwr >= 0.45 else "red"
        logger.info(
            f"[{color}]📊 Win-Rate[/{color}] "
            f"Rolling({self._window_size}): {rwr:.1%} | "
            f"Global: {self.global_winrate:.1%} | "
            f"Verlust-Serie: {self._consecutive_losses} | "
            f"Trades: {self._total_trades}"
        )
