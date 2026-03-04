#!/usr/bin/env python3
"""
KRYPTO-BOT ORIGINALS
====================
Einstiegspunkt des Trading Bots und Backtesters.

Trading:
    python main.py --once       # Einmaligen Zyklus ausführen
    python main.py --status     # Kontostand & DB-Statistik anzeigen
    python main.py --multi      # Multi-Strategy-Modus (Meta-Selector)

Backtesting:
    python main.py --backtest --csv data/BTC_1h.csv --strategy trend_continuation
    python main.py --backtest --csv data/BTC_1h.csv --multi
    python main.py --backtest --csv data/BTC_1h.csv --multi --export results/
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from src.bot import TradingBot, MultiStrategyBot
from src.storage.trade_repository import TradeRepository
from config.settings import settings

console = Console()


def print_banner(multi: bool = False):
    mode_str = "[green]Multi-Strategy (Auto)[/green]" if multi else "[yellow]Single-Strategy[/yellow]"
    console.print(Panel.fit(
        f"[bold cyan]KRYPTO-BOT ORIGINALS[/bold cyan]\n"
        f"[dim]Automatisierter Krypto Trading Bot[/dim]\n"
        f"Modus: {mode_str}",
        border_style="cyan",
    ))


def show_status(bot):
    stats = bot.risk.get_stats()

    # Runtime-Statistik (in-memory)
    table = Table(title="Bot Status (Session)", border_style="cyan")
    table.add_column("Parameter", style="bold")
    table.add_column("Wert")

    table.add_row("Modus", settings.TRADING_MODE.upper())
    table.add_row("Strategie", settings.STRATEGY)
    table.add_row("Exchange", settings.EXCHANGE)
    table.add_row("Balance", f"{stats['balance']:.2f} USDT")
    table.add_row("Gesamt PnL (Session)", f"{stats['total_pnl']:+.4f} USDT")
    table.add_row("Trades (Session)", str(stats['total_trades']))
    table.add_row("Win-Rate (Session)", f"{stats['winrate_pct']:.1f}%")
    table.add_row("Offene Positionen", str(stats['open_positions']))
    console.print(table)

    # Datenbank-Statistik (persistiert)
    repo = TradeRepository()
    db_stats = repo.get_summary_stats()
    if db_stats:
        db_table = Table(title="Datenbank-Statistik (Gesamt)", border_style="green")
        db_table.add_column("Parameter", style="bold")
        db_table.add_column("Wert")

        db_table.add_row("Abgeschlossene Trades", str(db_stats.get("closed_trades", 0)))
        db_table.add_row("Offene Trades (DB)", str(db_stats.get("open_trades", 0)))
        db_table.add_row("Blockierte Signale", str(db_stats.get("rejected_trades", 0)))
        db_table.add_row("Gewinner", str(db_stats.get("winners", 0)))
        db_table.add_row("Verlierer", str(db_stats.get("losers", 0)))
        db_table.add_row("Win-Rate (DB)", f"{db_stats.get('winrate_pct', 0):.1f}%")
        db_table.add_row("Gesamt PnL (DB)", f"{db_stats.get('total_pnl', 0):+.4f} USDT")
        db_table.add_row("Ø PnL pro Trade", f"{db_stats.get('avg_pnl', 0):+.4f} USDT")
        console.print(db_table)

    # Letzte 5 Trades aus DB
    recent = repo.get_recent_trades(limit=5, status="closed")
    if recent:
        rt = Table(title="Letzte 5 abgeschlossene Trades", border_style="blue")
        for col in ["id", "timestamp_open", "symbol", "strategy_name", "side",
                    "entry_price", "exit_price", "pnl_abs", "status"]:
            rt.add_column(col, overflow="fold")
        for row in recent:
            pnl = row.get("pnl_abs")
            pnl_str = f"{pnl:+.4f}" if pnl is not None else "-"
            rt.add_row(
                str(row.get("id", "")),
                str(row.get("timestamp_open", ""))[:16],
                str(row.get("symbol", "")),
                str(row.get("strategy_name", "")),
                str(row.get("side", "")),
                f"{row.get('entry_price', 0):.4f}",
                f"{row.get('exit_price', 0):.4f}" if row.get("exit_price") else "-",
                pnl_str,
                str(row.get("status", "")),
            )
        console.print(rt)


def _show_strategy_stats() -> None:
    """
    Gibt eine formatierte Übersicht der Strategie-Performance aus der DB aus.
    Aufgerufen durch: python3 main.py --strategy-stats
    """
    from src.engine.performance_tracker import PerformanceTracker
    from src.engine.strategy_scorer import StrategyScorer

    console.print()
    console.print(Panel.fit(
        "[bold cyan]STRATEGIE-PERFORMANCE-ÜBERSICHT[/bold cyan]\n"
        f"[dim]Modus: {settings.TRADING_MODE.upper()} | "
        f"DB: {settings.DATABASE_URL}[/dim]",
        border_style="cyan",
    ))

    tracker = PerformanceTracker()
    scorer = StrategyScorer(tracker)

    if not tracker.available:
        console.print("[red]Datenbank nicht verfügbar.[/red]")
        return

    all_global = tracker.all_global()
    if not all_global:
        console.print(
            f"[yellow]Noch keine abgeschlossenen Trades in der DB.\n"
            f"Starte den Bot und führe Trades durch, um Performance-Daten zu sammeln.[/yellow]"
        )
        return

    # ── Globale Übersicht ──────────────────────────────────────────────────
    gt = Table(
        title="📊 Globale Performance (alle Regimes)",
        box=box.ROUNDED, border_style="cyan",
    )
    gt.add_column("Strategie", style="bold", min_width=22)
    gt.add_column("Trades", justify="right")
    gt.add_column("Win-%", justify="right")
    gt.add_column("PnL (USDT)", justify="right", min_width=12)
    gt.add_column("PF", justify="right")
    gt.add_column("Ø Win", justify="right")
    gt.add_column("Ø Loss", justify="right")
    gt.add_column("Max DD%", justify="right")
    gt.add_column("Streak", justify="right")
    gt.add_column("Recency", justify="right")
    gt.add_column("Score", justify="right", min_width=7)

    for name, m in sorted(all_global.items()):
        score = scorer.get_score(name, "GLOBAL")
        pnl_c = "green" if m.pnl_abs_sum >= 0 else "red"
        dd_c = "red" if m.max_drawdown_pct > 20 else "yellow" if m.max_drawdown_pct > 10 else "green"
        score_c = "green" if score > 0.55 else "red" if score < 0.45 else "yellow"
        streak_c = "red" if m.losing_streak >= 3 else "white"
        gt.add_row(
            name,
            str(m.trade_count),
            f"{m.win_rate:.1f}%",
            f"[{pnl_c}]{m.pnl_abs_sum:+,.4f}[/{pnl_c}]",
            f"{m.profit_factor:.2f}",
            f"+{m.avg_win:.4f}",
            f"-{m.avg_loss:.4f}",
            f"[{dd_c}]{m.max_drawdown_pct:.1f}%[/{dd_c}]",
            f"[{streak_c}]{m.losing_streak}[/{streak_c}]",
            f"{m.recency_win_rate:.0%}",
            f"[{score_c}]{score:.2f}[/{score_c}]",
        )
    console.print(gt)

    # ── Per-Regime-Aufschlüsselung ─────────────────────────────────────────
    all_regime = tracker.all_regime()
    if all_regime:
        rt = Table(
            title="🔭 Performance pro Strategie × Regime",
            box=box.ROUNDED, border_style="blue",
        )
        rt.add_column("Strategie", style="bold", min_width=22)
        rt.add_column("Regime", min_width=16)
        rt.add_column("Trades", justify="right")
        rt.add_column("Win-%", justify="right")
        rt.add_column("PnL (USDT)", justify="right", min_width=12)
        rt.add_column("PF", justify="right")
        rt.add_column("Score(reg)", justify="right")

        for name in sorted(all_regime.keys()):
            for regime, m in sorted(all_regime[name].items()):
                reg_score = scorer.get_score(name, regime)
                pnl_c = "green" if m.pnl_abs_sum >= 0 else "red"
                sc_c = "green" if reg_score > 0.55 else "red" if reg_score < 0.45 else "yellow"
                rt.add_row(
                    name,
                    regime,
                    str(m.trade_count),
                    f"{m.win_rate:.1f}%",
                    f"[{pnl_c}]{m.pnl_abs_sum:+,.4f}[/{pnl_c}]",
                    f"{m.profit_factor:.2f}",
                    f"[{sc_c}]{reg_score:.2f}[/{sc_c}]",
                )
        console.print(rt)

    # ── Score-Legende ──────────────────────────────────────────────────────
    console.print(
        f"\n[dim]Score-Legende: "
        f"[green]> 0.55[/green] = überdurchschnittlich | "
        f"[yellow]0.45–0.55[/yellow] = neutral | "
        f"[red]< 0.45[/red] = unterdurchschnittlich\n"
        f"Neutral-Score 0.50 = weniger als {settings.PERF_TRACKER_MIN_TRADES} Trades "
        f"(kein ausreichender Datensatz)\n"
        f"Performance-Gewicht im Meta-Selector: {settings.PERF_SELECTOR_WEIGHT} "
        f"(max ±{settings.PERF_SELECTOR_WEIGHT * 0.5:.3f} Anpassung)[/dim]"
    )
    console.print()


def main():
    parser = argparse.ArgumentParser(
        description="Krypto Trading Bot + Backtester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Backtest-Beispiele:\n"
            "  python main.py --backtest --csv data/BTC_1h.csv --strategy trend_continuation\n"
            "  python main.py --backtest --csv data/BTC_1h.csv --multi\n"
            "  python main.py --backtest --csv data/BTC_1h.csv --multi --export results/\n"
        ),
    )

    # ── Trading-Args ──────────────────────────────────────────────────────
    parser.add_argument("--once", action="store_true", help="Nur einen Zyklus ausführen")
    parser.add_argument("--status", action="store_true", help="Status anzeigen und beenden")
    parser.add_argument("--interval", type=int, default=None, help="Wartezeit in Sekunden")
    parser.add_argument(
        "--multi", action="store_true",
        help="Multi-Strategy-Modus (Meta-Selector) – gilt für Trading UND Backtest",
    )
    parser.add_argument(
        "--strategy-stats", action="store_true", dest="strategy_stats",
        help="Strategie-Performance-Übersicht aus DB anzeigen und beenden",
    )

    # ── Backtest-Args ─────────────────────────────────────────────────────
    parser.add_argument(
        "--backtest", action="store_true",
        help="Backtest-Modus aktivieren (kein Live-/Paper-Trading)",
    )
    parser.add_argument(
        "--csv", type=str, default=None,
        help="Pfad zur OHLCV-CSV-Datei (Pflicht im Backtest-Modus)",
    )
    parser.add_argument(
        "--strategy", type=str, default=None,
        help=(
            "Strategie für Backtest: momentum_pullback, range_reversion, "
            "volatility_breakout, trend_continuation"
        ),
    )
    parser.add_argument(
        "--timeframe", type=str, default=None,
        help="Zeitrahmen-Override für Backtest (z.B. 1h, 4h) – sonst aus Dateiname",
    )
    parser.add_argument(
        "--export", type=str, default=None,
        help="Ausgabeverzeichnis für Backtest-Ergebnisse (CSV + JSON)",
    )
    parser.add_argument(
        "--initial-balance", type=float, default=10_000.0, dest="initial_balance",
        help="Startkapital für Backtest (Standard: 10000 USDT)",
    )
    parser.add_argument(
        "--fee", type=float, default=0.10,
        help="Handelsgebühr in %% pro Seite (Standard: 0.10)",
    )
    parser.add_argument(
        "--slippage", type=float, default=0.05,
        help="Slippage in %% (Standard: 0.05)",
    )
    parser.add_argument(
        "--position-size", type=float, default=2.0, dest="position_size",
        help="Positionsgröße in %% des Kapitals pro Trade (Standard: 2.0)",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=40.0, dest="min_confidence",
        help="Mindest-Konfidenz 0-100 für Signale im Backtest (Standard: 40)",
    )

    args = parser.parse_args()

    # ── Strategy-Stats-Modus ──────────────────────────────────────────────
    if args.strategy_stats:
        _show_strategy_stats()
        return

    # ── Backtest-Modus ────────────────────────────────────────────────────
    if args.backtest:
        from backtest.cli import run_backtest
        run_backtest(args)
        return

    # ── Trading-Modus ─────────────────────────────────────────────────────
    # Multi-Modus aktiv wenn: --multi Flag gesetzt ODER STRATEGY=auto in .env
    use_multi = args.multi or settings.STRATEGY.lower() == "auto"

    print_banner(multi=use_multi)

    bot = MultiStrategyBot() if use_multi else TradingBot()

    if args.status:
        show_status(bot)
        return

    if args.once:
        bot.run_cycle()
        show_status(bot)
        return

    bot.run(interval_seconds=args.interval)


if __name__ == "__main__":
    main()
