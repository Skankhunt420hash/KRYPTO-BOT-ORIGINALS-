from enum import Enum
import math
from typing import Dict

import pandas as pd
import ta

from src.utils.logger import setup_logger

logger = setup_logger("regime")


class Regime(Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    TREND_MARKET = "TREND_MARKET"
    SIDEWAYS_MARKET = "SIDEWAYS_MARKET"
    MANIPULATION_PHASE = "MANIPULATION_PHASE"
    NEWS_SHOCK = "NEWS_SHOCK"
    LOW_VOL_TRAP = "LOW_VOL_TRAP"
    PUMP_DUMP_RISK = "PUMP_DUMP_RISK"
    LIQUIDATION_CASCADE = "LIQUIDATION_CASCADE"


class RegimeEngine:
    """
    Erkennt das aktuelle Marktregime aus Preisstruktur, Volatilität, Momentum,
    Kerzenstruktur und Volumen.

    Kern-Phasen:
    - TREND_MARKET
    - SIDEWAYS_MARKET
    - MANIPULATION_PHASE
    - NEWS_SHOCK
    - LOW_VOL_TRAP
    - PUMP_DUMP_RISK
    - LIQUIDATION_CASCADE

    Legacy-Fallbacks bleiben erhalten (TREND_UP/DOWN, RANGE, HIGH/LOW_VOLATILITY),
    damit bestehende Komponenten und historische Daten kompatibel bleiben.
    """

    HIGH_VOL_ATR_PCT: float = 3.5
    LOW_VOL_ATR_PCT: float = 0.5
    ADX_TREND_THRESHOLD: float = 25.0
    EMA_PERIOD: int = 50
    EMA_SLOPE_LOOKBACK: int = 5

    def __init__(self) -> None:
        self._last_context: Dict = {
            "regime": Regime.RANGE.value,
            "reason": "init",
        }

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        try:
            out = float(value)
            if math.isnan(out) or math.isinf(out):
                return default
            return out
        except Exception:
            return default

    def get_last_context(self) -> Dict:
        return dict(self._last_context)

    def detect(self, df: pd.DataFrame) -> Regime:
        try:
            if df is None or len(df) < 80:
                self._last_context = {
                    "regime": Regime.SIDEWAYS_MARKET.value,
                    "reason": "insufficient_data",
                }
                return Regime.SIDEWAYS_MARKET

            df = df.copy()
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["open", "high", "low", "close"])
            if len(df) < 80:
                self._last_context = {
                    "regime": Regime.SIDEWAYS_MARKET.value,
                    "reason": "insufficient_data_after_clean",
                }
                return Regime.SIDEWAYS_MARKET

            atr_series = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=14
            ).average_true_range()
            last_atr = self._safe_float(atr_series.iloc[-1], 0.0)
            last_close = self._safe_float(df["close"].iloc[-1], 0.0)
            atr_pct = (last_atr / last_close * 100) if last_close > 0 else 0.0

            adx_series = ta.trend.ADXIndicator(
                df["high"], df["low"], df["close"], window=14
            ).adx()
            adx = self._safe_float(adx_series.iloc[-1], 0.0)

            ema_fast_series = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()
            ema_slow_series = ta.trend.EMAIndicator(
                df["close"], window=self.EMA_PERIOD
            ).ema_indicator()
            ema_fast = self._safe_float(ema_fast_series.iloc[-1], last_close)
            ema_slow = self._safe_float(ema_slow_series.iloc[-1], last_close)
            ema_ref = self._safe_float(
                ema_fast_series.iloc[-(self.EMA_SLOPE_LOOKBACK + 1)], ema_fast
            )
            ema_slope_pct = (
                ((ema_fast - ema_ref) / ema_ref) * 100.0 if abs(ema_ref) > 1e-12 else 0.0
            )

            close = df["close"]
            prev_close = self._safe_float(close.iloc[-2], last_close)
            shock_return_pct = (
                abs((last_close - prev_close) / prev_close) * 100.0
                if abs(prev_close) > 1e-12
                else 0.0
            )
            intrabar_range_pct = (
                self._safe_float(df["high"].iloc[-1] - df["low"].iloc[-1], 0.0) / last_close * 100.0
                if last_close > 0
                else 0.0
            )

            bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
            bb_high = self._safe_float(bb.bollinger_hband().iloc[-1], last_close)
            bb_low = self._safe_float(bb.bollinger_lband().iloc[-1], last_close)
            bb_width_pct = ((bb_high - bb_low) / last_close * 100.0) if last_close > 0 else 0.0

            vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
            vol_sma = self._safe_float(vol.rolling(20).mean().iloc[-1], 0.0)
            vol_ratio = self._safe_float(vol.iloc[-1], 0.0) / vol_sma if vol_sma > 0 else 1.0

            body = (df["close"] - df["open"]).abs()
            upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
            lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
            wick_ratio = ((upper_wick + lower_wick) / (body + 1e-9)).replace(
                [math.inf, -math.inf], 0.0
            ).fillna(0.0)
            wick_mean = self._safe_float(wick_ratio.tail(5).mean(), 0.0)

            ret_pct = close.pct_change().fillna(0.0) * 100.0
            recent3 = ret_pct.tail(3).tolist()
            same_sign = (
                len(recent3) == 3
                and ((all(x > 0 for x in recent3)) or (all(x < 0 for x in recent3)))
            )
            cascade_move_pct = float(sum(abs(x) for x in recent3)) if recent3 else 0.0

            window = df.tail(12)
            local_low = self._safe_float(window["low"].min(), last_close)
            local_high = self._safe_float(window["high"].max(), last_close)
            swing_pct = (
                ((local_high - local_low) / local_low) * 100.0 if local_low > 0 else 0.0
            )
            retrace_from_high_pct = (
                ((local_high - last_close) / local_high) * 100.0 if local_high > 0 else 0.0
            )
            if not window.empty:
                # Positionsbasiert statt Label-basiert (robust für DatetimeIndex).
                peak_pos = int(window["high"].to_numpy().argmax())
                bars_since_peak = max(0, (len(window) - 1) - peak_pos)
            else:
                bars_since_peak = 0

            trend_direction = "flat"
            if ema_fast > ema_slow and ema_slope_pct > 0.08:
                trend_direction = "up"
            elif ema_fast < ema_slow and ema_slope_pct < -0.08:
                trend_direction = "down"

            regime = Regime.RANGE
            reason = "fallback_range"

            if shock_return_pct >= 2.4 and vol_ratio >= 2.0 and intrabar_range_pct >= 2.8:
                regime = Regime.NEWS_SHOCK
                reason = "shock_return+volume_spike"
            elif (
                same_sign
                and cascade_move_pct >= 3.6
                and atr_pct >= 1.2
                and vol_ratio >= 1.35
            ):
                regime = Regime.LIQUIDATION_CASCADE
                reason = "one_sided_move_with_volume"
            elif (
                swing_pct >= 5.0
                and retrace_from_high_pct >= 2.0
                and bars_since_peak <= 4
                and vol_ratio >= 1.45
            ) or (
                shock_return_pct >= 3.2 and intrabar_range_pct >= 4.0 and vol_ratio >= 1.8
            ):
                regime = Regime.PUMP_DUMP_RISK
                reason = "explosive_swing_with_fast_retrace"
            elif atr_pct <= 0.45 and bb_width_pct <= 1.25 and adx <= 16.0:
                regime = Regime.LOW_VOL_TRAP
                reason = "compressed_volatility_and_flat_trend"
            elif wick_mean >= 2.2 and adx <= 20.0 and vol_ratio >= 1.1:
                regime = Regime.MANIPULATION_PHASE
                reason = "wick_spikes_and_weak_trend"
            elif adx >= 24.0 and abs(ema_slope_pct) >= 0.25:
                regime = Regime.TREND_MARKET
                reason = f"adx+ema_slope trend={trend_direction}"
            elif adx <= 19.0 and abs(ema_slope_pct) <= 0.18:
                regime = Regime.SIDEWAYS_MARKET
                reason = "low_adx_flat_slope"
            elif atr_pct >= self.HIGH_VOL_ATR_PCT:
                regime = Regime.HIGH_VOLATILITY
                reason = "legacy_high_volatility"
            elif atr_pct <= self.LOW_VOL_ATR_PCT:
                regime = Regime.LOW_VOLATILITY
                reason = "legacy_low_volatility"

            self._last_context = {
                "regime": regime.value,
                "reason": reason,
                "trend_direction": trend_direction,
                "atr_pct": round(float(atr_pct), 4),
                "adx": round(float(adx), 4),
                "ema_slope_pct": round(float(ema_slope_pct), 4),
                "bb_width_pct": round(float(bb_width_pct), 4),
                "volume_ratio": round(float(vol_ratio), 4),
                "wick_ratio_5": round(float(wick_mean), 4),
                "shock_return_pct": round(float(shock_return_pct), 4),
                "intrabar_range_pct": round(float(intrabar_range_pct), 4),
                "cascade_move_pct": round(float(cascade_move_pct), 4),
                "swing_12_pct": round(float(swing_pct), 4),
                "retrace_from_high_pct": round(float(retrace_from_high_pct), 4),
            }

            logger.debug(
                "Regime: [bold]%s[/bold] | reason=%s | ATR%%=%.2f | ADX=%.1f | "
                "EMA-slope%%=%.2f | BB%%=%.2f | volRatio=%.2f",
                regime.value,
                reason,
                atr_pct,
                adx,
                ema_slope_pct,
                bb_width_pct,
                vol_ratio,
            )
            return regime

        except Exception as e:
            logger.warning(f"Regime-Erkennung fehlgeschlagen: {e} – Fallback: RANGE")
            self._last_context = {
                "regime": Regime.RANGE.value,
                "reason": f"exception:{type(e).__name__}",
            }
            return Regime.RANGE
