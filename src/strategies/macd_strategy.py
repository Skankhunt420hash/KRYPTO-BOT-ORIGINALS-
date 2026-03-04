import pandas as pd
import ta
from config.settings import settings
from src.utils.logger import setup_logger
from .base_strategy import BaseStrategy, Signal, TradeSignal

logger = setup_logger("strategy.macd")


class MacdStrategy(BaseStrategy):
    """
    MACD Crossover-Strategie.

    KAUF:    MACD-Linie kreuzt über Signal-Linie (bullisches Kreuz)
    VERKAUF: MACD-Linie kreuzt unter Signal-Linie (bärisches Kreuz)
    """

    def __init__(self):
        super().__init__("MACD_Crossover")
        self.fast = settings.MACD_FAST
        self.slow = settings.MACD_SLOW
        self.signal_period = settings.MACD_SIGNAL

    def analyze(self, df: pd.DataFrame, symbol: str) -> TradeSignal:
        if not self._validate_df(df, min_rows=self.slow + self.signal_period + 10):
            return TradeSignal(Signal.HOLD, symbol, 0.0, reason="Nicht genug Daten")

        df = df.copy()
        macd_indicator = ta.trend.MACD(
            df["close"],
            window_fast=self.fast,
            window_slow=self.slow,
            window_sign=self.signal_period,
        )
        df["macd"] = macd_indicator.macd()
        df["macd_signal"] = macd_indicator.macd_signal()
        df["macd_diff"] = macd_indicator.macd_diff()

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(last["close"])

        macd_now = float(last["macd"])
        signal_now = float(last["macd_signal"])
        macd_prev = float(prev["macd"])
        signal_prev = float(prev["macd_signal"])
        diff = float(last["macd_diff"])

        bullish_cross = macd_prev < signal_prev and macd_now >= signal_now
        bearish_cross = macd_prev > signal_prev and macd_now <= signal_now

        if bullish_cross:
            return TradeSignal(
                Signal.BUY, symbol, price,
                confidence=0.75,
                reason=f"MACD bullisches Kreuz (diff={diff:.4f})"
            )

        if bearish_cross:
            return TradeSignal(
                Signal.SELL, symbol, price,
                confidence=0.75,
                reason=f"MACD bärisches Kreuz (diff={diff:.4f})"
            )

        return TradeSignal(
            Signal.HOLD, symbol, price,
            reason=f"MACD={macd_now:.4f} Signal={signal_now:.4f} – kein Kreuz"
        )
