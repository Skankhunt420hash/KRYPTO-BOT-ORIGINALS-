import time
from typing import List
from config.settings import settings
from src.exchange.connector import ExchangeConnector
from src.strategies import get_strategy, Signal
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
