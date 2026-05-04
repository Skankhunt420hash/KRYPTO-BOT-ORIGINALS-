import pandas as pd
import ta

from src.strategies.base_strategy import EnhancedBaseStrategy
from src.strategies.signal import EnhancedSignal, Side
from src.utils.logger import setup_logger

logger = setup_logger("strategy.liquidity_sweep_reversal")


class LiquiditySweepReversalStrategy(EnhancedBaseStrategy):
    """
    Liquidity Sweep Reversal:
    - LONG: Low swept previous swing low, closes back above previous low.
    - SHORT: High swept previous swing high, closes back below previous high.
    """

    SWING_LOOKBACK = 12
    ATR_SL_MULT = 1.2
    TP_R_MULT = 2.4

    def __init__(self):
        super().__init__("LiquiditySweepReversal")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=self.SWING_LOOKBACK + 20):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")
        try:
            df = df.copy()
            df["atr"] = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=14
            ).average_true_range()
            df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()

            last = df.iloc[-1]
            prev = df.iloc[-2]
            atr = float(last["atr"])
            if atr <= 0:
                return self._no_signal(symbol, timeframe, "ATR ungültig")

            price = float(last["close"])
            prev_high = float(df["high"].iloc[-(self.SWING_LOOKBACK + 1):-1].max())
            prev_low = float(df["low"].iloc[-(self.SWING_LOOKBACK + 1):-1].min())
            curr_high = float(last["high"])
            curr_low = float(last["low"])
            prev_close = float(prev["close"])
            rsi = float(last["rsi"])

            # LONG sweep
            swept_low = curr_low < prev_low and price > prev_low and prev_close <= prev_low
            if swept_low and rsi < 52:
                entry = price
                sl = min(curr_low, prev_low) - self.ATR_SL_MULT * atr
                risk = entry - sl
                if risk > 0:
                    tp = entry + self.TP_R_MULT * risk
                    rr = self._calc_rr(entry, sl, tp)
                    confidence = min(86.0, round(52.0 + max(0.0, (52.0 - rsi)) * 0.6, 1))
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
                        reason=f"[LONG] Liquidity Sweep unter {prev_low:.4f} und Reclaim",
                        volume_confirmed=self._confirm_volume(df),
                    )

            # SHORT sweep
            swept_high = curr_high > prev_high and price < prev_high and prev_close >= prev_high
            if swept_high and rsi > 48:
                entry = price
                sl = max(curr_high, prev_high) + self.ATR_SL_MULT * atr
                risk = sl - entry
                if risk > 0:
                    tp = entry - self.TP_R_MULT * risk
                    rr = self._calc_rr(entry, sl, tp)
                    confidence = min(86.0, round(52.0 + max(0.0, (rsi - 48.0)) * 0.6, 1))
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
                        reason=f"[SHORT] Liquidity Sweep über {prev_high:.4f} und Reject",
                        volume_confirmed=self._confirm_volume(df),
                    )

            return self._no_signal(symbol, timeframe, "Kein Sweep-Reversal")
        except Exception as e:
            logger.error(f"Fehler in LiquiditySweepReversal.analyze ({symbol}): {e}")
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
