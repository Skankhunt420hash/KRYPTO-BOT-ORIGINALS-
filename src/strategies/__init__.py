from .base_strategy import BaseStrategy, EnhancedBaseStrategy, Signal, TradeSignal
from .signal import EnhancedSignal, Side
from .rsi_ema_strategy import RsiEmaStrategy
from .macd_strategy import MacdStrategy
from .combined_strategy import CombinedStrategy
from .momentum_pullback import MomentumPullbackStrategy
from .range_reversion import RangeReversionStrategy
from .volatility_breakout import VolatilityBreakoutStrategy
from .trend_continuation import TrendContinuationStrategy
from .ema_reclaim_breakout import EMAReclaimBreakoutStrategy
from .liquidity_sweep_reversal import LiquiditySweepReversalStrategy
from .rsi_macd_confluence import RSIMACDConfluenceStrategy
from .stoch_rsi_mean_reversion import StochRSIMeanReversionStrategy
from .keltner_channel_breakout import KeltnerChannelBreakoutStrategy
from .legacy_adapter import LegacyEnhancedAdapter


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
    """
    Alle Strategien für den Multi-Strategy-Modus (STRATEGY=auto).

    Enhanced-Strategien + Legacy (RSI/EMA, MACD, Combined) als Adapter —
    Regime-Engine + Meta-Selector + Brain wählen pro Symbol die passende.
    """
    core = [
        MomentumPullbackStrategy(),
        RangeReversionStrategy(),
        VolatilityBreakoutStrategy(),
        TrendContinuationStrategy(),
        EMAReclaimBreakoutStrategy(),
        LiquiditySweepReversalStrategy(),
        RSIMACDConfluenceStrategy(),
        StochRSIMeanReversionStrategy(),
        KeltnerChannelBreakoutStrategy(),
    ]
    legacy_wrapped = [
        LegacyEnhancedAdapter(RsiEmaStrategy()),
        LegacyEnhancedAdapter(MacdStrategy()),
        LegacyEnhancedAdapter(CombinedStrategy()),
    ]
    return core + legacy_wrapped


# Registry für den Backtest-Modus (Namen → Klasse)
_ENHANCED_STRATEGY_REGISTRY = {
    "momentum_pullback":    MomentumPullbackStrategy,
    "range_reversion":      RangeReversionStrategy,
    "volatility_breakout":  VolatilityBreakoutStrategy,
    "trend_continuation":   TrendContinuationStrategy,
    "ema_reclaim_breakout": EMAReclaimBreakoutStrategy,
    "liquidity_sweep_reversal": LiquiditySweepReversalStrategy,
    "rsi_macd_confluence": RSIMACDConfluenceStrategy,
    "stoch_rsi_mean_reversion": StochRSIMeanReversionStrategy,
    "keltner_channel_breakout": KeltnerChannelBreakoutStrategy,
}


def get_enhanced_strategy(name: str) -> EnhancedBaseStrategy:
    """
    Gibt eine Enhanced-Strategie für den Backtest-Modus zurück.
    Unterstützte Namen: momentum_pullback, range_reversion,
                        volatility_breakout, trend_continuation
    """
    cls = _ENHANCED_STRATEGY_REGISTRY.get(name.lower().replace("-", "_"))
    if cls is None:
        available = list(_ENHANCED_STRATEGY_REGISTRY.keys())
        raise ValueError(
            f"Unbekannte Enhanced-Strategie '{name}'. "
            f"Verfügbar: {available}. "
            f"Für alle Strategien gleichzeitig: --multi"
        )
    return cls()


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
    "EMAReclaimBreakoutStrategy",
    "LiquiditySweepReversalStrategy",
    "RSIMACDConfluenceStrategy",
    "StochRSIMeanReversionStrategy",
    "KeltnerChannelBreakoutStrategy",
    "get_strategy",
    "get_all_enhanced_strategies",
    "get_enhanced_strategy",
    "LegacyEnhancedAdapter",
]
