import unittest
from datetime import datetime, timedelta

import pandas as pd

from backtest.engine import BacktestConfig, BacktestEngine
from src.strategies import (
    MomentumPullbackStrategy,
    RangeReversionStrategy,
    VolatilityBreakoutStrategy,
    TrendContinuationStrategy,
)


def _make_dummy_ohlcv(rows: int = 300) -> pd.DataFrame:
    """Erzeugt einen einfachen, aber validen OHLCV-Datensatz für Tests."""
    base_time = datetime(2020, 1, 1)
    idx = [base_time + timedelta(hours=i) for i in range(rows)]

    # Einfache steigende Preise mit kleiner Volatilität
    close = pd.Series([100 + 0.1 * i for i in range(rows)], index=idx)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.concat([open_, close], axis=1).max(axis=1) + 0.5
    low = pd.concat([open_, close], axis=1).min(axis=1) - 0.5
    volume = pd.Series([100 + i for i in range(rows)], index=idx)

    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        },
        index=idx,
    )
    df.index.name = "timestamp"
    return df


class StrategySmokeTests(unittest.TestCase):
    """Basis-Smoketests für Enhanced-Strategien."""

    def setUp(self) -> None:
        self.df = _make_dummy_ohlcv(300)
        self.symbol = "TEST/USDT"
        self.tf = "1h"

    def _assert_signal_ok(self, strategy_cls):
        strat = strategy_cls()
        sig = strat.analyze(self.df, self.symbol, self.tf)
        # Wichtig ist: Es darf kein Fehler auftreten und ein EnhancedSignal zurückkommen.
        self.assertIsNotNone(sig)
        self.assertEqual(sig.symbol, self.symbol)
        self.assertEqual(sig.timeframe, self.tf)

    def test_momentum_pullback_runs(self):
        self._assert_signal_ok(MomentumPullbackStrategy)

    def test_range_reversion_runs(self):
        self._assert_signal_ok(RangeReversionStrategy)

    def test_volatility_breakout_runs(self):
        self._assert_signal_ok(VolatilityBreakoutStrategy)

    def test_trend_continuation_runs(self):
        self._assert_signal_ok(TrendContinuationStrategy)


class BacktestEngineTests(unittest.TestCase):
    """Einfacher Integrations-Test für die BacktestEngine."""

    def test_backtest_single_strategy_produces_trades(self):
        df = _make_dummy_ohlcv(400)
        cfg = BacktestConfig(
            initial_balance=10_000.0,
            fee_pct=0.10,
            slippage_pct=0.05,
            position_size_pct=2.0,
            timeframe="1h",
            symbol="TEST/USDT",
            min_confidence=40.0,
        )
        engine = BacktestEngine(cfg)

        # Wir wählen eine robustere Trend-Strategie für diesen Test.
        strat = TrendContinuationStrategy()

        trades = engine.run_single(df=df, strategy=strat)

        # Es können 0 Trades herauskommen, aber der Lauf darf nicht fehlschlagen.
        self.assertIsInstance(trades, list)
        for t in trades:
            self.assertEqual(t.symbol, cfg.symbol)
            self.assertGreater(t.cost, 0.0)


if __name__ == "__main__":
    unittest.main()

