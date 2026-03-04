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
from src.bot import TradingBot
from config.settings import settings

console = Console()


def print_banner():
    console.print(Panel.fit(
        "[bold cyan]KRYPTO-BOT ORIGINALS[/bold cyan]\n"
        "[dim]Automatisierter Krypto Trading Bot[/dim]",
        border_style="cyan",
    ))


def show_status(bot: TradingBot):
    stats = bot.risk.get_stats()
    table = Table(title="Bot Status", border_style="cyan")
    table.add_column("Parameter", style="bold")
    table.add_column("Wert")

    table.add_row("Modus", settings.TRADING_MODE.upper())
    table.add_row("Strategie", settings.STRATEGY)
    table.add_row("Exchange", settings.EXCHANGE)
    table.add_row("Balance", f"{stats['balance']:.2f} USDT")
    table.add_row("Gesamt PnL", f"{stats['total_pnl']:+.4f} USDT")
    table.add_row("Trades", str(stats['total_trades']))
    table.add_row("Win-Rate", f"{stats['winrate_pct']:.1f}%")
    table.add_row("Offene Positionen", str(stats['open_positions']))

    console.print(table)


def main():
    parser = argparse.ArgumentParser(description="Krypto Trading Bot")
    parser.add_argument("--once", action="store_true", help="Nur einen Zyklus ausführen")
    parser.add_argument("--status", action="store_true", help="Status anzeigen und beenden")
    parser.add_argument("--interval", type=int, default=None, help="Wartezeit in Sekunden (überschreibt Zeitrahmen)")
    args = parser.parse_args()

    print_banner()

    bot = TradingBot()

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
