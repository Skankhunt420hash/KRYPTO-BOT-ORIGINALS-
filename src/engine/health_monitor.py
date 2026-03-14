"""
Health Monitor & Watchdog

Zentrale Überwachung des Bot-Gesundheitsstatus für 24/7-Betrieb.

Überwacht:
  1. Main Loop Heartbeat        – Lebenszeichen des run_cycle()
  2. Marktdaten-Freshness        – OHLCV-Daten pro Symbol
  3. Error-Rate (rolling window) – Fehleranzahl in konfigurierbarem Zeitfenster
  4. Ressourcen                  – CPU/RAM via psutil (optional, graceful fallback)
  5. Execution Engine Status     – Circuit Breaker, Emergency Pause

Health-Zustände:
  HEALTHY  → alles normal, Trading läuft
  DEGRADED → Warnzeichen sichtbar, Trading läuft noch
  PAUSED   → Trading pausiert (Watchdog-Reaktion oder extern)
  ERROR    → Kritischer Fehler, sofortige Aufmerksamkeit nötig

Watchdog-Reaktionen (konservativ):
  warn    → loggen + optionale Telegram-Alert (rate-limited)
  pause   → Trading via ExecutionEngine._trigger_pause() pausieren
  (kein process-kill, kein aggressives Neustarten)

Backtest-Kompatibilität:
  Kein Einfluss auf Backtest-Code (nur für laufenden Bot-Loop gedacht).

TradingBot (Legacy):
  Nicht integriert – bleibt unverändert. Nur MultiStrategyBot nutzt Health Monitor.
"""

import time
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("health")

# Optionale psutil-Abhängigkeit – graceful fallback wenn nicht installiert
try:
    import psutil as _psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _psutil = None
    _PSUTIL_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Datenstrukturen
# ─────────────────────────────────────────────────────────────────────────────

class HealthStatus(Enum):
    HEALTHY  = "healthy"
    DEGRADED = "degraded"
    PAUSED   = "paused"
    ERROR    = "error"


# ─────────────────────────────────────────────────────────────────────────────
# Health Monitor
# ─────────────────────────────────────────────────────────────────────────────

class HealthMonitor:
    """
    Zentrale Health-Monitoring-Komponente für den MultiStrategyBot.

    Verwendung:
        monitor = HealthMonitor(exec_engine=self.exec_engine, tg=self.tg)

        # Am Anfang jedes run_cycle():
        monitor.update_heartbeat()

        # Nach erfolgreichem OHLCV-Fetch:
        monitor.update_data_freshness("BTC/USDT")

        # Bei Fehler in Fehlerhandlern:
        monitor.record_error("error", "Beschreibung")

        # Am Ende jedes run_cycle():
        monitor.check_and_react()

        # Status abfragen:
        if not monitor.is_healthy:
            ...

    Thread-Hinweis: Ausgelegt für Single-Thread-Betrieb (asyncio: nicht nötig).
    """

    def __init__(
        self,
        exec_engine: Optional[Any] = None,  # ExecutionEngine
        tg: Optional[Any] = None,            # TelegramNotifier
    ):
        self._exec_engine = exec_engine
        self._tg = tg

        # ── Heartbeat ─────────────────────────────────────────────────
        self._last_heartbeat: float = time.monotonic()
        self._start_time: float = time.monotonic()

        # ── Marktdaten-Freshness ──────────────────────────────────────
        self._data_timestamps: Dict[str, float] = {}

        # ── Error-Rolling-Window ──────────────────────────────────────
        self._errors: Deque[Tuple[float, str, str]] = deque()

        # ── Status ────────────────────────────────────────────────────
        self._status: HealthStatus = HealthStatus.HEALTHY
        self._status_reason: str = ""
        self._last_snapshot_time: float = 0.0

        # ── Alert-Cooldown ────────────────────────────────────────────
        self._alert_times: Dict[str, float] = {}

        # ── Letzter Snapshot (für --health CLI und get_snapshot()) ────
        self._snapshot: dict = {}

        if not settings.HEALTH_MONITOR_ENABLED:
            logger.info(
                "[dim]HealthMonitor deaktiviert "
                "(HEALTH_MONITOR_ENABLED=false)[/dim]"
            )
            return

        psutil_info = (
            "verfuegbar" if _PSUTIL_AVAILABLE
            else "nicht installiert (pip install psutil)"
        )
        logger.info(
            f"[cyan]HealthMonitor aktiv[/cyan] | "
            f"HB-Timeout={settings.HEALTH_HEARTBEAT_TIMEOUT_SEC}s | "
            f"Data-Stale={settings.DATA_STALE_TIMEOUT_SEC}s | "
            f"Err-Window={settings.ERROR_WINDOW_MINUTES}min | "
            f"Snapshot-Interval={settings.HEALTH_CHECK_INTERVAL_SEC}s | "
            f"psutil: {psutil_info}"
        )

    # ── Public API ────────────────────────────────────────────────────────

    def update_heartbeat(self) -> None:
        """Zu Beginn jedes run_cycle() aufrufen."""
        if not settings.HEALTH_MONITOR_ENABLED:
            return
        self._last_heartbeat = time.monotonic()

    def update_data_freshness(self, symbol: str) -> None:
        """Nach erfolgreichem OHLCV-Fetch aufrufen."""
        if not settings.HEALTH_MONITOR_ENABLED:
            return
        self._data_timestamps[symbol] = time.time()

    def record_error(self, level: str, message: str) -> None:
        """
        Fehler im rolling Window registrieren.
        level: "warning" | "error" | "critical"
        """
        if not settings.HEALTH_MONITOR_ENABLED:
            return
        now = time.time()
        self._errors.append((now, level, message[:200]))
        self._cleanup_errors(now)

    @property
    def status(self) -> HealthStatus:
        return self._status

    @property
    def is_healthy(self) -> bool:
        """True wenn Status HEALTHY oder DEGRADED (Trading kann weiterlaufen)."""
        return self._status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)

    def check_and_react(self) -> HealthStatus:
        """
        Evaluiert alle Health-Signale, aktualisiert Status und reagiert
        bei Bedarf (Warnung / Pause / Telegram-Alert).

        Wird am Ende jedes run_cycle() aufgerufen.
        Gibt den aktuellen HealthStatus zurück.
        """
        if not settings.HEALTH_MONITOR_ENABLED:
            return HealthStatus.HEALTHY

        now_mono = time.monotonic()
        now_wall = time.time()
        self._cleanup_errors(now_wall)

        issues: List[Tuple[str, str]] = []  # (severity, description)

        # 1. Heartbeat-Check
        hb_age = now_mono - self._last_heartbeat
        if hb_age > settings.HEALTH_HEARTBEAT_TIMEOUT_SEC:
            issues.append((
                "critical",
                f"Heartbeat zu alt: {hb_age:.0f}s "
                f"(Limit: {settings.HEALTH_HEARTBEAT_TIMEOUT_SEC}s)"
            ))

        # 2. Datenfreshness-Check
        stale_ok, stale_syms = self._check_data_freshness(now_wall)
        if not stale_ok:
            issues.append(("error", f"Stale Marktdaten: {', '.join(stale_syms)}"))

        # 3. Error-Rate-Check
        w = sum(1 for _, lvl, _ in self._errors if lvl == "warning")
        e = sum(1 for _, lvl, _ in self._errors if lvl == "error")
        c = sum(1 for _, lvl, _ in self._errors if lvl == "critical")
        if c >= settings.MAX_CRITICAL_ERRORS_PER_WINDOW:
            issues.append((
                "critical",
                f"Kritische Fehler: {c}/{settings.MAX_CRITICAL_ERRORS_PER_WINDOW} "
                f"in {settings.ERROR_WINDOW_MINUTES}min"
            ))
        elif e + c >= settings.MAX_ERRORS_PER_WINDOW:
            issues.append((
                "error",
                f"Fehlerrate zu hoch: {e+c}/{settings.MAX_ERRORS_PER_WINDOW} "
                f"in {settings.ERROR_WINDOW_MINUTES}min"
            ))

        # 4. Ressourcen-Check (psutil, optional)
        res = self._get_resource_usage()
        if res:
            if res["mem_pct"] > settings.MAX_MEMORY_PCT:
                issues.append((
                    "warning",
                    f"Hoher Speicherverbrauch: {res['mem_pct']:.1f}% "
                    f"> {settings.MAX_MEMORY_PCT}%"
                ))
            if res["cpu_pct"] > settings.MAX_CPU_PCT:
                issues.append((
                    "warning",
                    f"Hohe CPU-Auslastung: {res['cpu_pct']:.1f}% "
                    f"> {settings.MAX_CPU_PCT}%"
                ))

        # 5. Execution Engine Status
        if self._exec_engine:
            try:
                es = self._exec_engine.get_status()
                if es.get("emergency_paused"):
                    issues.append((
                        "error",
                        f"Execution Emergency Pause: "
                        f"{es.get('pause_reason', '?')[:80]}"
                    ))
                elif es.get("circuit_state") == "open":
                    issues.append(("warning", "Execution Circuit Breaker offen"))
            except Exception:
                pass

        # ── Neuen Status bestimmen ────────────────────────────────────
        if any(sev == "critical" for sev, _ in issues):
            new_status = HealthStatus.ERROR
        elif any(sev in ("error", "warning") for sev, _ in issues):
            new_status = HealthStatus.DEGRADED
        else:
            new_status = HealthStatus.HEALTHY

        # ── Status-Übergang verarbeiten ───────────────────────────────
        old_status = self._status
        if new_status != old_status:
            self._on_status_change(old_status, new_status, issues)

        self._status = new_status
        self._status_reason = (
            "; ".join(desc for _, desc in issues) if issues else ""
        )

        # ── Watchdog-Reaktionen auslösen ─────────────────────────────
        self._react(issues, stale_syms, hb_age)

        # ── Periodischen Snapshot loggen ─────────────────────────────
        if now_wall - self._last_snapshot_time >= settings.HEALTH_CHECK_INTERVAL_SEC:
            self._log_snapshot(res, w, e, c)
            self._last_snapshot_time = now_wall

        return self._status

    def get_snapshot(self) -> dict:
        """Gibt den letzten gespeicherten Health-Snapshot zurück."""
        return dict(self._snapshot)

    def log_snapshot_now(self) -> None:
        """Erzwingt sofortigen Snapshot-Log (z.B. beim Start oder --health)."""
        if not settings.HEALTH_MONITOR_ENABLED:
            return
        res = self._get_resource_usage()
        now = time.time()
        self._cleanup_errors(now)
        w = sum(1 for _, lvl, _ in self._errors if lvl == "warning")
        e = sum(1 for _, lvl, _ in self._errors if lvl == "error")
        c = sum(1 for _, lvl, _ in self._errors if lvl == "critical")
        self._log_snapshot(res, w, e, c)

    # ── Interne Methoden ─────────────────────────────────────────────────

    def _check_data_freshness(
        self, now: float
    ) -> Tuple[bool, List[str]]:
        stale = [
            sym for sym, ts in self._data_timestamps.items()
            if now - ts > settings.DATA_STALE_TIMEOUT_SEC
        ]
        return len(stale) == 0, stale

    def _cleanup_errors(self, now: float) -> None:
        """Entfernt Fehler die älter als das konfigurierte Zeitfenster sind."""
        cutoff = now - settings.ERROR_WINDOW_MINUTES * 60
        while self._errors and self._errors[0][0] < cutoff:
            self._errors.popleft()

    def _get_resource_usage(self) -> Optional[dict]:
        """Gibt CPU/RAM-Nutzung zurück. None wenn psutil nicht verfügbar."""
        if not _PSUTIL_AVAILABLE or not settings.RESOURCE_MONITOR_ENABLED:
            return None
        try:
            proc = _psutil.Process()
            # cpu_percent(interval=None) ist non-blocking (Diff seit letztem Aufruf)
            return {
                "cpu_pct": round(proc.cpu_percent(interval=None), 1),
                "mem_pct": round(proc.memory_percent(), 1),
                "mem_mb":  round(proc.memory_info().rss / 1024 / 1024, 1),
            }
        except Exception:
            return None

    def _can_alert(self, key: str) -> bool:
        """Rate-Limiting für Telegram-Alerts. True = Alert erlaubt."""
        now = time.time()
        if now - self._alert_times.get(key, 0) >= settings.TELEGRAM_ALERT_COOLDOWN_SEC:
            self._alert_times[key] = now
            return True
        return False

    def _on_status_change(
        self,
        old: HealthStatus,
        new: HealthStatus,
        issues: List[Tuple[str, str]],
    ) -> None:
        """Loggt Status-Übergänge und sendet Telegram-Alert bei Bedarf."""
        if new == HealthStatus.HEALTHY:
            logger.info(
                f"[green]HEALTH RECOVERED[/green]: "
                f"{old.value} → {new.value}"
            )
            if self._tg and self._can_alert("recovered"):
                self._tg.send(
                    f"✅ <b>Bot Health RECOVERED</b>\n"
                    f"Status: {old.value} → {new.value}"
                )
        else:
            level_str = "ERROR" if new == HealthStatus.ERROR else "DEGRADED"
            desc = "; ".join(d for _, d in issues[:3])
            logger.warning(
                f"[yellow]HEALTH {level_str}[/yellow]: "
                f"{old.value} → {new.value} | {desc}"
            )
            if self._tg and self._can_alert(f"status_{new.value}"):
                icon = "🔴" if new == HealthStatus.ERROR else "🟡"
                self._tg.send(
                    f"{icon} <b>Bot Health {level_str}</b>\n"
                    f"Status: {old.value} → {new.value}\n"
                    f"📋 {desc}"
                )

    def _react(
        self,
        issues: List[Tuple[str, str]],
        stale_syms: List[str],
        hb_age: float,
    ) -> None:
        """Watchdog-Reaktionen basierend auf erkannten Problemen."""

        # Stale-Data-Reaktion
        if stale_syms:
            reason = f"Stale Marktdaten: {', '.join(stale_syms)}"
            if settings.HEALTH_PAUSE_ON_STALE_DATA:
                if self._exec_engine and self._can_alert("stale_pause"):
                    logger.warning(
                        f"[yellow]WATCHDOG: Trading pausiert (stale data)[/yellow] | "
                        f"{reason}"
                    )
                    if hasattr(self._exec_engine, "_trigger_pause"):
                        self._exec_engine._trigger_pause(
                            f"HEALTH WATCHDOG: {reason}"
                        )
            elif self._can_alert("stale_warn"):
                logger.warning(
                    f"[yellow]HEALTH: Stale Marktdaten[/yellow] | "
                    f"{reason} (kein Update seit >{settings.DATA_STALE_TIMEOUT_SEC}s)"
                )
                if self._tg and self._can_alert("stale_tg"):
                    self._tg.send(
                        f"⏰ <b>Stale Marktdaten</b>\n"
                        f"Symbole: {', '.join(stale_syms)}\n"
                        f"Kein Update seit >{settings.DATA_STALE_TIMEOUT_SEC}s"
                    )

        # Heartbeat-Miss-Reaktion
        if hb_age > settings.HEALTH_HEARTBEAT_TIMEOUT_SEC:
            hb_reason = f"Heartbeat zu alt: {hb_age:.0f}s"
            if settings.HEALTH_PAUSE_ON_HEARTBEAT_MISS:
                if self._exec_engine and self._can_alert("hb_pause"):
                    logger.error(
                        f"[red]WATCHDOG: Trading pausiert (Heartbeat-Miss)[/red] | "
                        f"{hb_reason}"
                    )
                    if hasattr(self._exec_engine, "_trigger_pause"):
                        self._exec_engine._trigger_pause(
                            f"HEALTH WATCHDOG: {hb_reason}"
                        )
            elif self._can_alert("hb_warn"):
                logger.warning(
                    f"[yellow]HEALTH: Heartbeat-Miss[/yellow] | {hb_reason}"
                )

    def _log_snapshot(
        self,
        res: Optional[dict],
        warnings: int,
        errors: int,
        criticals: int,
    ) -> None:
        """Loggt einen periodischen Health-Snapshot und speichert ihn intern."""
        uptime_s = int(time.monotonic() - self._start_time)
        h = uptime_s // 3600
        m = (uptime_s % 3600) // 60
        s = uptime_s % 60
        uptime_str = f"{h}h{m:02d}m{s:02d}s"

        hb_age = time.monotonic() - self._last_heartbeat

        # Daten-Freshness
        if self._data_timestamps:
            max_age = max(
                time.time() - ts for ts in self._data_timestamps.values()
            )
            data_str = f"max {max_age:.0f}s alt"
        else:
            data_str = "keine Daten (noch kein Zyklus)"

        # Execution Engine Status
        exec_str = "n/a"
        if self._exec_engine:
            try:
                st = self._exec_engine.get_status()
                exec_str = (
                    f"CB={st['circuit_state']} "
                    f"Pause={st['emergency_paused']} "
                    f"Err={st['consecutive_errors']}"
                )
            except Exception:
                exec_str = "status nicht verfügbar"

        # Ressourcen
        if res:
            res_str = (
                f"RAM={res['mem_mb']:.0f}MB ({res['mem_pct']:.1f}%) "
                f"CPU={res['cpu_pct']:.1f}%"
            )
        else:
            res_str = "psutil nicht installiert"

        # Farbe je nach Status
        color = {
            HealthStatus.HEALTHY:  "green",
            HealthStatus.DEGRADED: "yellow",
            HealthStatus.PAUSED:   "yellow",
            HealthStatus.ERROR:    "red",
        }.get(self._status, "white")

        logger.info(
            f"[{color}]◆ HEALTH SNAPSHOT[/{color}] "
            f"[bold]{self._status.value.upper()}[/bold] | "
            f"Uptime={uptime_str} | "
            f"HB={hb_age:.0f}s | "
            f"Daten={data_str} | "
            f"Err({settings.ERROR_WINDOW_MINUTES}min): "
            f"W={warnings} E={errors} C={criticals} | "
            f"Exec: {exec_str} | "
            f"{res_str}"
        )
        if self._status_reason:
            logger.warning(
                f"[yellow]HEALTH GRUND[/yellow]: {self._status_reason}"
            )

        # Snapshot für get_snapshot() und --health Flag
        self._snapshot = {
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "status":           self._status.value,
            "status_reason":    self._status_reason,
            "uptime_sec":       uptime_s,
            "heartbeat_age_sec": round(hb_age, 1),
            "data_ages_sec": {
                sym: round(time.time() - ts, 1)
                for sym, ts in self._data_timestamps.items()
            },
            "errors_window": {
                "warnings":  warnings,
                "errors":    errors,
                "criticals": criticals,
                "window_minutes": settings.ERROR_WINDOW_MINUTES,
            },
            "exec_engine":     exec_str,
            "resources":       res,
            "settings": {
                "heartbeat_timeout_sec": settings.HEALTH_HEARTBEAT_TIMEOUT_SEC,
                "data_stale_timeout_sec": settings.DATA_STALE_TIMEOUT_SEC,
                "pause_on_stale":         settings.HEALTH_PAUSE_ON_STALE_DATA,
                "pause_on_hb_miss":       settings.HEALTH_PAUSE_ON_HEARTBEAT_MISS,
            },
        }
