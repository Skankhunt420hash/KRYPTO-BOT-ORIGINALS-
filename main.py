#!/usr/bin/env python3
"""
KRYPTO-BOT ORIGINALS
====================
Einstiegspunkt des Trading Bots.

Verwendung:
    python main.py              # Bot starten (Loop)
    python main.py --once       # Einmaligen Zyklus ausführen
    python main.py --status     # Nur Kontostand & Status anzeigen
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def main():
    parser = argparse.ArgumentParser(description="Krypto Trading Bot")
    parser.add_argument("--once", action="store_true", help="Nur einen Zyklus ausführen")
    parser.add_argument("--status", action="store_true", help="Status anzeigen und beenden")
    parser.add_argument("--interval", type=int, default=None, help="Wartezeit in Sekunden")
    parser.add_argument(
        "--multi", action="store_true",
        help="Multi-Strategy-Modus mit automatischer Strategie-Auswahl (Meta-Selector)"
    )
    args = parser.parse_args()

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
