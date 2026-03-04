"""
Backtest-Reporter: Konsolen-Ausgabe und Datei-Export.

Ausgaben:
- Rich-Tabellen auf der Konsole (übersichtlich, farbig)
- Trades als CSV (für externe Auswertung, Excel, etc.)
- Zusammenfassung als JSON (für Weiterverarbeitung)
"""

import csv
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from backtest.engine import BacktestConfig, BacktestTrade
from backtest.stats import BacktestStats

console = Console()


def print_report(
    stats: BacktestStats,
    config: BacktestConfig,
    title: str = "BACKTEST ERGEBNIS",
) -> None:
    """Gibt den vollständigen Backtest-Report auf der Konsole aus."""

    pnl_ok = stats.total_pnl_abs >= 0
    pnl_color = "green" if pnl_ok else "red"

    # ── Header ────────────────────────────────────────────────────────────
    console.print()
    console.print(Panel.fit(
        f"[bold cyan]{title}[/bold cyan]\n"
        f"[dim]{config.symbol} | {config.timeframe} | "
        f"Startkapital: {config.initial_balance:,.0f} USDT | "
        f"Fee: {config.fee_pct}%/Seite | Slippage: {config.slippage_pct}% | "
        f"Positionsgröße: {config.position_size_pct}%[/dim]",
        border_style="cyan",
    ))

    # ── Gesamt-Performance ────────────────────────────────────────────────
    t = Table(title="📊 Gesamt-Performance", box=box.ROUNDED, border_style="cyan")
    t.add_column("Kennzahl", style="bold", min_width=22)
    t.add_column("Wert", justify="right", min_width=20)

    pf_str = (
        f"{stats.profit_factor:.2f}"
        if stats.profit_factor != float("inf")
        else "∞  (keine Verluste)"
    )
    dd_color = (
        "red" if stats.max_drawdown_pct > 20
        else "yellow" if stats.max_drawdown_pct > 10
        else "green"
    )
    sharpe_color = (
        "green" if stats.sharpe_ratio > 1
        else "yellow" if stats.sharpe_ratio > 0
        else "red"
    )

    t.add_row("Startkapital",  f"{stats.initial_balance:>12,.2f} USDT")
    t.add_row(
        "Endkapital",
        f"[{pnl_color}]{stats.final_balance:>12,.2f} USDT[/{pnl_color}]",
    )
    t.add_row(
        "Gesamt-PnL",
        f"[{pnl_color}]{stats.total_pnl_abs:>+12,.4f} USDT  ({stats.total_pnl_pct:+.2f}%)[/{pnl_color}]",
    )
    t.add_row("Gebühren gesamt",  f"{stats.total_fees:>12,.4f} USDT")
    t.add_row("", "")
    t.add_row("Trades gesamt",    f"{stats.n_trades:>12}")
    t.add_row("Gewinner",         f"[green]{stats.n_wins:>12}[/green]")
    t.add_row("Verlierer",        f"[red]{stats.n_losses:>12}[/red]")
    if stats.n_end_of_data:
        t.add_row(
            "davon End-of-Data",
            f"[dim]{stats.n_end_of_data:>12}[/dim]  [dim](kein echter Exit)[/dim]",
        )
    t.add_row("Win-Rate",         f"{stats.winrate_pct:>11.1f}%")
    t.add_row("", "")
    t.add_row("Profit Factor",    f"{pf_str:>20}")
    t.add_row("Ø Gewinn",         f"[green]{stats.avg_win_abs:>+12,.4f} USDT[/green]")
    t.add_row("Ø Verlust",        f"[red]{stats.avg_loss_abs:>+12,.4f} USDT[/red]  [dim](nach Fees)[/dim]")
    t.add_row("", "")
    t.add_row(
        "Max Drawdown",
        f"[{dd_color}]{stats.max_drawdown_pct:>11.2f}%[/{dd_color}]",
    )
    t.add_row(
        "Sharpe Ratio",
        f"[{sharpe_color}]{stats.sharpe_ratio:>12.2f}[/{sharpe_color}]  [dim](ann.)[/dim]",
    )
    console.print(t)

    # ── Pro Strategie ─────────────────────────────────────────────────────
    if stats.per_strategy:
        st = Table(
            title="📈 Performance pro Strategie",
            box=box.ROUNDED,
            border_style="blue",
        )
        st.add_column("Strategie", style="bold", min_width=22)
        st.add_column("Trades", justify="right")
        st.add_column("Wins", justify="right")
        st.add_column("Win-%", justify="right")
        st.add_column("PnL (USDT)", justify="right", min_width=14)
        st.add_column("PF", justify="right")
        st.add_column("Ø Win", justify="right")
        st.add_column("Ø Loss", justify="right")

        for s in stats.per_strategy.values():
            c = "green" if s.total_pnl >= 0 else "red"
            pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "∞"
            st.add_row(
                s.name,
                str(s.n_trades),
                str(s.n_wins),
                f"{s.winrate_pct:.1f}%",
                f"[{c}]{s.total_pnl:+,.4f}[/{c}]",
                pf,
                f"[green]+{s.avg_win:.4f}[/green]",
                f"[red]-{s.avg_loss:.4f}[/red]",
            )
        console.print(st)

    # ── LONG vs SHORT ─────────────────────────────────────────────────────
    if stats.long_stats or stats.short_stats:
        ls = Table(
            title="🟢🔴 LONG vs SHORT",
            box=box.ROUNDED,
            border_style="green",
        )
        ls.add_column("Seite", style="bold")
        ls.add_column("Trades", justify="right")
        ls.add_column("Wins", justify="right")
        ls.add_column("Win-%", justify="right")
        ls.add_column("PnL (USDT)", justify="right", min_width=14)
        ls.add_column("PF", justify="right")

        for side_s in [stats.long_stats, stats.short_stats]:
            if side_s is None:
                continue
            c = "green" if side_s.total_pnl >= 0 else "red"
            icon = "🟢 LONG" if side_s.side == "long" else "🔴 SHORT"
            pf = (
                f"{side_s.profit_factor:.2f}"
                if side_s.profit_factor != float("inf")
                else "∞"
            )
            ls.add_row(
                f"[bold]{icon}[/bold]",
                str(side_s.n_trades),
                str(side_s.n_wins),
                f"{side_s.winrate_pct:.1f}%",
                f"[{c}]{side_s.total_pnl:+,.4f}[/{c}]",
                pf,
            )
        console.print(ls)

    # ── Per Regime ────────────────────────────────────────────────────────
    if stats.per_regime:
        rt = Table(
            title="🔭 Performance pro Regime",
            box=box.ROUNDED,
            border_style="yellow",
        )
        rt.add_column("Regime", style="bold")
        rt.add_column("Trades", justify="right")
        rt.add_column("Wins", justify="right")
        rt.add_column("Win-%", justify="right")
        rt.add_column("PnL (USDT)", justify="right", min_width=14)

        for reg, d in sorted(stats.per_regime.items()):
            c = "green" if d["total_pnl"] >= 0 else "red"
            rt.add_row(
                reg,
                str(d["n_trades"]),
                str(d["n_wins"]),
                f"{d['winrate_pct']:.1f}%",
                f"[{c}]{d['total_pnl']:+,.4f}[/{c}]",
            )
        console.print(rt)

    console.print()


def export_trades_csv(trades: List[BacktestTrade], output_path: str) -> None:
    """Exportiert alle Trades als CSV-Datei."""
    if not trades:
        console.print("[dim]Keine Trades zum Exportieren.[/dim]")
        return

    _ensure_dir(output_path)

    fields = [
        "id", "strategy_name", "symbol", "side",
        "entry_time", "exit_time",
        "entry_price", "exit_price",
        "stop_loss", "take_profit",
        "position_size", "cost",
        "rr_planned", "pnl_abs", "pnl_pct",
        "fee_entry", "fee_exit",
        "exit_reason", "confidence", "regime",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            row = {k: getattr(trade, k, "") for k in fields}
            writer.writerow(row)

    console.print(f"[green]✓ Trades CSV:[/green] {output_path}  ({len(trades)} Zeilen)")


def export_summary_json(
    stats: BacktestStats,
    config: BacktestConfig,
    output_path: str,
) -> None:
    """Exportiert die Zusammenfassung als JSON-Datei."""
    _ensure_dir(output_path)

    import dataclasses

    def _serialize(obj):
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {k: _serialize(v) for k, v in dataclasses.asdict(obj).items()}
        if isinstance(obj, dict):
            return {k: _serialize(v) for k, v in obj.items()}
        if isinstance(obj, float):
            if math.isinf(obj) or math.isnan(obj):
                return None
        return obj

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": _serialize(config),
        "stats": _serialize(stats),
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    console.print(f"[green]✓ Summary JSON:[/green] {output_path}")


def _ensure_dir(path: str) -> None:
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
