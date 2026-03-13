import pandas as pd
import ta
from config.settings import settings
from src.utils.logger import setup_logger
from .base_strategy import BaseStrategy, Signal, TradeSignal

logger = setup_logger("strategy.rsi_ema")


class RsiEmaStrategy(BaseStrategy):
    """
    RSI + EMA Kreuzungsstrategie.

    KAUF:  RSI unter Oversold-Level UND kurze EMA kreuzt über lange EMA
    VERKAUF: RSI über Overbought-Level ODER kurze EMA kreuzt unter lange EMA
    """

    def __init__(self):
        super().__init__("RSI_EMA")
        self.rsi_period = settings.RSI_PERIOD
        self.rsi_oversold = settings.RSI_OVERSOLD
        self.rsi_overbought = settings.RSI_OVERBOUGHT
        self.ema_short = settings.EMA_SHORT
        self.ema_long = settings.EMA_LONG

    def analyze(self, df: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self._validate_df(df, min_rows=self.ema_long + 10):
            return TradeSignal(Signal.HOLD, symbol, 0.0, reason="Nicht genug Daten")

        df = df.copy()
        df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=self.rsi_period).rsi()
        df["ema_short"] = ta.trend.EMAIndicator(df["close"], window=self.ema_short).ema_indicator()
        df["ema_long"] = ta.trend.EMAIndicator(df["close"], window=self.ema_long).ema_indicator()

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(last["close"])

        rsi = float(last["rsi"])
        ema_short_now = float(last["ema_short"])
        ema_long_now = float(last["ema_long"])
        ema_short_prev = float(prev["ema_short"])
        ema_long_prev = float(prev["ema_long"])

        # EMA-Kreuzung nach oben (bullisch)
        ema_bullish_cross = ema_short_prev <= ema_long_prev and ema_short_now > ema_long_now
        # EMA-Kreuzung nach unten (bärisch)
        ema_bearish_cross = ema_short_prev >= ema_long_prev and ema_short_now < ema_long_now

        if rsi < self.rsi_oversold and ema_bullish_cross:
            confidence = round((self.rsi_oversold - rsi) / self.rsi_oversold, 2)
            return TradeSignal(
                Signal.BUY, symbol, price,
                confidence=min(confidence, 1.0),
                reason=f"RSI={rsi:.1f} (oversold) + EMA-Kreuzung bullisch"
            )

        if rsi < self.rsi_oversold and ema_short_now > ema_long_now:
            confidence = round((self.rsi_oversold - rsi) / self.rsi_oversold * 0.7, 2)
            return TradeSignal(
                Signal.BUY, symbol, price,
                confidence=confidence,
                reason=f"RSI={rsi:.1f} (oversold) + EMA bullisch"
            )

        if rsi > self.rsi_overbought or ema_bearish_cross:
            reason = f"RSI={rsi:.1f} (overbought)" if rsi > self.rsi_overbought else "EMA-Kreuzung bärisch"
            return TradeSignal(
                Signal.SELL, symbol, price,
                confidence=0.7,
                reason=reason
            )

        return TradeSignal(Signal.HOLD, symbol, price, reason=f"RSI={rsi:.1f} – kein klares Signal")
