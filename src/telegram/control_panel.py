"""
Telegram Control Panel für KRYPTO-BOT ORIGINALS.

Ziel:
    - Separate, modulare Schicht für Telegram-Steuerung und -Monitoring
    - Keine direkte Vermischung mit Trading-Engine / Risk-Engine
    - Panel ist optional aktivierbar über Settings / .env

Wichtige Hinweise:
    - Diese Implementierung nutzt Polling über die Telegram Bot API (getUpdates)
    - Sie ist als leichtgewichtige Steuerschicht gedacht und kann in einem
      separaten Prozess oder Thread laufen.
    - Schreibende Aktionen (z.B. echten Bot-Start/-Stop) werden bewusst
      konservativ gehalten und sollten nur über explizite Integrationspunkte
      (Callbacks) angebunden werden.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import requests

from config.settings import settings
from src.engine.runtime_control import runtime_control
from src.engine.runtime_state import runtime_state
from src.storage.trade_repository import TradeRepository
from src.utils.logger import setup_logger
from src.utils.telegram_notifier import TelegramNotifier

logger = setup_logger("telegram.panel")

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_STRATEGY_ALIASES = {
    "momentum_pullback": "MomentumPullback",
    "rangereversion": "RangeReversion",
    "range_reversion": "RangeReversion",
    "volatilitybreakout": "VolatilityBreakout",
    "volatility_breakout": "VolatilityBreakout",
    "trendcontinuation": "TrendContinuation",
    "trend_continuation": "TrendContinuation",
    "rsi_ema": "RSI_EMA",
    "macd_crossover": "MACD_Crossover",
    "combined": "Combined",
    "auto": "auto",
}


@dataclass
class PanelCallbacks:
    """
    Optionale Callbacks, über die der Panel-Code mit der Engine
    interagieren kann, ohne von ihr abhängig zu sein.

    Diese Struktur kann bei Bedarf erweitert werden (z.B. für Moduswechsel).
    """

    get_runtime_status: Optional[Callable[[], Dict]] = None
    request_bot_stop: Optional[Callable[[], None]] = None
    request_bot_start: Optional[Callable[[], Tuple[bool, str]]] = None
    request_bot_restart: Optional[Callable[[], Tuple[bool, str]]] = None
    get_bot_status: Optional[Callable[[], Dict]] = None


class TelegramControlPanel:
    """
    Pollt Telegram-Nachrichten und reagiert auf einfache Textbefehle.

    Sicherheitsmerkmale:
        - Aktivierung nur wenn TELEGRAM_ENABLED und PANEl-Settings gesetzt
        - optionales Whitelisting von User-IDs
        - robuste Fehlerbehandlung (keine Exceptions nach außen)

    Aktuell implementierte Befehle (Text, nicht Slash-zwingend):
        - /help              → Übersicht
        - /status            → Modus, Strategie, Risk-/DB-Status
        - /mode              → aktueller TRADING_MODE
        - /strategy          → aktive STRATEGY-Einstellung
        - /risk              → ausgewählte Risiko-Parameter
        - /positions         → offene Trades aus der DB
        - /trades            → letzte Trades (closed/rejected/open)
        - /balance           → DB-Statistik (PnL, Winrate)

    Weitere Befehle (/start_bot, /stop_bot, /logs, /mode_* etc.) können über
    PanelCallbacks sauber angebunden werden.
    """

    def __init__(
        self,
        notifier: Optional[TelegramNotifier] = None,
        callbacks: Optional[PanelCallbacks] = None,
    ) -> None:
        self._token = settings.TELEGRAM_BOT_TOKEN
        self._chat_id = settings.TELEGRAM_CHAT_ID
        self._enabled = bool(
            settings.TELEGRAM_ENABLED
            and settings.TELEGRAM_PANEL_ENABLED
            and bool(self._token)
        )
        self._poll_interval = int(
            getattr(settings, "TELEGRAM_PANEL_POLL_INTERVAL_SEC", 10)
        )
        self._log_lines = int(
            getattr(settings, "TELEGRAM_PANEL_LOG_LINES", 20)
        )
        # Optionales Whitelisting: kommaseparierte User-/Chat-IDs
        raw_ids = getattr(settings, "TELEGRAM_PANEL_ALLOWED_IDS", "")
        self._allowed_ids = {
            part.strip() for part in raw_ids.split(",") if part.strip()
        }

        self._notifier = notifier or TelegramNotifier()
        self._callbacks = callbacks or PanelCallbacks()

        self._repo = TradeRepository()
        self._last_update_id: int = 0
        self._stop_flag = False
        self._thread: Optional[threading.Thread] = None
        self._poll_fail_streak: int = 0
        self._last_conflict_warn_ts: float = 0.0
        token_state = "set" if bool(self._token) else "missing"
        chat_state = "set" if bool(self._chat_id) else "missing"
        logger.info(
            "Telegram-Panel Init | telegram_enabled=%s | panel_enabled=%s | token=%s | chat_id=%s",
            settings.TELEGRAM_ENABLED,
            settings.TELEGRAM_PANEL_ENABLED,
            token_state,
            chat_state,
        )

        if self._enabled:
            logger.info(
                "Telegram-Control-Panel aktiviert "
                f"(Poll-Intervall={self._poll_interval}s, "
                f"Log-Lines={self._log_lines}, "
                f"Whitelist={'aktiv' if self._allowed_ids else 'inaktiv'})"
            )
        else:
            if settings.TELEGRAM_ENABLED and settings.TELEGRAM_PANEL_ENABLED and not self._token:
                logger.warning(
                    "Telegram-Control-Panel deaktiviert: TELEGRAM_BOT_TOKEN fehlt."
                )
            logger.info(
                "Telegram-Control-Panel deaktiviert "
                "(ENABLE_TELEGRAM/TELEGRAM_ENABLED=false, "
                "TELEGRAM_PANEL_ENABLED=false oder kein Bot-Token gesetzt)"
            )

    # ------------------------------------------------------------------
    # Öffentliche API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start_in_background(self) -> None:
        """Startet das Panel in einem Hintergrund-Thread (optional)."""
        if not self._enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag = False
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="telegram-control-panel",
            daemon=True,
        )
        self._thread.start()
        logger.info("Telegram-Control-Panel Polling-Thread gestartet.")

    def stop(self) -> None:
        """Beendet das Polling sanft."""
        self._stop_flag = True
        if (
            self._thread
            and self._thread.is_alive()
            and threading.current_thread() is not self._thread
        ):
            # Long-Polling kann bis zu timeout+net latenz blockieren.
            # Daher Join-Timeout an Poll-Intervall koppeln, um False-Warnings zu vermeiden.
            self._thread.join(timeout=max(3.0, float(self._poll_interval) + 6.0))
            if self._thread.is_alive():
                logger.info(
                    "Telegram-Control-Panel beendet sich verzoegert (Long-Poll aktiv); "
                    "Shutdown laeuft weiter."
                )
            else:
                logger.info("Telegram-Control-Panel gestoppt.")

    # ------------------------------------------------------------------
    # Interner Polling-Loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Einfaches Long-Polling der Telegram Bot API."""
        if not self._enabled:
            return

        url = _API_BASE.format(token=self._token, method="getUpdates")
        while not self._stop_flag:
            try:
                resp = requests.get(
                    url,
                    params={
                        "timeout": 20,
                        "offset": self._last_update_id + 1,
                    },
                    timeout=self._poll_interval + 5,
                )
                if resp.status_code != 200:
                    self._poll_fail_streak += 1
                    details = ""
                    try:
                        payload = resp.json()
                        details = payload.get("description", "")
                    except Exception:
                        details = ""
                    is_conflict = resp.status_code == 409
                    if is_conflict:
                        now = time.monotonic()
                        if now - self._last_conflict_warn_ts > 60:
                            logger.warning(
                                "Telegram-Panel getUpdates HTTP 409 – "
                                "zweite Instanz aktiv? Bitte nur einen Poller laufen lassen."
                            )
                            self._last_conflict_warn_ts = now
                        time.sleep(max(self._poll_interval, 15))
                        continue

                    logger.warning(
                        f"Telegram-Panel getUpdates HTTP {resp.status_code} – "
                        f"{details or 'warte auf nächsten Versuch'}"
                    )
                    if resp.status_code in (401, 403):
                        logger.error(
                            "Telegram-Panel deaktiviert wegen Authentifizierungsfehler "
                            "(Token ungültig oder nicht berechtigt)."
                        )
                        break
                    time.sleep(self._poll_interval)
                    continue

                data = resp.json()
                if not data.get("ok", False):
                    self._poll_fail_streak += 1
                    desc = data.get("description", "Unbekannter Telegram-API-Fehler")
                    logger.warning(f"Telegram-Panel API-Fehler: {desc}")
                    if "unauthorized" in desc.lower():
                        logger.error(
                            "Telegram-Panel deaktiviert wegen ungültigem Token (unauthorized)."
                        )
                        break
                    time.sleep(self._poll_interval)
                    continue

                self._poll_fail_streak = 0
                for update in data.get("result", []):
                    self._last_update_id = max(
                        self._last_update_id, update.get("update_id", 0)
                    )
                    self._handle_update(update)

            except requests.exceptions.Timeout:
                # normal bei Long-Polling – einfach weiter
                continue
            except Exception as e:
                self._poll_fail_streak += 1
                logger.error(
                    f"Telegram-Panel Polling-Fehler ({type(e).__name__}): {e}"
                )
                time.sleep(self._poll_interval)

        logger.info("Telegram-Control-Panel Polling-Loop beendet.")

    # ------------------------------------------------------------------
    # Update-Handling
    # ------------------------------------------------------------------

    def _handle_update(self, update: Dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return

        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip()

        if not text:
            return

        if self._allowed_ids and chat_id not in self._allowed_ids:
            logger.debug(f"Telegram-Panel: Chat {chat_id} nicht whitelisted – ignoriert")
            return

        logger.info(f"Telegram-Panel Command von Chat {chat_id}: {text}")
        self._dispatch_command(chat_id, text)

    # ------------------------------------------------------------------
    # Command-Dispatcher
    # ------------------------------------------------------------------

    def _dispatch_command(self, chat_id: str, text: str) -> None:
        cmd = text.split()[0].lower()
        try:
            if cmd == "/start":
                self._send_start(chat_id)
            elif cmd == "/help":
                self._send_help(chat_id)
            elif cmd == "/status":
                self._send_status(chat_id)
            elif cmd == "/mode":
                self._send_mode(chat_id)
            elif cmd == "/strategy":
                self._send_strategy(chat_id)
            elif cmd == "/risk":
                self._send_risk(chat_id)
            elif cmd == "/positions":
                self._send_positions(chat_id)
            elif cmd == "/trades":
                self._send_trades(chat_id)
            elif cmd == "/balance":
                self._send_balance(chat_id)
            elif cmd == "/logs":
                self._send_logs(chat_id)
            elif cmd == "/summary":
                self._send_summary(chat_id)
            elif cmd == "/pause":
                self._handle_pause(chat_id)
            elif cmd == "/resume":
                self._handle_resume(chat_id)
            elif cmd == "/riskoff":
                self._handle_riskoff(chat_id)
            elif cmd == "/riskon":
                self._handle_riskon(chat_id)
            elif cmd == "/killswitch":
                self._handle_killswitch_on(chat_id)
            elif cmd == "/killswitchoff":
                self._handle_killswitch_off(chat_id)
            elif cmd == "/setmode":
                self._handle_setmode(chat_id, text)
            elif cmd == "/setstrategy":
                self._handle_setstrategy(chat_id, text)
            elif cmd == "/stop_bot":
                self._handle_stop_bot(chat_id)
            elif cmd == "/start_bot":
                self._handle_start_bot(chat_id)
            elif cmd == "/botstart":
                self._handle_bot_start(chat_id)
            elif cmd == "/botstop":
                self._handle_bot_stop(chat_id)
            elif cmd == "/botrestart":
                self._handle_bot_restart(chat_id)
            elif cmd == "/botstatus":
                self._send_bot_status(chat_id)
            else:
                self._send_text(chat_id, "Unbekannter Befehl. Sende /help für eine Übersicht.")
        except Exception as e:
            logger.error(f"Telegram-Panel Dispatch-Fehler ({cmd}): {e}")
            self._send_text(chat_id, "Interner Fehler im Telegram-Panel. Siehe Logs.")

    # ------------------------------------------------------------------
    # Senden an den anfragenden Chat
    # ------------------------------------------------------------------

    def _send_text(self, chat_id: str, text: str) -> bool:
        """
        Sendet Antworten direkt an den anfragenden Chat.
        Kein Fallback auf globalen Notifier, damit Antworten nicht in
        einen falschen Chat umgeleitet werden.
        """
        if not self._enabled:
            return False
        try:
            url = _API_BASE.format(token=self._token, method="sendMessage")
            resp = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=8,
            )
            if resp.status_code == 200:
                return True
            desc = ""
            try:
                payload = resp.json()
                desc = payload.get("description", "")
            except Exception:
                desc = ""
            logger.warning(
                f"Telegram-Panel sendMessage HTTP {resp.status_code}"
                f"{f' ({desc})' if desc else ''}"
            )
            return False
        except Exception as e:
            logger.warning(f"Telegram-Panel Sendefehler ({type(e).__name__}): {e}")
            return False

    def _safe_runtime_status(self) -> Dict:
        """
        Liefert Runtime-Status robust, auch wenn die Engine nicht gestartet ist
        oder kein Callback gesetzt wurde.
        """
        state_snap = runtime_state.snapshot()
        base = {
            "running": state_snap.get("running", False),
            "engine": "state_only",
            "mode": state_snap.get("mode", settings.TRADING_MODE),
            "active_strategy": state_snap.get("active_strategy", settings.STRATEGY),
            "enabled_strategies": state_snap.get("enabled_strategies", []),
            "balance": state_snap.get("balance", 0.0),
            "equity": state_snap.get("equity", state_snap.get("balance", 0.0)),
            "available_capital": state_snap.get("available_capital", state_snap.get("balance", 0.0)),
            "total_trades": state_snap.get("total_trades", 0),
            "open_positions_detail": state_snap.get("open_positions", []),
            "recent_trades": state_snap.get("recent_trades", []),
            "recent_logs": state_snap.get("recent_logs", []),
            "health_status": state_snap.get("health_status", "n/a"),
            "last_signal": state_snap.get("last_signal", {}),
            "last_decision": state_snap.get("last_decision", {}),
            "selector": {},
            "risk_gate": {},
            "brain": state_snap.get("brain", {}),
            "app_context": state_snap.get("app_context", {}),
        }
        ctrl = runtime_control.get_snapshot()
        base["paused"] = ctrl.get("paused", False)
        base["risk_off"] = ctrl.get("risk_off", False)

        if not self._callbacks.get_runtime_status:
            base["engine"] = "not_connected"
            return base
        try:
            status = self._callbacks.get_runtime_status() or {}
            merged = {**base, **status}
            if "running" not in merged:
                merged["running"] = False
            return merged
        except Exception as e:
            logger.warning(f"Runtime-Status Callback-Fehler: {e}")
            base["engine"] = "callback_error"
            return base

    # ------------------------------------------------------------------
    # Handler-Implementierungen (read-only + Callback-Hooks)
    # ------------------------------------------------------------------

    def _send_start(self, chat_id: str) -> None:
        self._send_text(
            chat_id,
            "🤖 <b>KRYPTO-BOT Control Panel aktiv</b>\n"
            "Nutze /help für die Befehlsübersicht.\n"
            "Sicherheits-Hinweis: Trading-Modus bleibt durch .env gesteuert."
        )

    def _send_help(self, chat_id: str) -> None:
        self._send_text(
            chat_id,
            "<b>KRYPTO-BOT Control Center</b>\n"
            "📖 <b>Lesend</b>: /status /summary /balance /positions /trades /risk /strategy /mode /logs\n"
            "🎛 <b>Steuerung</b>: /pause /resume /riskoff /riskon /killswitch /killswitchoff\n"
            "⚙ <b>Optional</b>: /setstrategy &lt;name&gt;, /setmode paper\n"
            "🤖 <b>Supervisor</b>: /botstart /botstop /botrestart /botstatus\n"
            "🧠 Alle Kernbefehle lesen echte Runtime-, Brain-, Risk- und Trade-Daten."
        )

    def _send_mode(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        mode = str(rt.get("mode", settings.TRADING_MODE)).lower()
        if mode == "paper":
            desc = "Paper-Trading (simuliert, kein Echtgeld)"
        elif mode == "live":
            if bool(getattr(settings, "LIVE_TEST_MODE", False)):
                desc = "MINI-LIVE TESTMODE (strikt begrenzt, kleine Positionsgröße, Safety-Gates aktiv)."
            else:
                desc = "Normaler Live-Modus (nur bei expliziter Freigabe + Risk-Gates)."
        else:
            desc = f"Unbekannter Modus '{mode}' – bitte .env prüfen."

        self._send_text(
            chat_id,
            f"🔧 <b>Modus:</b> <code>{mode}</code>\n"
            f"🧪 Mini-Live: <code>{bool(getattr(settings, 'LIVE_TEST_MODE', False))}</code>\n"
            f"{desc}"
        )

    def _send_strategy(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        strat = settings.STRATEGY
        active = rt.get("active_strategy") or runtime_control.get_snapshot().get("preferred_strategy") or strat
        ranking = (rt.get("brain") or {}).get("last_strategy_ranking") or []
        top_lines = []
        for item in ranking[:3]:
            top_lines.append(
                f"- {item.get('strategy')} ({item.get('side')}) "
                f"score={item.get('brain_score')} eligible={item.get('eligible')}"
            )
        top_txt = "\n".join(top_lines) if top_lines else "- n/a"

        self._send_text(
            chat_id,
            f"📊 <b>Strategie</b>\n"
            f"Config: <code>{strat}</code> | Aktiv: <code>{active}</code>\n"
            f"Priorität: <code>{runtime_control.get_snapshot().get('preferred_strategy') or 'keine'}</code>\n"
            f"Selector Winner: <code>{(rt.get('selector') or {}).get('winner') or 'n/a'}</code> "
            f"(score={(rt.get('selector') or {}).get('winner_score') or 'n/a'})\n"
            f"Brain Decision: <code>{(rt.get('brain') or {}).get('last_decision_reason') or 'n/a'}</code>\n"
            f"Top Ranking:\n{top_txt}"
        )

    def _send_risk(self, chat_id: str) -> None:
        runtime_daily_loss = "n/a"
        runtime_risk_pct = "n/a"
        rt = self._safe_runtime_status()
        if "daily_loss" in rt:
            runtime_daily_loss = f"{rt.get('daily_loss')} USDT"
        if "portfolio_risk_pct" in rt:
            runtime_risk_pct = f"{rt.get('portfolio_risk_pct')}%"
        ctrl = runtime_control.get_snapshot()
        gate = rt.get("risk_gate") or {}

        text = (
            "<b>🛡 Risk-Status</b>\n"
            f"Pause/RiskOff: {ctrl.get('paused')}/{ctrl.get('risk_off')}\n"
            f"KillSwitch File: {Path(settings.KILL_SWITCH_FILE).exists()}\n"
            f"Risk/Trade: {settings.RISK_PER_TRADE_PCT}% | MaxOpenRisk: {settings.MAX_TOTAL_OPEN_RISK_PCT}%\n"
            f"OpenPos Limit: {settings.MAX_POSITIONS_TOTAL} | DailyLimit: {settings.DAILY_LOSS_LIMIT_PCT}%\n"
            f"DailyLoss Runtime: {runtime_daily_loss} | PortfolioRisk: {runtime_risk_pct}\n"
            f"Gate Last: {gate.get('last_gate_reason', 'n/a')}\n"
            f"Live Gate: {gate.get('live_last_gate_reason', 'n/a')} "
            f"(enabled={gate.get('live_hard_gate_enabled', False)})\n"
            f"Live Limits: minEq={gate.get('live_min_equity_usdt', 'n/a')} "
            f"minFree={gate.get('live_min_free_capital_usdt', 'n/a')} "
            f"maxLosingStreak={gate.get('live_max_losing_streak', 'n/a')}\n"
            f"Mini-Live: {gate.get('live_test_mode', False)} | "
            f"maxPosSize={gate.get('live_test_max_position_size', 'n/a')} | "
            f"dailyLimit={gate.get('live_test_daily_loss_limit_pct', 'n/a')}%\n"
            f"Mini-Live Allow: symbols={gate.get('live_allowed_symbols', '') or 'all'} | "
            f"strategies={gate.get('live_allowed_strategies', '') or 'all'}\n"
            f"Recovery: startup_ok={gate.get('recovery_startup_ok', 'n/a')} | "
            f"blocked_symbols={len(gate.get('recovery_blocked_symbols', []) or [])}\n"
            f"Gate Daily: {gate.get('daily_loss_usdt', 'n/a')} / {gate.get('daily_loss_limit_usdt', 'n/a')} USDT\n"
            f"Cooldowns: coin={gate.get('active_coin_cooldowns', 'n/a')} strat={gate.get('active_strategy_cooldowns', 'n/a')} "
            f"losingStreak={gate.get('global_losing_streak', 'n/a')}\n"
            f"Brain Risky: {(rt.get('brain') or {}).get('risky_phase', 'n/a')}"
        )
        self._send_text(chat_id, text)

    def _send_summary(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        ctrl = runtime_control.get_snapshot()
        parts = [
            "<b>📌 Bot Summary</b>",
            f"• Modus: <code>{rt.get('mode', settings.TRADING_MODE)}</code>",
            f"• Mini-Live: <code>{bool(getattr(settings, 'LIVE_TEST_MODE', False))}</code>",
            f"• Strategie: <code>{rt.get('active_strategy') or settings.STRATEGY}</code>",
            f"• Pause: {rt.get('paused', ctrl.get('paused'))}",
            f"• RiskOff: {rt.get('risk_off', ctrl.get('risk_off'))}",
            f"• KillSwitch: {Path(settings.KILL_SWITCH_FILE).exists()}",
            f"• Health: {rt.get('health_status', 'n/a')}",
        ]
        parts.append(
            f"• Balance/Equity/Available: {float(rt.get('balance', 0.0)):.2f} / "
            f"{float(rt.get('equity', rt.get('balance', 0.0))):.2f} / "
            f"{float(rt.get('available_capital', rt.get('balance', 0.0))):.2f} USDT"
        )
        enabled = rt.get("enabled_strategies") or []
        pos = rt.get("open_positions_detail") or []
        parts.append(f"• Open Positions (Runtime): {len(pos)}")
        brain = rt.get("brain") or {}
        if brain:
            parts.append(
                f"• Brain: regime={brain.get('last_regime')} | "
                f"score={brain.get('last_signal_score')} | "
                f"risky={brain.get('risky_phase')}"
            )
            parts.append(f"• Brain Decision: {brain.get('last_decision_reason')}")
        gate = rt.get("risk_gate") or {}
        if gate:
            parts.append(
                f"• Live Gate: {gate.get('live_last_gate_reason', 'n/a')} "
                f"(enabled={gate.get('live_hard_gate_enabled', False)})"
            )
            parts.append(
                f"• Recovery: startup_ok={gate.get('recovery_startup_ok', 'n/a')} "
                f"blocked_symbols={len(gate.get('recovery_blocked_symbols', []) or [])}"
            )
        if self._repo.available:
            stats = self._repo.get_summary_stats()
            if stats:
                parts.append(
                    f"• DB PnL: {stats.get('total_pnl', 0.0):+.4f} USDT | "
                    f"Winrate: {stats.get('winrate_pct', 0.0):.1f}% | "
                    f"Open: {stats.get('open_trades', 0)}"
                )
        perf = rt.get("performance") or {}
        snap = perf.get("snapshot") or {}
        day = perf.get("daily_summary") or {}
        if snap:
            parts.append(
                f"• Perf: unrealized={float(snap.get('unrealized_pnl_total', 0.0)):+.4f} | "
                f"realized={float(snap.get('realized_pnl_total', 0.0)):+.4f} | "
                f"maxDD={float(snap.get('max_drawdown_pct', 0.0)):.2f}%"
            )
        if day:
            parts.append(
                f"• Today: trades={int(day.get('trades_count', 0))} | "
                f"pnl={float(day.get('pnl_abs', 0.0)):+.4f} | "
                f"best={day.get('best_strategy') or 'n/a'} | "
                f"worst={day.get('worst_strategy') or 'n/a'}"
            )
        self._send_text(chat_id, "\n".join(parts))

    def _send_status(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        parts = []
        parts.append(
            f"⚙ <b>Modus:</b> <code>{rt.get('mode', settings.TRADING_MODE)}</code> | "
            f"Strategie: <code>{rt.get('active_strategy') or settings.STRATEGY}</code>"
        )
        parts.append(
            f"🩺 <b>Runtime:</b> run={rt.get('running', False)} | "
            f"health={rt.get('health_status', 'n/a')} | "
            f"pause/riskoff={rt.get('paused', '?')}/{rt.get('risk_off', '?')}"
        )
        parts.append(
            f"🧪 <b>Mini-Live:</b> {bool(getattr(settings, 'LIVE_TEST_MODE', False))} | "
            f"maxPos={getattr(settings, 'LIVE_MAX_POSITION_SIZE', 'n/a')}"
        )
        parts.append(
            f"💰 <b>Kapital:</b> bal/eq/avail="
            f"{rt.get('balance', '?')}/{rt.get('equity', '?')}/{rt.get('available_capital', '?')} | "
            f"open={rt.get('open_positions', '?')} trades={rt.get('total_trades', '?')}"
        )
        selector = rt.get("selector") or {}
        if selector:
            parts.append(
                f"🧠 <b>Selector:</b> regime={selector.get('regime')} | "
                f"actionable={selector.get('actionable')} | eligible={selector.get('eligible')} | "
                f"winner={selector.get('winner') or 'none'} | score={selector.get('winner_score') or 'n/a'}"
            )
        gate = rt.get("risk_gate") or {}
        if gate:
            parts.append(
                f"🛡 <b>Risk Gate:</b> last={gate.get('last_gate_reason', 'n/a')} | "
                f"open={gate.get('open_positions', 'n/a')}/{gate.get('max_open_positions', 'n/a')} | "
                f"daily={gate.get('daily_loss_usdt', 'n/a')}/{gate.get('daily_loss_limit_usdt', 'n/a')} USDT"
            )
            parts.append(
                f"🧯 <b>Live Gate:</b> {gate.get('live_last_gate_reason', 'n/a')} | "
                f"enabled={gate.get('live_hard_gate_enabled', False)}"
            )
        enabled = rt.get("enabled_strategies") or []
        if enabled:
            parts.append(f"🧠 <b>Enabled Strategies:</b> {', '.join(enabled)}")
        last_signal = rt.get("last_signal") or {}
        if last_signal:
            parts.append(
                f"📡 <b>Last Signal:</b> {last_signal.get('symbol')} {last_signal.get('side')} | "
                f"{last_signal.get('strategy')} | conf={last_signal.get('confidence')} | "
                f"reason={last_signal.get('reason')}"
            )
        last_decision = rt.get("last_decision") or {}
        if last_decision:
            parts.append(
                f"✅ <b>Last Decision:</b> {last_decision.get('decision')} | "
                f"{last_decision.get('strategy')} | reason={last_decision.get('reason')}"
            )
        brain = rt.get("brain") or {}
        if brain:
            ranking = brain.get("last_strategy_ranking") or []
            top = ranking[0] if ranking else {}
            parts.append(
                f"🧠 <b>Brain:</b> regime={brain.get('last_regime')} | "
                f"score={brain.get('last_signal_score')} | "
                f"risky={brain.get('risky_phase')} | "
                f"decision={brain.get('last_decision_reason')}"
            )
            if top:
                parts.append(
                    f"🧠 <b>Brain Top Ranking:</b> {top.get('strategy')} "
                    f"({top.get('side')}) score={top.get('brain_score')} "
                    f"eligible={top.get('eligible')}"
                )
        if self._repo.available:
            stats = self._repo.get_summary_stats()
            if stats:
                parts.append(
                    "📊 <b>DB (Paper-Modus):</b> "
                    f"Closed: {stats.get('closed_trades', 0)} | "
                    f"Open: {stats.get('open_trades', 0)} | "
                    f"Rejected: {stats.get('rejected_trades', 0)} | "
                    f"Winrate: {stats.get('winrate_pct', 0.0):.1f}% | "
                    f"Total PnL: {stats.get('total_pnl', 0.0):+.4f} USDT"
                )
        else:
            parts.append("💾 DB: nicht verfügbar (Persistenz deaktiviert).")

        self._send_text(chat_id, "\n".join(parts))

    def _send_positions(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        if self._repo.available:
            trades = self._repo.get_open_trades(limit=10)
            if trades:
                lines = ["📂 <b>Offene Positionen (DB)</b>"]
                for t in trades:
                    lines.append(
                        f"- {t.get('symbol')} | {t.get('strategy_name')} | "
                        f"{t.get('side')} @ {t.get('entry_price', 0):.4f} | "
                        f"SL={t.get('stop_loss', 0):.4f} TP={t.get('take_profit', 0):.4f} | "
                        f"Size={t.get('position_size', 0):.4f}"
                    )
                self._send_text(chat_id, "\n".join(lines))
                return

        runtime_positions = rt.get("open_positions_detail") or []
        if runtime_positions:
            lines = ["📂 <b>Offene Positionen (Runtime-Fallback)</b>"]
            for p in runtime_positions[:10]:
                lines.append(
                    f"- {p.get('symbol')} | {p.get('strategy')} | "
                    f"{p.get('side')} @ {float(p.get('entry_price', 0.0)):.4f} | "
                    f"SL={float(p.get('stop_loss', 0.0)):.4f} "
                    f"TP={float(p.get('take_profit', 0.0)):.4f} | "
                    f"Size={float(p.get('amount', 0.0)):.4f}"
                )
            self._send_text(chat_id, "\n".join(lines))
            return
        self._send_text(chat_id, "Derzeit sind keine offenen Positionen vermerkt.")

    def _send_trades(self, chat_id: str) -> None:
        if self._repo.available:
            trades = self._repo.get_recent_trades(limit=10)
            if trades:
                lines = ["📜 <b>Letzte Trades (DB)</b>"]
                for t in trades:
                    status = t.get("status", "")
                    pnl = t.get("pnl_abs")
                    pnl_str = f"{pnl:+.4f}" if pnl is not None else "-"
                    score = t.get("signal_score")
                    score_txt = f"{float(score):.3f}" if score is not None else "n/a"
                    lines.append(
                        f"- [{status}] {t.get('symbol')} | {t.get('strategy_name')} | "
                        f"{t.get('side')} @ {t.get('entry_price', 0):.4f} → "
                        f"{t.get('exit_price') or '-'} | PnL={pnl_str} | "
                        f"regime={t.get('regime') or 'n/a'} | score={score_txt}"
                    )
                self._send_text(chat_id, "\n".join(lines))
                return

        rt = self._safe_runtime_status()
        runtime_trades = rt.get("recent_trades") or []
        if runtime_trades:
            lines = ["📜 <b>Letzte Trades (Runtime-Fallback)</b>"]
            for t in runtime_trades[:10]:
                pnl = t.get("pnl")
                pnl_str = f"{float(pnl):+.4f}" if pnl is not None else "-"
                lines.append(
                    f"- [{t.get('event', '?')}] {t.get('symbol')} | {t.get('strategy')} | "
                    f"{t.get('side')} | PnL={pnl_str} | {t.get('reason', '')}"
                )
            self._send_text(chat_id, "\n".join(lines))
            return
        self._send_text(chat_id, "Noch keine Trades vorhanden.")

    def _send_balance(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        balance = float(rt.get("balance", 0.0))
        equity = float(rt.get("equity", balance))
        perf = rt.get("performance") or {}
        snap = perf.get("snapshot") or {}
        day = perf.get("daily_summary") or {}
        if not self._repo.available:
            self._send_text(
                chat_id,
                "💰 <b>Runtime-Balance</b>\n"
                f"Balance: {balance:.2f} USDT\n"
                f"Equity: {equity:.2f} USDT\n"
                f"Unrealized: {float(snap.get('unrealized_pnl_total', 0.0)):+.4f} USDT\n"
                f"Day PnL: {float(day.get('pnl_abs', 0.0)):+.4f} USDT\n"
                "Hinweis: DB nicht verfügbar, daher keine Trade-Historie-Auswertung."
            )
            return

        stats = self._repo.get_summary_stats()
        if not stats:
            self._send_text(
                chat_id,
                "💰 <b>Runtime-Balance</b>\n"
                f"Balance: {balance:.2f} USDT\n"
                f"Equity: {equity:.2f} USDT\n"
                "Noch keine abgeschlossenen Trades im aktuellen Modus."
            )
            return

        self._send_text(
            chat_id,
            "💰 <b>Paper-Balance / PnL (DB)</b>\n"
            f"Runtime Balance/Equity: {balance:.2f}/{equity:.2f} USDT\n"
            f"Unrealized/Realized: {float(snap.get('unrealized_pnl_total', 0.0)):+.4f}/"
            f"{float(snap.get('realized_pnl_total', 0.0)):+.4f} USDT\n"
            f"Day PnL: {float(day.get('pnl_abs', 0.0)):+.4f} USDT | "
            f"Day Trades: {int(day.get('trades_count', 0))}\n"
            f"Closed Trades: {stats.get('closed_trades', 0)}\n"
            f"Winrate: {stats.get('winrate_pct', 0.0):.1f}%\n"
            f"Total PnL: {stats.get('total_pnl', 0.0):+.4f} USDT\n"
            f"Avg PnL: {stats.get('avg_pnl', 0.0):+.4f} USDT"
        )

    def _send_logs(self, chat_id: str) -> None:
        """
        Sendet die letzten wichtigen Ereignisse aus der aktuellsten Log-Datei.
        """
        try:
            rt = self._safe_runtime_status()
            runtime_logs = rt.get("recent_logs") or []
            if runtime_logs:
                payload = "\n".join(runtime_logs[: self._log_lines])
                if len(payload) > 3500:
                    payload = payload[:3500]
                self._send_text(
                    chat_id,
                    "🧾 <b>Letzte Events (Runtime)</b>\n"
                    f"<pre>{payload}</pre>"
                )
                return

            logs_dir = Path("logs")
            if not logs_dir.exists():
                self._send_text(chat_id, "Keine Log-Datei gefunden (Ordner 'logs' fehlt).")
                return

            files = sorted(
                logs_dir.glob("bot_*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not files:
                self._send_text(chat_id, "Noch keine Log-Dateien vorhanden.")
                return

            latest = files[0]
            with latest.open("r", encoding="utf-8", errors="ignore") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]

            tail = lines[-self._log_lines:]
            if not tail:
                self._send_text(chat_id, f"Log-Datei {latest.name} ist leer.")
                return

            payload = "\n".join(tail)
            if len(payload) > 3500:
                payload = payload[-3500:]

            self._send_text(
                chat_id,
                f"🧾 <b>Letzte Events ({latest.name})</b>\n"
                f"<pre>{payload}</pre>"
            )
        except Exception as e:
            logger.error(f"/logs Fehler: {e}")
            self._send_text(chat_id, "Fehler beim Lesen der Logs.")

    def _handle_stop_bot(self, chat_id: str) -> None:
        if not self._callbacks.request_bot_stop:
            self._send_text(
                chat_id,
                "🛑 Stop-Anfrage empfangen, aber kein Stop-Callback konfiguriert.\n"
                "Bitte Integration in die Engine ergänzen."
            )
            return
        try:
            self._callbacks.request_bot_stop()
            self._send_text(chat_id, "🛑 Bot-Stop wurde angefordert.")
        except Exception as e:
            logger.error(f"Stop-Callback-Fehler: {e}")
            self._send_text(chat_id, "Fehler beim Ausführen des Stop-Callbacks.")

    def _handle_start_bot(self, chat_id: str) -> None:
        if not self._callbacks.request_bot_start:
            self._send_text(
                chat_id,
                "▶️ Start-Anfrage empfangen, aber kein Start-Callback konfiguriert.\n"
                "Bitte Integration in die Engine ergänzen."
            )
            return
        try:
            ok, message = self._callbacks.request_bot_start()
            if ok:
                self._send_text(chat_id, f"▶️ {message}")
            else:
                self._send_text(chat_id, f"⚠️ {message}")
        except Exception as e:
            logger.error(f"Start-Callback-Fehler: {e}")
            self._send_text(chat_id, "Fehler beim Ausführen des Start-Callbacks.")

    def _handle_bot_start(self, chat_id: str) -> None:
        if self._callbacks.request_bot_start:
            ok, msg = self._callbacks.request_bot_start()
            self._send_text(chat_id, f"{'✅' if ok else '⚠️'} {msg}")
            return
        self._send_text(chat_id, "⚠️ Supervisor-Start nicht angebunden.")

    def _handle_bot_stop(self, chat_id: str) -> None:
        if self._callbacks.request_bot_stop:
            result = self._callbacks.request_bot_stop()
            if isinstance(result, tuple) and len(result) == 2:
                ok, msg = result
                self._send_text(chat_id, f"{'✅' if ok else '⚠️'} {msg}")
            else:
                self._send_text(chat_id, "✅ Stop-Anfrage gesendet.")
            return
        self._send_text(chat_id, "⚠️ Supervisor-Stop nicht angebunden.")

    def _handle_bot_restart(self, chat_id: str) -> None:
        if self._callbacks.request_bot_restart:
            ok, msg = self._callbacks.request_bot_restart()
            self._send_text(chat_id, f"{'✅' if ok else '⚠️'} {msg}")
            return
        self._send_text(chat_id, "⚠️ Supervisor-Restart nicht angebunden.")

    def _send_bot_status(self, chat_id: str) -> None:
        status = {}
        if self._callbacks.get_bot_status:
            try:
                status = self._callbacks.get_bot_status() or {}
            except Exception as e:
                logger.error("Bot-Status Callback-Fehler: %s", e)
                self._send_text(chat_id, "⚠️ Konnte Bot-Status nicht lesen.")
                return
        if not status:
            self._send_text(chat_id, "⚠️ Supervisor-Status nicht angebunden.")
            return
        running = bool(status.get("running"))
        pid = status.get("pid")
        uptime = status.get("uptime_sec")
        uptime_txt = f"{uptime}s" if uptime is not None else "n/a"
        self._send_text(
            chat_id,
            "🤖 <b>Bot-Prozess Status</b>\n"
            f"Running: <code>{running}</code>\n"
            f"PID: <code>{pid if pid is not None else 'n/a'}</code>\n"
            f"Uptime: <code>{uptime_txt}</code>\n"
            f"PID-File: <code>{status.get('pidfile', 'n/a')}</code>"
        )

    def _handle_pause(self, chat_id: str) -> None:
        runtime_control.pause_entries()
        runtime_state.update_engine(paused=True)
        runtime_state.append_log("TELEGRAM /pause -> entries pausiert")
        logger.warning("Telegram-Aktion: /pause -> neue Entries pausiert")
        self._notifier.notify_bot_paused("telegram:/pause")
        self._send_text(
            chat_id,
            "⏸️ Neue Entries wurden pausiert. Bestehende Positionen werden weiter verwaltet."
        )

    def _handle_resume(self, chat_id: str) -> None:
        runtime_control.resume_entries()
        runtime_state.update_engine(paused=False)
        runtime_state.append_log("TELEGRAM /resume -> entries aktiviert")
        logger.info("Telegram-Aktion: /resume -> Entries wieder aktiv")
        self._notifier.notify_bot_resumed("telegram:/resume")
        self._send_text(chat_id, "▶️ Entry-Pause aufgehoben. Neue Entries sind wieder erlaubt.")

    def _handle_riskoff(self, chat_id: str) -> None:
        runtime_control.enable_risk_off()
        runtime_state.update_engine(risk_off=True)
        runtime_state.append_log("TELEGRAM /riskoff -> risk_off aktiv")
        logger.warning("Telegram-Aktion: /riskoff -> Risk-Off aktiviert")
        self._notifier.notify_risk_off(True, "telegram:/riskoff")
        self._send_text(chat_id, "🛡️ Risk-Off aktiviert. Neue Entries sind gesperrt.")

    def _handle_riskon(self, chat_id: str) -> None:
        runtime_control.disable_risk_off()
        runtime_state.update_engine(risk_off=False)
        runtime_state.append_log("TELEGRAM /riskon -> risk_off deaktiviert")
        logger.info("Telegram-Aktion: /riskon -> Risk-Off deaktiviert")
        self._notifier.notify_risk_off(False, "telegram:/riskon")
        self._send_text(
            chat_id,
            "🟢 Risk-Off deaktiviert. Neue Entries sind wieder möglich (wenn Risk-Regeln erfüllt)."
        )

    def _handle_killswitch_on(self, chat_id: str) -> None:
        path = Path(settings.KILL_SWITCH_FILE)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("KILL_SWITCH=1\n", encoding="utf-8")
            runtime_control.pause_entries()
            runtime_control.enable_risk_off()
            runtime_state.update_engine(paused=True, risk_off=True)
            runtime_state.append_log("TELEGRAM /killswitch -> kill switch aktiviert")
            logger.error("Telegram-Aktion: /killswitch -> KILL SWITCH AKTIV")
            self._notifier.notify_bot_paused("telegram:/killswitch")
            self._notifier.notify_risk_off(True, "telegram:/killswitch")
            self._send_text(
                chat_id,
                "🛑 Kill-Switch AKTIV. Neue Orders sind hart gesperrt, "
                "bis /killswitchoff ausgeführt wird."
            )
        except Exception as e:
            logger.error(f"Kill-Switch Aktivierung fehlgeschlagen: {e}")
            self._send_text(chat_id, "⚠️ Kill-Switch konnte nicht aktiviert werden.")

    def _handle_killswitch_off(self, chat_id: str) -> None:
        path = Path(settings.KILL_SWITCH_FILE)
        try:
            if path.exists():
                path.unlink()
            runtime_state.append_log("TELEGRAM /killswitchoff -> kill switch deaktiviert")
            logger.warning("Telegram-Aktion: /killswitchoff -> Kill-Switch deaktiviert")
            self._send_text(
                chat_id,
                "✅ Kill-Switch deaktiviert. "
                "Hinweis: /resume und /riskon ggf. separat setzen."
            )
        except Exception as e:
            logger.error(f"Kill-Switch Deaktivierung fehlgeschlagen: {e}")
            self._send_text(chat_id, "⚠️ Kill-Switch konnte nicht deaktiviert werden.")

    def _handle_setmode(self, chat_id: str, text: str) -> None:
        parts = text.split()
        if len(parts) < 2:
            self._send_text(chat_id, "Verwendung: /setmode paper")
            return
        target = parts[1].strip().lower()
        if target != "paper":
            self._send_text(
                chat_id,
                "Aus Sicherheitsgründen wird aktuell nur /setmode paper unterstützt. "
                "Live-Modus kann nicht per Telegram aktiviert werden."
            )
            return

        prev_mode = settings.TRADING_MODE
        settings.TRADING_MODE = "paper"
        runtime_control.request_mode("paper")
        runtime_control.enable_risk_off()  # defensive Übergangsmaßnahme
        runtime_state.update_engine(mode="paper", risk_off=True)
        runtime_state.append_log(f"TELEGRAM /setmode paper (vorher={prev_mode})")
        logger.warning(
            f"Telegram-Aktion: /setmode paper (vorher={prev_mode}) -> Risk-Off gesetzt"
        )
        self._notifier.notify_risk_off(True, "setmode->paper")
        self._send_text(
            chat_id,
            "✅ Modus-Anforderung auf PAPER gesetzt. "
            "Risk-Off wurde vorsorglich aktiviert. "
            "Hinweis: laufende Komponenten können einen Neustart benötigen."
        )

    def _handle_setstrategy(self, chat_id: str, text: str) -> None:
        parts = text.split()
        if len(parts) < 2:
            self._send_text(
                chat_id,
                "Verwendung: /setstrategy <name>\n"
                "Beispiele: momentum_pullback, trend_continuation, range_reversion, "
                "volatility_breakout, auto"
            )
            return
        raw = parts[1].strip().lower()
        mapped = _STRATEGY_ALIASES.get(raw)
        if not mapped:
            self._send_text(chat_id, f"Unbekannte Strategie: {raw}")
            return

        if mapped == "auto":
            settings.STRATEGY = "auto"
            runtime_control.clear_preferred_strategy()
            runtime_state.update_engine(active_strategy="auto")
            runtime_state.append_log("TELEGRAM /setstrategy auto -> priorität gelöscht")
            logger.info("Telegram-Aktion: /setstrategy auto -> keine Priorität")
            self._notifier.notify_strategy_changed("auto")
            self._send_text(
                chat_id,
                "✅ STRATEGY=auto gesetzt. Strategie-Priorität wurde zurückgesetzt."
            )
            return

        runtime_control.set_preferred_strategy(mapped)
        runtime_state.update_engine(active_strategy=mapped)
        runtime_state.append_log(f"TELEGRAM /setstrategy {mapped} -> priorität gesetzt")
        logger.info(f"Telegram-Aktion: /setstrategy {mapped} -> Priorität gesetzt")
        self._notifier.notify_strategy_changed(mapped)
        self._send_text(
            chat_id,
            f"✅ Strategie-Priorität gesetzt auf <code>{mapped}</code>.\n"
            "Hinweis: Meta-Selector berücksichtigt dies als Bonus, "
            "Risk-Gates bleiben unverändert aktiv."
        )

