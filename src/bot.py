import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from config.settings import settings
from src.exchange.connector import ExchangeConnector
from src.exchange.universe import resolve_trading_pairs, format_pairs_for_log
from src.engine.portfolio_risk import PortfolioRiskEngine, build_config_from_settings
from src.strategies import get_strategy, get_all_enhanced_strategies, Signal
from src.strategies.signal import Side
from src.engine.regime import RegimeEngine
from src.engine.meta_selector import MetaSelector
from src.engine.brain import IntelligenceBrain
from src.engine.risk_engine import RiskEngine
from src.engine.performance_tracker import PerformanceTracker
from src.engine.strategy_scorer import StrategyScorer
from src.engine.execution_engine import ExecutionEngine
from src.engine.health_monitor import HealthMonitor
from src.engine.self_reflection_memory import SelfReflectionMemory
from src.engine.runtime_control import runtime_control
from src.engine.runtime_state import runtime_state
from src.storage.trade_repository import TradeRepository
from src.storage.decision_repository import DecisionRepository
from src.storage.performance_repository import PerformanceRepository
from src.telegram.control_panel import TelegramControlPanel, PanelCallbacks
from src.utils.logger import setup_logger
from src.utils.risk_manager import RiskManager, Position, paper_equity_ledger_enabled
from src.utils.telegram_notifier import TelegramNotifier
from src.utils.win_chance import (
    compute_trade_win_chance_pct,
    effective_entry_win_chance_pct,
    historical_win_rate_block_reason,
    bitter_reward_block_reason,
)

logger = setup_logger("bot", settings.LOG_LEVEL)


class TradingBot:
    """Haupt-Trading-Bot: Verbindet Exchange, Strategie und Risikomanagement."""

    def __init__(self, autostart_services: bool = True):
        logger.info("[bold cyan]KRYPTO-BOT ORIGINALS startet...[/bold cyan]")
        logger.info(f"Modus: [yellow]{settings.TRADING_MODE.upper()}[/yellow]")
        logger.info(f"Strategie: [cyan]{settings.STRATEGY}[/cyan]")
        logger.info(f"Zeitrahmen: {settings.TIMEFRAME}")

        self.exchange = ExchangeConnector()
        self.pairs: List[str] = resolve_trading_pairs(self.exchange)
        logger.info(
            "Paare (%d): %s",
            len(self.pairs),
            format_pairs_for_log(self.pairs),
        )
        self.strategy = get_strategy(settings.STRATEGY)
        self.risk = RiskManager()
        self.perf_tracker = PerformanceTracker()
        self.repo = TradeRepository()
        self.perf_repo = PerformanceRepository()
        self.decision_repo = DecisionRepository()
        self.tg = TelegramNotifier()
        self.panel = TelegramControlPanel(
            notifier=self.tg,
            callbacks=PanelCallbacks(
                get_runtime_status=self._runtime_status,
                request_bot_stop=self.stop,
                request_bot_start=self._request_start_from_panel,
                request_bot_restart=self._request_bot_restart_from_panel,
                request_test_trade=self._trigger_test_trade_from_panel,
                request_close_oldest_open_trades=self._request_close_oldest_open_trades_from_panel,
                apply_runtime_settings=self._apply_runtime_settings_from_panel,
                request_auto_heal=self._request_auto_heal,
                get_market_status=self._get_market_status_from_panel,
                get_master_status=self._get_master_status_from_panel,
            ),
        )
        self.running = False
        self._open_trade_ids: Dict[str, int] = {}  # symbol → DB-trade-id
        self._last_prices: Dict[str, float] = {}
        self._active_strategy_runtime: str = getattr(self.strategy, "name", settings.STRATEGY)
        self._last_selector_snapshot: Dict = {}
        self._last_brain_snapshot: Dict = {}
        self._sync_runtime_state()

        logger.info(
            "Telegram Bootstrap | enabled=%s | panel_enabled=%s | token=%s | chat_id=%s",
            settings.TELEGRAM_ENABLED,
            settings.TELEGRAM_PANEL_ENABLED,
            "set" if bool(settings.TELEGRAM_BOT_TOKEN) else "missing",
            "set" if bool(settings.TELEGRAM_CHAT_ID) else "missing",
        )
        if autostart_services:
            self.panel.start_in_background()
            if self.panel.enabled:
                logger.info("Telegram Polling gestartet.")
            else:
                logger.info("Telegram Polling nicht gestartet (deaktiviert).")
            self.tg.notify_bot_start(
                mode=settings.TRADING_MODE,
                strategy=settings.STRATEGY,
                pairs=self.pairs,
                timeframe=settings.TIMEFRAME,
            )
        logger.info("Bot bereit.")

    def _risk_state_at_entry_snapshot(self) -> Dict:
        ctrl = runtime_control.get_snapshot()
        stats = self.risk.get_stats()
        return {
            "paused": bool(ctrl.get("paused", False)),
            "risk_off": bool(ctrl.get("risk_off", False)),
            "open_positions": int(stats.get("open_positions", 0)),
            "balance": float(stats.get("balance", 0.0)),
            "daily_loss": float(stats.get("daily_loss", 0.0)),
            "mode": settings.TRADING_MODE,
        }

    def _process_pair(self, symbol: str):
        """Analysiert ein Handelspaar und führt ggf. eine Order aus."""
        df = self.exchange.fetch_ohlcv(symbol)
        if df.empty:
            self._record_last_decision(symbol=symbol, decision="skip", reason="no_data")
            return

        if bool(getattr(settings, "SHORT_ONLY_TRADING", False)):
            self._record_last_decision(
                symbol=symbol,
                decision="skip",
                reason="short_only_trading_requires_multi_mode",
                strategy=self.strategy.name,
            )
            return

        signal = self.strategy.analyze(df, symbol)
        current_price = float(df["close"].iloc[-1])
        self._last_prices[symbol] = current_price
        self._record_last_signal(
            symbol=symbol,
            strategy=self._active_strategy_runtime,
            side="buy" if signal.is_buy() else "sell" if signal.is_sell() else "none",
            confidence=round(float(signal.confidence) * 100, 1),
            reason=signal.reason,
            entry=current_price,
            timeframe=settings.TIMEFRAME,
        )

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
                    try:
                        self.perf_tracker.refresh()
                    except Exception:
                        pass
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
                    self._record_trade_event(
                        event="closed",
                        symbol=symbol,
                        side="long",
                        strategy=self.strategy.name,
                        pnl=pnl,
                        reason=exit_reason,
                    )
                    self._record_last_decision(
                        symbol=symbol,
                        decision="exit_closed",
                        reason=exit_reason,
                        strategy=self.strategy.name,
                    )
            return

        # Kaufsignal
        if signal.is_buy() and self.risk.can_open_trade(symbol):
            amount = self.risk.calculate_position_size(current_price)
            # Gleiche SL/TP-Logik wie RiskManager.open_position (für RR vor der Order)
            sl_pre = current_price * (1 - self.risk.stop_loss_pct / 100)
            tp_pre = current_price * (1 + self.risk.take_profit_pct / 100)
            rr_pre = (tp_pre - current_price) / max(current_price - sl_pre, 1e-9)
            conf_pct_gate = round(signal.confidence * 100, 1)
            min_wc = float(getattr(settings, "MIN_WIN_CHANCE_PCT", 0.0) or 0.0)
            hist_r = historical_win_rate_block_reason(
                self.strategy.name, self.perf_tracker
            )
            if hist_r:
                logger.info(
                    f"[yellow]SKIP historische Win-Rate[/yellow] {symbol} | {hist_r}"
                )
                self.repo.save_rejected_signal(
                    symbol=symbol,
                    timeframe=settings.TIMEFRAME,
                    strategy_name=self.strategy.name,
                    side="long",
                    entry_price=current_price,
                    stop_loss=sl_pre,
                    take_profit=tp_pre,
                    rr_planned=round(rr_pre, 2),
                    confidence=conf_pct_gate,
                    regime="UNKNOWN",
                    reason_rejected=hist_r,
                )
                self._record_last_decision(
                    symbol=symbol,
                    decision="blocked_historical_win_rate",
                    reason=hist_r,
                    strategy=self.strategy.name,
                )
                return
            bitter_r = bitter_reward_block_reason(
                self.strategy.name, self.perf_tracker
            )
            if bitter_r:
                logger.info(
                    f"[yellow]SKIP bitteres Reward-Gate[/yellow] {symbol} | {bitter_r}"
                )
                self.repo.save_rejected_signal(
                    symbol=symbol,
                    timeframe=settings.TIMEFRAME,
                    strategy_name=self.strategy.name,
                    side="long",
                    entry_price=current_price,
                    stop_loss=sl_pre,
                    take_profit=tp_pre,
                    rr_planned=round(rr_pre, 2),
                    confidence=conf_pct_gate,
                    regime="UNKNOWN",
                    reason_rejected=bitter_r,
                )
                self._record_last_decision(
                    symbol=symbol,
                    decision="blocked_bitter_reward",
                    reason=bitter_r,
                    strategy=self.strategy.name,
                )
                return
            if min_wc > 0:
                wc_gate, _ = effective_entry_win_chance_pct(
                    conf_pct_gate,
                    brain_score=None,
                    rr=round(rr_pre, 2),
                    strategy_name=self.strategy.name,
                    perf_tracker=self.perf_tracker,
                )
                if wc_gate < min_wc:
                    reason_gate = f"MIN_WIN_CHANCE:{wc_gate:.1f}<{min_wc:.0f}"
                    logger.info(
                        f"[yellow]SKIP Mindest-Gewinnchance[/yellow] {symbol} | "
                        f"{wc_gate:.1f}% < {min_wc:.0f}% | Konfidenz {conf_pct_gate:.0f}/100"
                    )
                    self.repo.save_rejected_signal(
                        symbol=symbol,
                        timeframe=settings.TIMEFRAME,
                        strategy_name=self.strategy.name,
                        side="long",
                        entry_price=current_price,
                        stop_loss=sl_pre,
                        take_profit=tp_pre,
                        rr_planned=round(rr_pre, 2),
                        confidence=conf_pct_gate,
                        regime="UNKNOWN",
                        reason_rejected=reason_gate,
                    )
                    self._record_last_decision(
                        symbol=symbol,
                        decision="blocked_min_win_chance",
                        reason=reason_gate,
                        strategy=self.strategy.name,
                    )
                    return
            order = self.exchange.create_market_buy_order(symbol, amount)
            if order:
                pos = self.risk.open_position(symbol, current_price, amount)
                # DB speichern
                if pos:
                    rr = (pos.take_profit - pos.entry_price) / max(
                        pos.entry_price - pos.stop_loss, 1e-9
                    )
                    _wc1, _wl1 = effective_entry_win_chance_pct(
                        round(signal.confidence * 100, 1),
                        brain_score=None,
                        rr=round(rr, 2),
                        strategy_name=self.strategy.name,
                        perf_tracker=self.perf_tracker,
                    )
                    logger.info(
                        f"[bold green]KAUF[/bold green] {symbol} | "
                        f"Grund: {signal.reason} | Konfidenz: {signal.confidence:.0%} | "
                        f"Gewinnchance(effektiv): {_wc1:.0f}% ({_wl1})"
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
                        signal_score=round(float(signal.confidence), 4),
                        risk_state_at_entry=self._risk_state_at_entry_snapshot(),
                        order_id=str(order.get("id", "")),
                    )
                    if trade_id:
                        self._open_trade_ids[symbol] = trade_id
                    conf_pct = round(signal.confidence * 100, 1)
                    self.tg.notify_trade_opened(
                        symbol=symbol,
                        side="long",
                        entry=current_price,
                        sl=pos.stop_loss,
                        tp=pos.take_profit,
                        rr=round(rr, 2),
                        amount=amount,
                        strategy=self.strategy.name,
                        confidence=conf_pct,
                        regime="UNKNOWN",
                        is_paper=settings.TRADING_MODE == "paper",
                        brain_score=None,
                    )
                    self._record_trade_event(
                        event="opened",
                        symbol=symbol,
                        side="long",
                        strategy=self.strategy.name,
                        pnl=None,
                        reason=signal.reason,
                    )
                    self._record_last_decision(
                        symbol=symbol,
                        decision="entry_opened",
                        reason=signal.reason,
                        strategy=self.strategy.name,
                    )
            else:
                self._record_last_decision(
                    symbol=symbol,
                    decision="entry_failed",
                    reason="exchange_order_failed",
                    strategy=self.strategy.name,
                )
        elif signal.is_buy() and not self.risk.can_open_trade(symbol):
            self._record_trade_event(
                event="blocked",
                symbol=symbol,
                side="long",
                strategy=self.strategy.name,
                pnl=None,
                reason="risk_gate_single",
            )
            self._record_last_decision(
                symbol=symbol,
                decision="entry_blocked",
                reason="risk_gate_single",
                strategy=self.strategy.name,
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
                    try:
                        self.perf_tracker.refresh()
                    except Exception:
                        pass
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
                    self._record_trade_event(
                        event="closed",
                        symbol=symbol,
                        side="long",
                        strategy=self.strategy.name,
                        pnl=pnl,
                        reason=signal.reason,
                    )
                    self._record_last_decision(
                        symbol=symbol,
                        decision="manual_exit_closed",
                        reason=signal.reason,
                        strategy=self.strategy.name,
                    )
        else:
            self._record_last_decision(
                symbol=symbol,
                decision="hold",
                reason=signal.reason,
                strategy=self.strategy.name,
            )
            logger.debug(f"HOLD {symbol} | {signal.reason}")

    def run_cycle(self):
        """Führt einen vollständigen Analyse-Zyklus für alle Paare durch."""
        logger.info(f"[dim]── Neuer Zyklus gestartet ──[/dim]")
        try:
            self.perf_tracker.refresh()
        except Exception:
            pass
        cycle_symbols = list(
            dict.fromkeys(list(self.pairs) + list(self.risk.open_positions.keys()))
        )
        for symbol in cycle_symbols:
            try:
                self._process_pair(symbol)
            except Exception as e:
                logger.error(f"Fehler bei {symbol}: {e}")
                self.tg.notify_error(f"TradingBot:{symbol}", str(e))

        stats = self.risk.get_stats()
        logger.info(
            f"Balance: [bold]{stats['balance']:.2f} USDT[/bold] | "
            f"PnL: [{'green' if stats['total_pnl'] >= 0 else 'red'}]{stats['total_pnl']:+.4f}[/] | "
            f"Trades: {stats['total_trades']} | "
            f"Winrate: {stats['winrate_pct']:.1f}% | "
            f"Offene Pos.: {stats['open_positions']}"
        )
        self._update_performance_tracking()
        self._sync_runtime_state()

    def run(self, interval_seconds: int = None):
        """Startet den Bot in einer Dauerschleife."""
        # Zeitrahmen -> Sekunden Mapping
        tf_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        wait = interval_seconds or tf_map.get(settings.TIMEFRAME, 3600)

        self.running = True
        self._sync_runtime_state()
        logger.info(f"[bold]Bot läuft. Interval: {wait}s ({settings.TIMEFRAME})[/bold]")
        logger.info("Drücke [bold]Ctrl+C[/bold] zum Beenden.\n")

        try:
            while self.running:
                self.run_cycle()
                logger.info(f"Nächster Zyklus in {wait} Sekunden...\n")
                time.sleep(wait)
        except KeyboardInterrupt:
            self.stop()

    def _runtime_status(self) -> Dict:
        stats = self.risk.get_stats()
        snap = runtime_state.snapshot()
        return {
            "running": self.running,
            "engine": "connected",
            "balance": stats.get("balance"),
            "equity": snap.get("equity", stats.get("balance")),
            "available_capital": snap.get("available_capital", stats.get("balance")),
            "total_trades": stats.get("total_trades"),
            "open_positions": stats.get("open_positions"),
            "open_positions_detail": snap.get("open_positions", []),
            "recent_trades": snap.get("recent_trades", []),
            "recent_logs": snap.get("recent_logs", []),
            "active_strategy": self._active_strategy_runtime,
            "enabled_strategies": snap.get("enabled_strategies", [self._active_strategy_runtime]),
            "last_signal": snap.get("last_signal", {}),
            "last_decision": snap.get("last_decision", {}),
            "health_status": "n/a",
            "daily_loss": stats.get("daily_loss", 0.0),
            "portfolio_risk_pct": stats.get("portfolio_risk_pct", 0.0),
            "selector": self._last_selector_snapshot,
            "risk_gate": {},
            "brain": snap.get("brain", {}),
            "app_context": snap.get("app_context", {}),
            "performance": snap.get("performance", {}),
        }

    def _build_open_positions_snapshot(self) -> List[Dict]:
        rows: List[Dict] = []
        for sym, pos in self.risk.open_positions.items():
            rows.append(
                {
                    "symbol": sym,
                    "side": getattr(pos, "side", "long"),
                    "strategy": getattr(pos, "strategy_name", self._active_strategy_runtime),
                    "entry_price": getattr(pos, "entry_price", 0.0),
                    "stop_loss": getattr(pos, "stop_loss", 0.0),
                    "take_profit": getattr(pos, "take_profit", 0.0),
                    "amount": getattr(pos, "amount", 0.0),
                }
            )
        return rows

    def _sync_runtime_state(self) -> None:
        stats = self.risk.get_stats()
        ctrl = runtime_control.get_snapshot()
        equity = self._calculate_equity()
        runtime_state.update_engine(
            running=self.running,
            mode=settings.TRADING_MODE,
            paused=ctrl.get("paused", False),
            risk_off=ctrl.get("risk_off", False),
            active_strategy=self._active_strategy_runtime,
            enabled_strategies=[self._active_strategy_runtime],
            balance=stats.get("balance", 0.0),
            equity=equity,
            available_capital=stats.get("balance", 0.0),
            health_status="n/a",
            total_trades=stats.get("total_trades", 0),
            open_positions=self._build_open_positions_snapshot(),
        )
        runtime_state.update_brain(self._last_brain_snapshot)

    def _calculate_equity(self) -> float:
        base_balance = float(self.risk.balance)
        for sym, pos in self.risk.open_positions.items():
            mark = self._last_prices.get(sym, pos.entry_price)
            reserved = pos.entry_price * pos.amount
            if getattr(pos, "side", "long") == "short":
                unrealized = (pos.entry_price - mark) * pos.amount
            else:
                unrealized = (mark - pos.entry_price) * pos.amount
            base_balance += reserved + unrealized
        return round(base_balance, 4)

    def _update_performance_tracking(self) -> None:
        stats = self.risk.get_stats()
        balance = float(stats.get("balance", 0.0))
        equity = float(self._calculate_equity())
        unrealized = round(equity - balance, 6)
        realized = float(stats.get("total_pnl", 0.0))
        total_trades = int(stats.get("total_trades", 0))
        win_rate = float(stats.get("winrate_pct", 0.0))
        if self.perf_repo.available:
            day = self.perf_repo.update_daily_summary(mode=settings.TRADING_MODE) or {}
            self.perf_repo.save_snapshot(
                mode=settings.TRADING_MODE,
                current_balance=balance,
                current_equity=equity,
                open_positions_count=int(stats.get("open_positions", 0)),
                realized_pnl_total=realized,
                unrealized_pnl_total=unrealized,
                day_pnl=float(day.get("pnl_abs", 0.0)),
                total_trades=total_trades,
                win_rate=win_rate,
            )
            runtime_state.update_performance(
                {
                    "snapshot": self.perf_repo.latest_snapshot(mode=settings.TRADING_MODE),
                    "daily_summary": day,
                }
            )

    def _record_trade_event(
        self,
        *,
        event: str,
        symbol: str,
        side: str,
        strategy: str,
        pnl: Optional[float],
        reason: str,
    ) -> None:
        runtime_state.append_trade(
            {
                "event": event,
                "symbol": symbol,
                "side": side,
                "strategy": strategy,
                "pnl": pnl,
                "reason": reason,
            }
        )
        runtime_state.append_log(
            f"{event.upper()} {symbol} [{side}] {strategy} "
            f"{f'PnL={pnl:+.4f}' if pnl is not None else ''} {reason}".strip()
        )

    def _record_last_signal(
        self,
        *,
        symbol: str,
        strategy: str,
        side: str,
        confidence: float,
        reason: str,
        entry: float,
        timeframe: str,
    ) -> None:
        runtime_state.set_last_signal(
            {
                "symbol": symbol,
                "strategy": strategy,
                "side": side,
                "confidence": confidence,
                "reason": reason,
                "entry": entry,
                "timeframe": timeframe,
            }
        )

    def _record_last_decision(
        self,
        *,
        symbol: str,
        decision: str,
        reason: str,
        strategy: Optional[str] = None,
    ) -> None:
        runtime_state.set_last_decision(
            {
                "symbol": symbol,
                "decision": decision,
                "reason": reason,
                "strategy": strategy or self._active_strategy_runtime,
            }
        )

    def _request_start_from_panel(self) -> Tuple[bool, str]:
        """
        Telegram-Start im bestehenden Prozess:
        - Wenn Loop läuft: nur Runtime-Sperren lösen.
        - Kein Cold-Start einer neuen Hauptschleife aus dem Panel-Thread.
        """
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        runtime_state.update_engine(paused=False, risk_off=False)
        if self.running:
            return True, "Bot läuft bereits. Entry-Pause/Risk-Off wurden aufgehoben."
        return False, (
            "Engine läuft aktuell nicht. Bitte Bot-Prozess lokal starten "
            "(Telegram kann keinen sicheren Cold-Start auslösen)."
        )

    def _request_bot_restart_from_panel(self) -> Tuple[bool, str]:
        runtime_control.pause_entries()
        runtime_control.enable_risk_off()
        runtime_state.update_engine(paused=True, risk_off=True)
        runtime_state.append_log("TELEGRAM /botrestart -> safe_restart_marked")
        self._sync_runtime_state()
        return (
            True,
            "Safe-Restart markiert: Entries pausiert + Risk-Off aktiv. "
            "Bitte Service per systemd neu starten.",
        )

    def _trigger_test_trade_from_panel(self) -> Tuple[bool, str]:
        try:
            if not self.running:
                return False, "Bot läuft nicht."
            runtime_control.resume_entries()
            runtime_control.disable_risk_off()
            runtime_state.update_engine(paused=False, risk_off=False)
            self._sync_runtime_state()
            return True, (
                "Testtrade-Bridge aktiv: Entries entsperrt. "
                "Der nächste valide Zyklus-Signalpfad darf wieder eröffnen."
            )
        except Exception as e:
            return False, f"Testtrade-Bridge fehlgeschlagen: {e}"

    def _request_close_oldest_open_trades_from_panel(
        self, close_count: int, keep_newest: int
    ) -> Tuple[bool, str]:
        _ = close_count, keep_newest
        return False, "Close-Oldest ist nur im Multi-Strategy-Modus verfügbar."

    def _apply_runtime_settings_from_panel(self, updates: Dict[str, float]) -> Tuple[bool, str]:
        if not isinstance(updates, dict) or not updates:
            return False, "Keine Runtime-Parameter übergeben."
        changed: List[str] = []
        for key, value in updates.items():
            k = str(key or "").strip().lower()
            try:
                if k == "max_positions_total":
                    v = max(1, min(50, int(value)))
                    setattr(settings, "MAX_OPEN_TRADES", v)
                    setattr(settings, "MAX_POSITIONS_TOTAL", v)
                    if hasattr(self.risk, "max_open_trades"):
                        self.risk.max_open_trades = v
                    changed.append(f"MAX_POSITIONS_TOTAL={v}")
                elif k == "daily_loss_limit_pct":
                    v = max(0.1, min(100.0, float(value)))
                    setattr(settings, "DAILY_LOSS_LIMIT_PCT", v)
                    changed.append(f"DAILY_LOSS_LIMIT_PCT={v:.2f}")
                elif k == "coin_cooldown_minutes":
                    v = max(0, min(240, int(value)))
                    setattr(settings, "COIN_COOLDOWN_MINUTES", v)
                    changed.append(f"COIN_COOLDOWN_MINUTES={v}")
                elif k == "strategy_cooldown_minutes":
                    v = max(0, min(240, int(value)))
                    setattr(settings, "STRATEGY_COOLDOWN_MINUTES", v)
                    changed.append(f"STRATEGY_COOLDOWN_MINUTES={v}")
                elif k == "duplicate_signal_minutes":
                    v = max(0, min(240, int(value)))
                    setattr(settings, "DUPLICATE_SIGNAL_MINUTES", v)
                    changed.append(f"DUPLICATE_SIGNAL_MINUTES={v}")
                elif k == "brain_min_score_to_trade":
                    v = max(0.05, min(1.0, float(value)))
                    setattr(settings, "BRAIN_MIN_SCORE_TO_TRADE", v)
                    changed.append(f"BRAIN_MIN_SCORE_TO_TRADE={v:.3f}")
                elif k == "brain_risky_phase_score":
                    v = max(0.05, min(1.0, float(value)))
                    setattr(settings, "BRAIN_RISKY_PHASE_SCORE", v)
                    changed.append(f"BRAIN_RISKY_PHASE_SCORE={v:.3f}")
                elif k == "perf_selector_weight":
                    v = max(0.0, min(1.0, float(value)))
                    setattr(settings, "PERF_SELECTOR_WEIGHT", v)
                    changed.append(f"PERF_SELECTOR_WEIGHT={v:.3f}")
                elif k == "brain_reward_weight":
                    v = max(0.0, min(1.0, float(value)))
                    setattr(settings, "BRAIN_REWARD_WEIGHT", v)
                    changed.append(f"BRAIN_REWARD_WEIGHT={v:.3f}")
                elif k == "brain_reward_window":
                    v = max(2, min(80, int(value)))
                    setattr(settings, "BRAIN_REWARD_WINDOW", v)
                    changed.append(f"BRAIN_REWARD_WINDOW={v}")
                elif k == "brain_bitter_treat_block_threshold":
                    v = max(-1.0, min(0.0, float(value)))
                    setattr(settings, "BRAIN_BITTER_TREAT_BLOCK_THRESHOLD", v)
                    changed.append(f"BRAIN_BITTER_TREAT_BLOCK_THRESHOLD={v:.3f}")
                else:
                    return False, f"Unbekannter Runtime-Key: {k}"
            except Exception:
                return False, f"Ungültiger Wert für {k}: {value}"
        self._sync_runtime_state()
        return True, "Runtime-Parameter aktualisiert: " + ", ".join(changed)

    def _request_auto_heal(self) -> Tuple[bool, str]:
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        runtime_state.update_engine(paused=False, risk_off=False)
        self._sync_runtime_state()
        return True, "Autoheal ausgeführt: Pause und Risk-Off aufgehoben."

    def _get_market_status_from_panel(self) -> Dict:
        return {
            "pair_count": len(self.pairs),
            "pairs": list(self.pairs),
            "open_positions": len(self.risk.open_positions),
            "stale_symbols": [],
        }

    def _get_master_status_from_panel(self) -> Dict:
        return {
            "enabled": False,
            "min_trades": int(getattr(settings, "MASTER_BRAIN_MIN_TRADES", 20)),
            "target_winrate_pct": float(getattr(settings, "MASTER_BRAIN_TARGET_WINRATE_PCT", 70.0)),
            "last_winrate_pct": 0.0,
            "consecutive_fail_windows": 0,
            "auto_paused": bool(runtime_control.get_snapshot().get("paused", False)),
            "last_reason": "single_strategy_mode",
            "last_snapshot_file": "n/a",
        }

    def _run_master_watchdog(self) -> None:
        return

    def _save_master_snapshot(self, reason: str) -> Tuple[bool, str]:
        try:
            out_dir = Path("data/master_snapshots")
            out_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "reason": reason,
                "mode": settings.TRADING_MODE,
                "runtime": runtime_state.snapshot(),
                "risk_stats": self.risk.get_stats(),
            }
            out_path = out_dir / f"master_snapshot_{int(time.time())}.json"
            out_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
            return True, str(out_path)
        except Exception as e:
            return False, f"snapshot_error:{e}"

    def stop(self):
        self.running = False
        if self.panel:
            self.panel.stop()

        # Offene Trades bleiben bewusst erhalten für sicheren Restart-Recovery.
        for symbol, trade_id in list(self._open_trade_ids.items()):
            logger.info(
                "Stop ohne Auto-Cancel: offener Trade bleibt für Recovery erhalten | %s -> trade_id=%s",
                symbol,
                trade_id,
            )
        self._open_trade_ids.clear()

        stats = self.risk.get_stats()
        self._sync_runtime_state()
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

    def __init__(self, autostart_services: bool = True):
        logger.info("[bold cyan]KRYPTO-BOT ORIGINALS – Multi-Strategy-Modus[/bold cyan]")
        logger.info(f"Modus: [yellow]{settings.TRADING_MODE.upper()}[/yellow]")
        logger.info(f"Strategie: [cyan]AUTO (Meta-Selector)[/cyan]")
        logger.info(f"Zeitrahmen: {settings.TIMEFRAME}")

        self.exchange = ExchangeConnector()
        self.pairs: List[str] = resolve_trading_pairs(self.exchange)
        logger.info(
            "Paare (%d): %s",
            len(self.pairs),
            format_pairs_for_log(self.pairs),
        )
        self.strategies = get_all_enhanced_strategies()
        self.regime_engine = RegimeEngine()
        self.risk = RiskEngine()
        self.risk.portfolio = PortfolioRiskEngine(
            build_config_from_settings(self.pairs)
        )
        self.repo = TradeRepository()
        self.perf_repo = PerformanceRepository()
        self.decision_repo = DecisionRepository()
        self.tg = TelegramNotifier()
        self.running = False
        self._open_trade_ids: Dict[str, int] = {}  # symbol → DB-trade-id
        self._last_prices: Dict[str, float] = {}
        self._recovery_blocked_symbols: Set[str] = set()
        self._startup_checks_ok: bool = True
        self._startup_block_reason: str = ""
        self._active_strategy_runtime: str = "AUTO"
        self._last_selector_snapshot: Dict = {}
        self._last_brain_snapshot: Dict = {}
        self._last_regime_context: Dict = {}
        self._master_last_winrate_pct: float = 0.0
        self._master_fail_windows: int = 0
        self._master_auto_paused: bool = False
        self._master_last_reason: str = "init"
        self._master_last_snapshot_file: str = "n/a"
        self._master_cadence_override_until_ts: float = 0.0
        self._started_at_ts: float = time.time()
        self._last_entry_open_ts: float = 0.0
        self._entry_cadence_level: int = 0
        self._entry_cadence_status: Dict[str, object] = {}
        self._entry_cadence_baseline: Dict[str, float] = {
            "min_confidence": float(getattr(settings, "MIN_CONFIDENCE", 40.0)),
            "min_rr": float(getattr(settings, "MIN_RR", 1.5)),
            "min_win_chance_pct": float(getattr(settings, "MIN_WIN_CHANCE_PCT", 80.0)),
            "brain_min_score_to_trade": float(getattr(settings, "BRAIN_MIN_SCORE_TO_TRADE", 0.45)),
        }
        self._mtf_cache: Dict[str, Dict[str, object]] = {}
        self._last_mtf_context: Dict[str, object] = {}
        self._self_reflection = SelfReflectionMemory()
        self._self_reflection_last_repair_ts: float = 0.0
        self._queued_forced_closes: List[Dict[str, str]] = []
        self._forced_close_lock = threading.Lock()

        # Performance-Tracking und adaptives Scoring
        self.perf_tracker = PerformanceTracker()
        self.scorer = StrategyScorer(self.perf_tracker)
        self.selector = MetaSelector(scorer=self.scorer)
        self.brain = IntelligenceBrain(
            tracker=self.perf_tracker,
            scorer=self.scorer,
            selector=self.selector,
        )

        # Execution Quality Layer (Retry, Slippage-Schutz, Circuit Breaker, Fail-Safes)
        self.exec_engine = ExecutionEngine(self.exchange, self.tg)

        # Health Monitor & Watchdog (24/7 Überwachung)
        self.health = HealthMonitor(exec_engine=self.exec_engine, tg=self.tg)
        self.panel = TelegramControlPanel(
            notifier=self.tg,
            callbacks=PanelCallbacks(
                get_runtime_status=self._runtime_status,
                request_bot_stop=self.stop,
                request_bot_start=self._request_start_from_panel,
                request_bot_restart=self._request_bot_restart_from_panel,
                request_close_oldest_open_trades=self._request_close_oldest_open_trades_from_panel,
                apply_runtime_settings=self._apply_runtime_settings_from_panel,
                request_auto_heal=self._request_auto_heal,
                get_market_status=self._get_market_status_from_panel,
                get_master_status=self._get_master_status_from_panel,
            ),
        )

        strat_names = [s.name for s in self.strategies]
        _pool = ", ".join(strat_names)
        # Eine Zeile (kein Umbruch in journalctl bei langen Listen)
        logger.info(
            "Aktive Strategien (%d): %s — Regime-Engine + Meta-Selector + Brain wählen pro Symbol.",
            len(strat_names),
            _pool,
        )
        # Zusätzlich grep-freundlich (ohne Rich): in journalctl klar erkennbar
        logger.info("STRATEGY_POOL_COUNT=%d", len(strat_names))
        logger.info("STRATEGY_POOL_NAMES=%s", _pool)
        # Pro Symbol werden Einzel-Strategien nur bei LOG_LEVEL=DEBUG geloggt (sonst zu viel Spam).
        if bool(getattr(settings, "SHORT_ONLY_TRADING", False)):
            logger.info(
                "[yellow]SHORT_ONLY_TRADING aktiv[/yellow] – nur SHORT-Entries "
                "(Meta-Selector nur unter SHORT-Signalen). "
                "FUTURES_MODE=true für Live-Short auf Perps empfohlen."
            )
            if not bool(getattr(settings, "SHORT_ENABLED", True)):
                logger.warning(
                    "SHORT_ONLY_TRADING ist an, aber SHORT_ENABLED=false – bitte SHORT_ENABLED=true setzen."
                )
        self._recover_after_restart()
        self._bootstrap_entry_cadence_from_history()
        self._entry_cadence_status = {
            "enabled": bool(getattr(settings, "ENTRY_CADENCE_GUARD_ENABLED", True)),
            "level": 0,
            "reason": "bootstrapped",
        }
        self._apply_entry_cadence_guard()
        self._sync_runtime_state()
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

        logger.info(
            "Telegram Bootstrap | enabled=%s | panel_enabled=%s | token=%s | chat_id=%s",
            settings.TELEGRAM_ENABLED,
            settings.TELEGRAM_PANEL_ENABLED,
            "set" if bool(settings.TELEGRAM_BOT_TOKEN) else "missing",
            "set" if bool(settings.TELEGRAM_CHAT_ID) else "missing",
        )
        if autostart_services:
            # Panel zuerst wie Single-Bot — getUpdates läuft, bevor große Start-Pushes
            self.panel.start_in_background()
            if self.panel.enabled:
                logger.info("Telegram Polling gestartet.")
            else:
                logger.info("Telegram Polling nicht gestartet (deaktiviert).")
            self.tg.notify_bot_start(
                mode=settings.TRADING_MODE,
                strategy="AUTO (Meta-Selector)",
                pairs=self.pairs,
                timeframe=settings.TIMEFRAME,
            )
            if self._is_mini_live():
                limits = self._mini_live_limits_text()
                logger.warning("MINI-LIVE START: %s", limits)
                self.tg.notify_live_test_mode_start(limits)
        logger.info("Multi-Bot bereit.")

    def _recovery_state_path(self) -> Path:
        return Path(settings.STATE_RECOVERY_FILE)

    @staticmethod
    def _parse_db_timestamp(raw: object) -> Optional[datetime]:
        txt = str(raw or "").strip()
        if not txt:
            return None
        try:
            # SQLite speichert naive UTC-ISO-Strings.
            return datetime.fromisoformat(txt).replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _bootstrap_entry_cadence_from_history(self) -> None:
        """
        Initialisiert die Entry-Cadence anhand der letzten Trades, damit ein
        Bot-Restart nicht fälschlich als lange Inaktivität gewertet wird.
        """
        try:
            rows = self.repo.get_recent_trades(limit=200, current_mode_only=True)
            latest_ts = 0.0
            for row in rows:
                if str(row.get("status") or "").strip().lower() == "rejected":
                    continue
                dt = self._parse_db_timestamp(row.get("timestamp_open") or row.get("created_at"))
                if not dt:
                    continue
                latest_ts = max(latest_ts, dt.timestamp())
            self._last_entry_open_ts = latest_ts
        except Exception:
            self._last_entry_open_ts = 0.0

    def _count_entries_today(self) -> int:
        try:
            rows = self.repo.get_recent_trades(limit=1500, current_mode_only=True)
        except Exception:
            return 0
        today = datetime.now(timezone.utc).date()
        count = 0
        for row in rows:
            st = str(row.get("status") or "").strip().lower()
            if st == "rejected":
                continue
            dt = self._parse_db_timestamp(row.get("timestamp_open") or row.get("created_at"))
            if not dt:
                continue
            if dt.date() == today:
                count += 1
        return count

    def _update_entry_cadence_app_context(self, extra: Optional[Dict] = None) -> None:
        snap = runtime_state.snapshot()
        app_ctx = dict(snap.get("app_context") or {})
        app_ctx["entry_cadence"] = dict(self._entry_cadence_status or {})
        if extra:
            app_ctx.update(extra)
        runtime_state.update_app_context(app_ctx)

    def _reset_entry_filters_to_baseline(self) -> None:
        settings.MIN_CONFIDENCE = float(self._entry_cadence_baseline.get("min_confidence", settings.MIN_CONFIDENCE))
        settings.MIN_RR = float(self._entry_cadence_baseline.get("min_rr", settings.MIN_RR))
        settings.BRAIN_MIN_SCORE_TO_TRADE = float(
            self._entry_cadence_baseline.get("brain_min_score_to_trade", settings.BRAIN_MIN_SCORE_TO_TRADE)
        )
        base_wc = float(self._entry_cadence_baseline.get("min_win_chance_pct", settings.MIN_WIN_CHANCE_PCT))
        if base_wc <= 0:
            settings.MIN_WIN_CHANCE_PCT = 0.0
        else:
            settings.MIN_WIN_CHANCE_PCT = base_wc

    def _register_entry_opened(self, symbol: str, strategy_name: str) -> None:
        self._last_entry_open_ts = time.time()
        if self._entry_cadence_level > 0:
            self._reset_entry_filters_to_baseline()
            runtime_state.append_log("CADENCE relaxed_filters_reset_on_entry")
        self._entry_cadence_level = 0
        self._entry_cadence_status = {
            "enabled": bool(getattr(settings, "ENTRY_CADENCE_GUARD_ENABLED", True)),
            "level": 0,
            "reason": "entry_opened",
            "symbol": symbol,
            "strategy": strategy_name,
            "last_entry_minutes_ago": 0.0,
        }
        self._update_entry_cadence_app_context()

    def _apply_entry_cadence_guard(self) -> None:
        if not bool(getattr(settings, "ENTRY_CADENCE_GUARD_ENABLED", True)):
            self._entry_cadence_level = 0
            self._entry_cadence_status = {"enabled": False, "level": 0, "reason": "disabled"}
            self._update_entry_cadence_app_context()
            return

        now_ts = time.time()
        target_per_day = max(1, int(getattr(settings, "ENTRY_CADENCE_TARGET_TRADES_PER_DAY", 8)))
        inactivity_trigger_min = max(10, int(getattr(settings, "ENTRY_CADENCE_INACTIVITY_MINUTES", 120)))
        last_ts = self._last_entry_open_ts or self._started_at_ts
        inactivity_min = max(0.0, (now_ts - last_ts) / 60.0)
        entries_today = self._count_entries_today()
        progress = entries_today / float(target_per_day)

        level = 0
        if progress < 0.75:
            level = max(level, 1)
        if progress < 0.50:
            level = max(level, 2)
        if progress < 0.25:
            level = max(level, 3)
        if inactivity_min >= inactivity_trigger_min:
            extra = int((inactivity_min - inactivity_trigger_min) // max(inactivity_trigger_min, 1))
            level = max(level, min(3, 1 + extra))

        prev_level = int(self._entry_cadence_level)
        self._entry_cadence_level = int(max(0, min(3, level)))

        base_conf = float(self._entry_cadence_baseline.get("min_confidence", settings.MIN_CONFIDENCE))
        base_rr = float(self._entry_cadence_baseline.get("min_rr", settings.MIN_RR))
        base_wc = float(self._entry_cadence_baseline.get("min_win_chance_pct", settings.MIN_WIN_CHANCE_PCT))
        base_brain = float(
            self._entry_cadence_baseline.get("brain_min_score_to_trade", settings.BRAIN_MIN_SCORE_TO_TRADE)
        )

        lvl = float(self._entry_cadence_level)
        settings.MIN_CONFIDENCE = max(
            float(getattr(settings, "ENTRY_CADENCE_MIN_CONFIDENCE_FLOOR", 30.0)),
            base_conf - lvl * float(getattr(settings, "ENTRY_CADENCE_RELAX_MIN_CONF_STEP", 4.0)),
        )
        settings.MIN_RR = max(
            float(getattr(settings, "ENTRY_CADENCE_MIN_RR_FLOOR", 1.10)),
            base_rr - lvl * float(getattr(settings, "ENTRY_CADENCE_RELAX_MIN_RR_STEP", 0.08)),
        )
        settings.BRAIN_MIN_SCORE_TO_TRADE = max(
            float(getattr(settings, "ENTRY_CADENCE_BRAIN_SCORE_FLOOR", 0.20)),
            base_brain - lvl * float(getattr(settings, "ENTRY_CADENCE_RELAX_BRAIN_SCORE_STEP", 0.03)),
        )
        if base_wc <= 0:
            settings.MIN_WIN_CHANCE_PCT = 0.0
        else:
            settings.MIN_WIN_CHANCE_PCT = max(
                float(getattr(settings, "ENTRY_CADENCE_MIN_WIN_CHANCE_FLOOR", 58.0)),
                base_wc - lvl * float(getattr(settings, "ENTRY_CADENCE_RELAX_MIN_WIN_CHANCE_STEP", 3.0)),
            )

        self._entry_cadence_status = {
            "enabled": True,
            "level": int(self._entry_cadence_level),
            "entries_today": int(entries_today),
            "target_trades_per_day": int(target_per_day),
            "inactivity_minutes": round(inactivity_min, 1),
            "min_confidence_active": round(float(settings.MIN_CONFIDENCE), 3),
            "min_rr_active": round(float(settings.MIN_RR), 3),
            "min_win_chance_active": round(float(settings.MIN_WIN_CHANCE_PCT), 3),
            "brain_min_score_active": round(float(settings.BRAIN_MIN_SCORE_TO_TRADE), 3),
        }
        self._update_entry_cadence_app_context()
        if prev_level != self._entry_cadence_level:
            runtime_state.append_log(
                "CADENCE level_change "
                f"{prev_level}->{self._entry_cadence_level} "
                f"entries_today={entries_today}/{target_per_day} inactivity={inactivity_min:.1f}m"
            )
            if self._entry_cadence_level >= 2:
                self.tg.send(
                    "⚙️ <b>ENTRY CADENCE GUARD</b>\n"
                    f"Level {self._entry_cadence_level} aktiv.\n"
                    f"Entries heute: {entries_today}/{target_per_day} | "
                    f"Inaktiv: {inactivity_min:.0f}min.\n"
                    "Entry-Filter werden temporär gelockert, um Stalls zu vermeiden."
                )

    def _maybe_override_master_autopause_for_cadence(self) -> None:
        if not bool(getattr(settings, "ENTRY_CADENCE_GUARD_ENABLED", True)):
            return
        if not bool(getattr(settings, "MASTER_BRAIN_ENABLED", True)):
            return
        if self._entry_cadence_level < 2:
            return
        now_ts = time.time()
        last_ts = self._last_entry_open_ts or self._started_at_ts
        inactivity_min = max(0.0, (now_ts - last_ts) / 60.0)
        override_trigger = max(
            int(getattr(settings, "ENTRY_CADENCE_MASTER_OVERRIDE_MINUTES", 180)),
            int(getattr(settings, "ENTRY_CADENCE_INACTIVITY_MINUTES", 120)),
        )
        if inactivity_min < float(override_trigger):
            return
        ctrl = runtime_control.get_snapshot()
        if not (bool(ctrl.get("paused")) or bool(ctrl.get("risk_off")) or self._master_auto_paused):
            return
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        self._master_auto_paused = False
        hold_minutes = max(30, int(getattr(settings, "ENTRY_CADENCE_INACTIVITY_MINUTES", 120)))
        self._master_cadence_override_until_ts = now_ts + hold_minutes * 60
        until_txt = datetime.fromtimestamp(
            self._master_cadence_override_until_ts, tz=timezone.utc
        ).strftime("%H:%M:%S UTC")
        self._master_last_reason = (
            f"cadence_override_active_until:{until_txt} inactivity={inactivity_min:.0f}m"
        )
        runtime_state.append_log(
            "MASTER cadence_override "
            f"inactivity={inactivity_min:.1f}m hold_until={until_txt}"
        )
        self.tg.send(
            "🟢 <b>MASTER CADENCE OVERRIDE</b>\n"
            f"Inaktivität {inactivity_min:.0f}min erkannt. Master-AutoPause temporär übersteuert bis {until_txt}."
        )

    def _build_reflection_context(self) -> Dict:
        rt = runtime_state.snapshot()
        gate = {}
        try:
            gate = self.risk.get_gate_status()
        except Exception:
            gate = {}
        stale_symbols = 0
        try:
            hs = self.health.get_snapshot() if self.health else {}
            ages = hs.get("data_ages_sec") or {}
            stale_timeout = float(getattr(settings, "DATA_STALE_TIMEOUT_SEC", 600))
            stale_symbols = len(
                [sym for sym, age in ages.items() if float(age or 0.0) > stale_timeout]
            )
        except Exception:
            stale_symbols = 0
        master_status = self._get_master_status_from_panel()
        return {
            "paused": bool(rt.get("paused", False)),
            "risk_off": bool(rt.get("risk_off", False)),
            "open_positions": int(gate.get("open_positions", 0) or 0),
            "max_open_positions": int(gate.get("max_open_positions", 0) or 0),
            "gate_last_reason": str(gate.get("last_gate_reason", "n/a")),
            "master_reason": str(master_status.get("last_reason", "n/a")),
            "stale_symbols": int(stale_symbols),
            "cadence_level": int((self._entry_cadence_status or {}).get("level", 0) or 0),
            "entries_today": int((self._entry_cadence_status or {}).get("entries_today", 0) or 0),
            "target_trades_per_day": int(
                (self._entry_cadence_status or {}).get(
                    "target_trades_per_day",
                    int(getattr(settings, "ENTRY_CADENCE_TARGET_TRADES_PER_DAY", 8)),
                )
                or 0
            ),
            "master_enabled": bool(master_status.get("enabled", True)),
            "master_auto_paused": bool(master_status.get("auto_paused", False)),
        }

    def _queue_close_oldest_from_panel(
        self, close_count: int, keep_newest: int
    ) -> Tuple[bool, str]:
        """
        Plant das Schließen der ältesten offenen Trades (nach timestamp_open),
        lässt die neuesten keep_newest offen.
        """
        try:
            close_n = max(1, int(close_count))
            keep_n = max(0, int(keep_newest))
        except Exception:
            return False, "Ungültige Parameter."
        open_rows = self.repo.get_open_trades(limit=500)
        if not open_rows:
            return False, "Keine offenen Trades in DB."
        try:
            ordered = sorted(
                open_rows,
                key=lambda r: (
                    str(r.get("timestamp_open") or r.get("created_at") or ""),
                    int(r.get("id") or 0),
                ),
            )
        except Exception:
            ordered = list(open_rows)
        max_closable = max(0, len(ordered) - keep_n)
        if max_closable <= 0:
            return False, f"Nichts zu schließen: keep_newest={keep_n} deckt alle offenen Trades ab."
        to_close = ordered[: min(close_n, max_closable)]
        self._queued_forced_closes = [
            {
                "symbol": str(row.get("symbol") or ""),
                "reason": "forced_close_oldest",
            }
            for row in to_close
            if str(row.get("symbol") or "").strip()
        ]
        if not self._queued_forced_closes:
            return False, "Keine validen Symbole zum Schließen gefunden."
        runtime_state.append_log(
            "FORCE_CLOSE_QUEUED oldest="
            + ",".join(item["symbol"] for item in self._queued_forced_closes)
        )
        return (
            True,
            f"Forciertes Schließen geplant: {len(self._queued_forced_closes)} älteste Position(en), "
            f"{keep_n} neueste bleiben offen.",
        )

    def _request_close_oldest_open_trades_from_panel(
        self, close_count: int, keep_newest: int
    ) -> Tuple[bool, str]:
        ok, msg = self._queue_close_oldest_from_panel(
            close_count=close_count,
            keep_newest=keep_newest,
        )
        if not ok:
            return ok, msg
        if self.running:
            closed_now = self._run_forced_close_queue()
            self._sync_runtime_state()
            if closed_now > 0:
                return True, f"{msg} Sofort geschlossen: {closed_now}."
            return True, f"{msg} Ausführung im nächsten Zyklus."
        return True, msg

    def _run_forced_close_queue(self) -> int:
        """
        Führt geplante Forced-Closes sicher im Bot-Thread aus.
        """
        if not self._forced_close_lock.acquire(blocking=False):
            return 0
        try:
            queue = list(self._queued_forced_closes)
            if not queue:
                return 0
            self._queued_forced_closes = []
            closed_count = 0
            for item in queue:
                symbol = str(item.get("symbol") or "").strip()
                if not symbol:
                    continue
                pos = self.risk.open_positions.get(symbol)
                if not pos:
                    continue
                # nutze zuletzt bekannten Mark-Preis; wenn nicht vorhanden, Entry als Fallback
                mark = float(self._last_prices.get(symbol, getattr(pos, "entry_price", 0.0)) or 0.0)
                if mark <= 0:
                    mark = float(getattr(pos, "entry_price", 0.0) or 0.0)
                if mark <= 0:
                    continue
                exit_side = "sell" if getattr(pos, "side", "long") == "long" else "buy"
                exec_result = self.exec_engine.execute_exit(symbol, exit_side, float(getattr(pos, "amount", 0.0) or 0.0))
                if not exec_result.success:
                    logger.warning(
                        "FORCE_CLOSE Exit-Order Problem %s: %s (lokaler Close wird fortgesetzt)",
                        symbol,
                        exec_result.reason,
                    )
                pnl = self.risk.close_position(symbol, mark)
                trade_id = self._open_trade_ids.pop(symbol, None)
                if trade_id is not None and pnl is not None:
                    try:
                        entry_price = float(getattr(pos, "entry_price", 0.0) or 0.0)
                        amount = float(getattr(pos, "amount", 0.0) or 0.0)
                        cost = entry_price * amount
                        pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
                        self.repo.close_trade(
                            int(trade_id),
                            mark,
                            float(pnl),
                            float(pnl_pct),
                            "forced_close_oldest",
                        )
                        self.tg.notify_trade_closed(
                            symbol=symbol,
                            side=getattr(pos, "side", "long"),
                            entry=entry_price,
                            exit_price=mark,
                            pnl=float(pnl),
                            pnl_pct=float(pnl_pct),
                            reason="forced_close_oldest",
                            strategy=getattr(pos, "strategy_name", self._active_strategy_runtime),
                            is_paper=settings.TRADING_MODE == "paper",
                        )
                    except Exception as e:
                        logger.warning("FORCE_CLOSE DB/Notify Fehler %s: %s", symbol, e)
                runtime_state.append_log(f"FORCE_CLOSE_DONE {symbol} reason=forced_close_oldest")
                closed_count += 1
            return closed_count
        finally:
            self._forced_close_lock.release()

    def _apply_self_reflection_repairs(self) -> None:
        if not bool(getattr(settings, "SELF_REFLECTION_ENABLED", True)):
            return
        now_ts = time.time()
        cooldown_sec = max(
            60,
            int(getattr(settings, "SELF_REFLECTION_REPAIR_COOLDOWN_MINUTES", 20)) * 60,
        )
        if now_ts - float(self._self_reflection_last_repair_ts) < float(cooldown_sec):
            return
        insight = self._self_reflection.latest_insight()
        if not insight:
            return
        actions = list(insight.get("repair_actions") or [])
        if not actions:
            return
        changed: List[str] = []
        rt = runtime_state.snapshot()
        gate = self.risk.get_gate_status()
        if "unlock_entries" in actions:
            if bool(rt.get("paused")) or bool(rt.get("risk_off")):
                runtime_control.resume_entries()
                runtime_control.disable_risk_off()
                changed.append("unlock_entries")
        if "reduce_master_strictness" in actions:
            if bool(getattr(settings, "MASTER_BRAIN_ENABLED", True)):
                min_trades = int(getattr(settings, "MASTER_BRAIN_MIN_TRADES", 20))
                fail_windows = int(getattr(settings, "MASTER_BRAIN_FAIL_WINDOWS", 2))
                settings.MASTER_BRAIN_MIN_TRADES = max(8, min_trades - 4)
                settings.MASTER_BRAIN_FAIL_WINDOWS = min(8, fail_windows + 1)
                settings.MASTER_BRAIN_AUTO_PAUSE = False
                self._master_auto_paused = False
                runtime_control.resume_entries()
                runtime_control.disable_risk_off()
                changed.append(
                    "master_relaxed:min_trades-4,fail_windows+1,auto_pause=false"
                )
        if "increase_max_positions" in actions:
            current = int(self.risk.max_open_trades)
            target = int(getattr(settings, "SELF_REFLECTION_MAX_POSITIONS_CEIL", 12))
            if current < target:
                new_val = min(target, current + 1)
                settings.MAX_OPEN_TRADES = new_val
                settings.MAX_POSITIONS_TOTAL = new_val
                self.risk.max_open_trades = new_val
                if hasattr(self.risk, "portfolio") and getattr(self.risk, "portfolio", None):
                    self.risk.portfolio.cfg.max_positions_total = new_val
                changed.append(f"max_positions={new_val}")
        if "clear_noncritical_risk_off" in actions:
            reason = str(gate.get("last_gate_reason", ""))
            if "DAILY LOSS" not in reason.upper():
                runtime_control.disable_risk_off()
                changed.append("risk_off_cleared")
        if changed:
            self._self_reflection_last_repair_ts = now_ts
            runtime_state.append_log("SELF_REFLECTION_REPAIR " + ", ".join(changed))
            self._self_reflection.remember(
                event_type="self_repair",
                severity="info",
                details={
                    "changes": list(changed),
                    "pattern": str(insight.get("pattern", "n/a")),
                    "reason": str(insight.get("reason", "n/a")),
                },
            )
            self.tg.send(
                "🛠 <b>SELF-REPAIR</b>\n"
                f"Reflexionsmodus hat Reparatur ausgeführt:\n<code>{', '.join(changed)}</code>"
            )
            self._sync_runtime_state()
            self._save_master_snapshot("self_reflection_auto_repair")

    def _persist_recovery_state(self) -> None:
        if not settings.STATE_RECOVERY_ENABLED:
            return
        try:
            path = self._recovery_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            ctrl = runtime_control.get_snapshot()
            snap = runtime_state.snapshot()
            payload = {
                "mode": settings.TRADING_MODE,
                "paused": bool(ctrl.get("paused")),
                "risk_off": bool(ctrl.get("risk_off")),
                "preferred_strategy": ctrl.get("preferred_strategy") or "",
                "mode_request": ctrl.get("mode_request") or "",
                "last_signal": snap.get("last_signal") or {},
                "last_decision": snap.get("last_decision") or {},
                "brain": snap.get("brain") or {},
                "updated_at": snap.get("updated_at"),
            }
            path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Recovery-State konnte nicht gespeichert werden: {e}")

    def _restore_control_state_from_file(self) -> None:
        if not settings.STATE_RECOVERY_ENABLED:
            return
        path = self._recovery_state_path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("paused"):
                runtime_control.pause_entries()
            else:
                runtime_control.resume_entries()
            if raw.get("risk_off"):
                runtime_control.enable_risk_off()
            else:
                runtime_control.disable_risk_off()
            preferred = str(raw.get("preferred_strategy") or "").strip()
            if preferred:
                runtime_control.set_preferred_strategy(preferred)
            mode_request = str(raw.get("mode_request") or "").strip().lower()
            if mode_request:
                runtime_control.request_mode(mode_request)
            if isinstance(raw.get("last_signal"), dict):
                runtime_state.set_last_signal(raw.get("last_signal") or {})
            if isinstance(raw.get("last_decision"), dict):
                runtime_state.set_last_decision(raw.get("last_decision") or {})
            if isinstance(raw.get("brain"), dict):
                self._last_brain_snapshot = raw.get("brain") or {}
                runtime_state.update_brain(self._last_brain_snapshot)
            runtime_state.append_log("RECOVERY control_state_restored")
        except Exception as e:
            logger.warning(f"Recovery-Control-State konnte nicht geladen werden: {e}")

    def _recover_open_positions_from_db(self) -> int:
        restored = 0
        restored_notional = 0.0
        open_rows = self.repo.get_open_trades(
            limit=int(getattr(settings, "RECOVERY_MAX_OPEN_TRADES_RESTORE", 100))
        )
        seen_symbols: Set[str] = set()
        for row in open_rows:
            symbol = str(row.get("symbol") or "").strip()
            if not symbol or symbol in seen_symbols:
                # Duplicate offene Trades auf demselben Symbol bleiben konservativ blockiert.
                if symbol:
                    self._recovery_blocked_symbols.add(symbol)
                continue
            seen_symbols.add(symbol)
            try:
                entry = float(row.get("entry_price") or 0.0)
                amount = float(row.get("position_size") or 0.0)
                stop_loss = float(row.get("stop_loss") or 0.0)
                take_profit = float(row.get("take_profit") or 0.0)
                side = str(row.get("side") or "long").lower()
                if entry <= 0 or amount <= 0:
                    self._recovery_blocked_symbols.add(symbol)
                    continue
                pos = Position(
                    symbol=symbol,
                    entry_price=entry,
                    amount=amount,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    side=side,
                    highest_price=entry,
                    strategy_name=str(row.get("strategy_name") or ""),
                )
                self.risk.open_positions[symbol] = pos
                self._open_trade_ids[symbol] = int(row.get("id"))
                self._last_prices[symbol] = entry
                restored += 1
                restored_notional += entry * amount
            except Exception:
                self._recovery_blocked_symbols.add(symbol)
        if restored_notional > 0 and not paper_equity_ledger_enabled():
            self.risk.balance = max(0.0, float(self.risk.balance) - restored_notional)
        return restored

    def _startup_sanity_checks(self) -> List[str]:
        issues: List[str] = []
        markets: List[str] = []
        try:
            markets = self.exchange.get_markets()
            if settings.TRADING_MODE == "live" and not markets:
                issues.append("exchange_markets_unavailable")
        except Exception as e:
            issues.append(f"exchange_connection_failed:{type(e).__name__}")
        if markets:
            market_set = set(markets)
            invalid_pairs = [p for p in self.pairs if p not in market_set]
            if invalid_pairs:
                issues.append(f"invalid_pairs:{','.join(invalid_pairs[:5])}")
        try:
            if self.pairs:
                p = self.pairs[0]
                px = float(self.exchange.fetch_market_price(p) or 0.0)
                if px <= 0:
                    issues.append(f"no_market_price:{p}")
        except Exception as e:
            issues.append(f"ticker_check_failed:{type(e).__name__}")
        return issues

    @staticmethod
    def _is_soft_startup_issue(issue: str) -> bool:
        """
        Soft-Issues dürfen den Bot nicht dauerhaft blockieren.
        Diese Probleme sind oft temporär (API/Ticker/Netz).
        """
        txt = str(issue or "").strip().lower()
        return (
            txt.startswith("ticker_check_failed:")
            or txt.startswith("no_market_price:")
            or txt.startswith("exchange_connection_failed:")
            or txt.startswith("exchange_markets_unavailable")
        )

    def _reevaluate_startup_gate(self) -> None:
        """
        Re-evaluiert ein aktives Startup-Gate zur Laufzeit.
        Ziel: Kein permanenter Lock, wenn sich externe Bedingungen wieder normalisieren.
        """
        if self._startup_checks_ok:
            return
        try:
            issues = self._startup_sanity_checks()
            if issues:
                self._startup_block_reason = " | ".join(issues)
                return

            self._startup_checks_ok = True
            self._startup_block_reason = ""
            self._recovery_blocked_symbols.clear()
            runtime_control.resume_entries()
            runtime_control.disable_risk_off()
            runtime_state.append_log("RECOVERY startup_gate_auto_unlocked")
            self.tg.notify_risk_off(False, "startup_gate_auto_unlocked")
            logger.warning("[green]STARTUP-GATE GELÖST[/green] Recovery automatisch entsperrt.")
        except Exception as e:
            logger.warning("Startup-Gate-Recheck fehlgeschlagen: %s", e)

    def _recover_after_restart(self) -> None:
        self._restore_control_state_from_file()
        restored_positions = self._recover_open_positions_from_db()
        open_orders_count = 0
        exchange_order_symbols: Set[str] = set()
        exchange_pos_symbols: Set[str] = set()

        if settings.TRADING_MODE == "live":
            try:
                open_orders = self.exchange.fetch_open_orders() or []
                open_orders_count = len(open_orders)
                exchange_order_symbols = {
                    str(o.get("symbol") or "").strip() for o in open_orders if o.get("symbol")
                }
            except Exception as e:
                logger.warning(f"Recovery: Open-Orders konnten nicht geladen werden: {e}")
            try:
                open_positions = self.exchange.fetch_open_positions() or []
                exchange_pos_symbols = {
                    str(p.get("symbol") or "").strip() for p in open_positions if p.get("symbol")
                }
            except Exception as e:
                logger.warning(f"Recovery: Open-Positions konnten nicht geladen werden: {e}")

        db_symbols = set(self.risk.open_positions.keys())
        orphan_order_symbols = exchange_order_symbols - db_symbols
        orphan_position_symbols = exchange_pos_symbols - db_symbols
        self._recovery_blocked_symbols.update(orphan_order_symbols)
        self._recovery_blocked_symbols.update(orphan_position_symbols)

        issues = self._startup_sanity_checks()
        if orphan_position_symbols:
            issues.append(
                f"orphan_exchange_positions:{','.join(sorted(orphan_position_symbols))}"
            )
        if issues:
            self._startup_checks_ok = False
            self._startup_block_reason = " | ".join(issues)
            runtime_control.pause_entries()
            runtime_control.enable_risk_off()
            runtime_state.append_log(f"RECOVERY startup_block {self._startup_block_reason}")
            self.tg.notify_error("RECOVERY_STARTUP_BLOCK", self._startup_block_reason)
        else:
            self._startup_checks_ok = True
            self._startup_block_reason = ""
            runtime_state.append_log(
                f"RECOVERY ok restored_positions={restored_positions} open_orders={open_orders_count}"
            )

        self.tg.notify_recovery_status(
            restored_positions=restored_positions,
            open_orders=open_orders_count,
            blocked_symbols=sorted(self._recovery_blocked_symbols),
            risk_off=runtime_control.get_snapshot().get("risk_off", False),
            notes=self._startup_block_reason,
        )

    def _reevaluate_startup_gate(self) -> None:
        """
        Verhindert, dass der Bot nach einem temporären Startup-Problem
        dauerhaft im Startup-Block hängen bleibt.
        """
        if self._startup_checks_ok:
            return
        try:
            issues = self._startup_sanity_checks()
            hard_issues = [i for i in issues if not self._is_soft_startup_issue(i)]
            if not hard_issues:
                self._startup_checks_ok = True
                self._startup_block_reason = ""
                # Recovery-Blocker sind konservativ nur für den ersten Start gedacht.
                # Wenn die Startup-Prüfung wieder sauber ist, sollen Symbole wieder laufen.
                self._recovery_blocked_symbols.clear()
                ctrl = runtime_control.get_snapshot()
                if ctrl.get("paused"):
                    runtime_control.resume_entries()
                if ctrl.get("risk_off"):
                    runtime_control.disable_risk_off()
                runtime_state.append_log("RECOVERY startup_gate_auto_healed")
                logger.warning(
                    "STARTUP-GATE AUTO-HEAL: Startup-Block aufgehoben, Entries wieder aktiv."
                )
                self.tg.notify_info(
                    "RECOVERY_AUTO_HEAL",
                    "Startup-Block automatisch aufgehoben. Bot läuft wieder normal.",
                )
                return

            new_reason = " | ".join(hard_issues)
            if new_reason != self._startup_block_reason:
                self._startup_block_reason = new_reason
                runtime_state.append_log(f"RECOVERY startup_block_update {new_reason}")
        except Exception as e:
            logger.warning(f"Startup-Gate-Reevaluation fehlgeschlagen: {e}")

    def _reevaluate_startup_gate(self) -> None:
        """
        Verhindert, dass der Bot nach einem temporären Startup-Problem
        dauerhaft im Startup-Block hängen bleibt.
        """
        if self._startup_checks_ok:
            return
        try:
            issues = self._startup_sanity_checks()
            if not issues:
                self._startup_checks_ok = True
                self._startup_block_reason = ""
                # Recovery-Blocker sind konservativ nur für den ersten Start gedacht.
                # Wenn die Startup-Prüfung wieder sauber ist, sollen Symbole wieder laufen.
                self._recovery_blocked_symbols.clear()
                ctrl = runtime_control.get_snapshot()
                if ctrl.get("paused"):
                    runtime_control.resume_entries()
                if ctrl.get("risk_off"):
                    runtime_control.disable_risk_off()
                runtime_state.append_log("RECOVERY startup_gate_auto_healed")
                logger.warning(
                    "STARTUP-GATE AUTO-HEAL: Startup-Block aufgehoben, Entries wieder aktiv."
                )
                self.tg.notify_info(
                    "RECOVERY_AUTO_HEAL",
                    "Startup-Block automatisch aufgehoben. Bot läuft wieder normal.",
                )
                return

            new_reason = " | ".join(issues)
            if new_reason != self._startup_block_reason:
                self._startup_block_reason = new_reason
                runtime_state.append_log(f"RECOVERY startup_block_update {new_reason}")
        except Exception as e:
            logger.warning(f"Startup-Gate-Reevaluation fehlgeschlagen: {e}")

    def _live_allowed_symbols(self) -> List[str]:
        raw = str(getattr(settings, "LIVE_ALLOWED_SYMBOLS", "") or "").strip()
        if not raw:
            return []
        return [s.strip().upper() for s in raw.split(",") if s.strip()]

    def _is_mini_live(self) -> bool:
        return settings.TRADING_MODE == "live" and bool(getattr(settings, "LIVE_TEST_MODE", False))

    def _mini_live_limits_text(self) -> str:
        return (
            f"max_notional={float(getattr(settings, 'LIVE_MAX_POSITION_SIZE', 0.0)):.2f} USDT, "
            f"max_open=1, daily_loss={float(getattr(settings, 'LIVE_TEST_DAILY_LOSS_LIMIT_PCT', 0.0)):.2f}%, "
            f"symbols={getattr(settings, 'LIVE_ALLOWED_SYMBOLS', '') or 'all'}, "
            f"strategies={getattr(settings, 'LIVE_ALLOWED_STRATEGIES', '') or 'all'}"
        )

    def _notify_mini_live_order(self, *, symbol: str, side: str, amount: float, entry: float) -> None:
        if not self._is_mini_live():
            return
        notional = max(0.0, float(amount) * float(entry))
        cap = float(getattr(settings, "LIVE_MAX_POSITION_SIZE", 0.0) or 0.0)
        logger.warning(
            "MINI-LIVE ORDER WARN | %s %s | amount=%.6f | notional=%.2f/%.2f",
            side.upper(),
            symbol,
            amount,
            notional,
            cap,
        )
        self.tg.notify_live_test_order_warning(
            symbol=symbol,
            side=side,
            amount=float(amount),
            notional=notional,
            cap=cap,
        )

    def _risk_state_at_entry_snapshot(self) -> Dict:
        ctrl = runtime_control.get_snapshot()
        gate = {}
        try:
            gate = self.risk.get_gate_status()
        except Exception:
            gate = {}
        stats = self.risk.get_stats()
        return {
            "paused": bool(ctrl.get("paused", False)),
            "risk_off": bool(ctrl.get("risk_off", False)),
            "gate_last_reason": gate.get("last_gate_reason"),
            "live_gate_last_reason": gate.get("live_last_gate_reason"),
            "open_positions": int(stats.get("open_positions", 0)),
            "daily_loss": float(stats.get("daily_loss", 0.0)),
            "portfolio_risk_pct": float(stats.get("portfolio_risk_pct", 0.0)),
            "mode": settings.TRADING_MODE,
            "live_test_mode": bool(getattr(settings, "LIVE_TEST_MODE", False)),
        }

    def _market_context(self, df) -> Dict:
        try:
            closes = df["close"].astype(float)
            returns = closes.pct_change().dropna()
            vol = float(returns.tail(20).std()) if not returns.empty else 0.0
            fast = float(closes.tail(20).mean())
            slow = float(closes.tail(50).mean()) if len(closes) >= 50 else fast
            trend = ((fast - slow) / slow) if slow else 0.0
            momentum = (
                (float(closes.iloc[-1]) - float(closes.iloc[-10])) / float(closes.iloc[-10])
                if len(closes) > 10 and float(closes.iloc[-10]) != 0
                else 0.0
            )
            return {
                "volatility_20": round(vol, 6),
                "trend_20_50": round(trend, 6),
                "momentum_10": round(momentum, 6),
                "last_close": round(float(closes.iloc[-1]), 6),
            }
        except Exception:
            return {}

    def _mtf_regime_for(self, symbol: str, timeframe: str) -> Tuple[str, Dict]:
        cache_key = f"{symbol}::{timeframe}"
        now_ts = time.time()
        ttl_sec = max(
            5,
            int(
                getattr(
                    settings,
                    "MTF_CACHE_TTL_SEC",
                    getattr(settings, "MTF_FETCH_CACHE_TTL_SEC", 45),
                )
            ),
        )
        cached = self._mtf_cache.get(cache_key) or {}
        cached_ts = float(cached.get("ts", 0.0) or 0.0)
        if now_ts - cached_ts <= ttl_sec and cached.get("regime"):
            return str(cached.get("regime")), dict(cached.get("context") or {})

        lookback = int(
            getattr(
                settings,
                "MTF_CANDLE_LIMIT",
                getattr(settings, "MTF_FETCH_LIMIT", settings.CANDLE_LIMIT),
            )
        )
        df_tf = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=lookback)
        if df_tf.empty:
            return "NO_DATA", {"reason": "no_data"}

        re = RegimeEngine()
        regime = re.detect(df_tf).value
        ctx = re.get_last_context()
        self._mtf_cache[cache_key] = {"ts": now_ts, "regime": regime, "context": ctx}
        return regime, dict(ctx or {})

    @staticmethod
    def _tf_direction_hint(regime_name: str, ctx: Dict) -> int:
        txt = str(regime_name or "").upper()
        trend_dir = str((ctx or {}).get("trend_direction", "")).lower()
        if "TREND_UP" in txt:
            return 1
        if "TREND_DOWN" in txt:
            return -1
        if "TREND_MARKET" in txt:
            if trend_dir == "up":
                return 1
            if trend_dir == "down":
                return -1
        if "LIQUIDATION_CASCADE" in txt:
            if trend_dir == "up":
                return 1
            if trend_dir == "down":
                return -1
        return 0

    def _evaluate_mtf_guard(
        self,
        *,
        symbol: str,
        entry_side: Side,
        entry_regime: str,
        entry_context: Dict,
    ) -> Tuple[bool, str, Dict]:
        if not bool(getattr(settings, "MTF_KING_ENABLED", True)):
            return True, "mtf_guard_disabled", {"enabled": False}

        micro_tf = str(getattr(settings, "MTF_MICRO_TIMEFRAME", "5m") or "5m").strip()
        setup_tf = str(getattr(settings, "MTF_SETUP_TIMEFRAME", "15m") or "15m").strip()
        direction_tf = str(getattr(settings, "MTF_DIRECTION_TIMEFRAME", "1h") or "1h").strip()
        context_tf = str(getattr(settings, "MTF_CONTEXT_TIMEFRAME", "4h") or "4h").strip()
        frames = [micro_tf, setup_tf, direction_tf, context_tf]
        strict_block = bool(getattr(settings, "MTF_STRICT_DIRECTION_CONTEXT_BLOCK", True))
        min_support_ratio = float(getattr(settings, "MTF_MIN_SUPPORT_RATIO", 0.50))
        max_opposing = max(0, int(getattr(settings, "MTF_MAX_OPPOSING_HIGHER_TFS", 2)))
        strong_threshold = max(0.0, float(getattr(settings, "MTF_DIRECTION_STRONG_THRESHOLD", 0.18)))

        side_val = entry_side.value if isinstance(entry_side, Side) else str(entry_side)
        side_sign = 1 if side_val == Side.LONG.value else -1 if side_val == Side.SHORT.value else 0
        if side_sign == 0:
            return True, "mtf_side_none", {"enabled": True, "side": side_val}

        rows: List[Dict[str, object]] = []
        support_score = 0.0
        opposing_total = 0
        aligned_total = 0
        unknown_total = 0
        hard_opposing = 0
        hard_aligned = 0

        for tf in frames:
            tf_regime, tf_ctx = self._mtf_regime_for(symbol, tf)
            tf_sign = self._tf_direction_hint(tf_regime, tf_ctx)
            slope = float((tf_ctx or {}).get("ema_slope_pct", 0.0) or 0.0)
            relation = "neutral"
            if tf_sign != 0:
                relation = "aligned" if tf_sign == side_sign else "opposing"
                if relation == "opposing":
                    opposing_total += 1
                    if tf in (direction_tf, context_tf):
                        hard_opposing += 1
                else:
                    aligned_total += 1
                    if tf in (direction_tf, context_tf):
                        hard_aligned += 1
            else:
                if tf_regime == "NO_DATA":
                    relation = "no_data"
                    unknown_total += 1

            # Kleinere TFs weniger Gewicht, Richtung/Kontext höher.
            weight = 0.8
            if tf == setup_tf:
                weight = 1.0
            elif tf == direction_tf:
                weight = 1.4
            elif tf == context_tf:
                weight = 1.8

            # Klares Gegensignal auf hohen TFs wird stärker bestraft.
            strength_mult = 1.0
            if abs(slope) >= strong_threshold:
                strength_mult = 1.25
            if relation == "aligned":
                support_score += 1.0 * weight * strength_mult
            elif relation == "opposing":
                support_score -= 1.0 * weight * strength_mult

            rows.append(
                {
                    "tf": tf,
                    "regime": tf_regime,
                    "trend": str((tf_ctx or {}).get("trend_direction", "n/a")),
                    "relation": relation,
                    "reason": str((tf_ctx or {}).get("reason", "n/a")),
                }
            )

        context = {
            "enabled": True,
            "symbol": symbol,
            "entry_side": side_val,
            "entry_regime": entry_regime,
            "entry_reason": str((entry_context or {}).get("reason", "n/a")),
            "frames": rows,
            "entry_timeframe": str(getattr(settings, "MTF_ENTRY_TIMEFRAME", settings.TIMEFRAME)),
            "micro_timeframe": micro_tf,
            "setup_timeframe": setup_tf,
            "direction_timeframe": direction_tf,
            "context_timeframe": context_tf,
            "aligned_total": aligned_total,
            "opposing_total": opposing_total,
            "unknown_frames": unknown_total,
            "opposing_hard": hard_opposing,
            "aligned_hard": hard_aligned,
            # Legacy-Felder für bestehende Telegram-Ansichten:
            "hard_opposing": hard_opposing,
            "hard_aligned": hard_aligned,
            "support_score": round(float(support_score), 4),
            "support_ratio": 0.5,
            "min_support_ratio": round(float(min_support_ratio), 4),
            "max_opposing_higher_tfs": int(max_opposing),
            "strict_direction_context_block": bool(strict_block),
            "direction_strong_threshold": round(float(strong_threshold), 4),
        }
        known_directional = aligned_total + opposing_total
        support_ratio = (
            float(aligned_total) / float(known_directional) if known_directional > 0 else 0.5
        )
        context["support_ratio"] = round(support_ratio, 4)

        if strict_block and hard_opposing >= 1 and hard_aligned == 0:
            return (
                False,
                "MTF HARD VETO: 1h/4h are clearly against entry",
                context,
            )
        if opposing_total > max_opposing:
            return (
                False,
                f"MTF BLOCK: too many opposing higher timeframes ({opposing_total}>{max_opposing})",
                context,
            )
        if known_directional > 0 and support_ratio < min_support_ratio:
            return (
                False,
                f"MTF BLOCK: support ratio too low ({support_ratio:.2f}<{min_support_ratio:.2f})",
                context,
            )
        return True, "mtf_confirmed", context

    def _log_decision_cycle(
        self,
        *,
        symbol: str,
        regime: str,
        ranking: Optional[List[Dict]] = None,
        chosen_strategy: str = "",
        signal_score: float = 0.0,
        risk_decision: str = "",
        allow_trade: bool = False,
        reject_reason: str = "",
        last_decision_reason: str = "",
        market_context: Optional[Dict] = None,
    ) -> None:
        if not self.decision_repo.available:
            return
        rnk = list(ranking or [])
        eligible = [str(x.get("strategy")) for x in rnk if x.get("eligible")]
        self.decision_repo.save_decision(
            mode=settings.TRADING_MODE,
            symbol=symbol,
            timeframe=settings.TIMEFRAME,
            detected_regime=regime or "",
            eligible_strategies=eligible,
            strategy_ranking=rnk[:8],
            chosen_strategy=chosen_strategy or "",
            signal_score=float(signal_score or 0.0),
            risk_decision=risk_decision or "",
            allow_trade=bool(allow_trade),
            reject_reason=reject_reason or "",
            last_decision_reason=last_decision_reason or "",
            market_context=market_context or {},
        )

    def _update_performance_tracking(self) -> None:
        stats = self.risk.get_stats()
        balance = float(stats.get("balance", 0.0))
        equity = float(self._calculate_equity())
        unrealized = round(equity - balance, 6)
        realized = float(stats.get("total_pnl", 0.0))
        total_trades = int(stats.get("total_trades", 0))
        win_rate = float(stats.get("winrate_pct", 0.0))
        day_pnl = 0.0
        daily = {}
        if self.perf_repo.available:
            daily = self.perf_repo.update_daily_summary(mode=settings.TRADING_MODE) or {}
            day_pnl = float(daily.get("pnl_abs", 0.0))
            self.perf_repo.save_snapshot(
                mode=settings.TRADING_MODE,
                current_balance=balance,
                current_equity=equity,
                open_positions_count=int(stats.get("open_positions", 0)),
                realized_pnl_total=realized,
                unrealized_pnl_total=unrealized,
                day_pnl=day_pnl,
                total_trades=total_trades,
                win_rate=win_rate,
            )
            latest = self.perf_repo.latest_snapshot(mode=settings.TRADING_MODE)
            runtime_state.update_performance(
                {
                    "snapshot": latest,
                    "daily_summary": daily,
                }
            )

    def _live_capital_snapshot(self, symbol: str) -> Tuple[float, float]:
        """
        Liefert (free_capital_usdt, equity_usdt) für das harte Live-Gate.
        Wenn keine verlässlichen Daten abrufbar sind, wird fail-closed (0.0) zurückgegeben.
        """
        try:
            bal = self.exchange.fetch_balance() or {}
            market = self.exchange.fetch_symbol_info(symbol) or {}
            quote = (market.get("quote") or "USDT").upper()
            quote_data = bal.get(quote) or {}
            free_capital = float(quote_data.get("free") or 0.0)
            equity = float(quote_data.get("total") or free_capital or 0.0)
            return free_capital, equity
        except Exception as e:
            logger.error(f"Live-Kapital-Snapshot fehlgeschlagen für {symbol}: {e}")
            return 0.0, 0.0

    def _process_pair(self, symbol: str):
        """Führt den vollständigen Analyse- und Ausführungszyklus für ein Pair durch."""
        if symbol in self._recovery_blocked_symbols:
            logger.warning(
                f"[yellow]RECOVERY BLOCK[/yellow] {symbol} | "
                "Symbol nach Neustart konservativ gesperrt"
            )
            self._record_last_decision(
                symbol=symbol,
                decision="blocked_recovery",
                reason="recovery_symbol_blocked",
                strategy=self._active_strategy_runtime,
            )
            self._log_decision_cycle(
                symbol=symbol,
                regime="RECOVERY_BLOCKED",
                ranking=[],
                chosen_strategy="",
                signal_score=0.0,
                risk_decision="blocked_recovery",
                allow_trade=False,
                reject_reason="recovery_symbol_blocked",
                last_decision_reason="recovery_symbol_blocked",
                market_context={},
            )
            return
        df = self.exchange.fetch_ohlcv(symbol)
        if df.empty:
            logger.warning(f"{symbol} | Keine OHLCV-Daten erhalten – übersprungen")
            self.health.record_error("warning", f"{symbol}: Keine OHLCV-Daten")
            self._record_last_decision(symbol=symbol, decision="skip", reason="no_data")
            self._log_decision_cycle(
                symbol=symbol,
                regime="NO_DATA",
                ranking=[],
                chosen_strategy="",
                signal_score=0.0,
                risk_decision="no_data",
                allow_trade=False,
                reject_reason="no_data",
                last_decision_reason="no_data",
                market_context={},
            )
            return

        self.health.update_data_freshness(symbol)
        current_price = float(df["close"].iloc[-1])
        self._last_prices[symbol] = current_price
        market_ctx = self._market_context(df)

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
                    try:
                        self.perf_tracker.refresh()
                    except Exception:
                        pass
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
                    self._record_trade_event(
                        event="closed",
                        symbol=symbol,
                        side=pos_side,
                        strategy=position.strategy_name,
                        pnl=pnl,
                        reason=exit_reason,
                    )
                    self._record_last_decision(
                        symbol=symbol,
                        decision="exit_closed",
                        reason=exit_reason,
                        strategy=position.strategy_name,
                    )
            return

        # Offene Position: kein neuer Einstieg
        if symbol in self.risk.open_positions:
            self._log_decision_cycle(
                symbol=symbol,
                regime="OPEN_POSITION",
                ranking=[],
                chosen_strategy="",
                signal_score=0.0,
                risk_decision="skip_open_position",
                allow_trade=False,
                reject_reason="open_position_exists",
                last_decision_reason="open_position_exists",
                market_context=market_ctx,
            )
            return

        # 2. Regime erkennen
        try:
            regime = self.regime_engine.detect(df)
        except Exception as e:
            logger.error(f"{symbol} | Regime-Erkennung fehlgeschlagen: {e}")
            self._log_decision_cycle(
                symbol=symbol,
                regime="REGIME_ERROR",
                ranking=[],
                chosen_strategy="",
                signal_score=0.0,
                risk_decision="regime_error",
                allow_trade=False,
                reject_reason="regime_detection_failed",
                last_decision_reason="regime_detection_failed",
                market_context=market_ctx,
            )
            return

        self._last_regime_context = self.regime_engine.get_last_context()
        snap = runtime_state.snapshot()
        app_ctx = dict(snap.get("app_context") or {})
        app_ctx["regime_context"] = dict(self._last_regime_context)
        app_ctx["detected_regime"] = regime.value
        runtime_state.update_app_context(app_ctx)

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

        if bool(getattr(settings, "SHORT_ONLY_TRADING", False)):
            signals = [s for s in signals if s.side == Side.SHORT]

        # 4. Intelligence-Brain (inkl. Meta-Selector + adaptive Ranking)
        best, brain_snapshot = self.brain.evaluate(
            symbol=symbol,
            regime=regime,
            signals=signals,
        )
        self._last_brain_snapshot = brain_snapshot
        self._last_selector_snapshot = brain_snapshot.get("selector", {})
        runtime_state.update_brain(brain_snapshot)
        ranking = list(brain_snapshot.get("last_strategy_ranking") or [])
        signal_score = float(brain_snapshot.get("last_signal_score", 0.0) or 0.0)
        if best is None or best.side == Side.NONE:
            reason = brain_snapshot.get("last_decision_reason", f"no_valid_signal_in_regime:{regime.value}")
            self._record_last_decision(
                symbol=symbol,
                decision="no_trade",
                reason=reason,
            )
            self._log_decision_cycle(
                symbol=symbol,
                regime=regime.value,
                ranking=ranking,
                chosen_strategy="",
                signal_score=signal_score,
                risk_decision="selector_or_brain_block",
                allow_trade=False,
                reject_reason=reason,
                last_decision_reason=reason,
                market_context=market_ctx,
            )
            return
        mtf_allowed, mtf_reason, mtf_ctx = self._evaluate_mtf_guard(
            symbol=symbol,
            entry_side=best.side,
            entry_regime=(best.regime or regime.value),
            entry_context=self._last_regime_context,
        )
        mtf_ctx = dict(mtf_ctx or {})
        mtf_ctx["allowed"] = bool(mtf_allowed)
        mtf_ctx["reason"] = str(mtf_reason)
        self._last_mtf_context = dict(mtf_ctx)
        snap = runtime_state.snapshot()
        app_ctx = dict(snap.get("app_context") or {})
        app_ctx["mtf_context"] = dict(self._last_mtf_context)
        runtime_state.update_app_context(app_ctx)
        if not mtf_allowed:
            logger.info(
                f"[yellow]MTF BLOCKED[/yellow] {symbol} | "
                f"Strategie: {best.strategy_name} | Grund: {mtf_reason}"
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
                reason_rejected=mtf_reason,
            )
            self.tg.notify_trade_blocked(
                symbol=symbol,
                strategy=best.strategy_name,
                side=best.side.value,
                reason=mtf_reason,
            )
            self._record_trade_event(
                event="blocked",
                symbol=symbol,
                side=best.side.value,
                strategy=best.strategy_name,
                pnl=None,
                reason=mtf_reason,
            )
            self._record_last_decision(
                symbol=symbol,
                decision="blocked_mtf",
                reason=mtf_reason,
                strategy=best.strategy_name,
            )
            self._log_decision_cycle(
                symbol=symbol,
                regime=regime.value,
                ranking=ranking,
                chosen_strategy=best.strategy_name,
                signal_score=signal_score,
                risk_decision="mtf_block",
                allow_trade=False,
                reject_reason=mtf_reason,
                last_decision_reason=mtf_reason,
                market_context={**market_ctx, "mtf": mtf_ctx},
            )
            return
        self._active_strategy_runtime = best.strategy_name
        self._record_last_signal(
            symbol=symbol,
            strategy=best.strategy_name,
            side=best.side.value,
            confidence=round(float(best.confidence), 1),
            reason=best.reason,
            entry=float(best.entry),
            timeframe=best.timeframe,
            rr=float(best.rr),
            regime=best.regime or regime.value,
        )

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
            self._record_trade_event(
                event="blocked",
                symbol=symbol,
                side=best.side.value,
                strategy=best.strategy_name,
                pnl=None,
                reason=block_reason,
            )
            self._record_last_decision(
                symbol=symbol,
                decision="blocked_risk_engine",
                reason=block_reason,
                strategy=best.strategy_name,
            )
            self._log_decision_cycle(
                symbol=symbol,
                regime=regime.value,
                ranking=ranking,
                chosen_strategy=best.strategy_name,
                signal_score=signal_score,
                risk_decision="risk_engine_block",
                allow_trade=False,
                reject_reason=block_reason,
                last_decision_reason=block_reason,
                market_context=market_ctx,
            )
            if block_reason.upper().startswith("DAILY LOSS"):
                gate = self.risk.get_gate_status()
                self.tg.notify_daily_loss_limit(
                    daily_loss_usdt=float(gate.get("daily_loss_usdt", 0.0)),
                    limit_usdt=float(gate.get("daily_loss_limit_usdt", 0.0)),
                    mode=settings.TRADING_MODE,
                )
                runtime_state.append_log(
                    f"ALERT daily_loss_limit {gate.get('daily_loss_usdt', 0.0)}/"
                    f"{gate.get('daily_loss_limit_usdt', 0.0)} USDT"
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
            self._record_trade_event(
                event="blocked",
                symbol=symbol,
                side=best.side.value,
                strategy=best.strategy_name,
                pnl=None,
                reason=pf_reason,
            )
            self._record_last_decision(
                symbol=symbol,
                decision="blocked_portfolio",
                reason=pf_reason,
                strategy=best.strategy_name,
            )
            self._log_decision_cycle(
                symbol=symbol,
                regime=regime.value,
                ranking=ranking,
                chosen_strategy=best.strategy_name,
                signal_score=signal_score,
                risk_decision="portfolio_block",
                allow_trade=False,
                reject_reason=pf_reason,
                last_decision_reason=pf_reason,
                market_context=market_ctx,
            )
            return

        # 5c. Harte Live-Schutzschicht direkt vor echter Order
        if settings.TRADING_MODE == "live":
            free_capital, equity = self._live_capital_snapshot(best.symbol)
            live_allowed, live_reason = self.risk.check_live_hard_gate(
                best,
                amount,
                free_capital_usdt=free_capital,
                account_equity_usdt=equity,
                allowed_symbols=self._live_allowed_symbols(),
            )
            if not live_allowed:
                logger.error(
                    f"[red]LIVE HARD GATE BLOCKIERT[/red] {symbol} | "
                    f"Strategie: {best.strategy_name} | Grund: {live_reason}"
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
                    reason_rejected=live_reason,
                )
                self.tg.notify_trade_blocked(
                    symbol=symbol,
                    strategy=best.strategy_name,
                    side=best.side.value,
                    reason=live_reason,
                )
                self.tg.notify_error("LIVE_GATE", live_reason)
                self._record_trade_event(
                    event="blocked",
                    symbol=symbol,
                    side=best.side.value,
                    strategy=best.strategy_name,
                    pnl=None,
                    reason=live_reason,
                )
                self._record_last_decision(
                    symbol=symbol,
                    decision="blocked_live_hard_gate",
                    reason=live_reason,
                    strategy=best.strategy_name,
                )
                self._log_decision_cycle(
                    symbol=symbol,
                    regime=regime.value,
                    ranking=ranking,
                    chosen_strategy=best.strategy_name,
                    signal_score=signal_score,
                    risk_decision="live_hard_gate_block",
                    allow_trade=False,
                    reject_reason=live_reason,
                    last_decision_reason=live_reason,
                    market_context=market_ctx,
                )
                runtime_state.append_log(f"LIVE_GATE_BLOCK {symbol} {best.strategy_name} {live_reason}")
                return

        # 5d. Harte Mindest-Win-Rate aus Trade-Historie (optional)
        hist_reason = historical_win_rate_block_reason(
            best.strategy_name, self.perf_tracker
        )
        if hist_reason:
            logger.info(
                f"[yellow]SKIP historische Win-Rate[/yellow] {symbol} | "
                f"{best.strategy_name} | {hist_reason}"
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
                reason_rejected=hist_reason,
            )
            self._record_last_decision(
                symbol=symbol,
                decision="blocked_historical_win_rate",
                reason=hist_reason,
                strategy=best.strategy_name,
            )
            self._log_decision_cycle(
                symbol=symbol,
                regime=regime.value,
                ranking=ranking,
                chosen_strategy=best.strategy_name,
                signal_score=signal_score,
                risk_decision="historical_win_rate_block",
                allow_trade=False,
                reject_reason=hist_reason,
                last_decision_reason=hist_reason,
                market_context=market_ctx,
            )
            return

        bitter_reason = bitter_reward_block_reason(
            best.strategy_name, self.perf_tracker
        )
        if bitter_reason:
            logger.info(
                f"[yellow]SKIP bitteres Reward-Gate[/yellow] {symbol} | "
                f"{best.strategy_name} | {bitter_reason}"
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
                reason_rejected=bitter_reason,
            )
            self._record_last_decision(
                symbol=symbol,
                decision="blocked_bitter_reward",
                reason=bitter_reason,
                strategy=best.strategy_name,
            )
            self._log_decision_cycle(
                symbol=symbol,
                regime=regime.value,
                ranking=ranking,
                chosen_strategy=best.strategy_name,
                signal_score=signal_score,
                risk_decision="bitter_reward_block",
                allow_trade=False,
                reject_reason=bitter_reason,
                last_decision_reason=bitter_reason,
                market_context=market_ctx,
            )
            return

        # 5e. Mindest-Gewinnchance (Heuristik + optional DB-Kalibrierung)
        min_wc = float(getattr(settings, "MIN_WIN_CHANCE_PCT", 0.0) or 0.0)
        if min_wc > 0:
            _bsr_gate = brain_snapshot.get("last_signal_score")
            _bf_gate = float(_bsr_gate) if _bsr_gate is not None else None
            wc_gate_m, _wl_gate_m = effective_entry_win_chance_pct(
                best.confidence,
                brain_score=_bf_gate,
                rr=best.rr,
                strategy_name=best.strategy_name,
                perf_tracker=self.perf_tracker,
            )
            if wc_gate_m < min_wc:
                reason_wc = f"MIN_WIN_CHANCE:{wc_gate_m:.1f}<{min_wc:.0f}"
                logger.info(
                    f"[yellow]SKIP Mindest-Gewinnchance[/yellow] {symbol} | "
                    f"Strategie: {best.strategy_name} | {wc_gate_m:.1f}% < {min_wc:.0f}%"
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
                    reason_rejected=reason_wc,
                )
                self._record_last_decision(
                    symbol=symbol,
                    decision="blocked_min_win_chance",
                    reason=reason_wc,
                    strategy=best.strategy_name,
                )
                self._log_decision_cycle(
                    symbol=symbol,
                    regime=regime.value,
                    ranking=ranking,
                    chosen_strategy=best.strategy_name,
                    signal_score=signal_score,
                    risk_decision="min_win_chance_block",
                    allow_trade=False,
                    reject_reason=reason_wc,
                    last_decision_reason=reason_wc,
                    market_context=market_ctx,
                )
                return

        # 6. Signal registrieren (Duplikatschutz)
        self.risk.register_signal(best)

        # 7. Order ausführen via Execution Engine (Retry, Slippage, Fail-Safes)
        if best.side == Side.LONG:
            self._notify_mini_live_order(
                symbol=symbol, side="buy", amount=amount, entry=best.entry
            )
            exec_result = self.exec_engine.execute_entry(
                symbol=symbol, order_side="buy", amount=amount, signal=best
            )
            if not exec_result.success:
                logger.warning(
                    f"[yellow]EXECUTION BLOCKIERT[/yellow] {symbol} | "
                    f"Strategie: {best.strategy_name} | Grund: {exec_result.reason}"
                )
                self._record_last_decision(
                    symbol=symbol,
                    decision="execution_blocked",
                    reason=exec_result.reason,
                    strategy=best.strategy_name,
                )
                self._log_decision_cycle(
                    symbol=symbol,
                    regime=regime.value,
                    ranking=ranking,
                    chosen_strategy=best.strategy_name,
                    signal_score=signal_score,
                    risk_decision="execution_blocked",
                    allow_trade=False,
                    reject_reason=exec_result.reason,
                    last_decision_reason=exec_result.reason,
                    market_context=market_ctx,
                )
                return

            self.risk.open_with_signal(best, amount)
            self._register_entry_opened(symbol, best.strategy_name)
            _snap = self._last_brain_snapshot or {}
            _bs_raw = _snap.get("last_signal_score")
            _brain_f = float(_bs_raw) if _bs_raw is not None else None
            _wc, _wl = effective_entry_win_chance_pct(
                best.confidence,
                brain_score=_brain_f,
                rr=best.rr,
                strategy_name=best.strategy_name,
                perf_tracker=self.perf_tracker,
            )
            logger.info(
                f"[bold green]LONG ERÖFFNET[/bold green] {symbol} | "
                f"Strategie: {best.strategy_name} | "
                f"Einstieg: {best.entry:.4f} | Fill: {exec_result.fill_price:.4f} | "
                f"Menge: {amount:.6f} | SL: {best.stop_loss:.4f} | "
                f"TP: {best.take_profit:.4f} | RR: {best.rr:.2f} | "
                f"Konfidenz: {best.confidence:.0f}/100 | "
                f"Gewinnchance(effektiv): {_wc:.0f}% ({_wl}) | "
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
                signal_score=float((self._last_brain_snapshot or {}).get("last_signal_score", 0.0)),
                risk_state_at_entry=self._risk_state_at_entry_snapshot(),
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
                brain_score=_brain_f,
            )
            self._record_trade_event(
                event="opened",
                symbol=symbol,
                side="long",
                strategy=best.strategy_name,
                pnl=None,
                reason=best.reason,
            )
            self._record_last_decision(
                symbol=symbol,
                decision="entry_opened",
                reason=best.reason,
                strategy=best.strategy_name,
            )
            self._log_decision_cycle(
                symbol=symbol,
                regime=regime.value,
                ranking=ranking,
                chosen_strategy=best.strategy_name,
                signal_score=signal_score,
                risk_decision="allow_trade",
                allow_trade=True,
                reject_reason="",
                last_decision_reason=best.reason,
                market_context=market_ctx,
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
        self._notify_mini_live_order(
            symbol=symbol, side="sell", amount=amount, entry=signal.entry
        )
        exec_result = self.exec_engine.execute_entry(
            symbol=symbol, order_side="sell", amount=amount, signal=signal
        )
        if not exec_result.success:
            logger.warning(
                f"[yellow]SHORT EXECUTION BLOCKIERT[/yellow] {symbol} | "
                f"Strategie: {signal.strategy_name} | Grund: {exec_result.reason}"
            )
            self._record_last_decision(
                symbol=symbol,
                decision="execution_blocked",
                reason=exec_result.reason,
                strategy=signal.strategy_name,
            )
            self._log_decision_cycle(
                symbol=symbol,
                regime=signal.regime or "UNKNOWN",
                ranking=list((self._last_brain_snapshot or {}).get("last_strategy_ranking") or []),
                chosen_strategy=signal.strategy_name,
                signal_score=float((self._last_brain_snapshot or {}).get("last_signal_score", 0.0) or 0.0),
                risk_decision="execution_blocked",
                allow_trade=False,
                reject_reason=exec_result.reason,
                last_decision_reason=exec_result.reason,
                market_context={},
            )
            return

        self.risk.open_with_signal(signal, amount)
        self._register_entry_opened(symbol, signal.strategy_name)
        _snap_s = self._last_brain_snapshot or {}
        _bs_raw_s = _snap_s.get("last_signal_score")
        _brain_fs = float(_bs_raw_s) if _bs_raw_s is not None else None
        _wcs, _wls = effective_entry_win_chance_pct(
            signal.confidence,
            brain_score=_brain_fs,
            rr=signal.rr,
            strategy_name=signal.strategy_name,
            perf_tracker=self.perf_tracker,
        )
        logger.info(
            f"[bold red]SHORT ERÖFFNET [PAPER][/bold red] {symbol} | "
            f"Strategie: {signal.strategy_name} | "
            f"Einstieg: {signal.entry:.4f} | Fill: {exec_result.fill_price:.4f} | "
            f"Menge: {amount:.6f} | SL: {signal.stop_loss:.4f} (oben) | "
            f"TP: {signal.take_profit:.4f} (unten) | "
            f"RR: {signal.rr:.2f} | Konfidenz: {signal.confidence:.0f}/100 | "
            f"Gewinnchance(effektiv): {_wcs:.0f}% ({_wls}) | "
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
            signal_score=float((self._last_brain_snapshot or {}).get("last_signal_score", 0.0)),
            risk_state_at_entry=self._risk_state_at_entry_snapshot(),
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
            brain_score=_brain_fs,
        )
        self._record_trade_event(
            event="opened",
            symbol=symbol,
            side="short",
            strategy=signal.strategy_name,
            pnl=None,
            reason=signal.reason,
        )
        self._record_last_decision(
            symbol=symbol,
            decision="entry_opened",
            reason=signal.reason,
            strategy=signal.strategy_name,
        )
        self._log_decision_cycle(
            symbol=symbol,
            regime=signal.regime or "UNKNOWN",
            ranking=list((self._last_brain_snapshot or {}).get("last_strategy_ranking") or []),
            chosen_strategy=signal.strategy_name,
            signal_score=float((self._last_brain_snapshot or {}).get("last_signal_score", 0.0) or 0.0),
            risk_decision="allow_trade",
            allow_trade=True,
            reject_reason="",
            last_decision_reason=signal.reason,
            market_context={},
        )

    def run_cycle(self):
        """Führt einen vollständigen Analyse-Zyklus für alle konfigurierten Paare durch."""
        logger.info("[dim]── Multi-Strategy Zyklus gestartet ──[/dim]")
        self._reevaluate_startup_gate()
        if not self._startup_checks_ok:
            reason = self._startup_block_reason or "startup_checks_failed"
            logger.error(
                f"[red]STARTUP-GATE AKTIV[/red] – Zyklus übersprungen | Grund: {reason}"
            )
            runtime_state.set_last_decision(
                {
                    "symbol": "SYSTEM",
                    "decision": "startup_blocked",
                    "reason": reason,
                    "strategy": self._active_strategy_runtime,
                }
            )
            return

        # Heartbeat aktualisieren (Health Monitor Liveness-Tracking)
        self.health.update_heartbeat()

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
            runtime_state.set_last_decision(
                {
                    "symbol": "SYSTEM",
                    "decision": "cycle_skipped",
                    "reason": reason,
                    "strategy": self._active_strategy_runtime,
                }
            )
            return

        # Scorer zu Beginn jedes Zyklus aktualisieren (liest neue Trades aus DB)
        try:
            self.scorer.refresh()
        except Exception as e:
            logger.warning(f"Scorer-Refresh fehlgeschlagen (nicht kritisch): {e}")

        self._apply_entry_cadence_guard()

        cycle_symbols = list(
            dict.fromkeys(list(self.pairs) + list(self.risk.open_positions.keys()))
        )
        extra_symbols = [s for s in self.risk.open_positions.keys() if s not in self.pairs]
        if extra_symbols:
            logger.info(
                "Exit-Überwachung erweitert um offene Symbole außerhalb Pair-Liste: %s",
                ", ".join(extra_symbols[:20]),
            )
        for symbol in cycle_symbols:
            try:
                self._process_pair(symbol)
            except Exception as e:
                logger.error(f"Unerwarteter Fehler bei {symbol}: {e}")
                self.health.record_error("error", f"{symbol}: {e}")
                self.tg.notify_error(f"MultiStrategyBot:{symbol}", str(e))

        self._run_forced_close_queue()

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
        self._update_performance_tracking()
        self._sync_runtime_state()
        self._persist_recovery_state()

        self._run_master_watchdog()
        self._maybe_override_master_autopause_for_cadence()
        ctx = self._build_reflection_context()
        self._self_reflection.observe(ctx)
        self._apply_self_reflection_repairs()

        # Health-Monitor auswerten (Watchdog-Reaktionen, Snapshots)
        self.health.check_and_react()

    def run(self, interval_seconds: int = None):
        """Startet den Multi-Strategy-Bot in einer Dauerschleife."""
        tf_map = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
        wait = interval_seconds or tf_map.get(settings.TIMEFRAME, 3600)

        self.running = True
        self._sync_runtime_state()
        logger.info(f"[bold]Multi-Bot läuft. Interval: {wait}s ({settings.TIMEFRAME})[/bold]")
        logger.info("Drücke [bold]Ctrl+C[/bold] zum Beenden.\n")

        try:
            while self.running:
                self.run_cycle()
                logger.info(f"Nächster Zyklus in {wait} Sekunden...\n")
                time.sleep(wait)
        except KeyboardInterrupt:
            self.stop()

    def _runtime_status(self) -> Dict:
        stats = self.risk.get_stats()
        health_status = "n/a"
        try:
            health_status = self.health.status.value
        except Exception:
            pass
        gate_status = {}
        try:
            gate_status = self.risk.get_gate_status()
        except Exception:
            gate_status = {}
        gate_status["recovery_startup_ok"] = bool(self._startup_checks_ok)
        gate_status["recovery_startup_reason"] = self._startup_block_reason
        gate_status["recovery_blocked_symbols"] = sorted(self._recovery_blocked_symbols)
        snap = runtime_state.snapshot()
        return {
            "running": self.running,
            "engine": "connected",
            "balance": stats.get("balance"),
            "equity": snap.get("equity", stats.get("balance")),
            "available_capital": snap.get("available_capital", stats.get("balance")),
            "total_trades": stats.get("total_trades"),
            "open_positions": stats.get("open_positions"),
            "open_positions_detail": snap.get("open_positions", []),
            "recent_trades": snap.get("recent_trades", []),
            "recent_logs": snap.get("recent_logs", []),
            "active_strategy": self._active_strategy_runtime,
            "enabled_strategies": snap.get("enabled_strategies", [s.name for s in self.strategies]),
            "last_signal": snap.get("last_signal", {}),
            "last_decision": snap.get("last_decision", {}),
            "health_status": health_status,
            "daily_loss": stats.get("daily_loss", 0.0),
            "portfolio_risk_pct": stats.get("portfolio_risk_pct", 0.0),
            "selector": self._last_selector_snapshot,
            "risk_gate": gate_status,
            "brain": self._last_brain_snapshot,
            "app_context": {
                **(snap.get("app_context", {}) or {}),
                "self_reflection": self._self_reflection.latest_insight(),
            },
            "performance": snap.get("performance", {}),
        }

    def _build_open_positions_snapshot(self) -> List[Dict]:
        rows: List[Dict] = []
        for sym, pos in self.risk.open_positions.items():
            rows.append(
                {
                    "symbol": sym,
                    "side": getattr(pos, "side", "long"),
                    "strategy": getattr(pos, "strategy_name", self._active_strategy_runtime),
                    "entry_price": getattr(pos, "entry_price", 0.0),
                    "stop_loss": getattr(pos, "stop_loss", 0.0),
                    "take_profit": getattr(pos, "take_profit", 0.0),
                    "amount": getattr(pos, "amount", 0.0),
                }
            )
        return rows

    def _sync_runtime_state(self) -> None:
        stats = self.risk.get_stats()
        ctrl = runtime_control.get_snapshot()
        health_status = "n/a"
        try:
            health_status = self.health.status.value
        except Exception:
            pass
        equity = self._calculate_equity()
        runtime_state.update_engine(
            running=self.running,
            mode=settings.TRADING_MODE,
            paused=ctrl.get("paused", False),
            risk_off=ctrl.get("risk_off", False),
            active_strategy=self._active_strategy_runtime,
            enabled_strategies=[s.name for s in self.strategies],
            balance=stats.get("balance", 0.0),
            equity=equity,
            available_capital=stats.get("balance", 0.0),
            health_status=health_status,
            total_trades=stats.get("total_trades", 0),
            open_positions=self._build_open_positions_snapshot(),
        )
        runtime_state.update_brain(self._last_brain_snapshot)
        self._persist_recovery_state()

    def _calculate_equity(self) -> float:
        base_balance = float(self.risk.balance)
        for sym, pos in self.risk.open_positions.items():
            mark = self._last_prices.get(sym, pos.entry_price)
            reserved = pos.entry_price * pos.amount
            if getattr(pos, "side", "long") == "short":
                unrealized = (pos.entry_price - mark) * pos.amount
            else:
                unrealized = (mark - pos.entry_price) * pos.amount
            base_balance += reserved + unrealized
        return round(base_balance, 4)

    def _record_trade_event(
        self,
        *,
        event: str,
        symbol: str,
        side: str,
        strategy: str,
        pnl: Optional[float],
        reason: str,
    ) -> None:
        runtime_state.append_trade(
            {
                "event": event,
                "symbol": symbol,
                "side": side,
                "strategy": strategy,
                "pnl": pnl,
                "reason": reason,
            }
        )
        runtime_state.append_log(
            f"{event.upper()} {symbol} [{side}] {strategy} "
            f"{f'PnL={pnl:+.4f}' if pnl is not None else ''} {reason}".strip()
        )

    def _record_last_signal(
        self,
        *,
        symbol: str,
        strategy: str,
        side: str,
        confidence: float,
        reason: str,
        entry: float,
        timeframe: str,
        rr: float,
        regime: str,
    ) -> None:
        runtime_state.set_last_signal(
            {
                "symbol": symbol,
                "strategy": strategy,
                "side": side,
                "confidence": confidence,
                "reason": reason,
                "entry": entry,
                "timeframe": timeframe,
                "rr": rr,
                "regime": regime,
            }
        )

    def _record_last_decision(
        self,
        *,
        symbol: str,
        decision: str,
        reason: str,
        strategy: Optional[str] = None,
    ) -> None:
        runtime_state.set_last_decision(
            {
                "symbol": symbol,
                "decision": decision,
                "reason": reason,
                "strategy": strategy or self._active_strategy_runtime,
            }
        )

    def _request_start_from_panel(self) -> Tuple[bool, str]:
        """
        Telegram-Start im bestehenden Prozess:
        - Wenn Loop läuft: Runtime-Sperren lösen.
        - Kein Cold-Start aus Telegram-Thread.
        """
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        runtime_state.update_engine(paused=False, risk_off=False)
        if self.running:
            return True, "Multi-Bot läuft bereits. Entry-Pause/Risk-Off wurden aufgehoben."
        return False, (
            "Multi-Bot läuft aktuell nicht. Bitte Bot-Prozess lokal starten "
            "(Telegram kann keinen sicheren Cold-Start auslösen)."
        )

    def _request_bot_restart_from_panel(self) -> Tuple[bool, str]:
        runtime_control.pause_entries()
        runtime_control.enable_risk_off()
        runtime_state.update_engine(paused=True, risk_off=True)
        runtime_state.append_log("TELEGRAM /botrestart -> safe_restart_marked")
        self._sync_runtime_state()
        return (
            True,
            "Safe-Restart markiert: Entries pausiert + Risk-Off aktiv. "
            "Bitte Service per systemd neu starten.",
        )

    def _apply_runtime_settings_from_panel(self, updates: Dict[str, float]) -> Tuple[bool, str]:
        if not isinstance(updates, dict) or not updates:
            return False, "Keine Runtime-Updates übergeben."
        changed: List[str] = []
        try:
            for key, value in updates.items():
                k = str(key).strip().lower()
                if k == "max_positions_total":
                    v = int(value)
                    v = max(1, min(50, v))
                    settings.MAX_POSITIONS_TOTAL = v
                    settings.MAX_OPEN_TRADES = v
                    self.risk.max_open_trades = v
                    if hasattr(self.risk, "portfolio") and getattr(self.risk, "portfolio", None):
                        self.risk.portfolio.cfg.max_positions_total = v
                    changed.append(f"MAX_POSITIONS_TOTAL={v}")
                elif k == "daily_loss_limit_pct":
                    v = float(value)
                    v = max(0.1, min(100.0, v))
                    settings.DAILY_LOSS_LIMIT_PCT = v
                    changed.append(f"DAILY_LOSS_LIMIT_PCT={v:.2f}")
                elif k == "coin_cooldown_minutes":
                    v = int(value)
                    v = max(0, min(240, v))
                    settings.COIN_COOLDOWN_MINUTES = v
                    changed.append(f"COIN_COOLDOWN_MINUTES={v}")
                elif k == "strategy_cooldown_minutes":
                    v = int(value)
                    v = max(0, min(240, v))
                    settings.STRATEGY_COOLDOWN_MINUTES = v
                    changed.append(f"STRATEGY_COOLDOWN_MINUTES={v}")
                elif k == "duplicate_signal_minutes":
                    v = int(value)
                    v = max(0, min(240, v))
                    settings.DUPLICATE_SIGNAL_MINUTES = v
                    changed.append(f"DUPLICATE_SIGNAL_MINUTES={v}")
                elif k == "brain_min_score_to_trade":
                    v = float(value)
                    v = max(0.05, min(1.0, v))
                    settings.BRAIN_MIN_SCORE_TO_TRADE = v
                    changed.append(f"BRAIN_MIN_SCORE_TO_TRADE={v:.3f}")
                elif k == "brain_risky_phase_score":
                    v = float(value)
                    v = max(0.05, min(1.0, v))
                    settings.BRAIN_RISKY_PHASE_SCORE = v
                    changed.append(f"BRAIN_RISKY_PHASE_SCORE={v:.3f}")
                elif k == "perf_selector_weight":
                    v = float(value)
                    v = max(0.0, min(1.0, v))
                    settings.PERF_SELECTOR_WEIGHT = v
                    changed.append(f"PERF_SELECTOR_WEIGHT={v:.3f}")
                elif k == "brain_reward_weight":
                    v = float(value)
                    v = max(0.0, min(1.0, v))
                    settings.BRAIN_REWARD_WEIGHT = v
                    changed.append(f"BRAIN_REWARD_WEIGHT={v:.3f}")
                elif k == "brain_reward_window":
                    v = int(value)
                    v = max(2, min(80, v))
                    settings.BRAIN_REWARD_WINDOW = v
                    changed.append(f"BRAIN_REWARD_WINDOW={v}")
                elif k == "brain_bitter_treat_block_threshold":
                    v = float(value)
                    v = max(-1.0, min(0.0, v))
                    settings.BRAIN_BITTER_TREAT_BLOCK_THRESHOLD = v
                    changed.append(f"BRAIN_BITTER_TREAT_BLOCK_THRESHOLD={v:.3f}")
                elif k == "mtf_king_enabled":
                    v = bool(value)
                    settings.MTF_KING_ENABLED = v
                    changed.append(f"MTF_KING_ENABLED={v}")
                elif k == "mtf_entry_timeframe":
                    v = str(value).strip().lower()
                    if not v:
                        return False, "MTF_ENTRY_TIMEFRAME darf nicht leer sein."
                    settings.MTF_ENTRY_TIMEFRAME = v
                    changed.append(f"MTF_ENTRY_TIMEFRAME={v}")
                elif k == "mtf_micro_timeframe":
                    v = str(value).strip().lower()
                    if not v:
                        return False, "MTF_MICRO_TIMEFRAME darf nicht leer sein."
                    settings.MTF_MICRO_TIMEFRAME = v
                    changed.append(f"MTF_MICRO_TIMEFRAME={v}")
                elif k == "mtf_setup_timeframe":
                    v = str(value).strip().lower()
                    if not v:
                        return False, "MTF_SETUP_TIMEFRAME darf nicht leer sein."
                    settings.MTF_SETUP_TIMEFRAME = v
                    changed.append(f"MTF_SETUP_TIMEFRAME={v}")
                elif k == "mtf_direction_timeframe":
                    v = str(value).strip().lower()
                    if not v:
                        return False, "MTF_DIRECTION_TIMEFRAME darf nicht leer sein."
                    settings.MTF_DIRECTION_TIMEFRAME = v
                    changed.append(f"MTF_DIRECTION_TIMEFRAME={v}")
                elif k == "mtf_context_timeframe":
                    v = str(value).strip().lower()
                    if not v:
                        return False, "MTF_CONTEXT_TIMEFRAME darf nicht leer sein."
                    settings.MTF_CONTEXT_TIMEFRAME = v
                    changed.append(f"MTF_CONTEXT_TIMEFRAME={v}")
                elif k == "mtf_direction_strong_threshold":
                    v = float(value)
                    v = max(0.0, min(2.0, v))
                    settings.MTF_DIRECTION_STRONG_THRESHOLD = v
                    changed.append(f"MTF_DIRECTION_STRONG_THRESHOLD={v:.3f}")
                elif k == "mtf_min_support_ratio":
                    v = float(value)
                    v = max(0.0, min(1.0, v))
                    settings.MTF_MIN_SUPPORT_RATIO = v
                    changed.append(f"MTF_MIN_SUPPORT_RATIO={v:.3f}")
                elif k == "master_brain_enabled":
                    v = bool(value)
                    settings.MASTER_BRAIN_ENABLED = v
                    changed.append(f"MASTER_BRAIN_ENABLED={v}")
                elif k == "master_brain_min_trades":
                    v = int(value)
                    v = max(5, min(500, v))
                    settings.MASTER_BRAIN_MIN_TRADES = v
                    changed.append(f"MASTER_BRAIN_MIN_TRADES={v}")
                elif k == "master_brain_target_winrate_pct":
                    v = float(value)
                    v = max(1.0, min(99.0, v))
                    settings.MASTER_BRAIN_TARGET_WINRATE_PCT = v
                    changed.append(f"MASTER_BRAIN_TARGET_WINRATE_PCT={v:.2f}")
                elif k == "master_brain_fail_windows":
                    v = int(value)
                    v = max(1, min(20, v))
                    settings.MASTER_BRAIN_FAIL_WINDOWS = v
                    changed.append(f"MASTER_BRAIN_FAIL_WINDOWS={v}")
                elif k == "master_brain_auto_pause":
                    v = bool(value)
                    settings.MASTER_BRAIN_AUTO_PAUSE = v
                    changed.append(f"MASTER_BRAIN_AUTO_PAUSE={v}")
                else:
                    return False, f"Unbekannter Runtime-Key: {k}"
            if not changed:
                return False, "Keine gültigen Runtime-Keys angewendet."
            runtime_state.append_log("TELEGRAM runtime_update: " + ", ".join(changed))
            self._sync_runtime_state()
            return True, "Runtime-Parameter aktualisiert: " + ", ".join(changed)
        except Exception as e:
            return False, f"Runtime-Update fehlgeschlagen: {e}"

    def _save_master_snapshot(self, reason: str) -> Tuple[bool, str]:
        try:
            snap = runtime_state.snapshot()
            perf = snap.get("performance") or {}
            day = perf.get("daily_summary")
            if self.perf_repo.available and not day:
                day = self.perf_repo.update_daily_summary(mode=settings.TRADING_MODE) or {}
            payload = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "reason": reason,
                "mode": settings.TRADING_MODE,
                "risk_gate": self.risk.get_gate_status(),
                "runtime": {
                    "running": snap.get("running"),
                    "paused": snap.get("paused"),
                    "risk_off": snap.get("risk_off"),
                    "open_positions": snap.get("open_positions"),
                    "last_signal": snap.get("last_signal"),
                    "last_decision": snap.get("last_decision"),
                },
                "brain": self._last_brain_snapshot,
                "daily_summary": day or {},
            }
            out_dir = Path(getattr(settings, "MASTER_SNAPSHOT_DIR", "data/master_snapshots"))
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"master_snapshot_{int(time.time())}.json"
            out_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
            self._master_last_snapshot_file = str(out_path)
            runtime_state.update_app_context(
                {
                    "master_snapshot_file": str(out_path),
                    "master_snapshot_reason": reason,
                }
            )
            return True, str(out_path)
        except Exception as e:
            return False, f"Snapshot fehlgeschlagen: {e}"

    def _request_auto_heal(self) -> Tuple[bool, str]:
        details: List[str] = []
        try:
            self._reevaluate_startup_gate()
            if self._startup_checks_ok:
                details.append("startup_gate_ok")
            else:
                details.append("startup_gate_blocked")

            stale_symbols: List[str] = []
            try:
                hs = self.health.get_snapshot() if self.health else {}
                ages = hs.get("data_ages_sec") or {}
                stale_timeout = float(getattr(settings, "DATA_STALE_TIMEOUT_SEC", 600))
                stale_symbols = [
                    sym for sym, age in ages.items() if float(age or 0.0) > stale_timeout
                ]
            except Exception:
                stale_symbols = []

            if stale_symbols:
                runtime_control.pause_entries()
                runtime_control.enable_risk_off()
                details.append(f"stale_data_pause:{len(stale_symbols)}")
            else:
                if len(self.risk.open_positions) < int(self.risk.max_open_trades):
                    runtime_control.resume_entries()
                    runtime_control.disable_risk_off()
                    details.append("entries_reenabled")
                else:
                    details.append(
                        f"entries_hold_open_positions:{len(self.risk.open_positions)}/{self.risk.max_open_trades}"
                    )

            snap_ok, snap_msg = self._save_master_snapshot("manual_autoheal")
            details.append("snapshot_saved" if snap_ok else f"snapshot_error:{snap_msg}")
            self._sync_runtime_state()
            return True, " | ".join(details)
        except Exception as e:
            return False, f"Autoheal fehlgeschlagen: {e}"

    def _get_market_status_from_panel(self) -> Dict:
        stale_symbols: List[str] = []
        try:
            hs = self.health.get_snapshot() if self.health else {}
            ages = hs.get("data_ages_sec") or {}
            stale_timeout = float(getattr(settings, "DATA_STALE_TIMEOUT_SEC", 600))
            stale_symbols = [
                sym for sym, age in ages.items() if float(age or 0.0) > stale_timeout
            ]
        except Exception:
            stale_symbols = []
        return {
            "pair_count": len(self.pairs),
            "pairs": list(self.pairs),
            "open_positions": len(self.risk.open_positions),
            "stale_symbols": stale_symbols,
        }

    def _get_master_status_from_panel(self) -> Dict:
        cadence = dict(self._entry_cadence_status or {})
        override_until = "n/a"
        if float(self._master_cadence_override_until_ts) > time.time():
            try:
                override_until = datetime.fromtimestamp(
                    float(self._master_cadence_override_until_ts), tz=timezone.utc
                ).strftime("%H:%M:%S UTC")
            except Exception:
                override_until = "active"
        return {
            "enabled": bool(getattr(settings, "MASTER_BRAIN_ENABLED", True)),
            "min_trades": int(getattr(settings, "MASTER_BRAIN_MIN_TRADES", 20)),
            "target_winrate_pct": float(getattr(settings, "MASTER_BRAIN_TARGET_WINRATE_PCT", 70.0)),
            "last_winrate_pct": float(self._master_last_winrate_pct),
            "consecutive_fail_windows": int(self._master_fail_windows),
            "auto_paused": bool(self._master_auto_paused),
            "last_reason": self._master_last_reason,
            "last_snapshot_file": self._master_last_snapshot_file,
            "cadence_level": int(cadence.get("level", 0) or 0),
            "entries_today": int(cadence.get("entries_today", 0) or 0),
            "target_trades_per_day": int(
                cadence.get(
                    "target_trades_per_day",
                    int(getattr(settings, "ENTRY_CADENCE_TARGET_TRADES_PER_DAY", 8)),
                )
                or 0
            ),
            "cadence_override_until": override_until,
        }

    def _run_master_watchdog(self) -> None:
        if not bool(getattr(settings, "MASTER_BRAIN_ENABLED", True)):
            return
        if self._master_cadence_override_until_ts and time.time() >= float(
            self._master_cadence_override_until_ts
        ):
            self._master_cadence_override_until_ts = 0.0
        closed = self.repo.get_recent_trades(
            limit=int(getattr(settings, "MASTER_BRAIN_MIN_TRADES", 20)),
            status="closed",
            current_mode_only=True,
        )
        min_trades = int(getattr(settings, "MASTER_BRAIN_MIN_TRADES", 20))
        if len(closed) < min_trades:
            self._master_last_reason = f"insufficient_closed_trades:{len(closed)}/{min_trades}"
            return
        wins = 0
        for row in closed:
            try:
                if float(row.get("pnl_abs") or 0.0) > 0:
                    wins += 1
            except Exception:
                pass
        winrate = (wins / len(closed) * 100.0) if closed else 0.0
        self._master_last_winrate_pct = round(winrate, 2)
        target = float(getattr(settings, "MASTER_BRAIN_TARGET_WINRATE_PCT", 70.0))
        if winrate >= target:
            self._master_fail_windows = 0
            if self._master_auto_paused:
                runtime_control.resume_entries()
                runtime_control.disable_risk_off()
                self._master_auto_paused = False
                self._master_last_reason = f"recovered_winrate:{winrate:.2f}%"
                runtime_state.append_log(f"MASTER recovered winrate={winrate:.2f}%")
                self.tg.send(
                    f"🟢 <b>MASTER AUTOHEAL</b>\nWinrate wieder stabil: {winrate:.2f}% (Target {target:.2f}%).\nEntries wurden freigegeben."
                )
            else:
                self._master_last_reason = f"healthy_winrate:{winrate:.2f}%"
            return

        self._master_fail_windows += 1
        fail_threshold = int(getattr(settings, "MASTER_BRAIN_FAIL_WINDOWS", 2))
        self._master_last_reason = (
            f"under_target_winrate:{winrate:.2f}%<{target:.2f}% "
            f"(window_fail={self._master_fail_windows}/{fail_threshold})"
        )
        if self._master_fail_windows < fail_threshold:
            return

        if time.time() < float(self._master_cadence_override_until_ts or 0.0):
            self._master_last_reason += " | cadence_override_active"
            return

        if bool(getattr(settings, "MASTER_BRAIN_AUTO_PAUSE", True)):
            runtime_control.pause_entries()
            runtime_control.enable_risk_off()
            self._master_auto_paused = True
        snap_ok, snap_msg = self._save_master_snapshot("auto_watchdog_under_target_winrate")
        if snap_ok:
            self._master_last_snapshot_file = snap_msg
        self._master_last_reason += f" | snapshot={snap_msg if snap_ok else 'failed'}"
        runtime_state.append_log(
            f"MASTER auto_pause winrate={winrate:.2f}% target={target:.2f}%"
        )
        self.tg.send(
            "🛑 <b>MASTER AUTOHEAL</b>\n"
            f"Winrate unter Ziel: {winrate:.2f}% &lt; {target:.2f}%.\n"
            "Entries pausiert + Risk-Off aktiviert.\n"
            f"Snapshot: {snap_msg if snap_ok else 'fehlgeschlagen'}"
        )

    def stop(self):
        self.running = False
        if self.panel:
            self.panel.stop()

        # Offene Trades bleiben bewusst erhalten für sicheren Restart-Recovery.
        for symbol, trade_id in list(self._open_trade_ids.items()):
            logger.info(
                "Stop ohne Auto-Cancel: offener Trade bleibt für Recovery erhalten | %s -> trade_id=%s",
                symbol,
                trade_id,
            )
        self._open_trade_ids.clear()

        stats = self.risk.get_stats()
        self._sync_runtime_state()
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
