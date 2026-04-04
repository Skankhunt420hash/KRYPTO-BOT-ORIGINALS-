#!/usr/bin/env python3
"""
KRYPTO-BOT ORIGINALS
====================
Einstiegspunkt des Trading Bots und Backtesters.

Trading:
    python main.py --once        # Einmaligen Zyklus ausführen
    python main.py --status      # Kontostand & DB-Statistik anzeigen
    python main.py --multi       # Multi-Strategy-Modus (Meta-Selector)

Backtesting:
    python main.py --backtest --csv data/BTC_1h.csv --strategy trend_continuation
    python main.py --backtest --csv data/BTC_1h.csv --multi
    python main.py --backtest --csv data/BTC_1h.csv --multi --export results/

Hinweis:
    Die eigentliche Konfiguration erfolgt über die .env-Datei und
    wird in config/settings.py zentral geladen. Dieses Skript stellt
    sicher, dass im Live-Modus notwendige Einstellungen vorhanden sind
    und gibt im Fehlerfall verständliche Hinweise aus.
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
from src.app import TradingApplication
from src.storage.trade_repository import TradeRepository
from config.settings import settings

console = Console()


def _warn_if_no_env_file() -> None:
    """
    Gibt eine deutliche, aber nicht fatale Warnung aus, falls keine .env-Datei
    im Projektverzeichnis vorhanden ist. In diesem Fall werden alle Defaults
    aus config/settings.py verwendet.
    """
    project_root = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(project_root, ".env")
    if not os.path.exists(env_path):
        console.print(
            "[yellow]Hinweis:[/yellow] Keine [bold].env[/bold]-Datei gefunden – "
            "es werden Standardwerte aus [bold]config/settings.py[/bold] verwendet.\n"
            "Kopiere ggf. [bold].env.example[/bold] nach [bold].env[/bold] und passe sie an."
        )


def _validate_runtime_config(args: argparse.Namespace) -> None:
    """
    Prüft die wichtigsten Einstellungen zur Laufzeit und bricht bei
    kritischen Konfigurationsfehlern mit einer klaren Meldung ab.

    Ziel:
      - Papiermodus ohne .env funktionsfähig
      - Live-Modus nur mit gesetzten API-Keys
      - Plausible Handelskonfiguration (Paare, Timeframe)
    """
    errors = []

    mode = (settings.TRADING_MODE or "").lower()
    if mode not in ("paper", "live"):
        errors.append(
            f"BOT_MODE/TRADING_MODE muss 'paper' oder 'live' sein "
            f"(aktuell: '{settings.TRADING_MODE}')."
        )

    # Handelskonfiguration – nur relevant, wenn wir wirklich traden
    is_trading_run = not (args.backtest or args.walk_forward or args.strategy_stats or args.show_health)

    pairs = [p.strip() for p in settings.TRADING_PAIRS if p.strip()]
    universe = (getattr(settings, "TRADING_UNIVERSE", "") or "").strip().lower()
    if is_trading_run and universe == "kraken_perps":
        if settings.EXCHANGE.strip().lower() != "krakenfutures":
            errors.append(
                "TRADING_UNIVERSE=kraken_perps erfordert EXCHANGE=krakenfutures "
                "(Kraken Perpetuals / Futures API, nicht Spot „kraken“)."
            )
    elif is_trading_run and universe == "binance_usdm":
        if settings.EXCHANGE.strip().lower() != "binance":
            errors.append(
                "TRADING_UNIVERSE=binance_usdm erfordert EXCHANGE=binance (USDT-M Perps über ccxt swap)."
            )
        if not getattr(settings, "FUTURES_MODE", False):
            errors.append(
                "TRADING_UNIVERSE=binance_usdm: FUTURES_MODE=true setzen (Swap/Perp-Märkte)."
            )
    elif is_trading_run and not pairs:
        errors.append(
            "TRADING_PAIRS ist leer – konfiguriere Handelspaare in .env "
            "oder TRADING_UNIVERSE=kraken_perps (EXCHANGE=krakenfutures) "
            "bzw. binance_usdm (EXCHANGE=binance, FUTURES_MODE=true)."
        )

    if is_trading_run and not settings.TIMEFRAME:
        errors.append(
            "TIMEFRAME ist leer – setze z.B. TIMEFRAME=1h in .env."
        )

    # Live-Modus: API-Schlüssel sind Pflicht, aber nur wenn wir wirklich traden
    if is_trading_run and mode == "live":
        live_use_multi = bool(args.multi or settings.STRATEGY.lower() == "auto")
        if not live_use_multi:
            errors.append(
                "LIVE-Modus ist nur im Multi-Strategy-Flow erlaubt "
                "(--multi oder STRATEGY=auto).\n"
                "Single-Strategy TradingBot ist für Live absichtlich gesperrt."
            )
        if not settings.LIVE_TRADING_ENABLED:
            errors.append(
                "LIVE-Modus ist gesperrt (LIVE_TRADING_ENABLED=false).\n"
                "Setze LIVE_TRADING_ENABLED=true nur bewusst für einen echten Live-Test."
            )
        if not settings.API_KEY or not settings.API_SECRET:
            errors.append(
                "LIVE-Modus erfordert gültige API_KEY und API_SECRET in .env.\n"
                "Setze TRADING_MODE=paper zum Testen ohne echte Orders "
                "oder hinterlege die Zugangsdaten deines Exchanges."
            )
        if settings.LIVE_TEST_MODE:
            if settings.LIVE_MAX_POSITION_SIZE <= 0:
                errors.append(
                    "LIVE_TEST_MODE=true, aber LIVE_MAX_POSITION_SIZE ist <= 0.\n"
                    "Bitte einen kleinen positiven Wert setzen (z. B. 25)."
                )
            if settings.LIVE_TEST_DAILY_LOSS_LIMIT_PCT <= 0:
                errors.append(
                    "LIVE_TEST_MODE=true, aber LIVE_TEST_DAILY_LOSS_LIMIT_PCT ist <= 0."
                )
            if not settings.LIVE_ALLOWED_SYMBOLS.strip():
                errors.append(
                    "LIVE_TEST_MODE=true, aber LIVE_ALLOWED_SYMBOLS ist leer.\n"
                    "Bitte mindestens ein Symbol setzen (z. B. BTC/USDT)."
                )

    # Telegram-Konfiguration
    # Wenn Telegram explizit aktiviert ist, müssen Token + Chat-ID gesetzt sein.
    if settings.ENABLE_TELEGRAM:
        if not settings.TELEGRAM_BOT_TOKEN:
            errors.append(
                "ENABLE_TELEGRAM=true, aber TELEGRAM_BOT_TOKEN fehlt.\n"
                "Bitte Bot-Token aus BotFather in .env setzen."
            )
        if not settings.TELEGRAM_CHAT_ID:
            errors.append(
                "ENABLE_TELEGRAM=true, aber TELEGRAM_CHAT_ID fehlt.\n"
                "Bitte Chat-ID setzen (siehe README: getUpdates)."
            )

    if errors:
        console.print("[red]Konfigurationsfehler erkannt – Start abgebrochen.[/red]")
        for msg in errors:
            console.print(f"- {msg}")
        console.print(
            "\n[dim]Siehe README.md für den Abschnitt 'Lokaler Start (Paper-Modus)'.[/dim]"
        )
        sys.exit(1)


def print_banner(multi: bool = False):
    mode_str = "[green]Multi-Strategy (Auto)[/green]" if multi else "[yellow]Single-Strategy[/yellow]"
    console.print(Panel.fit(
        f"[bold cyan]KRYPTO-BOT ORIGINALS[/bold cyan]\n"
        f"[dim]Automatisierter Krypto Trading Bot[/dim]\n"
        f"Modus: {mode_str}",
        border_style="cyan",
    ))


def _log_telegram_startup_state() -> None:
    telegram_enabled = bool(settings.TELEGRAM_ENABLED)
    panel_enabled = bool(settings.TELEGRAM_PANEL_ENABLED)
    token_ok = bool(settings.TELEGRAM_BOT_TOKEN)
    chat_ok = bool(settings.TELEGRAM_CHAT_ID)
    console.print(
        "[cyan]Telegram-Startup:[/cyan] "
        f"enabled={telegram_enabled} | panel={panel_enabled} | "
        f"token={'erkannt' if token_ok else 'fehlt'} | "
        f"chat_id={'erkannt' if chat_ok else 'fehlt'}"
    )


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


def _show_health() -> None:
    """
    Zeigt Health-Monitor-Konfiguration und – falls der Bot lief –
    den letzten gespeicherten Snapshot.
    Aufgerufen durch: python3 main.py --health
    """
    from src.engine.health_monitor import HealthMonitor, HealthStatus

    console.print()
    console.print(Panel.fit(
        "[bold cyan]HEALTH MONITOR STATUS[/bold cyan]\n"
        f"[dim]Modus: {settings.TRADING_MODE.upper()} | "
        f"Enabled: {settings.HEALTH_MONITOR_ENABLED}[/dim]",
        border_style="cyan",
    ))

    # Konfiguration anzeigen
    cfg_table = Table(title="⚙️  Health-Monitor-Konfiguration", box=box.ROUNDED, border_style="blue")
    cfg_table.add_column("Parameter", style="bold")
    cfg_table.add_column("Wert", justify="right")
    cfg_table.add_row("HEALTH_MONITOR_ENABLED", str(settings.HEALTH_MONITOR_ENABLED))
    cfg_table.add_row("HEALTH_HEARTBEAT_TIMEOUT_SEC", f"{settings.HEALTH_HEARTBEAT_TIMEOUT_SEC}s")
    cfg_table.add_row("DATA_STALE_TIMEOUT_SEC", f"{settings.DATA_STALE_TIMEOUT_SEC}s")
    cfg_table.add_row("HEALTH_CHECK_INTERVAL_SEC", f"{settings.HEALTH_CHECK_INTERVAL_SEC}s")
    cfg_table.add_row("ERROR_WINDOW_MINUTES", f"{settings.ERROR_WINDOW_MINUTES}min")
    cfg_table.add_row("MAX_ERRORS_PER_WINDOW", str(settings.MAX_ERRORS_PER_WINDOW))
    cfg_table.add_row("MAX_CRITICAL_ERRORS_PER_WINDOW", str(settings.MAX_CRITICAL_ERRORS_PER_WINDOW))
    cfg_table.add_row("HEALTH_PAUSE_ON_STALE_DATA", str(settings.HEALTH_PAUSE_ON_STALE_DATA))
    cfg_table.add_row("HEALTH_PAUSE_ON_HEARTBEAT_MISS", str(settings.HEALTH_PAUSE_ON_HEARTBEAT_MISS))
    cfg_table.add_row("RESOURCE_MONITOR_ENABLED", str(settings.RESOURCE_MONITOR_ENABLED))
    cfg_table.add_row("MAX_MEMORY_PCT", f"{settings.MAX_MEMORY_PCT}%")
    cfg_table.add_row("MAX_CPU_PCT", f"{settings.MAX_CPU_PCT}%")
    cfg_table.add_row("TELEGRAM_ALERT_COOLDOWN_SEC", f"{settings.TELEGRAM_ALERT_COOLDOWN_SEC}s")
    console.print(cfg_table)

    # Kurzer Live-Snapshot (einmaliger Check)
    if settings.HEALTH_MONITOR_ENABLED:
        monitor = HealthMonitor()
        monitor.log_snapshot_now()
        snap = monitor.get_snapshot()
        if snap:
            snap_table = Table(title="📊 Aktueller Snapshot", box=box.ROUNDED, border_style="green")
            snap_table.add_column("Feld", style="bold")
            snap_table.add_column("Wert")
            for k, v in snap.items():
                if k not in ("settings",):
                    snap_table.add_row(str(k), str(v)[:80])
            console.print(snap_table)

    console.print(
        "\n[dim]Hinweis: Live-Status nur im laufenden Bot verfügbar "
        "(per Log oder künftig via health-file). "
        "Starte mit: python3 main.py --multi[/dim]\n"
    )


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
    parser.add_argument(
        "--health", action="store_true", dest="show_health",
        help="Health-Monitor-Konfiguration + letzten Snapshot anzeigen und beenden",
    )

    # ── Backtest / Walk-Forward Args ──────────────────────────────────────
    parser.add_argument(
        "--backtest", action="store_true",
        help="Backtest-Modus aktivieren (kein Live-/Paper-Trading)",
    )
    parser.add_argument(
        "--walk-forward", action="store_true", dest="walk_forward",
        help=(
            "Walk-Forward-Evaluation: IS/OOS-Splits über alle Daten "
            "(benötigt --csv + --strategy oder --multi)"
        ),
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

    # ── Walk-Forward-spezifische Args ─────────────────────────────────────
    parser.add_argument(
        "--is-candles", type=int, default=600, dest="is_candles",
        help="In-Sample Fenstergröße in Kerzen für WFO (Standard: 600)",
    )
    parser.add_argument(
        "--oos-candles", type=int, default=200, dest="oos_candles",
        help="Out-of-Sample Fenstergröße in Kerzen für WFO (Standard: 200)",
    )
    parser.add_argument(
        "--wf-step", type=int, default=None, dest="wf_step",
        help="Roll-Schritt in Kerzen für WFO (Standard: gleich oos-candles)",
    )
    parser.add_argument(
        "--wf-mode", type=str, default="rolling", dest="wf_mode",
        choices=["rolling", "anchored"],
        help="Walk-Forward-Modus: 'rolling' (festes IS) oder 'anchored' (wachsendes IS)",
    )
    parser.add_argument(
        "--min-splits", type=int, default=2, dest="min_splits",
        help="Mindestanzahl valider Splits für WFO (Standard: 2)",
    )
    parser.add_argument(
        "--min-trades-per-split", type=int, default=3, dest="min_trades_per_split",
        help="Mindest-OOS-Trades für einen 'validen' Split (Standard: 3)",
    )

    args = parser.parse_args()

    # Zentrale Konfigurationsprüfung (benutzerfreundliche Fehlermeldungen)
    _warn_if_no_env_file()
    _validate_runtime_config(args)

    # ── Strategy-Stats-Modus ──────────────────────────────────────────────
    if args.strategy_stats:
        _show_strategy_stats()
        return

    # ── Health-Monitor-Modus ──────────────────────────────────────────────
    if args.show_health:
        _show_health()
        return

    # ── Walk-Forward-Modus ────────────────────────────────────────────────
    if args.walk_forward:
        from backtest.cli import run_walk_forward
        run_walk_forward(args)
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
    _log_telegram_startup_state()
    if settings.TRADING_MODE == "live" and settings.LIVE_TEST_MODE:
        console.print(
            "[yellow]WARNUNG: MINI-LIVE TESTMODE aktiv[/yellow] | "
            f"max_pos={settings.LIVE_MAX_POSITION_SIZE} | "
            f"symbols={settings.LIVE_ALLOWED_SYMBOLS or 'n/a'} | "
            f"strategies={settings.LIVE_ALLOWED_STRATEGIES or 'all'} | "
            f"daily_loss={settings.LIVE_TEST_DAILY_LOSS_LIMIT_PCT}%"
        )

    if args.status:
        bot = MultiStrategyBot(autostart_services=False) if use_multi else TradingBot(autostart_services=False)
        show_status(bot)
        return

    app = TradingApplication(use_multi=use_multi, interval_seconds=args.interval)

    if args.once:
        app.run_once(autostart_services=False)
        show_status(app.bot)
        app.stop()
        return

    try:
        app.run_forever()
    except RuntimeError as e:
        if str(e) == "single_instance_lock_failed":
            console.print(
                "[red]Start abgebrochen:[/red] Es läuft bereits eine Instanz "
                "(oder ein altes Lockfile blockiert den Start)."
            )
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
