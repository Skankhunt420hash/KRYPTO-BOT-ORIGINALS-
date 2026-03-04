from .base_strategy import BaseStrategy, EnhancedBaseStrategy, Signal, TradeSignal
from .signal import EnhancedSignal, Side
from .rsi_ema_strategy import RsiEmaStrategy
from .macd_strategy import MacdStrategy
from .combined_strategy import CombinedStrategy
from .momentum_pullback import MomentumPullbackStrategy
from .range_reversion import RangeReversionStrategy
from .volatility_breakout import VolatilityBreakoutStrategy
from .trend_continuation import TrendContinuationStrategy


def get_strategy(name: str) -> BaseStrategy:
    """Gibt eine Einzel-Strategie (Legacy-Modus) zurück."""
    strategies = {
        "rsi_ema": RsiEmaStrategy,
        "macd_crossover": MacdStrategy,
        "combined": CombinedStrategy,
    }
    cls = strategies.get(name.lower())
    if cls is None:
        raise ValueError(
            f"Unbekannte Strategie '{name}'. "
            f"Verfügbar (Einzel-Modus): {list(strategies.keys())}. "
            f"Für Multi-Strategie: STRATEGY=auto"
        )
    return cls()


def get_all_enhanced_strategies() -> list:
    """Gibt alle Strategien für den Multi-Strategy-Modus zurück."""
    return [
        MomentumPullbackStrategy(),
        RangeReversionStrategy(),
        VolatilityBreakoutStrategy(),
        TrendContinuationStrategy(),
    ]


__all__ = [
    "BaseStrategy",
    "EnhancedBaseStrategy",
    "Signal",
    "TradeSignal",
    "EnhancedSignal",
    "Side",
    "RsiEmaStrategy",
    "MacdStrategy",
    "CombinedStrategy",
    "MomentumPullbackStrategy",
    "RangeReversionStrategy",
    "VolatilityBreakoutStrategy",
    "TrendContinuationStrategy",
    "get_strategy",
    "get_all_enhanced_strategies",
]
