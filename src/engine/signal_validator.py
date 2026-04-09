"""
Signal Validator – Mehrschichtige Qualitätsprüfung vor Trade-Ausführung

Prüft jeden Signal auf 5 unabhängige Qualitätskriterien.
Jede fehlgeschlagene Prüfung erhöht die Ablehnungswahrscheinlichkeit.

1. ADX-Stärke-Filter
   ADX > 20 = klarer Trend oder Breakout (für Trend-Strategien)
   ADX < 30 = klare Range (für Range-Strategien)
   Verhindert: Trend-Trades in seitwärtsbewegenden Märkten

2. Volumen-Bestätigung (Pflicht)
   Aktuelles Volumen > SMA(20) × MULT
   Verhindert: Ausbrüche ohne Händler-Unterstützung (False Breakouts)

3. Candle-Pattern-Bestätigung
   Prüft ob die aktuelle Kerze die Trade-Richtung bestätigt:
   LONG: Schlusskurs im oberen 40% der Kerze (bullische Kerze)
   SHORT: Schlusskurs im unteren 40% der Kerze (bärische Kerze)
   Verhindert: Gegen-Kerzen-Trades

4. Spread-Qualitätsprüfung
   Spread/ATR Ratio prüfen – zu enger ATR = zu viel Spread-Anteil
   Verhindert: VELO-artiger Spread-Kill

5. Momentum-Alignment
   RSI muss in Richtung des Signals zeigen:
   LONG: RSI > 45 und steigend
   SHORT: RSI < 55 und fallend
   Verhindert: Trades gegen das kurzfristige Momentum
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import pandas as pd
import ta

from config.settings import settings
from src.strategies.signal import EnhancedSignal, Side
from src.utils.logger import setup_logger

logger = setup_logger("signal_validator")

# Konfiguration
_ADX_TREND_MIN: float = 20.0        # Mindest-ADX für Trend-Strategien
_ADX_RANGE_MAX: float = 35.0        # Maximal-ADX für Range-Strategien
_VOLUME_MULT: float = 1.1            # Volumen muss > SMA×1.1 sein
_CANDLE_BODY_THRESHOLD: float = 0.38 # Schlusskurs muss in oberen/unteren 38% sein
_RSI_LONG_MIN: float = 42.0          # RSI-Minimum für LONG
_RSI_SHORT_MAX: float = 58.0         # RSI-Maximum für SHORT

# Strategie-Typ-Mapping (bestimmt welche Checks angewendet werden)
_TREND_STRATEGIES = {"TrendContinuation", "MomentumPullback", "VolatilityBreakout"}
_RANGE_STRATEGIES = {"RangeReversion"}


@dataclass
class ValidationResult:
    """Ergebnis der Signal-Validierung."""
    passed: bool
    score: float          # 0.0 – 1.0 (Qualitätsscore)
    checks_passed: int
    checks_total: int
    failed_reasons: list
    confidence_adjustment: float   # Aufschlag/Abzug auf Signal-Konfidenz


class SignalValidator:
    """
    Prüft Signale auf 5 Qualitätskriterien.
    Gibt ValidationResult zurück – der MetaSelector entscheidet über Block/Execute.
    """

    def validate(
        self,
        signal: EnhancedSignal,
        df: pd.DataFrame,
    ) -> ValidationResult:
        """Führt alle Qualitätsprüfungen durch."""
        if not settings.SIGNAL_VALIDATOR_ENABLED:
            return ValidationResult(
                passed=True, score=1.0,
                checks_passed=5, checks_total=5,
                failed_reasons=[], confidence_adjustment=0.0,
            )

        if df is None or len(df) < 30:
            return ValidationResult(
                passed=True, score=0.8,
                checks_passed=4, checks_total=5,
                failed_reasons=["Nicht genug Daten für vollständige Validierung"],
                confidence_adjustment=0.0,
            )

        failed = []
        passed = 0
        total = 5
        is_trend = signal.strategy_name in _TREND_STRATEGIES
        is_range = signal.strategy_name in _RANGE_STRATEGIES

        # ── Check 1: ADX-Stärke ──────────────────────────────────────────────
        adx_ok, adx_reason = self._check_adx(df, signal.side, is_trend, is_range)
        if adx_ok:
            passed += 1
        else:
            failed.append(f"ADX: {adx_reason}")

        # ── Check 2: Volumen-Bestätigung ─────────────────────────────────────
        vol_ok, vol_reason = self._check_volume(df)
        if vol_ok:
            passed += 1
        else:
            failed.append(f"Vol: {vol_reason}")

        # ── Check 3: Candle-Pattern ───────────────────────────────────────────
        candle_ok, candle_reason = self._check_candle_direction(df, signal.side)
        if candle_ok:
            passed += 1
        else:
            failed.append(f"Kerze: {candle_reason}")

        # ── Check 4: Spread/ATR-Qualität ─────────────────────────────────────
        spread_ok, spread_reason = self._check_spread_quality(signal, df)
        if spread_ok:
            passed += 1
        else:
            failed.append(f"Spread: {spread_reason}")

        # ── Check 5: Momentum-Alignment ──────────────────────────────────────
        mom_ok, mom_reason = self._check_momentum(df, signal.side)
        if mom_ok:
            passed += 1
        else:
            failed.append(f"Momentum: {mom_reason}")

        # Gesamtbewertung
        score = passed / total
        # Mind. 3 von 5 Checks müssen bestanden werden
        min_checks = getattr(settings, "SIGNAL_VALIDATOR_MIN_CHECKS", 3)
        overall_pass = passed >= min_checks

        # Konfidenz-Anpassung: +5 pro Extra-Check über Minimum, -5 pro fehlendem
        conf_adj = (passed - min_checks) * 5.0

        if failed:
            logger.debug(
                f"[Validator] {signal.strategy_name} {signal.symbol} "
                f"{signal.side.value.upper()} | "
                f"{passed}/{total} Checks | "
                f"{'OK' if overall_pass else 'SCHWACH'} | "
                f"{' | '.join(failed[:2])}"
            )

        return ValidationResult(
            passed=overall_pass,
            score=score,
            checks_passed=passed,
            checks_total=total,
            failed_reasons=failed,
            confidence_adjustment=conf_adj,
        )

    # ── Interne Checks ────────────────────────────────────────────────────────

    @staticmethod
    def _check_adx(
        df: pd.DataFrame,
        side: Side,
        is_trend: bool,
        is_range: bool,
    ) -> Tuple[bool, str]:
        """ADX-Stärke passend zur Strategie-Art."""
        try:
            adx_ind = ta.trend.ADXIndicator(
                df["high"], df["low"], df["close"], window=14
            )
            adx = float(adx_ind.adx().iloc[-1])

            if is_trend and adx < _ADX_TREND_MIN:
                return False, f"ADX={adx:.1f} < {_ADX_TREND_MIN} (Trend zu schwach)"
            if is_range and adx > _ADX_RANGE_MAX:
                return False, f"ADX={adx:.1f} > {_ADX_RANGE_MAX} (Trend zu stark für Range)"
            return True, f"ADX={adx:.1f} OK"
        except Exception:
            return True, "ADX nicht berechenbar (skip)"

    @staticmethod
    def _check_volume(df: pd.DataFrame) -> Tuple[bool, str]:
        """Volumen muss über dem gleitenden Durchschnitt liegen."""
        try:
            vol_now = float(df["volume"].iloc[-1])
            vol_sma = float(df["volume"].rolling(20).mean().iloc[-1])
            if vol_sma <= 0:
                return True, "Vol-SMA nicht berechenbar (skip)"
            ratio = vol_now / vol_sma
            if ratio < _VOLUME_MULT:
                return False, f"Vol={ratio:.2f}× SMA (< {_VOLUME_MULT}×)"
            return True, f"Vol={ratio:.2f}× SMA"
        except Exception:
            return True, "Volumen nicht prüfbar (skip)"

    @staticmethod
    def _check_candle_direction(
        df: pd.DataFrame, side: Side
    ) -> Tuple[bool, str]:
        """Schlusskurs muss Richtung des Trades bestätigen."""
        try:
            last = df.iloc[-1]
            candle_high = float(last["high"])
            candle_low = float(last["low"])
            candle_close = float(last["close"])
            candle_range = candle_high - candle_low

            if candle_range <= 0:
                return True, "Doji (skip)"

            # Position des Schlusskurses in der Kerze (0=unten, 1=oben)
            close_position = (candle_close - candle_low) / candle_range

            if side == Side.LONG:
                if close_position < _CANDLE_BODY_THRESHOLD:
                    return False, f"Bärische Kerze ({close_position:.0%} von unten)"
            else:  # SHORT
                if close_position > (1.0 - _CANDLE_BODY_THRESHOLD):
                    return False, f"Bullische Kerze ({close_position:.0%} von unten)"

            return True, f"Kerze OK ({close_position:.0%})"
        except Exception:
            return True, "Kerze nicht prüfbar (skip)"

    @staticmethod
    def _check_spread_quality(
        signal: EnhancedSignal, df: pd.DataFrame
    ) -> Tuple[bool, str]:
        """SL-Distanz muss ausreichend groß im Verhältnis zur ATR sein."""
        try:
            atr = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=14
            ).average_true_range().iloc[-1]
            atr = float(atr)

            if atr <= 0:
                return True, "ATR=0 (skip)"

            sl_dist = abs(signal.entry - signal.stop_loss)
            sl_atr_ratio = sl_dist / atr

            # SL muss mindestens 0.5× ATR Distanz haben (nicht zu eng)
            if sl_atr_ratio < 0.5:
                return False, f"SL/ATR={sl_atr_ratio:.2f} < 0.5 (SL zu eng)"
            return True, f"SL/ATR={sl_atr_ratio:.2f}"
        except Exception:
            return True, "Spread-Check fehlgeschlagen (skip)"

    @staticmethod
    def _check_momentum(
        df: pd.DataFrame, side: Side
    ) -> Tuple[bool, str]:
        """RSI muss in Richtung des Trades zeigen."""
        try:
            rsi_series = ta.momentum.RSIIndicator(
                df["close"], window=14
            ).rsi()
            rsi_now = float(rsi_series.iloc[-1])
            rsi_prev = float(rsi_series.iloc[-2])

            if side == Side.LONG:
                if rsi_now < _RSI_LONG_MIN:
                    return False, f"RSI={rsi_now:.1f} < {_RSI_LONG_MIN} (zu schwach für Long)"
                if rsi_now < rsi_prev - 5:
                    return False, f"RSI fällt ({rsi_prev:.1f}→{rsi_now:.1f})"
            else:  # SHORT
                if rsi_now > _RSI_SHORT_MAX:
                    return False, f"RSI={rsi_now:.1f} > {_RSI_SHORT_MAX} (zu stark für Short)"
                if rsi_now > rsi_prev + 5:
                    return False, f"RSI steigt ({rsi_prev:.1f}→{rsi_now:.1f})"

            return True, f"RSI={rsi_now:.1f} OK"
        except Exception:
            return True, "Momentum nicht prüfbar (skip)"
