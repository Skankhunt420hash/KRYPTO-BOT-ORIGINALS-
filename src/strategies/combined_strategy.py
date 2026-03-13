import pandas as pd
from src.utils.logger import setup_logger
from .base_strategy import BaseStrategy, Signal, TradeSignal
from .rsi_ema_strategy import RsiEmaStrategy
from .macd_strategy import MacdStrategy

logger = setup_logger("strategy.combined")


class CombinedStrategy(BaseStrategy):
    """
    Kombinierte Strategie: RSI/EMA + MACD müssen übereinstimmen.
    Höhere Zuverlässigkeit durch doppelte Bestätigung.
    """

    def __init__(self):
        super().__init__("Combined")
        self.rsi_ema = RsiEmaStrategy()
        self.macd = MacdStrategy()

    def analyze(self, df: pd.DataFrame, symbol: str) -> TradeSignal:
        signal_rsi = self.rsi_ema.analyze(df, symbol)
        signal_macd = self.macd.analyze(df, symbol)

        price = signal_rsi.price or signal_macd.price

        if signal_rsi.is_buy() and signal_macd.is_buy():
            combined_conf = round((signal_rsi.confidence + signal_macd.confidence) / 2, 2)
            return TradeSignal(
                Signal.BUY, symbol, price,
                confidence=combined_conf,
                reason=f"RSI/EMA + MACD bestätigen KAUF | {signal_rsi.reason}"
            )

        if signal_rsi.is_sell() or signal_macd.is_sell():
            return TradeSignal(
                Signal.SELL, symbol, price,
                confidence=0.8,
                reason=f"Verkaufssignal: RSI={signal_rsi.signal.value}, MACD={signal_macd.signal.value}"
            )

        return TradeSignal(
            Signal.HOLD, symbol, price,
            reason="Kein übereinstimmendes Signal"
        )
