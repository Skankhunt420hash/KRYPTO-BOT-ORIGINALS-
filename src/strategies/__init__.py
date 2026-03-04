from .base_strategy import BaseStrategy, Signal, TradeSignal
from .rsi_ema_strategy import RsiEmaStrategy
from .macd_strategy import MacdStrategy
from .combined_strategy import CombinedStrategy


def get_strategy(name: str) -> BaseStrategy:
    strategies = {
        "rsi_ema": RsiEmaStrategy,
        "macd_crossover": MacdStrategy,
        "combined": CombinedStrategy,
    }
    cls = strategies.get(name.lower())
    if cls is None:
        raise ValueError(
            f"Unbekannte Strategie '{name}'. Verfügbar: {list(strategies.keys())}"
        )
    return cls()


__all__ = [
    "BaseStrategy",
    "Signal",
    "TradeSignal",
    "RsiEmaStrategy",
    "MacdStrategy",
    "CombinedStrategy",
    "get_strategy",
]
