from datetime import datetime, date, timezone
from typing import Dict, Optional, Tuple, List

from config.settings import settings
from src.utils.risk_manager import RiskManager, Position, paper_equity_ledger_enabled
from src.strategies.signal import EnhancedSignal, Side
from src.engine.portfolio_risk import PortfolioRiskEngine, build_config_from_settings
from src.engine.loss_pattern_memory import LossPatternMemory
from src.engine.runtime_control import runtime_control
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
        self._last_gate_reason: str = "init"
        self._last_gate_at: str = datetime.now(timezone.utc).isoformat()
        self._daily_loss_risk_off_latched: bool = False
        self._global_losing_streak: int = 0
        self._last_live_gate_reason: str = "n/a"
        self._last_live_gate_at: str = datetime.now(timezone.utc).isoformat()

        # Portfolio Risk Engine (Position Sizing + Exposure-Limits)
        self.portfolio = PortfolioRiskEngine(build_config_from_settings())
        self._loss_pattern_memory = LossPatternMemory()

    # ------------------------------------------------------------------
    # Paper-Konto: Equity vs. freie Kasse
    # ------------------------------------------------------------------

    def _paper_heal_legacy_empty_cash(self) -> None:
        """
        Nach Umstellung auf Equity-Konto: alte Läufe hatten Kasse 0 bei vollem Kapital in Positionen.
        Einmalige Anhebung der Balance auf Kasse + gebundenes Notional.
        """
        if not paper_equity_ledger_enabled():
            return
        if float(self.balance) > 0.01:
            return
        invested = sum(
            float(p.entry_price) * float(p.amount) for p in self.open_positions.values()
        )
        if invested < 1.0:
            return
        healed = float(self.balance) + invested
        logger.warning(
            "Paper-Equity-Konto: Balance %.2f → %.2f USDT (gebundenes Kapital aus offenen Positionen übernommen).",
            self.balance,
            healed,
        )
        self.balance = healed

    def paper_account_equity(self) -> float:
        """
        Kontokapital für Sizing / Exposure.

        - Paper + PAPER_EQUITY_ACCOUNT: `balance` ist bereits die Equity (kein Doppelzählen).
        - Sonst (Spot-Paper / Live-Fallback): Kasse + gebundenes Entry-Notional.
        """
        if paper_equity_ledger_enabled():
            return float(self.balance)
        invested = 0.0
        for p in self.open_positions.values():
            invested += float(p.entry_price) * float(p.amount)
        return float(self.balance) + invested

    # ------------------------------------------------------------------
    # Hilfsmethoden
    # ------------------------------------------------------------------

    def _reset_daily_loss_if_new_day(self):
        today = date.today()
        if self._daily_loss_date != today:
            self._daily_loss = 0.0
            self._daily_loss_date = today
            self._daily_loss_risk_off_latched = False
            logger.info("Daily Loss Counter zurückgesetzt (neuer Tag).")

    def _reject(self, reason: str) -> Tuple[bool, str]:
        self._last_gate_reason = reason
        self._last_gate_at = datetime.now(timezone.utc).isoformat()
        return False, reason

    # ------------------------------------------------------------------
    # Signal-Prüfung (läuft VOR jeder Order)
    # ------------------------------------------------------------------

    def check_signal(self, signal: EnhancedSignal) -> Tuple[bool, str]:
        """
        Prüft ob ein Signal ausgeführt werden darf.
        Returns: (erlaubt: bool, grund: str)
        """
        self._reset_daily_loss_if_new_day()
        now = datetime.now(timezone.utc)
        ctrl = runtime_control.get_snapshot()

        # 0. Runtime-Control-Layer: harte Entry-Sperren
        # /pause und /riskoff sollen neue Entries blockieren, ohne Exits zu verhindern.
        if ctrl.get("paused"):
            return self._reject("CONTROL PAUSE: Neue Entries sind pausiert")
        if ctrl.get("risk_off"):
            return self._reject("RISK OFF: Neue Entries sind vorübergehend deaktiviert")

        blocked_lp, lp_reason = self._loss_pattern_memory.is_blocked(
            signal.strategy_name, signal.symbol
        )
        if blocked_lp:
            return self._reject(lp_reason)

        # 0b. Signal-Integrität (verhindert kaputte Orders frühzeitig)
        if signal.entry <= 0:
            return self._reject(f"INVALID SIGNAL: Entry <= 0 ({signal.entry})")
        if signal.side == Side.LONG and not (signal.stop_loss < signal.entry < signal.take_profit):
            return self._reject("INVALID SIGNAL: LONG benötigt SL < Entry < TP")
        if signal.side == Side.SHORT and not (signal.stop_loss > signal.entry > signal.take_profit):
            return self._reject("INVALID SIGNAL: SHORT benötigt SL > Entry > TP")

        # 1. Daily Loss Limit
        daily_limit = self._initial_balance * (settings.DAILY_LOSS_LIMIT_PCT / 100)
        if abs(self._daily_loss) >= daily_limit:
            if not self._daily_loss_risk_off_latched:
                self._daily_loss_risk_off_latched = True
                logger.warning(
                    "Daily-Loss-Limit erreicht -> neue Entries werden blockiert "
                    "(interner Risk-Gate-Latch aktiv)."
                )
            return self._reject(
                f"DAILY LOSS LIMIT: Tagesverlust {abs(self._daily_loss):.2f} USDT "
                f">= Limit {daily_limit:.2f} USDT – Trading pausiert"
            )

        # 2. Optionaler Volatilitäts-Stop (Regime HIGH_VOLATILITY)
        if settings.RISK_BLOCK_HIGH_VOLATILITY:
            regime = (signal.regime or "").upper()
            if regime == "HIGH_VOLATILITY":
                return self._reject(
                    "VOLATILITY BLOCK: Regime=HIGH_VOLATILITY – "
                    "Trading in hoher Volatilität deaktiviert"
                )

        # 3. Coin-Cooldown
        coin_cd = self._coin_cooldown.get(signal.symbol)
        if coin_cd:
            elapsed_min = (now - coin_cd).total_seconds() / 60
            if elapsed_min < settings.COIN_COOLDOWN_MINUTES:
                remaining = settings.COIN_COOLDOWN_MINUTES - elapsed_min
                return self._reject(
                    f"COIN COOLDOWN: {signal.symbol} noch {remaining:.0f}min gesperrt"
                )

        # 4. Strategy-Cooldown
        strat_cd = self._strategy_cooldown.get(signal.strategy_name)
        if strat_cd:
            elapsed_min = (now - strat_cd).total_seconds() / 60
            if elapsed_min < settings.STRATEGY_COOLDOWN_MINUTES:
                remaining = settings.STRATEGY_COOLDOWN_MINUTES - elapsed_min
                return self._reject(
                    f"STRATEGY COOLDOWN: {signal.strategy_name} "
                    f"noch {remaining:.0f}min gesperrt"
                )

        # 5. Duplicate-Signal-Schutz (richtungsbewusst: long/short unabhängig)
        dup_key = f"{signal.strategy_name}_{signal.symbol}_{signal.side.value}"
        dup_ts = self._recent_signals.get(dup_key)
        if dup_ts:
            elapsed_min = (now - dup_ts).total_seconds() / 60
            if elapsed_min < settings.DUPLICATE_SIGNAL_MINUTES:
                return self._reject(
                    f"DUPLICATE SIGNAL: {dup_key} bereits vor "
                    f"{elapsed_min:.0f}min gesehen"
                )

        # 6. Position bereits offen / Max-Trades
        if signal.symbol in self.open_positions:
            return self._reject(f"Position für {signal.symbol} bereits offen")
        if len(self.open_positions) >= self.max_open_trades:
            return self._reject(
                f"MAX TRADES: {len(self.open_positions)}/{self.max_open_trades} "
                f"Positionen offen"
            )

        self._last_gate_reason = "OK"
        self._last_gate_at = datetime.now(timezone.utc).isoformat()
        return True, "OK"

    def register_signal(self, signal: EnhancedSignal):
        """Registriert ein Signal zur Duplikats-Erkennung (richtungsbewusst)."""
        dup_key = f"{signal.strategy_name}_{signal.symbol}_{signal.side.value}"
        self._recent_signals[dup_key] = datetime.now(timezone.utc)

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
        if not paper_equity_ledger_enabled():
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
        now = datetime.now(timezone.utc)

        self._coin_cooldown[symbol] = now

        if pnl < 0:
            self._daily_loss += pnl  # pnl ist negativ
            self._global_losing_streak += 1
            if strategy_name:
                self._strategy_cooldown[strategy_name] = now
                self._loss_pattern_memory.record_loss(strategy_name, symbol)
                logger.warning(
                    f"[yellow]STRATEGY COOLDOWN gesetzt:[/yellow] "
                    f"{strategy_name} für {settings.STRATEGY_COOLDOWN_MINUTES}min "
                    f"gesperrt nach Verlust ({pnl:.4f} USDT)"
                )
        else:
            self._global_losing_streak = 0

        logger.debug(
            f"Coin-Cooldown: {symbol} für {settings.COIN_COOLDOWN_MINUTES}min | "
            f"Daily Loss heute: {abs(self._daily_loss):.2f} USDT | "
            f"Global Losing Streak: {self._global_losing_streak}"
        )

    def check_live_hard_gate(
        self,
        signal: EnhancedSignal,
        amount: float,
        *,
        free_capital_usdt: float,
        account_equity_usdt: float,
        allowed_symbols: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """
        Harte Live-Schutzschicht: blockiert Entry sobald eine Regel verletzt ist.
        Paper-Modus wird hiervon bewusst nicht eingeschränkt.
        """
        if settings.TRADING_MODE != "live":
            self._last_live_gate_reason = "SKIP_NON_LIVE"
            self._last_live_gate_at = datetime.now(timezone.utc).isoformat()
            return True, "SKIP_NON_LIVE"
        if not settings.LIVE_HARD_RISK_GATE_ENABLED:
            self._last_live_gate_reason = "SKIP_GATE_DISABLED"
            self._last_live_gate_at = datetime.now(timezone.utc).isoformat()
            return True, "SKIP_GATE_DISABLED"

        self._reset_daily_loss_if_new_day()
        ctrl = runtime_control.get_snapshot()

        def _deny(reason: str) -> Tuple[bool, str]:
            self._last_live_gate_reason = reason
            self._last_live_gate_at = datetime.now(timezone.utc).isoformat()
            self._last_gate_reason = reason
            self._last_gate_at = self._last_live_gate_at
            return False, reason

        # 1) Risk-Off / Pause
        if ctrl.get("paused"):
            return _deny("LIVE HARD GATE: CONTROL PAUSE")
        if ctrl.get("risk_off"):
            return _deny("LIVE HARD GATE: RISK OFF")

        # 2) Symbol-Freigabe
        symbols = [s.strip().upper() for s in (allowed_symbols or []) if str(s).strip()]
        if symbols and signal.symbol.upper() not in symbols:
            return _deny(f"LIVE HARD GATE: SYMBOL NOT ALLOWED ({signal.symbol})")
        if settings.LIVE_TEST_MODE and not symbols:
            # Mini-Live ohne explizite Symbolfreigabe ist zu offen.
            return _deny("LIVE TEST GATE: NO SYMBOL WHITELIST CONFIGURED")

        # 2b) Strategie-Freigabe (optional, im Mini-Live empfohlen)
        allowed_strats = [
            s.strip().lower()
            for s in str(getattr(settings, "LIVE_ALLOWED_STRATEGIES", "") or "").split(",")
            if s.strip()
        ]
        if allowed_strats and str(signal.strategy_name or "").strip().lower() not in allowed_strats:
            return _deny(f"LIVE HARD GATE: STRATEGY NOT ALLOWED ({signal.strategy_name})")

        # 3) Min. Konto-/Freikapital
        if account_equity_usdt < float(settings.LIVE_MIN_ACCOUNT_EQUITY_USDT):
            return _deny(
                f"LIVE HARD GATE: EQUITY TOO LOW ({account_equity_usdt:.2f} < "
                f"{settings.LIVE_MIN_ACCOUNT_EQUITY_USDT:.2f} USDT)"
            )
        if free_capital_usdt < float(settings.LIVE_MIN_FREE_CAPITAL_USDT):
            return _deny(
                f"LIVE HARD GATE: FREE CAPITAL TOO LOW ({free_capital_usdt:.2f} < "
                f"{settings.LIVE_MIN_FREE_CAPITAL_USDT:.2f} USDT)"
            )

        # 4) Max offene Positionen
        hard_max_open = self.max_open_trades
        if settings.LIVE_TEST_MODE:
            hard_max_open = min(hard_max_open, 1)
        if len(self.open_positions) >= hard_max_open:
            return _deny(
                f"LIVE HARD GATE: MAX OPEN POSITIONS "
                f"{len(self.open_positions)}/{hard_max_open}"
            )

        # 5) Daily Loss Limit
        daily_limit_pct = float(settings.DAILY_LOSS_LIMIT_PCT)
        if settings.LIVE_TEST_MODE:
            daily_limit_pct = float(getattr(settings, "LIVE_TEST_DAILY_LOSS_LIMIT_PCT", daily_limit_pct))
        daily_limit = self._initial_balance * (daily_limit_pct / 100)
        if abs(self._daily_loss) >= daily_limit:
            return _deny(
                f"LIVE HARD GATE: DAILY LOSS LIMIT "
                f"{abs(self._daily_loss):.2f}/{daily_limit:.2f} USDT"
            )

        # 6) Verlustserie
        if self._global_losing_streak >= int(settings.LIVE_MAX_LOSING_STREAK):
            return _deny(
                f"LIVE HARD GATE: LOSING STREAK "
                f"{self._global_losing_streak}/{settings.LIVE_MAX_LOSING_STREAK}"
            )

        # 7) Risiko je Trade / Positionsgröße
        if amount <= 0:
            return _deny("LIVE HARD GATE: INVALID POSITION SIZE <= 0")
        if signal.entry <= 0:
            return _deny("LIVE HARD GATE: INVALID ENTRY <= 0")
        risk_usdt = abs(signal.entry - signal.stop_loss) * amount
        risk_pct = (risk_usdt / max(account_equity_usdt, 1e-9)) * 100.0
        if risk_pct > float(settings.RISK_PER_TRADE_PCT):
            return _deny(
                f"LIVE HARD GATE: RISK PER TRADE {risk_pct:.2f}% > "
                f"{settings.RISK_PER_TRADE_PCT:.2f}%"
            )
        notional = amount * signal.entry
        if notional > float(settings.MAX_POSITION_NOTIONAL):
            return _deny(
                f"LIVE HARD GATE: POSITION NOTIONAL {notional:.2f} > "
                f"{settings.MAX_POSITION_NOTIONAL:.2f} USDT"
            )
        if notional < float(settings.MIN_POSITION_NOTIONAL):
            return _deny(
                f"LIVE HARD GATE: POSITION NOTIONAL {notional:.2f} < "
                f"{settings.MIN_POSITION_NOTIONAL:.2f} USDT"
            )
        if settings.LIVE_TEST_MODE:
            test_cap = float(getattr(settings, "LIVE_MAX_POSITION_SIZE", 0.0) or 0.0)
            if test_cap <= 0:
                return _deny("LIVE TEST GATE: LIVE_MAX_POSITION_SIZE INVALID")
            if notional > test_cap:
                return _deny(
                    f"LIVE TEST GATE: POSITION NOTIONAL {notional:.2f} > "
                    f"LIVE_MAX_POSITION_SIZE {test_cap:.2f}"
                )

        self._last_live_gate_reason = "LIVE_HARD_GATE_OK"
        self._last_live_gate_at = datetime.now(timezone.utc).isoformat()
        return True, "LIVE_HARD_GATE_OK"

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
            self._paper_heal_legacy_empty_cash()
            # 1. Positionsgröße auf Basis Equity (nicht nur freie Kasse)
            equity = self.paper_account_equity()
            if equity <= 0:
                return (
                    False,
                    f"SIZING BLOCKIERT: Equity ≤ 0 (Equity={equity:.2f}, Kasse={self.balance:.2f})",
                    0.0,
                )

            amount, sizing_info = self.portfolio.calculate_size(signal, equity)

            if amount <= 0:
                return False, f"SIZING BLOCKIERT: {sizing_info}", 0.0

            entry = float(signal.entry)
            if entry <= 0:
                return False, "SIZING BLOCKIERT: ungültiger Entry", 0.0

            cash = float(self.balance)
            # 2. Spot-Paper / Live: Notional durch freie Kasse deckeln.
            # Paper-Equity-Konto: keine Kassen-Deckelung (Limits über Portfolio-Risk).
            if not paper_equity_ledger_enabled():
                max_amount_by_cash = cash / entry
                if amount > max_amount_by_cash:
                    if max_amount_by_cash <= 0:
                        return (
                            False,
                            (
                                "SIZING BLOCKIERT: Keine freie Kasse für weiteren Entry "
                                f"(Kasse={cash:.2f} USDT, Equity={equity:.2f} USDT). "
                                "Positionen schließen oder PAPER_TRADING_BALANCE erhöhen."
                            ),
                            0.0,
                        )
                    amount = max_amount_by_cash
                    sizing_info += f" | auf freie Kasse gedeckelt ({amount:.8f})"

            notional = amount * entry
            min_n = float(self.portfolio.cfg.min_position_notional)
            if notional + 1e-9 < min_n:
                return (
                    False,
                    (
                        f"SIZING BLOCKIERT: Nach Kassen-Deckel Notional {notional:.2f} < "
                        f"MIN_POSITION_NOTIONAL {min_n:.0f} – zu wenig freie Kasse für Mindestsize"
                    ),
                    0.0,
                )

            amount = round(amount, 8)

            # 3. Portfolio-Limits (% beziehen sich auf Equity)
            pf_allowed, pf_reason = self.portfolio.check_portfolio_limits(
                signal, equity, self.open_positions, amount
            )
            if not pf_allowed:
                return False, pf_reason, 0.0

            # Erfolg: Risiko-Kennzahlen für Logging berechnen
            risk_usd = abs(signal.entry - signal.stop_loss) * amount
            risk_pct = (risk_usd / equity * 100) if equity > 0 else 0.0
            logger.info(
                f"[cyan]SIZING[/cyan] {signal.symbol} | "
                f"Modus: {self.portfolio.cfg.sizing_mode} | "
                f"{sizing_info} | "
                f"Notional: {notional:.2f} USDT | "
                f"Risiko: {risk_pct:.2f}% ({risk_usd:.2f} USDT) | "
                f"Equity={equity:.2f} Kasse={cash:.2f}"
                + (" [Paper-Equity-Konto]" if paper_equity_ledger_enabled() else "")
            )
            return True, "OK", amount

        except Exception as e:
            logger.error(f"check_and_size Fehler für {signal.symbol}: {e}")
            # Sicherer Fallback: ursprüngliches einfaches Sizing
            fallback_amount = self.calculate_position_size(signal.entry)
            if fallback_amount <= 0:
                return False, (
                    f"FALLBACK-SIZING: Menge 0 nach Fehler ({type(e).__name__}) – "
                    f"Trade blockiert"
                ), 0.0
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
            snapshot = self.portfolio.get_exposure_snapshot(
                self.open_positions, self.paper_account_equity()
            )
            stats["portfolio_risk_pct"] = snapshot["total_risk_pct"]
        except Exception:
            stats["portfolio_risk_pct"] = 0.0
        stats["risk_gate_last_reason"] = self._last_gate_reason
        stats["risk_gate_last_at"] = self._last_gate_at
        return stats

    def get_gate_status(self) -> dict:
        self._reset_daily_loss_if_new_day()
        daily_limit = self._initial_balance * (settings.DAILY_LOSS_LIMIT_PCT / 100)
        ctrl = runtime_control.get_snapshot()
        return {
            "paused": bool(ctrl.get("paused")),
            "risk_off": bool(ctrl.get("risk_off")),
            "daily_loss_usdt": round(abs(self._daily_loss), 2),
            "daily_loss_limit_usdt": round(daily_limit, 2),
            "daily_loss_limit_pct": float(settings.DAILY_LOSS_LIMIT_PCT),
            "open_positions": len(self.open_positions),
            "max_open_positions": self.max_open_trades,
            "active_coin_cooldowns": len(self._coin_cooldown),
            "active_strategy_cooldowns": len(self._strategy_cooldown),
            "last_gate_reason": self._last_gate_reason,
            "last_gate_at": self._last_gate_at,
            "global_losing_streak": self._global_losing_streak,
            "live_hard_gate_enabled": bool(settings.LIVE_HARD_RISK_GATE_ENABLED),
            "live_last_gate_reason": self._last_live_gate_reason,
            "live_last_gate_at": self._last_live_gate_at,
            "live_min_equity_usdt": float(settings.LIVE_MIN_ACCOUNT_EQUITY_USDT),
            "live_min_free_capital_usdt": float(settings.LIVE_MIN_FREE_CAPITAL_USDT),
            "live_max_losing_streak": int(settings.LIVE_MAX_LOSING_STREAK),
            "live_test_mode": bool(getattr(settings, "LIVE_TEST_MODE", False)),
            "live_test_max_position_size": float(getattr(settings, "LIVE_MAX_POSITION_SIZE", 0.0)),
            "live_test_daily_loss_limit_pct": float(
                getattr(settings, "LIVE_TEST_DAILY_LOSS_LIMIT_PCT", settings.DAILY_LOSS_LIMIT_PCT)
            ),
            "live_allowed_symbols": str(getattr(settings, "LIVE_ALLOWED_SYMBOLS", "") or ""),
            "live_allowed_strategies": str(getattr(settings, "LIVE_ALLOWED_STRATEGIES", "") or ""),
            "risk_per_trade_pct": float(settings.RISK_PER_TRADE_PCT),
            "sizing_mode": self.portfolio.cfg.sizing_mode,
        }
