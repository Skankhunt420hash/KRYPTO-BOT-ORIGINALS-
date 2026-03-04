import time
from typing import Dict, List, Optional
from config.settings import settings
from src.exchange.connector import ExchangeConnector
from src.strategies import get_strategy, get_all_enhanced_strategies, Signal
from src.strategies.signal import Side
from src.engine.regime import RegimeEngine
from src.engine.meta_selector import MetaSelector
from src.engine.risk_engine import RiskEngine
from src.utils.logger import setup_logger
from src.utils.risk_manager import RiskManager

logger = setup_logger("bot", settings.LOG_LEVEL)


class TradingBot:
    """Haupt-Trading-Bot: Verbindet Exchange, Strategie und Risikomanagement."""

    def __init__(self):
        logger.info("[bold cyan]KRYPTO-BOT ORIGINALS startet...[/bold cyan]")
        logger.info(f"Modus: [yellow]{settings.TRADING_MODE.upper()}[/yellow]")
        logger.info(f"Strategie: [cyan]{settings.STRATEGY}[/cyan]")
        logger.info(f"Paare: {', '.join(settings.TRADING_PAIRS)}")
        logger.info(f"Zeitrahmen: {settings.TIMEFRAME}")

        self.exchange = ExchangeConnector()
        self.strategy = get_strategy(settings.STRATEGY)
        self.risk = RiskManager()
        self.pairs: List[str] = settings.TRADING_PAIRS
        self.running = False

    def _process_pair(self, symbol: str):
        """Analysiert ein Handelspaar und führt ggf. eine Order aus."""
        df = self.exchange.fetch_ohlcv(symbol)
        if df.empty:
            return

        signal = self.strategy.analyze(df, symbol)
        current_price = float(df["close"].iloc[-1])

        # Prüfe Exit-Bedingungen für offene Positionen
        exit_reason = self.risk.check_exit_conditions(symbol, current_price)
        if exit_reason:
            position = self.risk.open_positions.get(symbol)
            if position:
                self.exchange.create_market_sell_order(symbol, position.amount)
                self.risk.close_position(symbol, current_price)
            return

        # Kaufsignal
        if signal.is_buy() and self.risk.can_open_trade(symbol):
            amount = self.risk.calculate_position_size(current_price)
            order = self.exchange.create_market_buy_order(symbol, amount)
            if order:
                self.risk.open_position(symbol, current_price, amount)
                logger.info(
                    f"[bold green]KAUF[/bold green] {symbol} | "
                    f"Grund: {signal.reason} | Konfidenz: {signal.confidence:.0%}"
                )

        # Verkaufssignal
        elif signal.is_sell() and symbol in self.risk.open_positions:
            position = self.risk.open_positions[symbol]
            order = self.exchange.create_market_sell_order(symbol, position.amount)
            if order:
                self.risk.close_position(symbol, current_price)
                logger.info(
                    f"[bold red]VERKAUF[/bold red] {symbol} | "
                    f"Grund: {signal.reason}"
                )
        else:
            logger.debug(f"HOLD {symbol} | {signal.reason}")

    def run_cycle(self):
        """Führt einen vollständigen Analyse-Zyklus für alle Paare durch."""
        logger.info(f"[dim]── Neuer Zyklus gestartet ──[/dim]")
        for symbol in self.pairs:
            try:
                self._process_pair(symbol)
            except Exception as e:
                logger.error(f"Fehler bei {symbol}: {e}")

        stats = self.risk.get_stats()
        logger.info(
            f"Balance: [bold]{stats['balance']:.2f} USDT[/bold] | "
            f"PnL: [{'green' if stats['total_pnl'] >= 0 else 'red'}]{stats['total_pnl']:+.4f}[/] | "
            f"Trades: {stats['total_trades']} | "
            f"Winrate: {stats['winrate_pct']:.1f}% | "
            f"Offene Pos.: {stats['open_positions']}"
        )

    def run(self, interval_seconds: int = None):
        """Startet den Bot in einer Dauerschleife."""
        # Zeitrahmen -> Sekunden Mapping
        tf_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        wait = interval_seconds or tf_map.get(settings.TIMEFRAME, 3600)

        self.running = True
        logger.info(f"[bold]Bot läuft. Interval: {wait}s ({settings.TIMEFRAME})[/bold]")
        logger.info("Drücke [bold]Ctrl+C[/bold] zum Beenden.\n")

        try:
            while self.running:
                self.run_cycle()
                logger.info(f"Nächster Zyklus in {wait} Sekunden...\n")
                time.sleep(wait)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        self.running = False
        stats = self.risk.get_stats()
        logger.info("\n[bold cyan]Bot wurde gestoppt.[/bold cyan]")
        logger.info(
            f"[bold]ABSCHLUSS-STATISTIK[/bold]\n"
            f"  Final Balance:  {stats['balance']:.2f} USDT\n"
            f"  Gesamt PnL:     {stats['total_pnl']:+.4f} USDT\n"
            f"  Trades:         {stats['total_trades']}\n"
            f"  Gewinner:       {stats['winning_trades']}\n"
            f"  Win-Rate:       {stats['winrate_pct']:.1f}%"
        )


# ---------------------------------------------------------------------------
# Multi-Strategy Bot (verwendet Regime-Engine + Meta-Selector + Risk-Engine)
# ---------------------------------------------------------------------------

class MultiStrategyBot:
    """
    Multi-Strategie-Bot mit automatischer Strategie-Auswahl.

    Pipeline pro Zyklus & Pair:
      1. OHLCV laden
      2. Exits prüfen (SL/TP/Trailing)
      3. Regime erkennen (RegimeEngine)
      4. Alle Strategien parallel analysieren → EnhancedSignal-Liste
      5. Meta-Selector wählt bestes Signal (Regime-Fit, Konfidenz, RR, Volumen)
      6. Risk-Engine prüft: Daily-Limit, Cooldowns, Duplikate, Max-Trades
      7. Order ausführen (Paper oder Live)
    """

    def __init__(self):
        logger.info("[bold cyan]KRYPTO-BOT ORIGINALS – Multi-Strategy-Modus[/bold cyan]")
        logger.info(f"Modus: [yellow]{settings.TRADING_MODE.upper()}[/yellow]")
        logger.info(f"Strategie: [cyan]AUTO (Meta-Selector)[/cyan]")
        logger.info(f"Paare: {', '.join(settings.TRADING_PAIRS)}")
        logger.info(f"Zeitrahmen: {settings.TIMEFRAME}")

        self.exchange = ExchangeConnector()
        self.strategies = get_all_enhanced_strategies()
        self.regime_engine = RegimeEngine()
        self.selector = MetaSelector()
        self.risk = RiskEngine()
        self.pairs: List[str] = settings.TRADING_PAIRS
        self.running = False

        strat_names = [s.name for s in self.strategies]
        logger.info(f"Aktive Strategien: {strat_names}")

    def _process_pair(self, symbol: str):
        """Führt den vollständigen Analyse- und Ausführungszyklus für ein Pair durch."""
        df = self.exchange.fetch_ohlcv(symbol)
        if df.empty:
            logger.warning(f"{symbol} | Keine OHLCV-Daten erhalten – übersprungen")
            return

        current_price = float(df["close"].iloc[-1])

        # 1. Exits prüfen (SL, TP, Trailing Stop)
        exit_reason = self.risk.check_exit_conditions(symbol, current_price)
        if exit_reason:
            position = self.risk.open_positions.get(symbol)
            if position:
                self.exchange.create_market_sell_order(symbol, position.amount)
                pnl = self.risk.close_position(symbol, current_price)
                logger.info(
                    f"[bold]EXIT[/bold] {symbol} | Grund: {exit_reason} | "
                    f"PnL: {pnl:+.4f} USDT" if pnl is not None else
                    f"[bold]EXIT[/bold] {symbol} | Grund: {exit_reason}"
                )
            return

        # Offene Position: kein neuer Einstieg
        if symbol in self.risk.open_positions:
            return

        # 2. Regime erkennen
        try:
            regime = self.regime_engine.detect(df)
        except Exception as e:
            logger.error(f"{symbol} | Regime-Erkennung fehlgeschlagen: {e}")
            return

        logger.debug(f"{symbol} | Regime: [bold]{regime.value}[/bold] | Preis: {current_price:.4f}")

        # 3. Alle Strategien analysieren
        signals = []
        for strategy in self.strategies:
            try:
                sig = strategy.analyze(df, symbol, settings.TIMEFRAME)
                sig.regime = regime.value
                signals.append(sig)
                logger.debug(
                    f"  {strategy.name:<22} → {sig.side.value:<5} | "
                    f"conf={sig.confidence:.0f} rr={sig.rr:.2f} | {sig.reason}"
                )
            except Exception as e:
                logger.error(f"  {strategy.name} | Fehler: {e}")

        # 4. Meta-Selector
        best = self.selector.select(signals, regime, symbol)
        if best is None or best.side == Side.NONE:
            return

        # 5. Risk-Engine prüfen
        allowed, block_reason = self.risk.check_signal(best)
        if not allowed:
            logger.info(
                f"[yellow]BLOCKIERT[/yellow] {symbol} | "
                f"Strategie: {best.strategy_name} | Grund: {block_reason}"
            )
            return

        # 6. Signal registrieren (Duplikatschutz)
        self.risk.register_signal(best)

        # 7. Order ausführen
        if best.side == Side.LONG:
            amount = self.risk.calculate_position_size(best.entry)
            order = self.exchange.create_market_buy_order(symbol, amount)
            if order:
                self.risk.open_with_signal(best, amount)
                logger.info(
                    f"[bold green]KAUF[/bold green] {symbol} | "
                    f"Strategie: {best.strategy_name} | "
                    f"Einstieg: {best.entry:.4f} | Menge: {amount:.6f} | "
                    f"SL: {best.stop_loss:.4f} | TP: {best.take_profit:.4f} | "
                    f"RR: {best.rr:.2f} | Konfidenz: {best.confidence:.0f}/100"
                )
        elif best.side == Side.SHORT:
            # Im Spot-Modus: SHORT wird ignoriert, nur geloggt
            logger.debug(
                f"{symbol} | SHORT-Signal von {best.strategy_name} "
                f"– Spot-Modus unterstützt kein Short-Selling"
            )

    def run_cycle(self):
        """Führt einen vollständigen Analyse-Zyklus für alle konfigurierten Paare durch."""
        logger.info("[dim]── Multi-Strategy Zyklus gestartet ──[/dim]")
        for symbol in self.pairs:
            try:
                self._process_pair(symbol)
            except Exception as e:
                logger.error(f"Unerwarteter Fehler bei {symbol}: {e}")

        stats = self.risk.get_stats()
        logger.info(
            f"Balance: [bold]{stats['balance']:.2f} USDT[/bold] | "
            f"PnL: [{'green' if stats['total_pnl'] >= 0 else 'red'}]"
            f"{stats['total_pnl']:+.4f}[/] | "
            f"Trades: {stats['total_trades']} | "
            f"Winrate: {stats['winrate_pct']:.1f}% | "
            f"Offene: {stats['open_positions']} | "
            f"Daily Loss: {stats['daily_loss']:.2f} USDT"
        )

    def run(self, interval_seconds: int = None):
        """Startet den Multi-Strategy-Bot in einer Dauerschleife."""
        tf_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        wait = interval_seconds or tf_map.get(settings.TIMEFRAME, 3600)

        self.running = True
        logger.info(f"[bold]Multi-Bot läuft. Interval: {wait}s ({settings.TIMEFRAME})[/bold]")
        logger.info("Drücke [bold]Ctrl+C[/bold] zum Beenden.\n")

        try:
            while self.running:
                self.run_cycle()
                logger.info(f"Nächster Zyklus in {wait} Sekunden...\n")
                time.sleep(wait)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        self.running = False
        stats = self.risk.get_stats()
        logger.info("\n[bold cyan]Multi-Bot gestoppt.[/bold cyan]")
        logger.info(
            f"[bold]ABSCHLUSS-STATISTIK[/bold]\n"
            f"  Final Balance:  {stats['balance']:.2f} USDT\n"
            f"  Gesamt PnL:     {stats['total_pnl']:+.4f} USDT\n"
            f"  Trades:         {stats['total_trades']}\n"
            f"  Gewinner:       {stats['winning_trades']}\n"
            f"  Win-Rate:       {stats['winrate_pct']:.1f}%\n"
            f"  Daily Loss:     {stats['daily_loss']:.2f} USDT"
        )
