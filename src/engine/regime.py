from enum import Enum
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


class RegimeEngine:
    """
    Erkennt das aktuelle Marktregime anhand von ATR, ADX und EMA-Slope.

    Logik:
    1. ATR% > HIGH_VOL_THRESHOLD  → HIGH_VOLATILITY
    2. ADX > 25  → prüfe EMA50-Slope für Richtung (TREND_UP / TREND_DOWN)
    3. ATR% < LOW_VOL_THRESHOLD   → LOW_VOLATILITY
    4. Sonst                      → RANGE
    """

    HIGH_VOL_ATR_PCT: float = 3.5
    LOW_VOL_ATR_PCT: float = 0.5
    ADX_TREND_THRESHOLD: float = 25.0
    EMA_PERIOD: int = 50
    EMA_SLOPE_LOOKBACK: int = 5

    def detect(self, df: pd.DataFrame) -> Regime:
        try:
            if df is None or len(df) < 60:
                return Regime.RANGE

            df = df.copy()

            atr_series = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=14
            ).average_true_range()
            last_atr = float(atr_series.iloc[-1])
            last_close = float(df["close"].iloc[-1])
            atr_pct = (last_atr / last_close * 100) if last_close > 0 else 0.0

            adx_series = ta.trend.ADXIndicator(
                df["high"], df["low"], df["close"], window=14
            ).adx()
            adx = float(adx_series.iloc[-1])

            ema_series = ta.trend.EMAIndicator(
                df["close"], window=self.EMA_PERIOD
            ).ema_indicator()
            ema_slope = float(ema_series.iloc[-1]) - float(
                ema_series.iloc[-(self.EMA_SLOPE_LOOKBACK + 1)]
            )

            if atr_pct >= self.HIGH_VOL_ATR_PCT:
                regime = Regime.HIGH_VOLATILITY
            elif adx >= self.ADX_TREND_THRESHOLD:
                regime = Regime.TREND_UP if ema_slope > 0 else Regime.TREND_DOWN
            elif atr_pct <= self.LOW_VOL_ATR_PCT:
                regime = Regime.LOW_VOLATILITY
            else:
                regime = Regime.RANGE

            logger.debug(
                f"Regime: [bold]{regime.value}[/bold] | "
                f"ATR%={atr_pct:.2f} | ADX={adx:.1f} | EMA-Slope={ema_slope:.2f}"
            )
            return regime

        except Exception as e:
            logger.warning(f"Regime-Erkennung fehlgeschlagen: {e} – Fallback: RANGE")
            return Regime.RANGE
