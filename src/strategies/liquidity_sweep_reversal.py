import pandas as pd
import ta

from src.strategies.base_strategy import EnhancedBaseStrategy
from src.strategies.signal import EnhancedSignal, Side
from src.utils.logger import setup_logger

logger = setup_logger("strategy.liquidity_sweep_reversal")


class LiquiditySweepReversalStrategy(EnhancedBaseStrategy):
    """
    Liquidity Sweep Reversal (LONG/SHORT).

    Idee:
    - Markt holt kurz Liquidität unter/über einem Extrem (Band-Überschuss + Wick),
      kehrt aber schnell zurück (Reclaim).
    - Einstieg erst mit Folgebestätigung, um reine Falling-Knife-Signale zu reduzieren.
    """

    BB_WINDOW = 20
    BB_DEV = 2.0
    RSI_WINDOW = 14
    ATR_WINDOW = 14
    RSI_LONG_MAX = 42
    RSI_SHORT_MIN = 58
    TP_R_MULT = 2.2
    SL_ATR_BUFFER = 0.35

    def __init__(self):
        super().__init__("LiquiditySweepReversal")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=self.BB_WINDOW + 8):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")

        try:
            df = df.copy()
            bb = ta.volatility.BollingerBands(
                df["close"], window=self.BB_WINDOW, window_dev=self.BB_DEV
            )
            df["bb_upper"] = bb.bollinger_hband()
            df["bb_lower"] = bb.bollinger_lband()
            df["rsi"] = ta.momentum.RSIIndicator(
                df["close"], window=self.RSI_WINDOW
            ).rsi()
            df["atr"] = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=self.ATR_WINDOW
            ).average_true_range()

            prev = df.iloc[-2]
            last = df.iloc[-1]

            prev_low = float(prev["low"])
            prev_high = float(prev["high"])
            prev_close = float(prev["close"])
            prev_open = float(prev["open"])
            prev_bb_low = float(prev["bb_lower"])
            prev_bb_up = float(prev["bb_upper"])

            last_close = float(last["close"])
            last_open = float(last["open"])
            last_high = float(last["high"])
            last_low = float(last["low"])
            last_rsi = float(last["rsi"])
            prev_rsi = float(prev["rsi"])
            atr = float(last["atr"])

            # LONG: Sweep unteres Extrem + Reclaim + Folgekerze bestätigt Stärke
            bullish_sweep = prev_low < prev_bb_low and prev_close > prev_bb_low
            bullish_reclaim = (
                last_close > prev_high
                and last_close > last_open
                and last_rsi <= self.RSI_LONG_MAX
                and last_rsi > prev_rsi
            )
            if bullish_sweep and bullish_reclaim:
                entry = last_close
                sweep_low = min(prev_low, last_low)
                sl = sweep_low - self.SL_ATR_BUFFER * atr
                risk = entry - sl
                if risk <= 0:
                    return self._no_signal(symbol, timeframe, "Ungültiges LONG-Risiko")
                tp = entry + self.TP_R_MULT * risk
                rr = self._calc_rr(entry, sl, tp)
                confidence = min(
                    89.0,
                    round(
                        52.0
                        + min(max((prev_bb_low - prev_low) / (atr + 1e-9), 0.0), 2.0) * 10.0
                        + min(max((last_close - prev_high) / (atr + 1e-9), 0.0), 2.0) * 9.0,
                        1,
                    ),
                )
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
                        f"[LONG] Liquiditäts-Sweep unter BB-Lower + Reclaim "
                        f"(RSI {prev_rsi:.1f}->{last_rsi:.1f})"
                    ),
                    volume_confirmed=self._confirm_volume(df),
                )

            # SHORT: Sweep oberes Extrem + Reclaim nach unten
            bearish_sweep = prev_high > prev_bb_up and prev_close < prev_bb_up
            bearish_reclaim = (
                last_close < prev_low
                and last_close < last_open
                and last_rsi >= self.RSI_SHORT_MIN
                and last_rsi < prev_rsi
            )
            if bearish_sweep and bearish_reclaim:
                entry = last_close
                sweep_high = max(prev_high, last_high)
                sl = sweep_high + self.SL_ATR_BUFFER * atr
                risk = sl - entry
                if risk <= 0:
                    return self._no_signal(symbol, timeframe, "Ungültiges SHORT-Risiko")
                tp = entry - self.TP_R_MULT * risk
                rr = self._calc_rr(entry, sl, tp)
                confidence = min(
                    89.0,
                    round(
                        52.0
                        + min(max((prev_high - prev_bb_up) / (atr + 1e-9), 0.0), 2.0) * 10.0
                        + min(max((prev_low - last_close) / (atr + 1e-9), 0.0), 2.0) * 9.0,
                        1,
                    ),
                )
                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.SHORT,
                    confidence=confidence,
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=(
                        f"[SHORT] Liquiditäts-Sweep über BB-Upper + Reclaim "
                        f"(RSI {prev_rsi:.1f}->{last_rsi:.1f})"
                    ),
                    volume_confirmed=self._confirm_volume(df),
                )

            return self._no_signal(
                symbol,
                timeframe,
                "Kein Sweep-Reversal-Setup bestätigt",
            )
        except Exception as e:
            logger.error(f"Fehler in LiquiditySweepReversal.analyze ({symbol}): {e}")
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
