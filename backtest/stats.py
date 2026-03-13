"""
Backtest-Statistiken.

Berechnet alle Kennzahlen aus einer Liste von BacktestTrades.
Kein I/O, kein Logging – reine Berechnungen.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from backtest.engine import BacktestTrade


@dataclass
class SideStats:
    """Kennzahlen für eine Richtung (LONG oder SHORT)."""

    side: str
    n_trades: int = 0
    n_wins: int = 0
    total_pnl: float = 0.0
    winrate_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0


@dataclass
class StrategyStats:
    """Kennzahlen für eine einzelne Strategie."""

    name: str
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    total_pnl: float = 0.0
    winrate_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0


@dataclass
class BacktestStats:
    """Vollständige Backtest-Auswertung."""

    # Kapital
    initial_balance: float
    final_balance: float
    total_pnl_abs: float
    total_pnl_pct: float

    # Trade-Zähler
    n_trades: int
    n_wins: int
    n_losses: int
    n_end_of_data: int      # Trades durch Daten-Ende geschlossen (keine echten Exits)

    # Kern-Metriken
    winrate_pct: float
    profit_factor: float
    avg_win_abs: float
    avg_loss_abs: float
    total_fees: float

    # Risiko
    max_drawdown_pct: float
    sharpe_ratio: float      # annualisiert, per-Trade-Ansatz

    # Aufschlüsselung
    per_strategy: Dict[str, StrategyStats] = field(default_factory=dict)
    long_stats: Optional[SideStats] = None
    short_stats: Optional[SideStats] = None
    per_regime: Dict[str, Dict] = field(default_factory=dict)


def calculate_stats(
    trades: List[BacktestTrade],
    initial_balance: float,
) -> BacktestStats:
    """
    Berechnet alle Backtest-Kennzahlen aus einer Liste abgeschlossener Trades.

    Hinweis: Trades mit exit_reason='end_of_data' werden in die Statistik
    einbezogen, aber separat gezählt (der Schlusskurs ist kein echter Exit).
    """
    _empty = BacktestStats(
        initial_balance=initial_balance,
        final_balance=initial_balance,
        total_pnl_abs=0.0,
        total_pnl_pct=0.0,
        n_trades=0,
        n_wins=0,
        n_losses=0,
        n_end_of_data=0,
        winrate_pct=0.0,
        profit_factor=0.0,
        avg_win_abs=0.0,
        avg_loss_abs=0.0,
        total_fees=0.0,
        max_drawdown_pct=0.0,
        sharpe_ratio=0.0,
        per_strategy={},
        long_stats=_side_stats_empty("long"),
        short_stats=_side_stats_empty("short"),
        per_regime={},
    )

    if not trades:
        return _empty

    pnls = [t.pnl_abs for t in trades if t.pnl_abs is not None]
    if not pnls:
        return _empty

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    total_pnl = sum(pnls)
    n_trades = len(pnls)
    n_wins = len(wins)
    n_losses = len(losses)

    winrate = (n_wins / n_trades * 100) if n_trades else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    avg_win = (gross_profit / n_wins) if n_wins else 0.0
    avg_loss = (gross_loss / n_losses) if n_losses else 0.0

    total_fees = sum(
        (t.fee_entry or 0) + (t.fee_exit or 0) for t in trades
    )

    return BacktestStats(
        initial_balance=round(initial_balance, 2),
        final_balance=round(initial_balance + total_pnl, 2),
        total_pnl_abs=round(total_pnl, 4),
        total_pnl_pct=round(total_pnl / initial_balance * 100, 2),
        n_trades=n_trades,
        n_wins=n_wins,
        n_losses=n_losses,
        n_end_of_data=sum(1 for t in trades if t.exit_reason == "end_of_data"),
        winrate_pct=round(winrate, 1),
        profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else profit_factor,
        avg_win_abs=round(avg_win, 4),
        avg_loss_abs=round(avg_loss, 4),
        total_fees=round(total_fees, 4),
        max_drawdown_pct=round(_max_drawdown(pnls, initial_balance), 2),
        sharpe_ratio=round(_sharpe(pnls), 2),
        per_strategy=_per_strategy(trades),
        long_stats=_build_side_stats("long", trades),
        short_stats=_build_side_stats("short", trades),
        per_regime=_per_regime(trades),
    )


# ── Hilfsfunktionen ────────────────────────────────────────────────────────


def _max_drawdown(pnls: List[float], initial: float) -> float:
    """Max Drawdown in % des laufenden Hochpunkts (Equity-Kurve)."""
    equity = initial
    peak = initial
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _sharpe(pnls: List[float]) -> float:
    """
    Vereinfachter Sharpe-Ratio (per-Trade).
    Annualisiert mit sqrt(252) unter der Annahme ~1 Trade/Tag.
    """
    if len(pnls) < 2:
        return 0.0
    arr = np.array(pnls, dtype=float)
    mu = float(np.mean(arr))
    sigma = float(np.std(arr, ddof=1))
    if sigma == 0:
        return 0.0
    return mu / sigma * math.sqrt(252)


def _per_strategy(trades: List[BacktestTrade]) -> Dict[str, StrategyStats]:
    names = sorted({t.strategy_name for t in trades})
    result = {}
    for name in names:
        sub = [t for t in trades if t.strategy_name == name]
        pnls = [t.pnl_abs for t in sub if t.pnl_abs is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gp = sum(wins)
        gl = abs(sum(losses))
        result[name] = StrategyStats(
            name=name,
            n_trades=len(pnls),
            n_wins=len(wins),
            n_losses=len(losses),
            total_pnl=round(sum(pnls), 4),
            winrate_pct=round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
            avg_win=round(gp / len(wins), 4) if wins else 0.0,
            avg_loss=round(gl / len(losses), 4) if losses else 0.0,
            profit_factor=round(gp / gl, 2) if gl > 0 else float("inf"),
        )
    return result


def _build_side_stats(side: str, trades: List[BacktestTrade]) -> SideStats:
    sub = [t for t in trades if t.side == side]
    pnls = [t.pnl_abs for t in sub if t.pnl_abs is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    return SideStats(
        side=side,
        n_trades=len(pnls),
        n_wins=len(wins),
        total_pnl=round(sum(pnls), 4),
        winrate_pct=round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        avg_win=round(gp / len(wins), 4) if wins else 0.0,
        avg_loss=round(gl / len(losses), 4) if losses else 0.0,
        profit_factor=round(gp / gl, 2) if gl > 0 else float("inf"),
    )


def _side_stats_empty(side: str) -> SideStats:
    return SideStats(side=side)


def _per_regime(trades: List[BacktestTrade]) -> Dict[str, Dict]:
    regimes = sorted({t.regime for t in trades if t.regime})
    result = {}
    for reg in regimes:
        sub = [t for t in trades if t.regime == reg]
        pnls = [t.pnl_abs for t in sub if t.pnl_abs is not None]
        wins = [p for p in pnls if p > 0]
        result[reg] = {
            "n_trades": len(pnls),
            "n_wins": len(wins),
            "total_pnl": round(sum(pnls), 4),
            "winrate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        }
    return result
