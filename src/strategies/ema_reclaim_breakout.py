import pandas as pd
import ta

from src.strategies.base_strategy import EnhancedBaseStrategy
from src.strategies.signal import EnhancedSignal, Side
from src.utils.logger import setup_logger

logger = setup_logger("strategy.ema_reclaim_breakout")


class EMAReclaimBreakoutStrategy(EnhancedBaseStrategy):
    """
    EMA Reclaim Breakout:
    - LONG: Preis reclaimt EMA20, EMA20 > EMA50, RSI steigt über 50.
    - SHORT: Preis verliert EMA20, EMA20 < EMA50, RSI fällt unter 50.
    """

    def __init__(self):
        super().__init__("EMAReclaimBreakout")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=90):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")
        try:
            frame = df.copy()
            frame["ema20"] = ta.trend.EMAIndicator(frame["close"], window=20).ema_indicator()
            frame["ema50"] = ta.trend.EMAIndicator(frame["close"], window=50).ema_indicator()
            frame["rsi"] = ta.momentum.RSIIndicator(frame["close"], window=14).rsi()
            frame["atr"] = ta.volatility.AverageTrueRange(
                frame["high"], frame["low"], frame["close"], window=14
            ).average_true_range()

            last = frame.iloc[-1]
            prev = frame.iloc[-2]
            price = float(last["close"])
            ema20 = float(last["ema20"])
            ema50 = float(last["ema50"])
            rsi = float(last["rsi"])
            prev_rsi = float(prev["rsi"])
            atr = max(float(last["atr"]), 1e-9)

            # LONG: reclaim EMA20 + Trendstruktur
            if (
                float(prev["close"]) <= float(prev["ema20"])
                and price > ema20
                and ema20 > ema50
                and rsi >= 50.0
                and rsi > prev_rsi
            ):
                entry = price
                sl = entry - 1.25 * atr
                tp = entry + 2.8 * atr
                rr = self._calc_rr(entry, sl, tp)
                conf = min(89.0, 52.0 + max(0.0, rsi - 50.0) * 0.9)
                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.LONG,
                    confidence=round(conf, 1),
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=f"[LONG] EMA20 Reclaim + RSI>{50:.0f}",
                    volume_confirmed=self._confirm_volume(frame),
                )

            # SHORT: verliert EMA20 + Trendstruktur
            if (
                float(prev["close"]) >= float(prev["ema20"])
                and price < ema20
                and ema20 < ema50
                and rsi <= 50.0
                and rsi < prev_rsi
            ):
                entry = price
                sl = entry + 1.25 * atr
                tp = entry - 2.8 * atr
                rr = self._calc_rr(entry, sl, tp)
                conf = min(89.0, 52.0 + max(0.0, 50.0 - rsi) * 0.9)
                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.SHORT,
                    confidence=round(conf, 1),
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=f"[SHORT] EMA20 Loss + RSI<{50:.0f}",
                    volume_confirmed=self._confirm_volume(frame),
                )

            return self._no_signal(symbol, timeframe, "Kein EMA-Reclaim-Setup")
        except Exception as e:
            logger.error(f"EMAReclaimBreakout Fehler ({symbol}): {e}")
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
