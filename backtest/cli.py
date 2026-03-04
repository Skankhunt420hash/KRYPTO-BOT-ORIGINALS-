"""
Backtest CLI-Orchestration.

Wird von main.py aufgerufen wenn --backtest gesetzt ist.
Enthält die vollständige Steuerlogik für Einzel- und Multi-Backtests.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console

from backtest.data_loader import load_csv
from backtest.engine import BacktestConfig, BacktestEngine
from backtest.stats import calculate_stats
from backtest.reporter import print_report, export_trades_csv, export_summary_json
from src.strategies import get_enhanced_strategy, get_all_enhanced_strategies
from src.engine.regime import RegimeEngine
from src.engine.meta_selector import MetaSelector
from src.utils.logger import setup_logger

console = Console()
logger = setup_logger("backtest.cli")


def run_backtest(args: argparse.Namespace) -> None:
    """
    Entry-Point für den Backtest-Modus.
    Wird von main.py mit den geparsten CLI-Args aufgerufen.
    """
    console.print()
    console.print("[bold cyan]─── BACKTEST MODUS ───[/bold cyan]")

    # ── CSV laden und validieren ──────────────────────────────────────────
    if not args.csv:
        console.print("[red]Fehler: --csv <Pfad> ist erforderlich.[/red]")
        console.print("Beispiel: python3 main.py --backtest --csv data/BTCUSDT_1h.csv")
        sys.exit(1)

    symbol = _guess_symbol(args.csv)
    try:
        df = load_csv(args.csv, symbol=symbol)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]Datenfehler: {e}[/red]")
        sys.exit(1)

    # ── Backtest-Konfiguration ────────────────────────────────────────────
    timeframe = getattr(args, "timeframe", None) or _guess_timeframe(args.csv)
    config = BacktestConfig(
        initial_balance=getattr(args, "initial_balance", 10_000.0),
        fee_pct=getattr(args, "fee", 0.10),
        slippage_pct=getattr(args, "slippage", 0.05),
        position_size_pct=getattr(args, "position_size", 2.0),
        timeframe=timeframe,
        symbol=symbol,
        min_confidence=getattr(args, "min_confidence", 40.0),
    )

    console.print(
        f"[dim]Symbol: {symbol} | Zeitrahmen: {timeframe} | "
        f"Kerzen: {len(df):,} | "
        f"Fee: {config.fee_pct}%/Seite | Slippage: {config.slippage_pct}%[/dim]"
    )

    # ── Modus entscheiden: Einzel oder Multi ──────────────────────────────
    use_multi = getattr(args, "multi", False)
    strategy_name = getattr(args, "strategy", None)

    engine = BacktestEngine(config)
    trades = []

    if use_multi or strategy_name == "auto":
        # ── Multi-Strategie-Backtest ──────────────────────────────────────
        console.print("[cyan]Modus: Multi-Strategie (Meta-Selector)[/cyan]")
        strategies = get_all_enhanced_strategies()
        strat_names = [s.name for s in strategies]
        console.print(f"[dim]Strategien: {', '.join(strat_names)}[/dim]")

        try:
            trades = engine.run_multi(
                df=df,
                strategies=strategies,
                regime_engine=RegimeEngine(),
                selector=MetaSelector(),
            )
        except ValueError as e:
            console.print(f"[red]Backtest-Fehler: {e}[/red]")
            sys.exit(1)

        title = f"MULTI-BACKTEST | {symbol} | {timeframe}"

    elif strategy_name:
        # ── Einzel-Strategie-Backtest ─────────────────────────────────────
        try:
            strategy = get_enhanced_strategy(strategy_name)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            _print_available_strategies()
            sys.exit(1)

        console.print(f"[cyan]Modus: Einzelstrategie → {strategy.name}[/cyan]")

        try:
            trades = engine.run_single(df=df, strategy=strategy)
        except ValueError as e:
            console.print(f"[red]Backtest-Fehler: {e}[/red]")
            sys.exit(1)

        title = f"BACKTEST | {strategy.name} | {symbol} | {timeframe}"

    else:
        console.print(
            "[red]Fehler: --strategy <name> oder --multi erforderlich.[/red]\n"
        )
        _print_available_strategies()
        sys.exit(1)

    # ── Statistiken berechnen ─────────────────────────────────────────────
    stats = calculate_stats(trades, config.initial_balance)

    # ── Report ausgeben ───────────────────────────────────────────────────
    print_report(stats, config, title=title)

    if not trades:
        console.print(
            "[yellow]Keine Trades generiert. "
            "Versuche andere Strategie, längere Datenbasis oder "
            "niedrigere --min-confidence.[/yellow]"
        )
        return

    # ── Optionaler Export ─────────────────────────────────────────────────
    export_dir = getattr(args, "export", None)
    if export_dir:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_name = (strategy_name or "multi").replace("/", "_")
        base = Path(export_dir) / f"backtest_{safe_name}_{symbol.replace('/', '')}_{ts}"

        export_trades_csv(trades, str(base) + "_trades.csv")
        export_summary_json(stats, config, str(base) + "_summary.json")


# ── Hilfsfunktionen ────────────────────────────────────────────────────────


def _guess_symbol(csv_path: str) -> str:
    """Versucht das Symbol aus dem Dateinamen abzuleiten."""
    stem = Path(csv_path).stem.upper()
    # Typische Muster: BTCUSDT_1h, BTC_USDT_5m, ETHUSDT
    for sep in ["_", "-"]:
        parts = stem.split(sep)
        if len(parts) >= 2:
            candidate = f"{parts[0]}/{parts[1]}"
            # Plausibilitätsprüfung: erstes Teil 2-5 Zeichen, zweites 3-5 Zeichen
            if 2 <= len(parts[0]) <= 5 and 3 <= len(parts[1]) <= 5:
                return candidate
    # Fallback: gesamter Dateiname
    if len(stem) >= 6:
        return f"{stem[:3]}/{stem[3:6]}"
    return stem


def _guess_timeframe(csv_path: str) -> str:
    """Versucht den Zeitrahmen aus dem Dateinamen abzuleiten."""
    stem = Path(csv_path).stem.lower()
    for tf in ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w"]:
        if tf in stem:
            return tf
    return "1h"


def _print_available_strategies() -> None:
    console.print(
        "\n[bold]Verfügbare Strategien:[/bold]\n"
        "  Einzel:  momentum_pullback, range_reversion, "
        "volatility_breakout, trend_continuation\n"
        "  Multi:   --multi  (alle + Meta-Selector + Regime-Engine)\n"
        "\nBeispiele:\n"
        "  python3 main.py --backtest --csv data/BTC_1h.csv --strategy trend_continuation\n"
        "  python3 main.py --backtest --csv data/BTC_1h.csv --multi\n"
        "  python3 main.py --backtest --csv data/BTC_1h.csv --multi --export results/\n"
    )
