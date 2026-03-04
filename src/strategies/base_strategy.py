from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import pandas as pd

from src.strategies.signal import EnhancedSignal, Side


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


class EnhancedBaseStrategy(ABC):
    """
    Basisklasse für alle Multi-Strategy-Strategien.
    Gibt EnhancedSignal zurück (einheitliches Format mit SL, TP, RR, confidence 0-100).
    """

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        """Analysiert OHLCV-Daten und gibt ein EnhancedSignal zurück."""
        pass

    def _validate_df(self, df: pd.DataFrame, min_rows: int = 60) -> bool:
        return df is not None and not df.empty and len(df) >= min_rows

    def _calc_rr(self, entry: float, sl: float, tp: float) -> float:
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        return round(reward / risk, 2) if risk > 0 else 0.0

    def _confirm_volume(self, df: pd.DataFrame, lookback: int = 20) -> bool:
        """True wenn das letzte Volumen über dem gleitenden Durchschnitt liegt."""
        if len(df) < lookback:
            return False
        vol_sma = float(df["volume"].rolling(lookback).mean().iloc[-1])
        return float(df["volume"].iloc[-1]) > vol_sma

    def _no_signal(self, symbol: str, timeframe: str, reason: str) -> EnhancedSignal:
        return EnhancedSignal(
            strategy_name=self.name,
            symbol=symbol,
            timeframe=timeframe,
            side=Side.NONE,
            confidence=0.0,
            entry=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            rr=0.0,
            reason=reason,
        )
