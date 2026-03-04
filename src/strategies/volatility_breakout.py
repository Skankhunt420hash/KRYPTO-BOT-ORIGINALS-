import pandas as pd
import ta
from src.strategies.signal import EnhancedSignal, Side
from src.strategies.base_strategy import EnhancedBaseStrategy
from src.utils.logger import setup_logger

logger = setup_logger("strategy.volatility_breakout")


class VolatilityBreakoutStrategy(EnhancedBaseStrategy):
    """
    Volatility Breakout (Squeeze-Breakout)

    Erkennt Konsolidierungsphasen (BB-Squeeze: Bandbreite unter ihrem
    20-Perioden-Durchschnitt) und handelt den Ausbruch mit Volumenbestätigung.

    Regime-Fit: HIGH_VOLATILITY (primär), TREND_UP/DOWN (sekundär)

    Bedingungen LONG:
    - BB-Bandbreite aktuell < SMA(BB-Bandbreite, 20) – Squeeze aktiv
    - Preis bricht über das 20-Perioden-Hoch (Ausbruch nach oben)
    - Volumen > 1.3× Durchschnitt (Bestätigung)
    SL: Mitte der Squeeze-Range (oder 1.0 × ATR)
    TP: Einstieg + 2.5 × Squeeze-Breite
    """

    BB_WINDOW = 20
    BB_DEV = 2.0
    SQUEEZE_LOOKBACK = 20
    BREAKOUT_LOOKBACK = 20
    VOLUME_MULT = 1.3
    ATR_SL_MULT = 1.0
    TP_RANGE_MULT = 2.5

    def __init__(self):
        super().__init__("VolatilityBreakout")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=self.SQUEEZE_LOOKBACK + self.BB_WINDOW + 5):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")

        try:
            df = df.copy()
            bb = ta.volatility.BollingerBands(
                df["close"], window=self.BB_WINDOW, window_dev=self.BB_DEV
            )
            df["bb_width"] = bb.bollinger_wband()
            df["atr"] = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=14
            ).average_true_range()

            # BB-Squeeze: aktuelle Bandbreite < Durchschnitt der Bandbreite
            width_sma = df["bb_width"].rolling(self.SQUEEZE_LOOKBACK).mean()
            squeeze_active = float(df["bb_width"].iloc[-1]) < float(width_sma.iloc[-1])

            if not squeeze_active:
                return self._no_signal(
                    symbol, timeframe,
                    f"Kein Squeeze | BB-Width={df['bb_width'].iloc[-1]:.4f} "
                    f"> SMA={width_sma.iloc[-1]:.4f}"
                )

            price = float(df["close"].iloc[-1])
            atr = float(df["atr"].iloc[-1])

            # Ausbruch nach oben: Preis > Hoch der letzten N Kerzen (ohne aktuelle)
            recent_high = float(df["high"].iloc[-(self.BREAKOUT_LOOKBACK + 1):-1].max())
            recent_low = float(df["low"].iloc[-(self.BREAKOUT_LOOKBACK + 1):-1].min())
            squeeze_range = recent_high - recent_low

            # Volumen-Bestätigung
            vol_sma = df["volume"].rolling(self.SQUEEZE_LOOKBACK).mean().iloc[-1]
            vol_confirmed = float(df["volume"].iloc[-1]) > float(vol_sma) * self.VOLUME_MULT

            if price > recent_high and vol_confirmed:
                entry = price
                sl = max(entry - self.ATR_SL_MULT * atr, recent_low)
                tp = entry + self.TP_RANGE_MULT * squeeze_range
                rr = self._calc_rr(entry, sl, tp)

                # Squeeze-Stärke als Konfidenz-Basis
                squeeze_ratio = 1.0 - (
                    float(df["bb_width"].iloc[-1]) / (float(width_sma.iloc[-1]) + 1e-9)
                )
                confidence = round(50.0 + squeeze_ratio * 30 + (10 if vol_confirmed else 0), 1)
                confidence = min(confidence, 88.0)

                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.LONG,
                    confidence=confidence,
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=(
                        f"BB-Squeeze Breakout über {recent_high:.4f} | "
                        f"Range={squeeze_range:.4f} | Vol={'✓' if vol_confirmed else '✗'}"
                    ),
                    volume_confirmed=vol_confirmed,
                )

            if price < recent_low and vol_confirmed:
                # Downside-Breakout – in Spot nur dokumentieren, kein SHORT
                return self._no_signal(
                    symbol, timeframe,
                    f"Downside-Breakout unter {recent_low:.4f} "
                    f"(SHORT nicht verfügbar im Spot-Modus)"
                )

            return self._no_signal(
                symbol, timeframe,
                f"Squeeze aktiv – kein Ausbruch | "
                f"Preis={price:.4f} Hoch={recent_high:.4f} "
                f"Vol={'✓' if vol_confirmed else '✗'}"
            )

        except Exception as e:
            logger.error(f"Fehler in VolatilityBreakout.analyze ({symbol}): {e}")
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
