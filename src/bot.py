import time
from typing import Dict, List, Optional
from config.settings import settings
from src.exchange.connector import ExchangeConnector
from src.strategies import get_strategy, get_all_enhanced_strategies, Signal
from src.strategies.signal import Side
from src.engine.regime import RegimeEngine
from src.engine.meta_selector import MetaSelector
from src.engine.risk_engine import RiskEngine
from src.engine.performance_tracker import PerformanceTracker
from src.engine.strategy_scorer import StrategyScorer
from src.engine.execution_engine import ExecutionEngine
from src.storage.trade_repository import TradeRepository
from src.utils.logger import setup_logger
from src.utils.risk_manager import RiskManager
from src.utils.telegram_notifier import TelegramNotifier

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
        self.repo = TradeRepository()
        self.tg = TelegramNotifier()
        self.pairs: List[str] = settings.TRADING_PAIRS
        self.running = False
        self._open_trade_ids: Dict[str, int] = {}  # symbol → DB-trade-id

        self.tg.notify_bot_start(
            mode=settings.TRADING_MODE,
            strategy=settings.STRATEGY,
            pairs=settings.TRADING_PAIRS,
            timeframe=settings.TIMEFRAME,
        )

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
                # Position-Daten vor dem Schließen sichern (danach aus open_positions entfernt)
                entry_price = position.entry_price
                pos_size = position.amount

                self.exchange.create_market_sell_order(symbol, position.amount)
                pnl = self.risk.close_position(symbol, current_price)

                # DB + Telegram: getrennt, damit Telegram auch ohne DB-Eintrag sendet
                trade_id = self._open_trade_ids.pop(symbol, None)
                if pnl is not None:
                    cost = entry_price * pos_size
                    pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
                    if trade_id is not None:
                        self.repo.close_trade(trade_id, current_price, pnl, pnl_pct, exit_reason)
                    self.tg.notify_trade_closed(
                        symbol=symbol,
                        side="long",
                        entry=entry_price,
                        exit_price=current_price,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        reason=exit_reason,
                        strategy=self.strategy.name,
                        is_paper=settings.TRADING_MODE == "paper",
                    )
            return

        # Kaufsignal
        if signal.is_buy() and self.risk.can_open_trade(symbol):
            amount = self.risk.calculate_position_size(current_price)
            order = self.exchange.create_market_buy_order(symbol, amount)
            if order:
                pos = self.risk.open_position(symbol, current_price, amount)
                logger.info(
                    f"[bold green]KAUF[/bold green] {symbol} | "
                    f"Grund: {signal.reason} | Konfidenz: {signal.confidence:.0%}"
                )
                # DB speichern
                if pos:
                    rr = (pos.take_profit - pos.entry_price) / max(
                        pos.entry_price - pos.stop_loss, 1e-9
                    )
                    trade_id = self.repo.save_open_trade(
                        symbol=symbol,
                        timeframe=settings.TIMEFRAME,
                        strategy_name=self.strategy.name,
                        side="long",
                        entry_price=current_price,
                        stop_loss=pos.stop_loss,
                        take_profit=pos.take_profit,
                        position_size=amount,
                        rr_planned=round(rr, 2),
                        confidence=round(signal.confidence * 100, 1),
                        regime="UNKNOWN",
                        reason_open=signal.reason,
                        order_id=str(order.get("id", "")),
                    )
                    if trade_id:
                        self._open_trade_ids[symbol] = trade_id
                    self.tg.notify_trade_opened(
                        symbol=symbol,
                        side="long",
                        entry=current_price,
                        sl=pos.stop_loss,
                        tp=pos.take_profit,
                        rr=round(rr, 2),
                        amount=amount,
                        strategy=self.strategy.name,
                        confidence=round(signal.confidence * 100, 1),
                        regime="UNKNOWN",
                        is_paper=settings.TRADING_MODE == "paper",
                    )

        # Verkaufssignal
        elif signal.is_sell() and symbol in self.risk.open_positions:
            position = self.risk.open_positions[symbol]
            entry_price = position.entry_price
            pos_size = position.amount

            order = self.exchange.create_market_sell_order(symbol, position.amount)
            if order:
                pnl = self.risk.close_position(symbol, current_price)
                logger.info(
                    f"[bold red]VERKAUF[/bold red] {symbol} | "
                    f"Grund: {signal.reason}"
                )
                trade_id = self._open_trade_ids.pop(symbol, None)
                if trade_id is not None and pnl is not None:
                    cost = entry_price * pos_size
                    pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
                    self.repo.close_trade(
                        trade_id, current_price, pnl, pnl_pct, signal.reason
                    )
                    self.tg.notify_trade_closed(
                        symbol=symbol,
                        side="long",
                        entry=entry_price,
                        exit_price=current_price,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        reason=signal.reason,
                        strategy=self.strategy.name,
                        is_paper=settings.TRADING_MODE == "paper",
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

        # Verbleibende offene DB-Einträge als cancelled markieren
        for symbol, trade_id in list(self._open_trade_ids.items()):
            self.repo.cancel_open_trade(trade_id, reason="bot_stopped")
        self._open_trade_ids.clear()

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
        self.tg.notify_bot_stop(
            balance=stats["balance"],
            total_pnl=stats["total_pnl"],
            total_trades=stats["total_trades"],
            winrate=stats["winrate_pct"],
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
        self.risk = RiskEngine()
        self.repo = TradeRepository()
        self.tg = TelegramNotifier()
        self.pairs: List[str] = settings.TRADING_PAIRS
        self.running = False
        self._open_trade_ids: Dict[str, int] = {}  # symbol → DB-trade-id

        # Performance-Tracking und adaptives Scoring
        self.perf_tracker = PerformanceTracker()
        self.scorer = StrategyScorer(self.perf_tracker)
        self.selector = MetaSelector(scorer=self.scorer)

        # Execution Quality Layer (Retry, Slippage-Schutz, Circuit Breaker, Fail-Safes)
        self.exec_engine = ExecutionEngine(self.exchange, self.tg)

        strat_names = [s.name for s in self.strategies]
        logger.info(f"Aktive Strategien: {strat_names}")
        if self.perf_tracker.available:
            known = self.perf_tracker.known_strategies()
            if known:
                logger.info(f"Performance-Daten geladen für: {known}")
            else:
                logger.info(
                    "Performance-Tracker aktiv – noch keine Trade-Daten "
                    "(neutrale Scores bis min. "
                    f"{settings.PERF_TRACKER_MIN_TRADES} Trades)"
                )

        self.tg.notify_bot_start(
            mode=settings.TRADING_MODE,
            strategy="AUTO (Meta-Selector)",
            pairs=settings.TRADING_PAIRS,
            timeframe=settings.TIMEFRAME,
        )

    def _process_pair(self, symbol: str):
        """Führt den vollständigen Analyse- und Ausführungszyklus für ein Pair durch."""
        df = self.exchange.fetch_ohlcv(symbol)
        if df.empty:
            logger.warning(f"{symbol} | Keine OHLCV-Daten erhalten – übersprungen")
            return

        current_price = float(df["close"].iloc[-1])

        # 1. Exits prüfen (SL, TP, Trailing Stop) – side-aware
        exit_reason = self.risk.check_exit_conditions(symbol, current_price)
        if exit_reason:
            position = self.risk.open_positions.get(symbol)
            if position:
                entry_price = position.entry_price
                pos_size = position.amount
                pos_side = position.side

                # LONG schließen: Sell-Order / SHORT schließen: Buy-Order (zurückkaufen)
                exit_side = "sell" if pos_side == "long" else "buy"
                exit_result = self.exec_engine.execute_exit(
                    symbol, exit_side, position.amount
                )
                if not exit_result.success:
                    logger.error(
                        f"[red]EXIT-ORDER FEHLER[/red] {symbol} | "
                        f"{exit_result.reason} | "
                        f"Position wird trotzdem lokal geschlossen"
                    )

                pnl = self.risk.close_position(symbol, current_price)

                side_label = "[LONG]" if pos_side == "long" else "[SHORT]"
                pnl_str = f"{pnl:+.4f} USDT" if pnl is not None else "?"
                logger.info(
                    f"[bold]EXIT {side_label}[/bold] {symbol} | "
                    f"Grund: {exit_reason} | PnL: {pnl_str}"
                )

                # DB + Telegram: getrennt, damit Telegram auch ohne DB-Eintrag sendet
                trade_id = self._open_trade_ids.pop(symbol, None)
                if pnl is not None:
                    cost = entry_price * pos_size
                    pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
                    if trade_id is not None:
                        self.repo.close_trade(trade_id, current_price, pnl, pnl_pct, exit_reason)
                    self.tg.notify_trade_closed(
                        symbol=symbol,
                        side=pos_side,
                        entry=entry_price,
                        exit_price=current_price,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        reason=exit_reason,
                        strategy=position.strategy_name,
                        is_paper=settings.TRADING_MODE == "paper",
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

        # 5. Risk-Engine prüfen (Cooldowns, Daily-Loss, Duplikat-Schutz)
        allowed, block_reason = self.risk.check_signal(best)
        if not allowed:
            logger.info(
                f"[yellow]BLOCKIERT[/yellow] {symbol} | "
                f"Strategie: {best.strategy_name} | Grund: {block_reason}"
            )
            self.repo.save_rejected_signal(
                symbol=symbol,
                timeframe=best.timeframe,
                strategy_name=best.strategy_name,
                side=best.side.value,
                entry_price=best.entry,
                stop_loss=best.stop_loss,
                take_profit=best.take_profit,
                rr_planned=best.rr,
                confidence=best.confidence,
                regime=best.regime,
                reason_rejected=block_reason,
            )
            self.tg.notify_trade_blocked(
                symbol=symbol,
                strategy=best.strategy_name,
                side=best.side.value,
                reason=block_reason,
            )
            return

        # 5b. Portfolio Risk Engine: Sizing + Exposure-Limits
        pf_allowed, pf_reason, amount = self.risk.check_and_size(best)
        if not pf_allowed:
            logger.info(
                f"[yellow]PORTFOLIO BLOCKIERT[/yellow] {symbol} | "
                f"Strategie: {best.strategy_name} | Grund: {pf_reason}"
            )
            self.repo.save_rejected_signal(
                symbol=symbol,
                timeframe=best.timeframe,
                strategy_name=best.strategy_name,
                side=best.side.value,
                entry_price=best.entry,
                stop_loss=best.stop_loss,
                take_profit=best.take_profit,
                rr_planned=best.rr,
                confidence=best.confidence,
                regime=best.regime,
                reason_rejected=pf_reason,
            )
            self.tg.notify_trade_blocked(
                symbol=symbol,
                strategy=best.strategy_name,
                side=best.side.value,
                reason=pf_reason,
            )
            return

        # 6. Signal registrieren (Duplikatschutz)
        self.risk.register_signal(best)

        # 7. Order ausführen via Execution Engine (Retry, Slippage, Fail-Safes)
        if best.side == Side.LONG:
            exec_result = self.exec_engine.execute_entry(
                symbol=symbol, order_side="buy", amount=amount, signal=best
            )
            if not exec_result.success:
                logger.warning(
                    f"[yellow]EXECUTION BLOCKIERT[/yellow] {symbol} | "
                    f"Strategie: {best.strategy_name} | Grund: {exec_result.reason}"
                )
                return

            self.risk.open_with_signal(best, amount)
            logger.info(
                f"[bold green]LONG ERÖFFNET[/bold green] {symbol} | "
                f"Strategie: {best.strategy_name} | "
                f"Einstieg: {best.entry:.4f} | Fill: {exec_result.fill_price:.4f} | "
                f"Menge: {amount:.6f} | SL: {best.stop_loss:.4f} | "
                f"TP: {best.take_profit:.4f} | RR: {best.rr:.2f} | "
                f"Konfidenz: {best.confidence:.0f}/100 | "
                f"Dev: {exec_result.deviation_pct:.3f}%"
            )
            trade_id = self.repo.save_open_trade(
                symbol=symbol,
                timeframe=best.timeframe,
                strategy_name=best.strategy_name,
                side=best.side.value,
                entry_price=best.entry,
                stop_loss=best.stop_loss,
                take_profit=best.take_profit,
                position_size=amount,
                rr_planned=best.rr,
                confidence=best.confidence,
                regime=best.regime,
                reason_open=best.reason,
                order_id=exec_result.order.get("id", ""),
            )
            if trade_id:
                self._open_trade_ids[symbol] = trade_id
            self.tg.notify_trade_opened(
                symbol=symbol,
                side="long",
                entry=best.entry,
                sl=best.stop_loss,
                tp=best.take_profit,
                rr=best.rr,
                amount=amount,
                strategy=best.strategy_name,
                confidence=best.confidence,
                regime=best.regime,
                is_paper=settings.TRADING_MODE == "paper",
            )

        elif best.side == Side.SHORT:
            self._execute_short(symbol, best, amount)

    def _execute_short(
        self, symbol: str, signal: "EnhancedSignal", amount: float
    ) -> None:
        """
        Führt ein SHORT-Signal aus (amount bereits von Portfolio Risk Engine berechnet):
        - Live + Spot:    blockiert (Spot kann nicht shorten)
        - Live + Futures: nicht implementiert, Warnung ausgeben
        - Paper-Modus:    SHORT vollständig simulieren
        """
        is_live = settings.TRADING_MODE == "live"

        if is_live and not settings.FUTURES_MODE:
            logger.warning(
                f"[yellow]SHORT BLOCKIERT (Spot-Modus)[/yellow] {symbol} | "
                f"Strategie: {signal.strategy_name} | "
                f"Für SHORT: FUTURES_MODE=true in .env setzen"
            )
            return

        if is_live and settings.FUTURES_MODE:
            logger.warning(
                f"[yellow]SHORT (Futures-Live) noch nicht implementiert[/yellow] "
                f"{symbol} – Paper-Simulation wird verwendet"
            )
            # Fällt durch in Paper-Simulation

        # Paper-SHORT-Simulation via Execution Engine (Retry, Slippage-Schutz)
        exec_result = self.exec_engine.execute_entry(
            symbol=symbol, order_side="sell", amount=amount, signal=signal
        )
        if not exec_result.success:
            logger.warning(
                f"[yellow]SHORT EXECUTION BLOCKIERT[/yellow] {symbol} | "
                f"Strategie: {signal.strategy_name} | Grund: {exec_result.reason}"
            )
            return

        self.risk.open_with_signal(signal, amount)
        logger.info(
            f"[bold red]SHORT ERÖFFNET [PAPER][/bold red] {symbol} | "
            f"Strategie: {signal.strategy_name} | "
            f"Einstieg: {signal.entry:.4f} | Fill: {exec_result.fill_price:.4f} | "
            f"Menge: {amount:.6f} | SL: {signal.stop_loss:.4f} (oben) | "
            f"TP: {signal.take_profit:.4f} (unten) | "
            f"RR: {signal.rr:.2f} | Konfidenz: {signal.confidence:.0f}/100 | "
            f"Dev: {exec_result.deviation_pct:.3f}%"
        )
        trade_id = self.repo.save_open_trade(
            symbol=symbol,
            timeframe=signal.timeframe,
            strategy_name=signal.strategy_name,
            side=signal.side.value,
            entry_price=signal.entry,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            position_size=amount,
            rr_planned=signal.rr,
            confidence=signal.confidence,
            regime=signal.regime,
            reason_open=signal.reason,
            order_id=exec_result.order.get("id", ""),
        )
        if trade_id:
            self._open_trade_ids[symbol] = trade_id
        self.tg.notify_trade_opened(
            symbol=symbol,
            side="short",
            entry=signal.entry,
            sl=signal.stop_loss,
            tp=signal.take_profit,
            rr=signal.rr,
            amount=amount,
            strategy=signal.strategy_name,
            confidence=signal.confidence,
            regime=signal.regime,
            is_paper=settings.TRADING_MODE == "paper",
        )

    def run_cycle(self):
        """Führt einen vollständigen Analyse-Zyklus für alle konfigurierten Paare durch."""
        logger.info("[dim]── Multi-Strategy Zyklus gestartet ──[/dim]")

        # Execution Engine Gesundheitscheck (Circuit Breaker, Emergency Pause, Kill-Switch)
        if not self.exec_engine.is_healthy:
            status = self.exec_engine.get_status()
            reason = status.get("pause_reason") or f"Circuit Breaker: {status['circuit_state']}"
            logger.warning(
                f"[yellow]EXECUTION PAUSIERT[/yellow] – "
                f"Zyklus übersprungen | Grund: {reason} | "
                f"Status: CB={status['circuit_state']} "
                f"Errors={status['consecutive_errors']} "
                f"KillSwitch={status['kill_switch']}"
            )
            return

        # Scorer zu Beginn jedes Zyklus aktualisieren (liest neue Trades aus DB)
        try:
            self.scorer.refresh()
        except Exception as e:
            logger.warning(f"Scorer-Refresh fehlgeschlagen (nicht kritisch): {e}")

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
            f"Daily Loss: {stats['daily_loss']:.2f} USDT | "
            f"Portfolio-Risiko: {stats.get('portfolio_risk_pct', 0.0):.2f}%"
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

        # Verbleibende offene DB-Einträge als cancelled markieren
        for symbol, trade_id in list(self._open_trade_ids.items()):
            self.repo.cancel_open_trade(trade_id, reason="bot_stopped")
        self._open_trade_ids.clear()

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
        self.tg.notify_bot_stop(
            balance=stats["balance"],
            total_pnl=stats["total_pnl"],
            total_trades=stats["total_trades"],
            winrate=stats["winrate_pct"],
        )
