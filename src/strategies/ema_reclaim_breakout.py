import pandas as pd
import ta

from src.strategies.base_strategy import EnhancedBaseStrategy
from src.strategies.signal import EnhancedSignal, Side
from src.utils.logger import setup_logger

logger = setup_logger("strategy.ema_reclaim_breakout")


class EmaReclaimBreakoutStrategy(EnhancedBaseStrategy):
    """
    EMA Reclaim Breakout (LONG/SHORT).

    Idee:
    - Trendstruktur über EMA34/EMA89.
    - Entry erst nach Reclaim + Trigger-Break, damit nicht jede Berührung gehandelt wird.
    """

    EMA_FAST = 34
    EMA_SLOW = 89
    ATR_WINDOW = 14
    ADX_WINDOW = 14
    ADX_MIN = 17.0
    TP_R_MULT = 2.4
    SL_ATR_BUFFER = 0.4

    def __init__(self):
        super().__init__("EMAReclaimBreakout")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=self.EMA_SLOW + 8):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")

        try:
            df = df.copy()
            df["ema_fast"] = ta.trend.EMAIndicator(
                df["close"], window=self.EMA_FAST
            ).ema_indicator()
            df["ema_slow"] = ta.trend.EMAIndicator(
                df["close"], window=self.EMA_SLOW
            ).ema_indicator()
            df["atr"] = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=self.ATR_WINDOW
            ).average_true_range()
            df["adx"] = ta.trend.ADXIndicator(
                df["high"], df["low"], df["close"], window=self.ADX_WINDOW
            ).adx()

            prev = df.iloc[-2]
            last = df.iloc[-1]

            prev_high = float(prev["high"])
            prev_low = float(prev["low"])
            prev_close = float(prev["close"])
            prev_open = float(prev["open"])

            last_close = float(last["close"])
            last_open = float(last["open"])
            last_high = float(last["high"])
            last_low = float(last["low"])
            ema_fast = float(last["ema_fast"])
            ema_slow = float(last["ema_slow"])
            atr = float(last["atr"])
            adx = float(last["adx"])

            if adx < self.ADX_MIN:
                return self._no_signal(symbol, timeframe, f"ADX zu niedrig ({adx:.1f})")

            # LONG: Trend oben + Reclaim fast-EMA + Break über prev high
            long_trend = ema_fast > ema_slow and last_close > ema_slow
            long_reclaim = prev_low <= ema_fast and last_close > ema_fast and last_close > last_open
            long_break = last_close > prev_high
            if long_trend and long_reclaim and long_break:
                entry = last_close
                sl = min(prev_low, last_low, ema_fast) - self.SL_ATR_BUFFER * atr
                risk = entry - sl
                if risk <= 0:
                    return self._no_signal(symbol, timeframe, "Ungültiges LONG-Risiko")
                tp = entry + self.TP_R_MULT * risk
                rr = self._calc_rr(entry, sl, tp)
                confidence = min(
                    90.0,
                    round(
                        50.0
                        + min(max((ema_fast - ema_slow) / (ema_slow + 1e-9) * 100.0, 0.0), 4.0)
                        * 6.0
                        + min(max((last_close - prev_high) / (atr + 1e-9), 0.0), 2.0) * 9.0
                        + min(max((adx - self.ADX_MIN) / 10.0, 0.0), 1.0) * 10.0,
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
                        f"[LONG] EMA-Reclaim + Breakout | "
                        f"EMA34>{self.EMA_SLOW}EMA ({ema_fast:.2f}>{ema_slow:.2f}) ADX={adx:.1f}"
                    ),
                    volume_confirmed=self._confirm_volume(df),
                )

            # SHORT: Trend unten + Reclaim fast-EMA von unten + Break unter prev low
            short_trend = ema_fast < ema_slow and last_close < ema_slow
            short_reclaim = prev_high >= ema_fast and last_close < ema_fast and last_close < last_open
            short_break = last_close < prev_low
            if short_trend and short_reclaim and short_break:
                entry = last_close
                sl = max(prev_high, last_high, ema_fast) + self.SL_ATR_BUFFER * atr
                risk = sl - entry
                if risk <= 0:
                    return self._no_signal(symbol, timeframe, "Ungültiges SHORT-Risiko")
                tp = entry - self.TP_R_MULT * risk
                rr = self._calc_rr(entry, sl, tp)
                confidence = min(
                    90.0,
                    round(
                        50.0
                        + min(max((ema_slow - ema_fast) / (ema_slow + 1e-9) * 100.0, 0.0), 4.0)
                        * 6.0
                        + min(max((prev_low - last_close) / (atr + 1e-9), 0.0), 2.0) * 9.0
                        + min(max((adx - self.ADX_MIN) / 10.0, 0.0), 1.0) * 10.0,
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
                        f"[SHORT] EMA-Reclaim + Breakdown | "
                        f"EMA34<{self.EMA_SLOW}EMA ({ema_fast:.2f}<{ema_slow:.2f}) ADX={adx:.1f}"
                    ),
                    volume_confirmed=self._confirm_volume(df),
                )

            return self._no_signal(
                symbol,
                timeframe,
                "Kein EMA-Reclaim-Breakout-Setup",
            )
        except Exception as e:
            logger.error(f"Fehler in EmaReclaimBreakout.analyze ({symbol}): {e}")
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
