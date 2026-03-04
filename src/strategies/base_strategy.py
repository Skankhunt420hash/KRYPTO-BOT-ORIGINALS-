from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import pandas as pd


class Signal(Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class TradeSignal:
    signal: Signal
    symbol: str
    price: float
    confidence: float = 0.0
    reason: str = ""

    def is_buy(self) -> bool:
        return self.signal == Signal.BUY

    def is_sell(self) -> bool:
        return self.signal == Signal.SELL


class BaseStrategy(ABC):
    """Basisklasse für alle Handelsstrategien."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def analyze(self, df: pd.DataFrame, symbol: str) -> TradeSignal:
        """Analysiert OHLCV-Daten und gibt ein Handelssignal zurück."""
        pass

    def _validate_df(self, df: pd.DataFrame, min_rows: int = 50) -> bool:
        if df is None or df.empty or len(df) < min_rows:
            return False
        return True
