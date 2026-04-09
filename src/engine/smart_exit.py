"""
Smart Exit Engine – Strategie-basiertes intelligentes Exit-Management

Jede Strategie hat eine eigene "Exit-Persönlichkeit":

  RangeReversion:    Schnell schliessen. Mean-Reversion ist kurz. Kein Trailing.
                     Wenn Preis nicht zur Mitte dreht → raus nach 20min.

  MomentumPullback:  Mittelschnell. Pullback-Trades müssen schnell funktionieren.
                     Enges Trailing (1× ATR). Max 45min.

  VolatilityBreakout: Ausbruch-Trade – kann laufen wenn Volumen hält.
                      Mittleres Trailing (1.5× ATR). Max 90min.

  TrendContinuation:  Trend-Folger – gibt dem Trend maximalen Raum.
                      Weites Trailing (2× ATR). Max 180min.
                      Schließt erst wenn EMA-Alignment bricht oder MACD flippt.

Universale Regeln (alle Strategien):
  - Break-Even Stop: sobald Trade +LOCK_IN_PCT erreicht → SL auf Entry verschieben
    (damit worst case = 0%, nie wieder negativ nach gutem Start)
  - Trailing Stop: folgt dem Preis mit ATR-Abstand (strategie-spezifisch)
  - Momentum-Erschöpfung: RSI-Extremwert der dreht → Exit
  - Keine starren TPs: Gewinne maximieren, Verluste begrenzen
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import pandas as pd
import ta

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("smart_exit")


# ─────────────────────────────────────────────────────────────────────────────
# Strategie-Profile
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StrategyProfile:
    """Exit-Persönlichkeit einer Strategie."""
    name: str
    max_duration_min: int       # Maximale Trade-Dauer (im Verlust)
    atr_trail_mult: float       # ATR-Multiplikator für Trailing Stop
    lock_in_pct: float          # Gewinn (%) ab dem Trailing/BreakEven aktiviert
    use_trailing: bool          # Trailing Stop aktiv?
    use_momentum_exit: bool     # RSI-Momentum-Exit aktiv?
    flat_exit_min: int          # Nach X Minuten flat/kein Fortschritt → raus
    rsi_overbought: float       # RSI Level für Momentum-Exit (LONG)
    rsi_oversold: float         # RSI Level für Momentum-Exit (SHORT)
    ema_exit: bool              # EMA-Alignment-Bruch als Exit nutzen?


_PROFILES: Dict[str, StrategyProfile] = {
    # ── Range Reversion ──────────────────────────────────────────────────
    # Mean-Reversion: Preis geht zur BB-Mitte zurück. Schnell oder nie.
    "RangeReversion": StrategyProfile(
        name="RangeReversion",
        max_duration_min=20,
        atr_trail_mult=0.8,
        lock_in_pct=0.2,
        use_trailing=False,     # Kein Trailing – TP trifft oder Zeit läuft ab
        use_momentum_exit=True,
        flat_exit_min=10,       # 10min ohne Fortschritt → raus
        rsi_overbought=70,
        rsi_oversold=30,
        ema_exit=False,
    ),

    # ── Momentum Pullback ────────────────────────────────────────────────
    # Pullback-Trade: muss schnell wieder in Richtung des Trends drehen.
    "MomentumPullback": StrategyProfile(
        name="MomentumPullback",
        max_duration_min=45,
        atr_trail_mult=1.0,
        lock_in_pct=0.25,
        use_trailing=True,
        use_momentum_exit=True,
        flat_exit_min=20,
        rsi_overbought=72,
        rsi_oversold=28,
        ema_exit=True,
    ),

    # ── Volatility Breakout ──────────────────────────────────────────────
    # Ausbruch aus Squeeze: kann stark laufen wenn Volumen da ist.
    "VolatilityBreakout": StrategyProfile(
        name="VolatilityBreakout",
        max_duration_min=90,
        atr_trail_mult=1.5,
        lock_in_pct=0.3,
        use_trailing=True,
        use_momentum_exit=True,
        flat_exit_min=30,
        rsi_overbought=75,
        rsi_oversold=25,
        ema_exit=False,
    ),

    # ── Trend Continuation ───────────────────────────────────────────────
    # Trend-Folger: maximaler Raum, schließt nur wenn Trend bricht.
    "TrendContinuation": StrategyProfile(
        name="TrendContinuation",
        max_duration_min=180,
        atr_trail_mult=2.0,
        lock_in_pct=0.35,
        use_trailing=True,
        use_momentum_exit=False,  # EMA-Exit ist besser für Trend
        flat_exit_min=60,
        rsi_overbought=80,
        rsi_oversold=20,
        ema_exit=True,
    ),

    # ── Default (unbekannte Strategie) ───────────────────────────────────
    "__default__": StrategyProfile(
        name="default",
        max_duration_min=60,
        atr_trail_mult=1.2,
        lock_in_pct=0.3,
        use_trailing=True,
        use_momentum_exit=True,
        flat_exit_min=25,
        rsi_overbought=72,
        rsi_oversold=28,
        ema_exit=False,
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Trade State
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeState:
    """Vollständiger Zustand eines offenen Trades."""

    symbol: str
    side: str                          # "long" | "short"
    strategy_name: str
    entry_price: float
    entry_time: float                   # Unix timestamp
    profile: StrategyProfile

    # Tracking
    highest_favorable_price: float = field(default=0.0)
    lowest_favorable_price: float = field(default=0.0)
    break_even_active: bool = False     # SL auf Entry verschoben
    trailing_stop: Optional[float] = None
    trailing_active: bool = False
    best_pnl_pct: float = 0.0          # Bisher bester unrealisierter Gewinn

    def __post_init__(self):
        self.highest_favorable_price = self.entry_price
        self.lowest_favorable_price = self.entry_price

    @property
    def duration_min(self) -> float:
        return (time.time() - self.entry_time) / 60

    @property
    def current_pnl_pct(self) -> float:
        """Aktueller unrealisierter PnL in % (wird extern gesetzt)."""
        return self._current_pnl_pct if hasattr(self, '_current_pnl_pct') else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Smart Exit Engine
# ─────────────────────────────────────────────────────────────────────────────

class SmartExitEngine:
    """
    Strategie-basierter intelligenter Exit-Manager.

    Für jede offene Position wird ein TradeState mit dem passenden
    StrategyProfile verwaltet. check_exit() gibt zurück ob und warum
    ein Trade geschlossen werden soll.

    Reihenfolge der Exit-Checks:
    1. Break-Even Stop (SL auf Entry wenn Gewinn erreicht)
    2. Trailing Stop (strategie-spezifische ATR-Distanz)
    3. Momentum-Erschöpfung (RSI dreht von Extremwert)
    4. EMA-Alignment-Bruch (für Trend-Strategien)
    5. Flat-Exit (kein Fortschritt nach X Minuten)
    6. Time-Safety-Exit (im Verlust nach max_duration_min)
    """

    def __init__(self) -> None:
        self._states: Dict[str, TradeState] = {}

    def register_trade(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        strategy_name: str = "",
    ) -> None:
        """Registriert einen neuen Trade."""
        profile = _PROFILES.get(strategy_name, _PROFILES["__default__"])
        self._states[symbol] = TradeState(
            symbol=symbol,
            side=side,
            strategy_name=strategy_name,
            entry_price=entry_price,
            entry_time=time.time(),
            profile=profile,
        )
        logger.info(
            f"[cyan]SmartExit[/cyan] {symbol} [{side.upper()}] | "
            f"Strategie: {strategy_name} | "
            f"Profil: max={profile.max_duration_min}min "
            f"trail={'✓' if profile.use_trailing else '✗'} "
            f"ATR×{profile.atr_trail_mult} "
            f"flat={profile.flat_exit_min}min"
        )

    def unregister_trade(self, symbol: str) -> None:
        """Entfernt Trade (nach Schließung)."""
        self._states.pop(symbol, None)

    def check_exit(
        self,
        symbol: str,
        current_price: float,
        df: pd.DataFrame,
    ) -> Tuple[bool, str]:
        """
        Hauptmethode: prüft ob Trade geschlossen werden soll.

        Returns:
            (should_close: bool, reason: str)
        """
        if not settings.SMART_EXIT_ENABLED:
            return False, ""

        state = self._states.get(symbol)
        if state is None:
            return False, ""

        profile = state.profile

        # Indikatoren berechnen (einmalig)
        atr = self._calc_atr(df)
        pnl_pct = self._calc_pnl_pct(state.entry_price, current_price, state.side)

        # Besten PnL tracken
        if pnl_pct > state.best_pnl_pct:
            state.best_pnl_pct = pnl_pct

        # Bestes/schlechtestes Preis-Level aktualisieren
        if state.side == "long":
            if current_price > state.highest_favorable_price:
                state.highest_favorable_price = current_price
        else:
            if current_price < state.lowest_favorable_price or state.lowest_favorable_price == state.entry_price:
                state.lowest_favorable_price = current_price

        # ── 1. Break-Even Stop ────────────────────────────────────────────────
        # Sobald Gewinn erreicht → SL auf Entry. Worst Case = 0%.
        if pnl_pct >= profile.lock_in_pct and not state.break_even_active:
            state.break_even_active = True
            logger.info(
                f"[green]BREAK-EVEN aktiv[/green] {symbol} | "
                f"Gewinn: +{pnl_pct:.2f}% → SL auf Entry {state.entry_price:.6f}"
            )

        # Break-Even prüfen (ist Preis wieder unter Entry?)
        if state.break_even_active:
            if state.side == "long" and current_price <= state.entry_price * 0.9998:
                return True, f"smart_break_even (SL auf Entry, Preis dreht)"
            elif state.side == "short" and current_price >= state.entry_price * 1.0002:
                return True, f"smart_break_even (SL auf Entry, Preis dreht)"

        # ── 2. Trailing Stop ──────────────────────────────────────────────────
        if profile.use_trailing and atr > 0:
            trail_dist = atr * profile.atr_trail_mult

            if pnl_pct >= profile.lock_in_pct:
                # Trailing aktivieren / nachziehen
                if state.side == "long":
                    new_trail = current_price - trail_dist
                    if not state.trailing_active or (state.trailing_stop is not None and new_trail > state.trailing_stop):
                        state.trailing_stop = new_trail
                        state.trailing_active = True
                    if state.trailing_active and state.trailing_stop is not None:
                        if current_price <= state.trailing_stop:
                            return True, (
                                f"smart_trailing_stop "
                                f"(+{state.best_pnl_pct:.2f}% peak, "
                                f"Trail={state.trailing_stop:.6f})"
                            )
                else:  # short
                    new_trail = current_price + trail_dist
                    if not state.trailing_active or (state.trailing_stop is not None and new_trail < state.trailing_stop):
                        state.trailing_stop = new_trail
                        state.trailing_active = True
                    if state.trailing_active and state.trailing_stop is not None:
                        if current_price >= state.trailing_stop:
                            return True, (
                                f"smart_trailing_stop "
                                f"(+{state.best_pnl_pct:.2f}% peak, "
                                f"Trail={state.trailing_stop:.6f})"
                            )

        # ── 3. Momentum-Erschöpfung (RSI) ─────────────────────────────────────
        if profile.use_momentum_exit and pnl_pct > 0.1:
            momentum_exit, momentum_reason = self._check_rsi_momentum(
                df, state.side, profile
            )
            if momentum_exit:
                return True, f"smart_momentum ({momentum_reason}, PnL=+{pnl_pct:.2f}%)"

        # ── 4. EMA-Alignment-Bruch (Trend-Strategien) ─────────────────────────
        if profile.ema_exit and state.trailing_active:
            # Nur wenn wir schon im Gewinn sind (Trailing läuft)
            ema_broken, ema_reason = self._check_ema_break(df, state.side)
            if ema_broken:
                return True, f"smart_ema_break ({ema_reason}, PnL=+{pnl_pct:.2f}%)"

        # ── 5. Flat-Exit (kein Fortschritt nach X Minuten) ────────────────────
        if state.duration_min >= profile.flat_exit_min:
            # Prüfe ob Trade "stagniert" (wenig Bewegung, kein Trending)
            if abs(pnl_pct) < 0.1 and not state.trailing_active:
                return True, (
                    f"smart_flat_exit ({state.duration_min:.0f}min, "
                    f"kein Fortschritt, PnL={pnl_pct:+.2f}%)"
                )

        # ── 6. Time-Safety-Exit ────────────────────────────────────────────────
        # Im Verlust nach max_duration_min → schließen (verhindert ewige Verlust-Trades)
        # Im Gewinn mit aktivem Trailing → läuft weiter (kein Zeitlimit!)
        if state.duration_min >= profile.max_duration_min:
            if pnl_pct < 0 and not state.trailing_active:
                return True, (
                    f"smart_time_exit ({state.duration_min:.0f}min > "
                    f"{profile.max_duration_min}min, "
                    f"PnL={pnl_pct:+.2f}%)"
                )
            elif pnl_pct < 0.05 and not state.trailing_active:
                # Nahezu Breakeven nach langer Zeit → raus
                return True, (
                    f"smart_time_exit ({state.duration_min:.0f}min, "
                    f"kein Trend erkennbar)"
                )

        return False, ""

    def get_trailing_stop(self, symbol: str) -> Optional[float]:
        state = self._states.get(symbol)
        return state.trailing_stop if state else None

    def get_trade_info(self, symbol: str) -> Optional[dict]:
        state = self._states.get(symbol)
        if not state:
            return None
        return {
            "strategy":      state.strategy_name,
            "duration_min":  round(state.duration_min, 1),
            "best_pnl_pct":  round(state.best_pnl_pct, 3),
            "break_even":    state.break_even_active,
            "trailing_active": state.trailing_active,
            "trailing_stop": state.trailing_stop,
            "profile_max_min": state.profile.max_duration_min,
        }

    # ── Indikatoren ───────────────────────────────────────────────────────────

    @staticmethod
    def _calc_pnl_pct(entry: float, current: float, side: str) -> float:
        if entry <= 0:
            return 0.0
        if side == "long":
            return (current - entry) / entry * 100
        else:
            return (entry - current) / entry * 100

    @staticmethod
    def _calc_atr(df: pd.DataFrame, period: int = 14) -> float:
        if df is None or len(df) < period + 1:
            return 0.0
        try:
            atr = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=period
            ).average_true_range()
            val = float(atr.iloc[-1])
            return val if val > 0 else 0.0
        except Exception:
            return float((df["high"] - df["low"]).tail(period).mean())

    @staticmethod
    def _check_rsi_momentum(
        df: pd.DataFrame,
        side: str,
        profile: StrategyProfile,
    ) -> Tuple[bool, str]:
        """Erkennt RSI-Erschöpfung + Drehung."""
        if len(df) < 16:
            return False, ""
        try:
            rsi = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
            r_now = float(rsi.iloc[-1])
            r_prev = float(rsi.iloc[-2])

            if side == "long" and r_now > profile.rsi_overbought and r_now < r_prev:
                return True, f"RSI={r_now:.0f} dreht von Überkauft"
            elif side == "short" and r_now < profile.rsi_oversold and r_now > r_prev:
                return True, f"RSI={r_now:.0f} dreht von Überverkauft"
        except Exception:
            pass
        return False, ""

    @staticmethod
    def _check_ema_break(
        df: pd.DataFrame,
        side: str,
    ) -> Tuple[bool, str]:
        """Erkennt EMA9/EMA21 Kreuzung gegen die Trade-Richtung."""
        if len(df) < 25:
            return False, ""
        try:
            ema9  = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
            ema21 = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()

            e9_now  = float(ema9.iloc[-1])
            e21_now = float(ema21.iloc[-1])
            e9_prev = float(ema9.iloc[-2])
            e21_prev = float(ema21.iloc[-2])

            if side == "long":
                # War EMA9 über EMA21, jetzt darunter → Bärisches Kreuz
                bearish_cross = e9_prev >= e21_prev and e9_now < e21_now
                if bearish_cross:
                    return True, "EMA9 kreuzt unter EMA21 (bärisch)"
            else:  # short
                # War EMA9 unter EMA21, jetzt darüber → Bullisches Kreuz
                bullish_cross = e9_prev <= e21_prev and e9_now > e21_now
                if bullish_cross:
                    return True, "EMA9 kreuzt über EMA21 (bullisch)"
        except Exception:
            pass
        return False, ""
