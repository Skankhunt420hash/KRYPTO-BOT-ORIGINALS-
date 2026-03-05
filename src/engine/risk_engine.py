from datetime import datetime, date
from typing import Dict, Optional, Tuple

from config.settings import settings
from src.utils.risk_manager import RiskManager, Position
from src.strategies.signal import EnhancedSignal, Side
from src.engine.portfolio_risk import PortfolioRiskEngine, build_config_from_settings
from src.utils.logger import setup_logger

logger = setup_logger("risk_engine")


class RiskEngine(RiskManager):
    """
    Erweitert RiskManager um:
    - Coin-Cooldown nach Trade-Schließung (verhindert sofortiges Wiedereinsteigen)
    - Strategy-Cooldown nach Verlust-Trade
    - Daily Loss Limit (stoppt Trading wenn Tagesverlust-Grenze erreicht)
    - Duplicate-Signal-Schutz (gleiche Strategie + Symbol innerhalb N Minuten)
    - open_with_signal(): eröffnet Position mit SL/TP direkt aus EnhancedSignal

    Alle Methoden des RiskManager bleiben unverändert verfügbar.
    """

    def __init__(self, initial_balance: float = None):
        super().__init__(initial_balance)
        self._initial_balance: float = self.balance

        self._coin_cooldown: Dict[str, datetime] = {}
        self._strategy_cooldown: Dict[str, datetime] = {}
        self._recent_signals: Dict[str, datetime] = {}

        self._daily_loss: float = 0.0
        self._daily_loss_date: date = date.today()

        # Portfolio Risk Engine (Position Sizing + Exposure-Limits)
        self.portfolio = PortfolioRiskEngine(build_config_from_settings())

    # ------------------------------------------------------------------
    # Hilfsmethoden
    # ------------------------------------------------------------------

    def _reset_daily_loss_if_new_day(self):
        today = date.today()
        if self._daily_loss_date != today:
            self._daily_loss = 0.0
            self._daily_loss_date = today
            logger.info("Daily Loss Counter zurückgesetzt (neuer Tag).")

    # ------------------------------------------------------------------
    # Signal-Prüfung (läuft VOR jeder Order)
    # ------------------------------------------------------------------

    def check_signal(self, signal: EnhancedSignal) -> Tuple[bool, str]:
        """
        Prüft ob ein Signal ausgeführt werden darf.
        Returns: (erlaubt: bool, grund: str)
        """
        self._reset_daily_loss_if_new_day()
        now = datetime.utcnow()

        # 1. Daily Loss Limit
        daily_limit = self._initial_balance * (settings.DAILY_LOSS_LIMIT_PCT / 100)
        if abs(self._daily_loss) >= daily_limit:
            return False, (
                f"DAILY LOSS LIMIT: Tagesverlust {abs(self._daily_loss):.2f} USDT "
                f">= Limit {daily_limit:.2f} USDT – Trading pausiert"
            )

        # 2. Coin-Cooldown
        coin_cd = self._coin_cooldown.get(signal.symbol)
        if coin_cd:
            elapsed_min = (now - coin_cd).total_seconds() / 60
            if elapsed_min < settings.COIN_COOLDOWN_MINUTES:
                remaining = settings.COIN_COOLDOWN_MINUTES - elapsed_min
                return False, (
                    f"COIN COOLDOWN: {signal.symbol} noch {remaining:.0f}min gesperrt"
                )

        # 3. Strategy-Cooldown
        strat_cd = self._strategy_cooldown.get(signal.strategy_name)
        if strat_cd:
            elapsed_min = (now - strat_cd).total_seconds() / 60
            if elapsed_min < settings.STRATEGY_COOLDOWN_MINUTES:
                remaining = settings.STRATEGY_COOLDOWN_MINUTES - elapsed_min
                return False, (
                    f"STRATEGY COOLDOWN: {signal.strategy_name} "
                    f"noch {remaining:.0f}min gesperrt"
                )

        # 4. Duplicate-Signal-Schutz (richtungsbewusst: long/short unabhängig)
        dup_key = f"{signal.strategy_name}_{signal.symbol}_{signal.side.value}"
        dup_ts = self._recent_signals.get(dup_key)
        if dup_ts:
            elapsed_min = (now - dup_ts).total_seconds() / 60
            if elapsed_min < settings.DUPLICATE_SIGNAL_MINUTES:
                return False, (
                    f"DUPLICATE SIGNAL: {dup_key} bereits vor "
                    f"{elapsed_min:.0f}min gesehen"
                )

        # 5. Position bereits offen / Max-Trades
        if signal.symbol in self.open_positions:
            return False, f"Position für {signal.symbol} bereits offen"
        if len(self.open_positions) >= self.max_open_trades:
            return False, (
                f"MAX TRADES: {len(self.open_positions)}/{self.max_open_trades} "
                f"Positionen offen"
            )

        return True, "OK"

    def register_signal(self, signal: EnhancedSignal):
        """Registriert ein Signal zur Duplikats-Erkennung (richtungsbewusst)."""
        dup_key = f"{signal.strategy_name}_{signal.symbol}_{signal.side.value}"
        self._recent_signals[dup_key] = datetime.utcnow()

    # ------------------------------------------------------------------
    # Position eröffnen mit Signal-Levels (SL/TP aus EnhancedSignal)
    # ------------------------------------------------------------------

    def open_with_signal(
        self, signal: EnhancedSignal, amount: float
    ) -> Optional[Position]:
        """Eröffnet eine Position mit SL/TP direkt aus dem EnhancedSignal."""
        if signal.symbol in self.open_positions:
            return None

        position = Position(
            symbol=signal.symbol,
            entry_price=signal.entry,
            amount=amount,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            side=signal.side.value,          # "long" oder "short" – PFLICHT für korrekte PnL/SL/TP
            highest_price=signal.entry,
            strategy_name=signal.strategy_name,
        )
        self.open_positions[signal.symbol] = position
        self.balance -= signal.entry * amount

        side_label = "[LONG]" if position.side == "long" else "[SHORT]"
        logger.info(
            f"[green]Position eröffnet {side_label}:[/green] {signal.symbol} | "
            f"Strategie: {signal.strategy_name} | "
            f"Einstieg: {signal.entry:.4f} | Menge: {amount:.6f} | "
            f"SL: {signal.stop_loss:.4f} | TP: {signal.take_profit:.4f} | "
            f"RR: {signal.rr:.2f} | Grund: {signal.reason}"
        )
        return position

    # ------------------------------------------------------------------
    # Position schließen – erweitert um Cooldown-Logik
    # ------------------------------------------------------------------

    def close_position(
        self, symbol: str, current_price: float
    ) -> Optional[float]:
        """Schließt Position und setzt Cooldowns basierend auf PnL."""
        position = self.open_positions.get(symbol)
        strategy_name = position.strategy_name if position else ""

        pnl = super().close_position(symbol, current_price)

        if pnl is not None:
            self._on_position_closed(symbol, pnl, strategy_name)

        return pnl

    def _on_position_closed(self, symbol: str, pnl: float, strategy_name: str):
        """Setzt Cooldowns und aktualisiert Daily Loss nach Trade-Schließung."""
        now = datetime.utcnow()

        self._coin_cooldown[symbol] = now

        if pnl < 0:
            self._daily_loss += pnl  # pnl ist negativ
            if strategy_name:
                self._strategy_cooldown[strategy_name] = now
                logger.warning(
                    f"[yellow]STRATEGY COOLDOWN gesetzt:[/yellow] "
                    f"{strategy_name} für {settings.STRATEGY_COOLDOWN_MINUTES}min "
                    f"gesperrt nach Verlust ({pnl:.4f} USDT)"
                )

        logger.debug(
            f"Coin-Cooldown: {symbol} für {settings.COIN_COOLDOWN_MINUTES}min | "
            f"Daily Loss heute: {abs(self._daily_loss):.2f} USDT"
        )

    # ------------------------------------------------------------------
    # Portfolio Risk: Sizing + Limits (kombiniert, für bot.py)
    # ------------------------------------------------------------------

    def check_and_size(
        self, signal: EnhancedSignal
    ) -> Tuple[bool, str, float]:
        """
        Kombiniert Positionsgröße-Berechnung und Portfolio-Limit-Prüfung.

        Ablauf:
          1. Positionsgröße berechnen (sizing_mode aus Settings)
          2. Portfolio-Limits prüfen (Exposure, Cluster, Richtung, ...)

        Returns:
          (allowed: bool, reason: str, amount: float)
          allowed=False + amount=0.0 → Trade soll blockiert werden
          allowed=True  + amount>0.0 → Trade kann mit dieser Menge ausgeführt werden

        Fehler im Portfolio-Check crashen den Main-Loop NICHT (try/except).
        Bei unerwartetem Fehler: Fallback auf alten einfachen Sizing-Modus.
        """
        try:
            # 1. Positionsgröße berechnen
            amount, sizing_info = self.portfolio.calculate_size(signal, self.balance)

            if amount <= 0:
                return False, f"SIZING BLOCKIERT: {sizing_info}", 0.0

            # 2. Portfolio-Limits prüfen (mit berechneter Menge)
            pf_allowed, pf_reason = self.portfolio.check_portfolio_limits(
                signal, self.balance, self.open_positions, amount
            )
            if not pf_allowed:
                return False, pf_reason, 0.0

            # Erfolg: Risiko-Kennzahlen für Logging berechnen
            risk_usd = abs(signal.entry - signal.stop_loss) * amount
            risk_pct = (risk_usd / self.balance * 100) if self.balance > 0 else 0.0
            notional = amount * signal.entry
            logger.info(
                f"[cyan]SIZING[/cyan] {signal.symbol} | "
                f"Modus: {self.portfolio.cfg.sizing_mode} | "
                f"{sizing_info} | "
                f"Notional: {notional:.2f} USDT | "
                f"Risiko: {risk_pct:.2f}% ({risk_usd:.2f} USDT)"
            )
            return True, "OK", amount

        except Exception as e:
            logger.error(f"check_and_size Fehler für {signal.symbol}: {e}")
            # Sicherer Fallback: ursprüngliches einfaches Sizing
            fallback_amount = self.calculate_position_size(signal.entry)
            logger.warning(
                f"Fallback auf einfaches Sizing: {fallback_amount:.6f} "
                f"(Portfolio-Check übersprungen)"
            )
            return True, f"FALLBACK-SIZING (Fehler: {type(e).__name__})", fallback_amount

    # ------------------------------------------------------------------
    # Erweiterte Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        stats = super().get_stats()
        stats["daily_loss"] = round(abs(self._daily_loss), 2)
        stats["active_coin_cooldowns"] = len(self._coin_cooldown)
        stats["active_strategy_cooldowns"] = len(self._strategy_cooldown)
        # Portfolio-Exposure-Snapshot
        try:
            snapshot = self.portfolio.get_exposure_snapshot(self.open_positions, self.balance)
            stats["portfolio_risk_pct"] = snapshot["total_risk_pct"]
        except Exception:
            stats["portfolio_risk_pct"] = 0.0
        return stats
