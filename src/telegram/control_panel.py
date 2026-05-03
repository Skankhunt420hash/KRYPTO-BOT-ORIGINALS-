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

import html
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

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
    apply_runtime_settings: Optional[Callable[[Dict[str, Any]], Tuple[bool, str]]] = None
    request_auto_heal: Optional[Callable[[], Tuple[bool, str]]] = None
    get_market_status: Optional[Callable[[], Dict]] = None
    get_master_status: Optional[Callable[[], Dict]] = None


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
        # Webhook blockiert getUpdates – einmal entfernen (häufiger Grund wenn „vorher ging es“)
        self._delete_webhook_for_polling()
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

    def _delete_webhook_for_polling(self) -> None:
        """Entfernt einen gesetzten Bot-Webhook, sonst liefert getUpdates nichts / Fehler."""
        if not self._token:
            return
        try:
            url = _API_BASE.format(token=self._token, method="deleteWebhook")
            resp = requests.get(
                url,
                params={"drop_pending_updates": "false"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    logger.info("Telegram-Panel: deleteWebhook OK (Polling-Modus)")
                else:
                    logger.warning(
                        "Telegram-Panel: deleteWebhook API: %s",
                        data.get("description", data),
                    )
            else:
                logger.warning(
                    "Telegram-Panel: deleteWebhook HTTP %s", resp.status_code
                )
        except Exception as e:
            logger.warning("Telegram-Panel: deleteWebhook fehlgeschlagen: %s", e)

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
            logger.warning(
                "Telegram-Panel: Chat %s nicht in TELEGRAM_PANEL_ALLOWED_IDS – Befehl ignoriert "
                "(Whitelist anpassen oder leer lassen).",
                chat_id,
            )
            return

        logger.info(f"Telegram-Panel Command von Chat {chat_id}: {text}")
        self._dispatch_command(chat_id, text)

    # ------------------------------------------------------------------
    # Command-Dispatcher
    # ------------------------------------------------------------------

    def _dispatch_command(self, chat_id: str, text: str) -> None:
        cmd = text.split()[0].lower()
        # Gruppen-Chats: /start@MeinBot_Bot — sonst kein Match
        if "@" in cmd:
            cmd = cmd.split("@", 1)[0]
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
            elif cmd == "/setrisk":
                self._handle_setrisk(chat_id, text)
            elif cmd == "/setbrain":
                self._handle_setbrain(chat_id, text)
            elif cmd == "/config":
                self._send_config(chat_id)
            elif cmd == "/brain":
                self._send_brain(chat_id)
            elif cmd == "/markets":
                self._send_markets(chat_id)
            elif cmd == "/autoheal":
                self._handle_autoheal(chat_id)
            elif cmd == "/masterstatus":
                self._send_master_status(chat_id)
            elif cmd == "/masterheal":
                self._handle_autoheal(chat_id)
            elif cmd in ("/ampel", "/ampelstatus"):
                self._send_ampel(chat_id)
            elif cmd == "/ampeldebug":
                self._send_ampel_debug(chat_id)
            elif cmd == "/ampelauto":
                self._handle_ampelauto(chat_id, text)
            elif cmd in ("/ampel_min_trades", "/ampelmintrades"):
                self._handle_ampel_min_trades(chat_id, text)
            elif cmd == "/setprofile":
                self._handle_setprofile(chat_id, text)
            elif cmd in ("/testtrade", "/testtrades"):
                self._handle_testtrade(chat_id)
            elif cmd == "/repair":
                self._handle_autoheal(chat_id)
            elif cmd == "/unlock":
                self._handle_unlock(chat_id)
            elif cmd == "/safemode":
                self._handle_safemode(chat_id)
            elif cmd == "/recovery":
                self._send_recovery(chat_id)
            elif cmd == "/snapshot":
                self._handle_snapshot(chat_id)
            elif cmd == "/setmaster":
                self._handle_setmaster(chat_id, text)
            elif cmd == "/ops":
                self._send_ops(chat_id)
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
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            resp = requests.post(url, json=payload, timeout=8)
            if resp.status_code == 200:
                return True

            desc = ""
            try:
                body = resp.json() or {}
                desc = str(body.get("description", "") or "")
            except Exception:
                desc = ""

            # Häufigster Grund für ausbleibende /status-Antwort: ungültiges HTML.
            # Fallback: escaped plain text ohne parse_mode erneut senden.
            if resp.status_code == 400 and (
                "can't parse entities" in desc.lower()
                or "can't find end tag" in desc.lower()
                or "unsupported start tag" in desc.lower()
            ):
                safe_text = html.escape(str(text or ""))
                fallback = {
                    "chat_id": chat_id,
                    "text": safe_text,
                    "disable_web_page_preview": True,
                }
                resp2 = requests.post(url, json=fallback, timeout=8)
                if resp2.status_code == 200:
                    logger.warning(
                        "Telegram-Panel HTML parse fallback aktiv (%s).", desc
                    )
                    return True

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
            "⚙ <b>Tuning</b>: /setstrategy &lt;name&gt;, /setmode paper, /setrisk &lt;key&gt; &lt;value&gt;, /setbrain &lt;key&gt; &lt;value&gt;\n"
            "🧠 <b>Diagnose</b>: /config /brain /markets /autoheal /masterstatus /masterheal /recovery /snapshot /ampel /ampeldebug\n"
            "🚦 <b>Ampel</b>: /ampel /ampeldebug /ampelauto status|on|off /ampel_min_trades &lt;n&gt;\n"
            "🧪 <b>Kompatibilität</b>: /setprofile &lt;growth|scalping|defensive|hf75&gt;, /testtrade, /testtrades\n"
            "🛠 <b>Ops</b>: /repair /unlock /safemode /ops /setmaster &lt;key&gt; &lt;value&gt;\n"
            "🤖 <b>Supervisor</b>: /botstart /botstop /botrestart /botstatus\n"
            "🧠 Alle Kernbefehle lesen echte Runtime-, Brain-, Risk- und Trade-Daten."
        )

    def _send_legacy_ampel_help(self, chat_id: str) -> None:
        self._send_text(
            chat_id,
            "🚦 <b>Ampel-Kommandos (Legacy-Bridge)</b>\n"
            "In dieser Version läuft Ampel über das neue Ops/Risk-Panel:\n"
            "• <code>/ampel</code> → Summary + Risk-Lage\n"
            "• <code>/ampeldebug</code> → Recovery + Market + Master-Status\n"
            "• <code>/ampelauto status</code> → Master-Status\n"
            "• <code>/ampelauto on|off</code> → /setmaster enabled true|false\n"
            "Tipp: Nutze zusätzlich <code>/ops</code> für Reparaturbefehle."
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

    def _handle_setrisk(self, chat_id: str, text: str) -> None:
        parts = text.split()
        if len(parts) < 3:
            self._send_text(
                chat_id,
                "Verwendung: /setrisk <key> <value>\n"
                "Keys: max_positions, daily_loss_limit_pct, coin_cooldown_minutes, strategy_cooldown_minutes, duplicate_signal_minutes"
            )
            return
        key = parts[1].strip().lower()
        value_raw = parts[2].strip()
        mapping = {
            "max_positions": ("max_positions_total", int, 1, 50),
            "daily_loss_limit_pct": ("daily_loss_limit_pct", float, 0.1, 100.0),
            "coin_cooldown_minutes": ("coin_cooldown_minutes", int, 0, 240),
            "strategy_cooldown_minutes": ("strategy_cooldown_minutes", int, 0, 240),
            "duplicate_signal_minutes": ("duplicate_signal_minutes", int, 0, 240),
        }
        if key not in mapping:
            self._send_text(chat_id, f"Unbekannter setrisk-Key: {key}")
            return
        runtime_key, caster, lo, hi = mapping[key]
        try:
            val = caster(value_raw)
        except Exception:
            self._send_text(chat_id, f"Ungültiger Wert für {key}: {value_raw}")
            return
        if not (lo <= val <= hi):
            self._send_text(chat_id, f"Wert außerhalb Bereich [{lo}, {hi}] für {key}")
            return
        if self._callbacks.apply_runtime_settings:
            ok, msg = self._callbacks.apply_runtime_settings({runtime_key: val})
            self._send_text(chat_id, f"{'✅' if ok else '⚠️'} {msg}")
            return
        self._send_text(chat_id, "⚠️ Runtime-Tuning-Callback nicht angebunden.")

    def _handle_setbrain(self, chat_id: str, text: str) -> None:
        parts = text.split()
        if len(parts) < 3:
            self._send_text(
                chat_id,
                "Verwendung: /setbrain <key> <value>\n"
                "Keys: min_score, risky_score, perf_weight, reward_weight, reward_window, bitter_threshold"
            )
            return
        key = parts[1].strip().lower()
        value_raw = parts[2].strip()
        mapping = {
            "min_score": ("brain_min_score_to_trade", float, 0.05, 1.0),
            "risky_score": ("brain_risky_phase_score", float, 0.05, 1.0),
            "perf_weight": ("perf_selector_weight", float, 0.0, 1.0),
            "reward_weight": ("brain_reward_weight", float, 0.0, 1.0),
            "reward_window": ("brain_reward_window", int, 2, 80),
            "bitter_threshold": ("brain_bitter_treat_block_threshold", float, -1.0, 0.0),
        }
        if key not in mapping:
            self._send_text(chat_id, f"Unbekannter setbrain-Key: {key}")
            return
        runtime_key, caster, lo, hi = mapping[key]
        try:
            val = caster(value_raw)
        except Exception:
            self._send_text(chat_id, f"Ungültiger Wert für {key}: {value_raw}")
            return
        if not (lo <= val <= hi):
            self._send_text(chat_id, f"Wert außerhalb Bereich [{lo}, {hi}] für {key}")
            return
        if self._callbacks.apply_runtime_settings:
            ok, msg = self._callbacks.apply_runtime_settings({runtime_key: val})
            self._send_text(chat_id, f"{'✅' if ok else '⚠️'} {msg}")
            return
        self._send_text(chat_id, "⚠️ Runtime-Tuning-Callback nicht angebunden.")

    def _send_config(self, chat_id: str) -> None:
        text = (
            "⚙️ <b>Runtime-Konfiguration</b>\n"
            f"MAX_POSITIONS_TOTAL: <code>{getattr(settings, 'MAX_POSITIONS_TOTAL', 'n/a')}</code>\n"
            f"MAX_OPEN_TRADES: <code>{getattr(settings, 'MAX_OPEN_TRADES', 'n/a')}</code>\n"
            f"DAILY_LOSS_LIMIT_PCT: <code>{getattr(settings, 'DAILY_LOSS_LIMIT_PCT', 'n/a')}</code>\n"
            f"BRAIN_MIN_SCORE_TO_TRADE: <code>{getattr(settings, 'BRAIN_MIN_SCORE_TO_TRADE', 'n/a')}</code>\n"
            f"BRAIN_RISKY_PHASE_SCORE: <code>{getattr(settings, 'BRAIN_RISKY_PHASE_SCORE', 'n/a')}</code>\n"
            f"BRAIN_REWARD_WEIGHT: <code>{getattr(settings, 'BRAIN_REWARD_WEIGHT', 'n/a')}</code>\n"
            f"BRAIN_REWARD_WINDOW: <code>{getattr(settings, 'BRAIN_REWARD_WINDOW', 'n/a')}</code>\n"
            f"BRAIN_BITTER_TREAT_BLOCK_THRESHOLD: <code>{getattr(settings, 'BRAIN_BITTER_TREAT_BLOCK_THRESHOLD', 'n/a')}</code>"
        )
        self._send_text(chat_id, text)

    def _send_brain(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        brain = rt.get("brain") or {}
        app_ctx = rt.get("app_context") or {}
        reflection = app_ctx.get("self_reflection") or {}
        ranking = list(brain.get("last_strategy_ranking") or [])
        lines = [
            "🧠 <b>Brain-Status</b>",
            f"Regime: <code>{brain.get('last_regime', 'n/a')}</code>",
            f"Score: <code>{brain.get('last_signal_score', 'n/a')}</code>",
            f"Decision: <code>{brain.get('last_decision_reason', 'n/a')}</code>",
            f"Risky: <code>{brain.get('risky_phase', 'n/a')}</code>",
        ]
        if reflection:
            lines.extend(
                [
                    f"Memory pattern: <code>{reflection.get('pattern', 'n/a')}</code>",
                    f"Memory score: <code>{reflection.get('severity_score', 0.0)}</code>",
                    f"Memory action: <code>{', '.join(reflection.get('repair_actions', []) or ['none'])}</code>",
                ]
            )
        if ranking:
            top = ranking[0]
            lines.append(
                f"Top: <code>{top.get('strategy')} {top.get('side')} score={top.get('brain_score')}</code>"
            )
        self._send_text(chat_id, "\n".join(lines))

    def _send_markets(self, chat_id: str) -> None:
        if not self._callbacks.get_market_status:
            self._send_text(chat_id, "⚠️ Market-Status Callback nicht angebunden.")
            return
        try:
            data = self._callbacks.get_market_status() or {}
        except Exception as e:
            logger.error("Market-Status Callback-Fehler: %s", e)
            self._send_text(chat_id, "⚠️ Konnte Market-Status nicht lesen.")
            return
        pairs = data.get("pairs") or []
        stale = data.get("stale_symbols") or []
        text = (
            "📈 <b>Market-Status</b>\n"
            f"Pairs aktiv: <code>{data.get('pair_count', len(pairs))}</code>\n"
            f"Open Positions: <code>{data.get('open_positions', 0)}</code>\n"
            f"Stale Symbols: <code>{len(stale)}</code>\n"
            f"Sample Pairs: <code>{', '.join(pairs[:8]) if pairs else 'n/a'}</code>"
        )
        self._send_text(chat_id, text)

    def _handle_autoheal(self, chat_id: str) -> None:
        if not self._callbacks.request_auto_heal:
            self._send_text(chat_id, "⚠️ Autoheal-Callback nicht angebunden.")
            return
        try:
            ok, msg = self._callbacks.request_auto_heal()
            self._send_text(chat_id, f"{'✅' if ok else '⚠️'} {msg}")
        except Exception as e:
            logger.error("Autoheal-Callback-Fehler: %s", e)
            self._send_text(chat_id, "⚠️ Autoheal fehlgeschlagen.")

    def _send_master_status(self, chat_id: str) -> None:
        if not self._callbacks.get_master_status:
            self._send_text(chat_id, "⚠️ Master-Status Callback nicht angebunden.")
            return
        try:
            data = self._callbacks.get_master_status() or {}
        except Exception as e:
            logger.error("Master-Status Callback-Fehler: %s", e)
            self._send_text(chat_id, "⚠️ Konnte Master-Status nicht lesen.")
            return
        text = (
            "🧠 <b>Master-Status</b>\n"
            f"Enabled: <code>{data.get('enabled', False)}</code>\n"
            f"Min Trades: <code>{data.get('min_trades', 'n/a')}</code>\n"
            f"Target Winrate: <code>{data.get('target_winrate_pct', 'n/a')}%</code>\n"
            f"Last Winrate: <code>{data.get('last_winrate_pct', 'n/a')}%</code>\n"
            f"Consecutive Fails: <code>{data.get('consecutive_fail_windows', 0)}</code>\n"
            f"Auto Pause: <code>{data.get('auto_paused', False)}</code>\n"
            f"Cadence Level: <code>{data.get('cadence_level', 0)}</code> | "
            f"Entries today: <code>{data.get('entries_today', 0)}/{data.get('target_trades_per_day', 'n/a')}</code>\n"
            f"Cadence Override until: <code>{data.get('cadence_override_until', 'n/a')}</code>\n"
            f"Last Reason: <code>{data.get('last_reason', 'n/a')}</code>\n"
            f"Last Snapshot: <code>{data.get('last_snapshot_file', 'n/a')}</code>"
        )
        self._send_text(chat_id, text)

    def _send_ampel(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        gate = rt.get("risk_gate") or {}
        brain = rt.get("brain") or {}
        stale_count = 0
        try:
            stale_count = len((self._callbacks.get_market_status() or {}).get("stale_symbols") or [])
        except Exception:
            stale_count = 0

        paused = bool(rt.get("paused", False))
        risk_off = bool(rt.get("risk_off", False))
        risky = bool(brain.get("risky_phase", False))
        gate_reason = str(gate.get("last_gate_reason", "n/a"))
        open_pos = int(gate.get("open_positions", rt.get("open_positions", 0)) or 0)
        max_pos = int(gate.get("max_open_positions", getattr(settings, "MAX_POSITIONS_TOTAL", 0)) or 0)

        if paused or risk_off or stale_count > 0:
            state = "🔴 RED"
            reason = "Pause/RiskOff oder stale Daten aktiv"
        elif "DAILY LOSS" in gate_reason.upper() or "MAX TRADES" in gate_reason.upper():
            state = "🔴 RED"
            reason = gate_reason
        elif risky or (open_pos >= max_pos and max_pos > 0):
            state = "🟡 YELLOW"
            reason = "Risky-Phase oder Positionslimit erreicht"
        else:
            state = "🟢 GREEN"
            reason = "Bedingungen für neue Entries sind grundsätzlich ok"

        self._send_text(
            chat_id,
            "🚦 <b>Ampel</b>\n"
            f"State: <code>{state}</code>\n"
            f"Reason: <code>{reason}</code>\n"
            f"Gate: <code>{gate_reason}</code>\n"
            f"Paused/RiskOff: <code>{paused}/{risk_off}</code>\n"
            f"Open: <code>{open_pos}/{max_pos}</code>\n"
            f"Brain risky: <code>{risky}</code>\n"
            f"Stale symbols: <code>{stale_count}</code>",
        )

    def _send_ampel_debug(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        gate = rt.get("risk_gate") or {}
        brain = rt.get("brain") or {}
        master = {}
        if self._callbacks.get_master_status:
            try:
                master = self._callbacks.get_master_status() or {}
            except Exception:
                master = {}
        markets = {}
        if self._callbacks.get_market_status:
            try:
                markets = self._callbacks.get_market_status() or {}
            except Exception:
                markets = {}
        self._send_text(
            chat_id,
            "🚦 <b>Ampel Debug</b>\n"
            f"Gate last: <code>{gate.get('last_gate_reason', 'n/a')}</code>\n"
            f"Startup OK: <code>{gate.get('recovery_startup_ok', 'n/a')}</code>\n"
            f"Blocked symbols: <code>{len(gate.get('recovery_blocked_symbols', []) or [])}</code>\n"
            f"Paused/RiskOff: <code>{rt.get('paused', False)}/{rt.get('risk_off', False)}</code>\n"
            f"Brain score/risky: <code>{brain.get('last_signal_score', 'n/a')}/{brain.get('risky_phase', 'n/a')}</code>\n"
            f"Master enabled: <code>{master.get('enabled', 'n/a')}</code> | "
            f"winrate: <code>{master.get('last_winrate_pct', 'n/a')}%</code>\n"
            f"Cadence level: <code>{master.get('cadence_level', 0)}</code> | "
            f"entries: <code>{master.get('entries_today', 0)}/{master.get('target_trades_per_day', 'n/a')}</code>\n"
            f"Cadence override until: <code>{master.get('cadence_override_until', 'n/a')}</code>\n"
            f"Master reason: <code>{master.get('last_reason', 'n/a')}</code>\n"
            f"Stale symbols: <code>{len(markets.get('stale_symbols', []) or [])}</code>",
        )

    def _handle_ampelauto(self, chat_id: str, text: str) -> None:
        parts = text.split()
        action = parts[1].strip().lower() if len(parts) > 1 else "status"
        if action == "status":
            self._send_master_status(chat_id)
            return
        if action not in {"on", "off"}:
            self._send_legacy_ampel_help(chat_id)
            return
        if not self._callbacks.apply_runtime_settings:
            self._send_text(chat_id, "⚠️ Runtime-Tuning-Callback nicht angebunden.")
            return
        target = action == "on"
        ok, msg = self._callbacks.apply_runtime_settings({"master_brain_enabled": target})
        self._send_text(chat_id, f"{'✅' if ok else '⚠️'} AMPEL_AUTO enabled={target} | {msg}")

    def _handle_ampel_min_trades(self, chat_id: str, text: str) -> None:
        parts = text.split()
        if len(parts) < 2:
            self._send_text(chat_id, "Verwendung: /ampel_min_trades <anzahl>")
            return
        try:
            val = int(parts[1].strip())
        except Exception:
            self._send_text(chat_id, f"Ungültige Zahl: {parts[1].strip()}")
            return
        if not self._callbacks.apply_runtime_settings:
            self._send_text(chat_id, "⚠️ Runtime-Tuning-Callback nicht angebunden.")
            return
        ok, msg = self._callbacks.apply_runtime_settings({"master_brain_min_trades": val})
        self._send_text(chat_id, f"{'✅' if ok else '⚠️'} {msg}")

    def _handle_setprofile(self, chat_id: str, text: str) -> None:
        parts = text.split()
        if len(parts) < 2:
            self._send_text(chat_id, "Verwendung: /setprofile <growth|scalping|defensive|hf75>")
            return
        profile = parts[1].strip().lower()
        presets = {
            "growth": {
                "daily_loss_limit_pct": 8.0,
                "coin_cooldown_minutes": 6,
                "strategy_cooldown_minutes": 3,
                "duplicate_signal_minutes": 2,
                "brain_min_score_to_trade": 0.42,
                "brain_risky_phase_score": 0.32,
                "brain_reward_weight": 0.20,
                "brain_reward_window": 24,
                "master_brain_target_winrate_pct": 70.0,
            },
            "scalping": {
                "daily_loss_limit_pct": 9.0,
                "coin_cooldown_minutes": 2,
                "strategy_cooldown_minutes": 1,
                "duplicate_signal_minutes": 1,
                "brain_min_score_to_trade": 0.34,
                "brain_risky_phase_score": 0.26,
                "brain_reward_weight": 0.22,
                "brain_reward_window": 16,
                "master_brain_target_winrate_pct": 70.0,
            },
            "defensive": {
                "daily_loss_limit_pct": 5.0,
                "coin_cooldown_minutes": 15,
                "strategy_cooldown_minutes": 8,
                "duplicate_signal_minutes": 6,
                "brain_min_score_to_trade": 0.52,
                "brain_risky_phase_score": 0.40,
                "brain_reward_weight": 0.16,
                "brain_reward_window": 28,
                "master_brain_target_winrate_pct": 70.0,
            },
            "hf75": {
                "daily_loss_limit_pct": 10.0,
                "coin_cooldown_minutes": 3,
                "strategy_cooldown_minutes": 2,
                "duplicate_signal_minutes": 1,
                "brain_min_score_to_trade": 0.30,
                "brain_risky_phase_score": 0.24,
                "brain_reward_weight": 0.24,
                "brain_reward_window": 14,
                "master_brain_target_winrate_pct": 75.0,
                "master_brain_fail_windows": 3,
            },
        }
        if profile not in presets:
            self._send_text(chat_id, f"Unbekanntes Profil: {profile}")
            return
        if not self._callbacks.apply_runtime_settings:
            self._send_text(chat_id, "⚠️ Runtime-Tuning-Callback nicht angebunden.")
            return
        ok, msg = self._callbacks.apply_runtime_settings(presets[profile])
        self._send_text(chat_id, f"{'✅' if ok else '⚠️'} Profil <code>{profile}</code> gesetzt.\n{msg}")

    def _handle_testtrade(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        self._send_text(
            chat_id,
            "🧪 <b>Testtrade-Bridge</b>\n"
            "Direkter Testtrade ist in diesem Build nicht als harte Market-Order verdrahtet.\n"
            "Stattdessen Schnellcheck:\n"
            f"• running: <code>{rt.get('running', False)}</code>\n"
            f"• paused/riskoff: <code>{rt.get('paused', False)}/{rt.get('risk_off', False)}</code>\n"
            "Nutze /unlock und danach /markets + /ampeldebug für sofortige Diagnostik.",
        )

    def _handle_unlock(self, chat_id: str) -> None:
        runtime_control.resume_entries()
        runtime_control.disable_risk_off()
        runtime_state.update_engine(paused=False, risk_off=False)
        runtime_state.append_log("TELEGRAM /unlock -> pause+riskoff aufgehoben")
        self._send_text(chat_id, "✅ Unlock ausgeführt: Pause/Risk-Off aufgehoben.")

    def _handle_safemode(self, chat_id: str) -> None:
        runtime_control.pause_entries()
        runtime_control.enable_risk_off()
        runtime_state.update_engine(paused=True, risk_off=True)
        runtime_state.append_log("TELEGRAM /safemode -> pause+riskoff gesetzt")
        self._send_text(chat_id, "🛡 SafeMode aktiv: Neue Entries pausiert und Risk-Off gesetzt.")

    def _send_recovery(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        gate = rt.get("risk_gate") or {}
        text = (
            "🧯 <b>Recovery-Status</b>\n"
            f"Startup OK: <code>{gate.get('recovery_startup_ok', 'n/a')}</code>\n"
            f"Startup Reason: <code>{gate.get('recovery_startup_reason', 'n/a')}</code>\n"
            f"Blocked Symbols: <code>{len(gate.get('recovery_blocked_symbols', []) or [])}</code>\n"
            f"Paused/RiskOff: <code>{rt.get('paused', False)}/{rt.get('risk_off', False)}</code>\n"
            f"Last Gate: <code>{gate.get('last_gate_reason', 'n/a')}</code>"
        )
        self._send_text(chat_id, text)

    def _handle_snapshot(self, chat_id: str) -> None:
        if not self._callbacks.request_auto_heal:
            self._send_text(chat_id, "⚠️ Snapshot/Autoheal Callback nicht angebunden.")
            return
        ok, msg = self._callbacks.request_auto_heal()
        self._send_text(
            chat_id,
            f"{'✅' if ok else '⚠️'} Snapshot-Heal ausgeführt.\n<code>{msg}</code>",
        )

    def _handle_setmaster(self, chat_id: str, text: str) -> None:
        parts = text.split()
        if len(parts) < 3:
            self._send_text(
                chat_id,
                "Verwendung: /setmaster <key> <value>\n"
                "Keys: enabled, min_trades, target_winrate, fail_windows, auto_pause",
            )
            return
        key = parts[1].strip().lower()
        value_raw = parts[2].strip()
        mapping = {
            "enabled": ("master_brain_enabled", lambda x: str(x).strip().lower() in {"1", "true", "yes", "on"}),
            "min_trades": ("master_brain_min_trades", int),
            "target_winrate": ("master_brain_target_winrate_pct", float),
            "fail_windows": ("master_brain_fail_windows", int),
            "auto_pause": ("master_brain_auto_pause", lambda x: str(x).strip().lower() in {"1", "true", "yes", "on"}),
        }
        if key not in mapping:
            self._send_text(chat_id, f"Unbekannter master-Key: {key}")
            return
        runtime_key, caster = mapping[key]
        try:
            val = caster(value_raw)
        except Exception:
            self._send_text(chat_id, f"Ungültiger Wert für {key}: {value_raw}")
            return
        if self._callbacks.apply_runtime_settings:
            ok, msg = self._callbacks.apply_runtime_settings({runtime_key: val})
            self._send_text(chat_id, f"{'✅' if ok else '⚠️'} {msg}")
            return
        self._send_text(chat_id, "⚠️ Runtime-Tuning-Callback nicht angebunden.")

    def _send_ops(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        self._send_text(
            chat_id,
            "🛠 <b>Ops-Panel</b>\n"
            "• /repair oder /autoheal: automatische Reparatur + Snapshot\n"
            "• /unlock: Pause/Risk-Off aufheben\n"
            "• /safemode: Pause/Risk-Off setzen\n"
            "• /recovery: Startup-/Recovery-Blocker anzeigen\n"
            "• /snapshot: sofort Snapshot+Heal triggern\n"
            "• /setrisk max_positions 8\n"
            "• /setmaster target_winrate 55\n"
            f"Aktuell pause/riskoff: <code>{rt.get('paused', False)}/{rt.get('risk_off', False)}</code>",
        )

    def _send_legacy_info(self, chat_id: str) -> None:
        self._send_text(
            chat_id,
            "ℹ️ <b>Legacy-Befehl erkannt</b>\n"
            "Dieser Befehl wurde in der aktuellen Version nicht mehr als eigener Command geführt.\n"
            "Nutze bitte stattdessen:\n"
            "• /ops (Repair/Unlock/SafeMode)\n"
            "• /masterstatus und /setmaster ...\n"
            "• /brain, /setbrain ...\n"
            "• /risk, /setrisk ...\n"
            "Wenn du einen ganz bestimmten alten Befehl 1:1 zurück willst, sende den exakten Namen."
        )


