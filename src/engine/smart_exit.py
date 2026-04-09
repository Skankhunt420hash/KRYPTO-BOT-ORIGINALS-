"""
Smart Exit Engine

Ersetzt statisches TP/SL durch intelligenten, adaptiven Ausstieg:

1. ATR-basierter Trailing Stop:
   - Aktiviert sich sobald Position im Gewinn (LOCK_IN_PCT überschritten)
   - Folgt dem Preis mit ATR-Abstand (verhindert vorzeitiges Schließen)
   - Gibt dem Trade Raum sich zu entfalten

2. Momentum-Exit:
   - Erkennt wenn der Preis-Momentum kippt (RSI-Übersättigung, Trendumkehr)
   - Schließt wenn keine weiteren Gewinne zu erwarten sind
   - Verhindert Gewinne wieder herzugeben

3. Time-based Safety Exit:
   - Trade der nach MAX_DURATION im Minus ist → schließen
   - Trade im Gewinn läuft so lange wie Momentum positiv bleibt
   - KEIN starres TP: maximaler Gewinn wird extrahiert

Die Idee: "Verluste schnell schneiden, Gewinne laufen lassen"
"""

import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import ta

from config.settings import settings
from src.utils.logger import setup_logger
from src.utils.risk_manager import Position

logger = setup_logger("smart_exit")


@dataclass
class TradeState:
    """Zustand eines offenen Trades für Smart Exit Tracking."""

    symbol: str
    side: str                         # "long" | "short"
    entry_price: float
    entry_time: float                  # Unix timestamp
    highest_favorable_price: float     # Bestes Preis-Level seit Entry
    trailing_stop: Optional[float]     # Aktiver Trailing Stop (None = noch nicht aktiv)
    locked_in: bool = False            # True wenn Trailing aktiv (im Gewinn)

    @property
    def duration_min(self) -> float:
        return (time.time() - self.entry_time) / 60


class SmartExitEngine:
    """
    Intelligente Exit-Logik die statisches TP/SL ersetzt.

    Für jeden offenen Trade wird ein TradeState verwaltet.
    `check_exit()` wird in jedem Zyklus aufgerufen und gibt zurück
    ob und warum ein Trade geschlossen werden soll.
    """

    def __init__(self) -> None:
        self._states: dict[str, TradeState] = {}
        self._atr_mult = settings.SMART_EXIT_ATR_MULT
        self._lock_in_pct = settings.SMART_EXIT_LOCK_IN_PCT
        self._max_duration = settings.SMART_EXIT_MAX_DURATION_MIN

    def register_trade(
        self,
        symbol: str,
        side: str,
        entry_price: float,
    ) -> None:
        """Registriert einen neuen Trade für Smart-Exit-Tracking."""
        self._states[symbol] = TradeState(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            entry_time=time.time(),
            highest_favorable_price=entry_price,
            trailing_stop=None,
        )
        logger.debug(f"SmartExit registriert: {symbol} {side} @ {entry_price:.6f}")

    def unregister_trade(self, symbol: str) -> None:
        """Entfernt Trade aus Smart-Exit-Tracking (nach Schließung)."""
        self._states.pop(symbol, None)

    def check_exit(
        self,
        symbol: str,
        current_price: float,
        df: pd.DataFrame,
    ) -> Tuple[bool, str]:
        """
        Prüft ob ein Trade geschlossen werden soll.

        Returns:
            (should_close: bool, reason: str)

        Wird aufgerufen NACHDEM check_exit_conditions() des RiskManagers
        gelaufen ist (statische SL werden weiterhin respektiert).
        """
        if not settings.SMART_EXIT_ENABLED:
            return False, ""

        state = self._states.get(symbol)
        if state is None:
            return False, ""

        # ATR für Trailing Stop berechnen
        atr = self._calc_atr(df)

        # Aktuellen unrealisierten Gewinn berechnen
        if state.side == "long":
            pnl_pct = (current_price - state.entry_price) / state.entry_price * 100
            favorable = current_price > state.highest_favorable_price
        else:  # short
            pnl_pct = (state.entry_price - current_price) / state.entry_price * 100
            favorable = current_price < state.highest_favorable_price

        # Bestes Level aktualisieren
        if favorable:
            state.highest_favorable_price = current_price

        # ── 1. Trailing Stop aktivieren wenn Lock-In-Level erreicht ──────────
        if pnl_pct >= self._lock_in_pct and not state.locked_in:
            state.locked_in = True
            trailing_dist = atr * self._atr_mult
            if state.side == "long":
                state.trailing_stop = current_price - trailing_dist
            else:
                state.trailing_stop = current_price + trailing_dist
            logger.info(
                f"[green]SmartExit TRAILING AKTIV[/green] {symbol} | "
                f"Gewinn: {pnl_pct:.2f}% | "
                f"Trailing-Stop: {state.trailing_stop:.6f} | "
                f"ATR-Dist: {trailing_dist:.6f}"
            )

        # ── 2. Trailing Stop nachziehen ───────────────────────────────────────
        if state.locked_in and state.trailing_stop is not None:
            trailing_dist = atr * self._atr_mult
            if state.side == "long":
                new_trail = current_price - trailing_dist
                if new_trail > state.trailing_stop:
                    state.trailing_stop = new_trail
                # Trailing Stop getroffen?
                if current_price <= state.trailing_stop:
                    return True, f"smart_trailing_stop (Gewinn geschützt: +{pnl_pct:.2f}%)"
            else:  # short
                new_trail = current_price + trailing_dist
                if new_trail < state.trailing_stop:
                    state.trailing_stop = new_trail
                # Trailing Stop getroffen?
                if current_price >= state.trailing_stop:
                    return True, f"smart_trailing_stop (Gewinn geschützt: +{pnl_pct:.2f}%)"

        # ── 3. Momentum-Exit: RSI-Erschöpfung erkennen ────────────────────────
        momentum_exit, momentum_reason = self._check_momentum_exit(
            df, state.side, pnl_pct
        )
        if momentum_exit and pnl_pct > 0:
            return True, f"smart_momentum_exit: {momentum_reason}"

        # ── 4. Time-based Safety Exit ─────────────────────────────────────────
        if state.duration_min > self._max_duration:
            if pnl_pct < 0:
                return True, (
                    f"smart_time_exit: {state.duration_min:.0f}min im Minus "
                    f"({pnl_pct:.2f}%) – kein Momentum"
                )
            elif pnl_pct < 0.1 and not state.locked_in:
                # Breakeven nach langer Zeit → schließen
                return True, (
                    f"smart_time_exit: {state.duration_min:.0f}min, kein Trend"
                )

        return False, ""

    def get_trailing_stop(self, symbol: str) -> Optional[float]:
        """Gibt aktiven Trailing Stop zurück (für Logging)."""
        state = self._states.get(symbol)
        return state.trailing_stop if state else None

    def get_trade_info(self, symbol: str) -> Optional[dict]:
        """Gibt aktuelle Trade-State-Info zurück."""
        state = self._states.get(symbol)
        if not state:
            return None
        return {
            "duration_min": round(state.duration_min, 1),
            "locked_in": state.locked_in,
            "trailing_stop": state.trailing_stop,
            "best_price": state.highest_favorable_price,
        }

    # ── Interne Berechnungen ──────────────────────────────────────────────────

    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """Berechnet ATR als Maß für Volatilität/Preis-Bewegungsraum."""
        if df is None or len(df) < period + 1:
            return 0.0
        try:
            atr_series = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=period
            ).average_true_range()
            return float(atr_series.iloc[-1])
        except Exception:
            # Fallback: einfache Range
            return float((df["high"] - df["low"]).tail(period).mean())

    def _check_momentum_exit(
        self,
        df: pd.DataFrame,
        side: str,
        pnl_pct: float,
    ) -> Tuple[bool, str]:
        """
        Erkennt Momentum-Erschöpfung durch RSI-Übersättigung.

        LONG: RSI > 75 nach Gewinn → Überkauft, Exit
        SHORT: RSI < 25 nach Gewinn → Überverkauft, Exit
        """
        if df is None or len(df) < 20:
            return False, ""

        try:
            rsi_series = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
            rsi = float(rsi_series.iloc[-1])
            rsi_prev = float(rsi_series.iloc[-2])

            if side == "long" and rsi > 75 and rsi < rsi_prev and pnl_pct > 0.2:
                return True, f"RSI={rsi:.0f} dreht von Überkauft"
            elif side == "short" and rsi < 25 and rsi > rsi_prev and pnl_pct > 0.2:
                return True, f"RSI={rsi:.0f} dreht von Überverkauft"

        except Exception:
            pass

        return False, ""
