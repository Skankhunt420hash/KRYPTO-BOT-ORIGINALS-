"""
Adapter: Legacy BaseStrategy (TradeSignal) → EnhancedSignal für den Multi-Modus.

Damit laufen RSI/EMA, MACD und Combined parallel zu den Enhanced-Strategien;
Meta-Selector + Brain wählen pro Symbol/Regime das beste Signal.
"""

from __future__ import annotations

import pandas as pd

from config.settings import settings
from src.strategies.base_strategy import BaseStrategy, EnhancedBaseStrategy, Signal, TradeSignal
from src.strategies.signal import EnhancedSignal, Side


def _allow_short_from_legacy() -> bool:
    """SHORT aus SELL-Signal: Paper ok; Live nur mit Futures."""
    if not bool(getattr(settings, "SHORT_ENABLED", True)):
        return False
    if str(getattr(settings, "TRADING_MODE", "paper")).lower() != "live":
        return True
    return bool(getattr(settings, "FUTURES_MODE", False))


def trade_signal_to_enhanced(
    ts: TradeSignal,
    *,
    strategy_name: str,
    symbol: str,
    timeframe: str,
    volume_confirmed: bool = False,
) -> EnhancedSignal:
    """Wandelt TradeSignal in EnhancedSignal; HOLD → kein Trade."""
    sl_pct = float(getattr(settings, "STOP_LOSS_PERCENT", 2.0))
    tp_pct = float(getattr(settings, "TAKE_PROFIT_PERCENT", 4.0))

    if ts.signal == Signal.HOLD or ts.price <= 0:
        return EnhancedSignal(
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            side=Side.NONE,
            confidence=0.0,
            entry=0.0,
            stop_loss=0.0,
            take_profit=0.0,
            rr=0.0,
            reason=ts.reason or "HOLD",
            volume_confirmed=False,
        )

    price = float(ts.price)
    conf = max(0.0, min(100.0, float(ts.confidence) * 100.0))

    if ts.signal == Signal.BUY:
        sl = price * (1.0 - sl_pct / 100.0)
        tp = price * (1.0 + tp_pct / 100.0)
        risk = abs(price - sl)
        reward = abs(tp - price)
        rr = round(reward / risk, 2) if risk > 1e-12 else 0.0
        return EnhancedSignal(
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            side=Side.LONG,
            confidence=conf,
            entry=price,
            stop_loss=sl,
            take_profit=tp,
            rr=rr,
            reason=ts.reason or "legacy_buy",
            volume_confirmed=volume_confirmed,
        )

    if ts.signal == Signal.SELL:
        if not _allow_short_from_legacy():
            return EnhancedSignal(
                strategy_name=strategy_name,
                symbol=symbol,
                timeframe=timeframe,
                side=Side.NONE,
                confidence=0.0,
                entry=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                rr=0.0,
                reason="legacy_sell_skipped_short_disabled",
                volume_confirmed=False,
            )
        sl = price * (1.0 + sl_pct / 100.0)
        tp = price * (1.0 - tp_pct / 100.0)
        risk = abs(sl - price)
        reward = abs(price - tp)
        rr = round(reward / risk, 2) if risk > 1e-12 else 0.0
        return EnhancedSignal(
            strategy_name=strategy_name,
            symbol=symbol,
            timeframe=timeframe,
            side=Side.SHORT,
            confidence=conf,
            entry=price,
            stop_loss=sl,
            take_profit=tp,
            rr=rr,
            reason=ts.reason or "legacy_sell_short",
            volume_confirmed=volume_confirmed,
        )

    return EnhancedSignal(
        strategy_name=strategy_name,
        symbol=symbol,
        timeframe=timeframe,
        side=Side.NONE,
        confidence=0.0,
        entry=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        rr=0.0,
        reason="legacy_unknown_signal",
        volume_confirmed=False,
    )


class LegacyEnhancedAdapter(EnhancedBaseStrategy):
    """
    Wrappt eine BaseStrategy-Instanz und liefert EnhancedSignal für den Multi-Bot.
    """

    def __init__(self, legacy: BaseStrategy):
        super().__init__(legacy.name)
        self._legacy = legacy

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=60):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")
        ts = self._legacy.analyze(df, symbol)
        vol_ok = self._confirm_volume(df, lookback=20)
        return trade_signal_to_enhanced(
            ts,
            strategy_name=self.name,
            symbol=symbol,
            timeframe=timeframe,
            volume_confirmed=vol_ok,
        )
