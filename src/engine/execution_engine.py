"""
Execution Quality Layer & Fail-Safe Engine

Robuste Order-Ausführung für Paper- und Live-Mode mit:
  - Retry + exponentieller Backoff (retryable vs non-retryable Fehler)
  - Slippage / Entry-Preisabweichungs-Schutz
  - Circuit Breaker (Exchange-Health-Guard)
  - Emergency Pause & Kill-Switch
  - Duplicate-Order-Fingerprinting (verhindert Doppel-Orders bei Retries)
  - Partial-Fill-Schnittstelle (vorbereitet, TODO für Live-Exchange)
  - Transparentes Logging aller Entscheidungen

Trennung der Verantwortlichkeiten:
  - Signal-Entscheidung:    MetaSelector
  - Risk/Sizing-Freigabe:   RiskEngine + PortfolioRiskEngine
  - Order-Ausführung:       ExecutionEngine  (diese Datei)

Paper-Mode: alle Checks laufen, Orders werden simuliert.
Backtest-unabhängig: kein direkter Kontakt zum Backtest-Modul.
TradingBot (Legacy): bleibt komplett unverändert.
"""

import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from src.strategies.signal import EnhancedSignal
from src.utils.logger import setup_logger

logger = setup_logger("execution")

# ─────────────────────────────────────────────────────────────────────────────
# Fehlerklassifizierung
# ─────────────────────────────────────────────────────────────────────────────

# Diese Ausnahmen lösen KEINEN Retry aus (sofort abbrechen)
_NON_RETRYABLE_PATTERNS: Tuple[str, ...] = (
    "InsufficientFunds",
    "InvalidOrder",
    "AuthenticationError",
    "PermissionDenied",
    "BadRequest",
    "BadSymbol",
    "OrderNotFound",
    "InvalidAddress",
    "InvalidNonce",
)


def _is_retryable(exc: Exception) -> bool:
    """True = temporärer Fehler (Netzwerk, Timeout, Ratelimit) → Retry sinnvoll."""
    exc_type = type(exc).__name__
    exc_msg = str(exc)
    for pattern in _NON_RETRYABLE_PATTERNS:
        if pattern in exc_type or pattern in exc_msg:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Circuit-Breaker-Zustand
# ─────────────────────────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED = "closed"       # Normal: Execution erlaubt
    OPEN = "open"           # Pausiert: Exchange mehrfach fehlgeschlagen
    HALF_OPEN = "half_open" # Testet ob Exchange wieder erreichbar ist


# ─────────────────────────────────────────────────────────────────────────────
# Ergebnis-Datenstruktur
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """Ergebnis einer Order-Ausführung."""

    success: bool
    order: Dict[str, Any]
    fill_price: float        # tatsächlicher Fill-Preis (0.0 wenn fehlgeschlagen)
    intended_price: float    # Signal-Entry-Preis
    deviation_pct: float     # Abweichung Fill vs Intended in %
    retries_used: int
    fingerprint: str
    reason: str              # leer bei Erfolg, Blockier-Grund bei Fehler

    @classmethod
    def rejected(
        cls,
        fingerprint: str,
        reason: str,
        deviation_pct: float = 0.0,
    ) -> "ExecutionResult":
        """Blockiert vor Order-Versuch (Slippage, Duplicate, Circuit Breaker)."""
        return cls(
            success=False, order={},
            fill_price=0.0, intended_price=0.0,
            deviation_pct=deviation_pct,
            retries_used=0, fingerprint=fingerprint, reason=reason,
        )

    @classmethod
    def failed(cls, fingerprint: str, reason: str) -> "ExecutionResult":
        """Order wurde versucht, ist aber nach allen Retries fehlgeschlagen."""
        return cls(
            success=False, order={},
            fill_price=0.0, intended_price=0.0,
            deviation_pct=0.0,
            retries_used=0, fingerprint=fingerprint, reason=reason,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Execution Engine
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionEngine:
    """
    Zentraler Execution Quality Layer.

    Verwendung:
        engine = ExecutionEngine(connector, tg)

        # Für Entry-Orders (LONG/SHORT):
        result = engine.execute_entry(symbol, "buy", amount, signal)
        if result.success:
            ...

        # Für Exit-Orders (SL/TP/Signal):
        result = engine.execute_exit(symbol, "sell", amount)

        # Gesundheitscheck am Zyklusanfang:
        if not engine.is_healthy:
            return  # Zyklus überspringen
    """

    def __init__(
        self,
        connector: Any,                    # ExchangeConnector (Type-Hint vermeidet Zirkelbezug)
        tg: Optional[Any] = None,          # TelegramNotifier, optional
    ):
        self._connector = connector
        self._tg = tg
        self.is_paper: bool = settings.TRADING_MODE == "paper"

        # ── Circuit Breaker ───────────────────────────────────────────
        self._circuit_state: CircuitState = CircuitState.CLOSED
        self._circuit_opened_at: float = 0.0

        # ── Fehler-Counter ────────────────────────────────────────────
        self._consecutive_errors: int = 0
        self._consecutive_rejections: int = 0

        # ── Emergency Pause ───────────────────────────────────────────
        self._emergency_paused: bool = False
        self._pause_reason: str = ""

        # ── Fingerprint-Cache (Duplicate-Schutz) ──────────────────────
        # fingerprint → unix-timestamp der letzten Ausführung
        self._fingerprints: Dict[str, float] = {}

        # ── Slippage-Event-Fenster ────────────────────────────────────
        self._slippage_events: deque = deque(
            maxlen=settings.MAX_SLIPPAGE_EVENTS_WINDOW
        )

        logger.info(
            f"[cyan]ExecutionEngine aktiv[/cyan] | "
            f"Modus={'PAPER' if self.is_paper else 'LIVE'} | "
            f"Max-Retries={settings.EXECUTION_MAX_RETRIES} | "
            f"Backoff={settings.EXECUTION_RETRY_BACKOFF_SEC}s | "
            f"Max-Dev={settings.MAX_ENTRY_DEVIATION_PCT}% | "
            f"CB-Cooldown={settings.CIRCUIT_BREAKER_COOLDOWN_SEC}s | "
            f"Kill-Switch='{settings.KILL_SWITCH_FILE}'"
        )

    # ── Gesundheitsstatus ─────────────────────────────────────────────────

    @property
    def is_healthy(self) -> bool:
        """
        True wenn neue Trades erlaubt sind.
        Prüft: Emergency Pause, Circuit Breaker, Kill-Switch-Datei.
        """
        # 1. Kill-Switch-Datei
        if _kill_switch_active():
            if not self._emergency_paused:
                self._trigger_pause(
                    f"KILL SWITCH: Datei '{settings.KILL_SWITCH_FILE}' gefunden"
                )
            return False

        # 2. Emergency Pause
        if self._emergency_paused:
            return False

        # 3. Circuit Breaker: Cooldown abgelaufen?
        if self._circuit_state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._circuit_opened_at
            if elapsed >= settings.CIRCUIT_BREAKER_COOLDOWN_SEC:
                self._circuit_state = CircuitState.HALF_OPEN
                logger.info(
                    "[cyan]Circuit Breaker: HALF-OPEN[/cyan] – "
                    "teste nächste Order"
                )
            else:
                return False

        return True

    def get_status(self) -> dict:
        """Gibt den aktuellen Status der Engine zurück (für Logging/Monitoring)."""
        return {
            "healthy": self.is_healthy,
            "circuit_state": self._circuit_state.value,
            "emergency_paused": self._emergency_paused,
            "pause_reason": self._pause_reason,
            "consecutive_errors": self._consecutive_errors,
            "consecutive_rejections": self._consecutive_rejections,
            "slippage_events": len(self._slippage_events),
            "kill_switch": _kill_switch_active(),
        }

    def reset(self) -> None:
        """
        Manueller Reset nach menschlicher Intervention.
        Aufrufen nachdem das Problem identifiziert und behoben wurde.
        """
        self._circuit_state = CircuitState.CLOSED
        self._consecutive_errors = 0
        self._consecutive_rejections = 0
        self._emergency_paused = False
        self._pause_reason = ""
        self._slippage_events.clear()
        logger.info("[green]ExecutionEngine: manueller Reset durchgeführt[/green]")

    # ── Öffentliche Ausführungs-Methoden ──────────────────────────────────

    def execute_entry(
        self,
        symbol: str,
        order_side: str,   # "buy" (LONG öffnen) oder "sell" (SHORT öffnen)
        amount: float,
        signal: Optional[EnhancedSignal] = None,
    ) -> ExecutionResult:
        """
        Führt eine Entry-Order aus mit vollem Quality-Check:
          1. Fingerprint / Duplicate-Schutz
          2. Preisabweichungs-Prüfung (Slippage-Protection)
          3. Circuit Breaker / Emergency Pause Check
          4. Order mit Retry + Backoff ausführen

        Exits sollten stattdessen execute_exit() verwenden (kein Slippage-Block).
        """
        intended_price = signal.entry if signal else 0.0
        strategy_name = signal.strategy_name if signal else "unknown"

        # 1. Fingerprint (5-Minuten-Bucket verhindert Doppel-Orders im gleichen Zyklus)
        fp = _make_fingerprint(symbol, order_side, strategy_name)
        if _is_duplicate(fp, self._fingerprints):
            reason = f"DUPLICATE ORDER BLOCKED: {fp}"
            logger.warning(f"[yellow]{reason}[/yellow]")
            self._consecutive_rejections += 1
            self._check_rejection_limit()
            return ExecutionResult.rejected(fp, reason)

        # 2. Preisabweichungs-Prüfung
        price_ok, deviation_pct, dev_reason = self._check_price_deviation(
            symbol, intended_price
        )
        if not price_ok:
            logger.warning(f"[yellow]{dev_reason}[/yellow]")
            self._slippage_events.append(time.monotonic())
            self._consecutive_rejections += 1
            if len(self._slippage_events) >= settings.MAX_SLIPPAGE_EVENTS_WINDOW:
                self._trigger_pause(
                    f"Zu viele Slippage-Events "
                    f"({len(self._slippage_events)}/{settings.MAX_SLIPPAGE_EVENTS_WINDOW})"
                )
            self._check_rejection_limit()
            return ExecutionResult.rejected(fp, dev_reason, deviation_pct=deviation_pct)

        # 3. Circuit Breaker / Emergency Pause
        if not self.is_healthy:
            status = self.get_status()
            reason = status.get("pause_reason") or f"Circuit Breaker: {status['circuit_state']}"
            return ExecutionResult.rejected(fp, reason)

        # 4. Order ausführen
        try:
            order, retries = self._execute_with_retry(symbol, order_side, amount)
            fill_price = _extract_fill_price(order, intended_price)
            actual_dev = (
                abs(fill_price - intended_price) / intended_price * 100
                if intended_price > 0 and fill_price > 0
                else 0.0
            )

            self._on_success()
            _register_fingerprint(fp, self._fingerprints)

            # Log: Erfolg mit Details
            if retries > 0:
                msg = (
                    f"Order nach {retries} Retry(s) erfolgreich: "
                    f"{symbol} {order_side.upper()} {amount:.6f}"
                )
                logger.info(f"[green]RETRY ERFOLGREICH[/green] {msg}")
                if self._tg:
                    self._tg.send(
                        f"🔄 <b>Order nach Retry OK</b> [{('PAPER' if self.is_paper else 'LIVE')}]\n"
                        f"💱 {symbol} [{order_side.upper()}] nach {retries} Versuch(en)"
                    )

            logger.info(
                f"[green]EXECUTION OK[/green] {symbol} {order_side.upper()} | "
                f"Intended={intended_price:.4f} Fill={fill_price:.4f} "
                f"Dev={actual_dev:.3f}% | Retries={retries} | "
                f"Status={order.get('status', '?')}"
            )

            return ExecutionResult(
                success=True,
                order=order,
                fill_price=fill_price,
                intended_price=intended_price,
                deviation_pct=actual_dev,
                retries_used=retries,
                fingerprint=fp,
                reason="",
            )

        except Exception as e:
            self._on_failure(str(e))
            reason = f"EXECUTION FEHLER: {type(e).__name__}: {str(e)[:120]}"
            logger.error(f"[red]{reason}[/red]")
            return ExecutionResult.failed(fp, reason)

    def execute_exit(
        self,
        symbol: str,
        order_side: str,   # "sell" (LONG schließen) oder "buy" (SHORT decken)
        amount: float,
    ) -> ExecutionResult:
        """
        Führt eine Exit-Order aus (nur Retry, kein Slippage/Duplicate-Block).
        Exits sind immer kritisch und werden nie durch Preisabweichung blockiert.
        Bei offenem Circuit Breaker wird trotzdem versucht (mit Warnung).
        """
        fp = f"exit_{symbol}_{order_side}_{int(time.time() / 60)}"

        if self._circuit_state == CircuitState.OPEN:
            logger.warning(
                f"[yellow]EXIT trotz offenem Circuit Breaker[/yellow]: "
                f"{symbol} – Position muss geschlossen werden"
            )

        try:
            order, retries = self._execute_with_retry(symbol, order_side, amount)
            fill_price = _extract_fill_price(order, 0.0)
            self._on_success()

            logger.info(
                f"[green]EXIT OK[/green] {symbol} {order_side.upper()} | "
                f"Fill={fill_price:.4f} | Retries={retries} | "
                f"Status={order.get('status', '?')}"
            )
            return ExecutionResult(
                success=True,
                order=order,
                fill_price=fill_price,
                intended_price=0.0,
                deviation_pct=0.0,
                retries_used=retries,
                fingerprint=fp,
                reason="",
            )

        except Exception as e:
            self._on_failure(str(e))
            reason = f"EXIT FEHLER (Position wird lokal geschlossen): {type(e).__name__}: {str(e)[:120]}"
            logger.error(f"[red]{reason}[/red]")
            if self._tg:
                self._tg.notify_error(
                    f"Exit-Order fehlgeschlagen: {symbol}",
                    f"{reason[:200]}"
                )
            return ExecutionResult.failed(fp, reason)

    # ── Interne Methoden ──────────────────────────────────────────────────

    def _execute_with_retry(
        self, symbol: str, side: str, amount: float
    ) -> Tuple[Dict[str, Any], int]:
        """
        Führt Order mit Retry + exponentiellem Backoff aus.
        Returns: (order_dict, retries_used)
        Raises bei endgültigem Fehler.

        Retryable:     Netzwerkfehler, Timeouts, Rate-Limits, Exchange-Unavailable
        Non-Retryable: Auth-Fehler, ungültige Order, InsufficientFunds
        """
        max_retries = settings.EXECUTION_MAX_RETRIES
        backoff = settings.EXECUTION_RETRY_BACKOFF_SEC
        last_exc: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                if side == "buy":
                    order = self._connector.create_market_buy_order(symbol, amount)
                else:
                    order = self._connector.create_market_sell_order(symbol, amount)

                if not order:
                    raise ValueError("Leeres Order-Ergebnis vom Connector")

                # TODO: Partial-Fill-Handling für Live-Exchange
                # status = order.get("status", "unknown")
                # if status == "canceled": raise ...
                # if status == "open": warte auf Fill ...

                return order, attempt  # Erfolg

            except Exception as exc:
                last_exc = exc
                retryable = _is_retryable(exc)

                logger.warning(
                    f"Order-Versuch {attempt + 1}/{max_retries + 1} fehlgeschlagen | "
                    f"{symbol} {side} {amount:.6f} | "
                    f"{'Retryable' if retryable else 'Non-Retryable'}: "
                    f"{type(exc).__name__}: {str(exc)[:80]}"
                )

                if not retryable or attempt >= max_retries:
                    break

                # Exponentieller Backoff (max 30 Sekunden)
                wait = min(backoff * (2 ** attempt), 30.0)
                logger.info(f"Warte {wait:.1f}s vor Retry {attempt + 2}/{max_retries + 1}...")
                time.sleep(wait)

        raise last_exc or RuntimeError("Order endgültig fehlgeschlagen")

    def _check_price_deviation(
        self,
        symbol: str,
        intended_price: float,
    ) -> Tuple[bool, float, str]:
        """
        Vergleicht aktuellen Ticker-Preis mit Signal-Entry.
        Returns: (ok, deviation_pct, block_reason)

        Wenn Ticker nicht verfügbar: Check übersprungen (Trade erlaubt).
        """
        max_dev = settings.MAX_ENTRY_DEVIATION_PCT
        if intended_price <= 0 or max_dev <= 0:
            return True, 0.0, ""  # Check deaktiviert oder kein Preis

        try:
            ticker = self._connector.fetch_ticker(symbol)
            current_price = float(
                ticker.get("last") or ticker.get("close") or 0
            )
        except Exception as e:
            logger.warning(
                f"Ticker-Abruf für {symbol} fehlgeschlagen: {type(e).__name__} – "
                f"Preisabweichungs-Prüfung übersprungen"
            )
            return True, 0.0, ""

        if current_price <= 0:
            logger.warning(
                f"Ticker-Preis = 0 für {symbol} – "
                f"Preisabweichungs-Prüfung übersprungen"
            )
            return True, 0.0, ""

        deviation_pct = abs(current_price - intended_price) / intended_price * 100

        if deviation_pct > max_dev:
            reason = (
                f"SLIPPAGE-BLOCK {symbol} | "
                f"Intended={intended_price:.4f} Current={current_price:.4f} | "
                f"Abweichung={deviation_pct:.3f}% > Limit={max_dev:.1f}%"
            )
            return False, deviation_pct, reason

        return True, deviation_pct, ""

    def _on_success(self) -> None:
        """Setzt Fehler-Counter zurück nach erfolgreicher Order."""
        self._consecutive_errors = 0
        self._consecutive_rejections = 0
        if self._circuit_state == CircuitState.HALF_OPEN:
            self._circuit_state = CircuitState.CLOSED
            logger.info(
                "[green]Circuit Breaker: CLOSED[/green] – "
                "Exchange wieder erreichbar"
            )

    def _on_failure(self, error: str) -> None:
        """Aktualisiert Error-Counter und löst Circuit Breaker bei Bedarf aus."""
        self._consecutive_errors += 1
        limit = settings.MAX_CONSECUTIVE_EXEC_ERRORS

        logger.debug(
            f"Execution-Fehler-Count: {self._consecutive_errors}/{limit}"
        )

        if self._consecutive_errors >= limit:
            if self._circuit_state not in (CircuitState.OPEN,):
                self._circuit_state = CircuitState.OPEN
                self._circuit_opened_at = time.monotonic()
                msg = (
                    f"CIRCUIT BREAKER AUSGELÖST: "
                    f"{self._consecutive_errors} Fehler hintereinander | "
                    f"Trading pausiert für {settings.CIRCUIT_BREAKER_COOLDOWN_SEC}s"
                )
                logger.error(f"[red]{msg}[/red]")
                if self._tg:
                    self._tg.send(f"⚡ <b>Circuit Breaker</b>\n{msg}")

            if settings.EMERGENCY_PAUSE_ON_EXEC_ERRORS:
                self._trigger_pause(
                    f"Emergency Pause: {self._consecutive_errors} Execution-Fehler"
                )

    def _check_rejection_limit(self) -> None:
        """Prüft ob Rejection-Limit überschritten wurde."""
        limit = settings.MAX_CONSECUTIVE_REJECTIONS
        if self._consecutive_rejections >= limit:
            self._trigger_pause(
                f"Zu viele aufeinanderfolgende Rejections: "
                f"{self._consecutive_rejections}/{limit}"
            )

    def _trigger_pause(self, reason: str) -> None:
        """Aktiviert Emergency Pause und benachrichtigt via Telegram."""
        if not self._emergency_paused:
            self._emergency_paused = True
            self._pause_reason = reason
            logger.error(f"[red]EMERGENCY PAUSE AKTIV: {reason}[/red]")
            if self._tg:
                if hasattr(self._tg, "notify_bot_paused"):
                    self._tg.notify_bot_paused(f"emergency:{reason}")
                else:
                    self._tg.send(
                        f"🛑 <b>EMERGENCY PAUSE</b>\n"
                        f"📋 {reason}\n"
                        f"🔧 Manueller Reset: <code>engine.reset()</code>"
                    )


# ─────────────────────────────────────────────────────────────────────────────
# Modul-Hilfsfunktionen (keine Klasse nötig)
# ─────────────────────────────────────────────────────────────────────────────

def _kill_switch_active() -> bool:
    """True wenn die Kill-Switch-Datei existiert."""
    try:
        return os.path.exists(settings.KILL_SWITCH_FILE)
    except Exception:
        return False


def _make_fingerprint(symbol: str, side: str, strategy: str) -> str:
    """
    5-Minuten-Zeitbucket-Fingerprint verhindert Doppel-Orders im gleichen Zyklus.
    Nach 5 Minuten läuft der Schutz ab (neuer Bucket).
    """
    bucket = int(time.time() / 300)
    return f"{symbol}_{side}_{strategy}_{bucket}"


def _is_duplicate(fingerprint: str, cache: Dict[str, float]) -> bool:
    """
    True wenn derselbe Fingerprint in den letzten 10 Minuten gesehen wurde.
    Räumt gleichzeitig abgelaufene Einträge (> 10min) auf.
    """
    now = time.time()
    expired = [k for k, ts in cache.items() if now - ts > 600]
    for k in expired:
        del cache[k]
    return fingerprint in cache


def _register_fingerprint(fingerprint: str, cache: Dict[str, float]) -> None:
    """Registriert einen Fingerprint nach erfolgreicher Ausführung."""
    cache[fingerprint] = time.time()


def _extract_fill_price(order: Dict[str, Any], fallback: float) -> float:
    """
    Extrahiert den Fill-Preis aus dem Order-Dict.
    Reihenfolge: average > price > last > fallback
    TODO: Für Live-Exchange: order["fills"] auswerten für gewichteten Durchschnitt
    """
    for key in ("average", "price", "last"):
        val = order.get(key)
        if val and float(val) > 0:
            return float(val)
    return fallback
