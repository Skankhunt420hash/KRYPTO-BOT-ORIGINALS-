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
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple, Any, List

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
    "liquiditysweepreversal": "LiquiditySweepReversal",
    "liquidity_sweep_reversal": "LiquiditySweepReversal",
    "emareclaimbreakout": "EMAReclaimBreakout",
    "ema_reclaim_breakout": "EMAReclaimBreakout",
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
    request_test_trade: Optional[Callable[[], Tuple[bool, str]]] = None


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
        self._autoheal_enabled: bool = False
        self._autoheal_cooldown_sec: int = int(
            getattr(settings, "TELEGRAM_AUTOHEAL_COOLDOWN_SEC", 900)
        )
        self._last_autoheal_ts: float = 0.0
        self._ampel_auto_enabled: bool = bool(getattr(settings, "AMPEL_AUTO_ENABLED", True))
        self._ampel_auto_interval_sec: int = int(
            getattr(settings, "AMPEL_AUTO_INTERVAL_SEC", 180)
        )
        self._last_ampel_auto_ts: float = 0.0
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
                self._safe_ampel_auto_tick()

            except requests.exceptions.Timeout:
                # normal bei Long-Polling – einfach weiter
                self._safe_ampel_auto_tick()
                continue
            except Exception as e:
                self._poll_fail_streak += 1
                logger.error(
                    f"Telegram-Panel Polling-Fehler ({type(e).__name__}): {e}"
                )
                time.sleep(self._poll_interval)

        logger.info("Telegram-Control-Panel Polling-Loop beendet.")

    def _safe_ampel_auto_tick(self) -> None:
        """
        Ampel-Auto darf den Polling-Thread niemals beenden.
        Alle Fehler werden geloggt und geschluckt.
        """
        try:
            self._maybe_run_ampel_auto()
        except Exception as e:
            logger.error("AmpelAuto Tick-Fehler (%s): %s", type(e).__name__, e)
            runtime_state.append_log(f"AMPEL_AUTO_ERROR {type(e).__name__}")

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
        parts = self._split_command_parts(text)
        if not parts:
            return
        cmd = parts[0]

        try:
            if cmd == "start":
                self._send_start(chat_id)
            elif cmd == "help":
                self._send_help(chat_id)
            elif cmd == "status":
                self._send_status(chat_id)
            elif cmd == "diag":
                self._send_diag(chat_id)
            elif cmd == "diagfull":
                self._send_diagfull(chat_id)
            elif cmd == "autoheal":
                self._handle_autoheal(chat_id, text)
            elif cmd == "ampel":
                self._send_ampel(chat_id)
            elif cmd == "ampelauto":
                self._handle_ampelauto(chat_id, text)
            elif cmd in ("testtrade", "testtrades"):
                self._handle_testtrade(chat_id, text)
            elif cmd == "mode":
                self._handle_mode_command(chat_id, text)
            elif cmd == "strategy":
                self._send_strategy(chat_id)
            elif cmd == "risk":
                self._handle_risk_command(chat_id, text)
            elif cmd == "positions":
                self._send_positions(chat_id)
            elif cmd == "trades":
                self._send_trades(chat_id)
            elif cmd == "balance":
                self._send_balance(chat_id)
            elif cmd == "logs":
                self._send_logs(chat_id)
            elif cmd == "summary":
                self._send_summary(chat_id)
            elif cmd == "analysis":
                self._send_analysis(chat_id)
            elif cmd == "brain":
                self._send_brain(chat_id)
            elif cmd == "config":
                self._send_config(chat_id)
            elif cmd == "pause":
                self._handle_pause(chat_id)
            elif cmd == "resume":
                self._handle_resume(chat_id)
            elif cmd == "riskoff":
                self._handle_riskoff(chat_id)
            elif cmd == "riskon":
                self._handle_riskon(chat_id)
            elif cmd == "killswitch":
                self._handle_killswitch_on(chat_id)
            elif cmd == "killswitchoff":
                self._handle_killswitch_off(chat_id)
            elif cmd in ("setmode", "mode_set"):
                self._handle_setmode(chat_id, text)
            elif cmd == "setstrategy":
                self._handle_setstrategy(chat_id, text)
            elif cmd == "setbrain":
                self._handle_setbrain(chat_id, text)
            elif cmd in ("setrisk", "riskset", "risktune", "risktuning"):
                self._handle_setrisk(chat_id, text)
            elif cmd in ("setprofile", "profile"):
                self._handle_setprofile(chat_id, text)
            elif cmd == "profiles":
                self._send_profiles(chat_id)
            elif cmd in ("stop_bot", "stopbot"):
                self._handle_stop_bot(chat_id)
            elif cmd in ("start_bot", "startbot"):
                self._handle_start_bot(chat_id)
            elif cmd in ("botstart", "start"):
                self._handle_bot_start(chat_id)
            elif cmd in ("botstop", "stop"):
                self._handle_bot_stop(chat_id)
            elif cmd in ("botrestart", "restartbot", "restart"):
                self._handle_bot_restart(chat_id)
            elif cmd == "botstatus":
                self._send_bot_status(chat_id)
            else:
                self._send_text(
                    chat_id,
                    "Unbekannter Befehl. Nutze /help.\n"
                    "Tipp: /setprofile defensive, /setrisk daily_loss_limit_pct 10, /ampel, /botrestart",
                )
        except Exception as e:
            logger.error(f"Telegram-Panel Dispatch-Fehler ({cmd}): {e}")
            self._send_text(chat_id, "Interner Fehler im Telegram-Panel. Siehe Logs.")

    # ------------------------------------------------------------------
    # Command-Parsing Helper
    # ------------------------------------------------------------------

    def _handle_mode_command(self, chat_id: str, text: str) -> None:
        """
        /mode
          -> zeigt aktuellen Modus
        /mode paper
          -> alias für /setmode paper
        /mode growth|continuous|defensive|balanced|aggressive|sniper|scalping
          -> alias für /setprofile <name>
        """
        parts = self._split_command_parts(text)
        if len(parts) <= 1:
            self._send_mode(chat_id)
            return
        target = parts[1].strip().lower()
        if target in self._profile_presets():
            self._handle_setprofile(chat_id, f"/setprofile {target}")
            return
        if target == "paper":
            self._handle_setmode(chat_id, "/setmode paper")
            return
        self._send_text(
            chat_id,
            "Verwendung:\n"
            "• /mode (Status)\n"
            "• /mode paper\n"
            "• /mode <growth|continuous|defensive|balanced|aggressive|sniper|scalping>",
        )

    def _handle_risk_command(self, chat_id: str, text: str) -> None:
        """
        /risk
          -> zeigt Risk-Status
        /risk <key> <value>
          -> alias für /setrisk <key> <value>
        /risk set|tune|tuning|live <key> <value>
          -> alias für /setrisk <key> <value>
        """
        parts = self._split_command_parts(text)
        if len(parts) <= 1:
            self._send_risk(chat_id)
            return

        marker = parts[1].strip().lower()
        if marker in ("set", "tune", "tuning", "live", "update"):
            key_idx = 2
            if len(parts) > key_idx and parts[key_idx].strip().lower() in (
                "set",
                "tune",
                "tuning",
                "live",
                "update",
            ):
                key_idx += 1
            if len(parts) <= key_idx + 1:
                self._send_text(
                    chat_id,
                    "Verwendung: /risk <key> <value>\n"
                    "oder: /risk live <key> <value>\n"
                    "Keys: risk_per_trade, max_open_risk, max_positions, "
                    "max_notional, min_notional, daily_loss_limit_pct, live_daily_loss_limit_pct",
                )
                return
            self._handle_setrisk(chat_id, f"/setrisk {parts[key_idx]} {parts[key_idx + 1]}")
            return

        if len(parts) < 3:
            self._send_text(
                chat_id,
                "Verwendung: /risk <key> <value>\n"
                "Keys: risk_per_trade, max_open_risk, max_positions, "
                "max_notional, min_notional, daily_loss_limit_pct, live_daily_loss_limit_pct",
            )
            return
        self._handle_setrisk(chat_id, f"/setrisk {parts[1]} {parts[2]}")

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

    @staticmethod
    def _strip_command_token(token: str) -> str:
        cleaned = token.strip().lower()
        cleaned = cleaned.strip("()[]{}")
        if "@" in cleaned:
            cleaned = cleaned.split("@", 1)[0]
        return cleaned.lstrip("/")

    def _split_command_parts(self, text: str) -> List[str]:
        cleaned_text = (text or "").replace("\u200b", " ").replace("\ufeff", " ").strip()
        parts = cleaned_text.split()
        if not parts:
            return []
        first = self._strip_command_token(parts[0])
        return [first] + [part.strip() for part in parts[1:]]

    def _send_help(self, chat_id: str) -> None:
        self._send_text(
            chat_id,
            "<b>KRYPTO-BOT Control Center</b>\n"
            "📖 <b>Lesend</b>: /status /diag /diagfull /summary /analysis /brain /config /balance /positions /trades /risk /strategy /mode /logs\n"
            "🎛 <b>Steuerung</b>: /pause /resume /riskoff /riskon /killswitch /killswitchoff /testtrade\n"
            "⚙ <b>Optional</b>: /setstrategy &lt;name&gt;, /setmode paper, /setbrain &lt;key&gt; &lt;value&gt;, /setrisk &lt;key&gt; &lt;value&gt;\n"
            "🩹 <b>Auto-Heal</b>: /autoheal status | /autoheal on | /autoheal off | /autoheal now\n"
            "🚦 <b>Ampel</b>: /ampel | /ampelauto status | /ampelauto on | /ampelauto off | /ampelauto now\n"
            "🎚 <b>Profile</b>: /profiles, /setprofile &lt;growth|continuous|defensive|balanced|aggressive|sniper|scalping|hf75|highfreq75&gt;\n"
            "🤖 <b>Supervisor</b>: /botstart /botstop /botrestart /botstatus\n"
            "ℹ️ /botrestart nutzt Callback oder automatisch Stop+Start-Fallback.\n"
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

    def _send_analysis(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        brain = rt.get("brain") or {}
        selector = rt.get("selector") or {}
        gate = rt.get("risk_gate") or {}
        last_signal = rt.get("last_signal") or {}
        last_decision = rt.get("last_decision") or {}
        ranking = list(brain.get("last_strategy_ranking") or [])

        lines = [
            "🧠 <b>Luxus Analyse</b>",
            f"Regime: <code>{brain.get('last_regime', 'n/a')}</code>",
            f"Brain-Score: <code>{brain.get('last_signal_score', 'n/a')}</code> | "
            f"Risky: <code>{brain.get('risky_phase', 'n/a')}</code>",
            f"Decision: <code>{brain.get('last_decision_reason', 'n/a')}</code>",
            f"Selector Winner: <code>{selector.get('winner') or 'none'}</code> | "
            f"Score: <code>{selector.get('winner_score', 'n/a')}</code> | "
            f"Eligible: <code>{selector.get('eligible', 'n/a')}</code>",
            f"Risk-Gate: <code>{gate.get('last_gate_reason', 'n/a')}</code>",
        ]

        if last_signal:
            lines.append(
                "Letztes Signal: "
                f"<code>{last_signal.get('symbol', 'n/a')} {last_signal.get('side', 'n/a')} "
                f"{last_signal.get('strategy', 'n/a')}</code> | "
                f"conf={last_signal.get('confidence', 'n/a')} rr={last_signal.get('rr', 'n/a')}"
            )
        if last_decision:
            lines.append(
                "Letzte Entscheidung: "
                f"<code>{last_decision.get('decision', 'n/a')}</code> | "
                f"{last_decision.get('strategy', 'n/a')} | "
                f"{last_decision.get('reason', 'n/a')}"
            )

        if ranking:
            lines.append("")
            lines.append("<b>Top 5 Ranking</b>")
            for i, item in enumerate(ranking[:5], start=1):
                comps = item.get("components") or {}
                lines.append(
                    f"{i}. {item.get('strategy')} [{item.get('side')}] "
                    f"score={item.get('brain_score')} elig={item.get('eligible')} "
                    f"reward={comps.get('reward_bias', 'n/a')} perf={comps.get('perf_score', 'n/a')}"
                )

        self._send_text(chat_id, "\n".join(lines))

    def _send_brain(self, chat_id: str) -> None:
        rt = self._safe_runtime_status()
        brain = rt.get("brain") or {}
        ranking = list(brain.get("last_strategy_ranking") or [])
        lines = [
            "🧠 <b>Brain Deep-Dive</b>",
            f"Regime: <code>{brain.get('last_regime', 'n/a')}</code>",
            f"Last Score: <code>{brain.get('last_signal_score', 'n/a')}</code>",
            f"Decision: <code>{brain.get('last_decision_reason', 'n/a')}</code>",
            f"Risky Phase: <code>{brain.get('risky_phase', 'n/a')}</code>",
            f"Min Trade Score: <code>{brain.get('min_trade_score', 'n/a')}</code>",
            f"Reward Weight: <code>{getattr(settings, 'BRAIN_REWARD_WEIGHT', 'n/a')}</code>",
            f"Perf Weight: <code>{settings.PERF_SELECTOR_WEIGHT}</code>",
            f"Priority Bonus: <code>{settings.CONTROL_STRATEGY_PRIORITY_BONUS}</code>",
        ]
        if ranking:
            lines.append("")
            lines.append("<b>Ranking-Details</b>")
            for i, item in enumerate(ranking[:6], start=1):
                comps = item.get("components") or {}
                lines.append(
                    f"{i}) {item.get('strategy')} {item.get('side')} "
                    f"s={item.get('brain_score')} e={item.get('eligible')} "
                    f"| trend={comps.get('trend_quality')} rr={comps.get('rr_quality')} "
                    f"perf={comps.get('perf_score')} rew={comps.get('reward_bias')}"
                )
        self._send_text(chat_id, "\n".join(lines))

    def _send_config(self, chat_id: str) -> None:
        lines = [
            "⚙️ <b>Aktive Runtime-Konfiguration</b>",
            f"MIN_CONFIDENCE: <code>{settings.MIN_CONFIDENCE}</code>",
            f"MIN_RR: <code>{settings.MIN_RR}</code>",
            f"BRAIN_MIN_SCORE_TO_TRADE: <code>{settings.BRAIN_MIN_SCORE_TO_TRADE}</code>",
            f"BRAIN_RISKY_PHASE_SCORE: <code>{settings.BRAIN_RISKY_PHASE_SCORE}</code>",
            f"PERF_SELECTOR_WEIGHT: <code>{settings.PERF_SELECTOR_WEIGHT}</code>",
            f"CONTROL_STRATEGY_PRIORITY_BONUS: <code>{settings.CONTROL_STRATEGY_PRIORITY_BONUS}</code>",
            f"BRAIN_REWARD_WEIGHT: <code>{getattr(settings, 'BRAIN_REWARD_WEIGHT', 'n/a')}</code>",
            f"BRAIN_REWARD_WINDOW: <code>{getattr(settings, 'BRAIN_REWARD_WINDOW', 'n/a')}</code>",
            f"BRAIN_POSITIVE_PATTERN_ENABLED: <code>{getattr(settings, 'BRAIN_POSITIVE_PATTERN_ENABLED', 'n/a')}</code>",
            f"BRAIN_POSITIVE_PATTERN_WINDOW: <code>{getattr(settings, 'BRAIN_POSITIVE_PATTERN_WINDOW', 'n/a')}</code>",
            f"BRAIN_POSITIVE_PATTERN_MIN_TRADES: <code>{getattr(settings, 'BRAIN_POSITIVE_PATTERN_MIN_TRADES', 'n/a')}</code>",
            f"BRAIN_POSITIVE_PATTERN_MIN_WINRATE_PCT: <code>{getattr(settings, 'BRAIN_POSITIVE_PATTERN_MIN_WINRATE_PCT', 'n/a')}</code>",
            f"BRAIN_POSITIVE_PATTERN_BONUS_WEIGHT: <code>{getattr(settings, 'BRAIN_POSITIVE_PATTERN_BONUS_WEIGHT', 'n/a')}</code>",
            f"RISK_PER_TRADE_PCT: <code>{settings.RISK_PER_TRADE_PCT}</code>",
            f"MAX_TOTAL_OPEN_RISK_PCT: <code>{settings.MAX_TOTAL_OPEN_RISK_PCT}</code>",
            f"MAX_POSITIONS_TOTAL: <code>{settings.MAX_POSITIONS_TOTAL}</code>",
            f"MAX_POSITION_NOTIONAL: <code>{settings.MAX_POSITION_NOTIONAL}</code>",
            "",
            "<i>Setzen via: /setbrain KEY VALUE oder /setrisk KEY VALUE</i>",
        ]
        self._send_text(chat_id, "\n".join(lines))

    def _handle_setbrain(self, chat_id: str, text: str) -> None:
        parts = self._split_command_parts(text)
        if len(parts) < 3:
            self._send_text(
                chat_id,
                "Verwendung: /setbrain <key> <value>\n"
                "Keys: min_score, risky_score, perf_weight, priority_bonus, reward_weight, reward_window, pattern_enabled, pattern_window, pattern_min_trades, pattern_min_winrate, pattern_bonus, min_confidence, min_rr, min_win_chance, min_historical_wr, perf_min_trades, min_expectancy, min_recency_wr, min_profit_factor, max_losing_streak, weak_phase_scale"
            )
            return
        key = parts[1].strip().lower()
        value_raw = parts[2].strip()
        mapping = {
            "min_score": ("brain_min_score_to_trade", float, 0.0, 1.5),
            "risky_score": ("brain_risky_phase_score", float, 0.0, 1.5),
            "perf_weight": ("perf_selector_weight", float, 0.0, 1.0),
            "priority_bonus": ("control_strategy_priority_bonus", float, 0.0, 1.0),
            "reward_weight": ("reward_weight", float, 0.0, 0.5),
            "reward_window": ("reward_window", int, 2, 50),
            "pattern_enabled": ("brain_positive_pattern_enabled", int, 0, 1),
            "pattern_window": ("brain_positive_pattern_window", int, 10, 300),
            "pattern_min_trades": ("brain_positive_pattern_min_trades", int, 3, 300),
            "pattern_min_winrate": ("brain_positive_pattern_min_winrate_pct", float, 40.0, 95.0),
            "pattern_bonus": ("brain_positive_pattern_bonus_weight", float, 0.0, 0.30),
            "min_confidence": ("min_confidence", float, 0.0, 100.0),
            "min_rr": ("min_rr", float, 0.0, 10.0),
            "min_win_chance": ("min_win_chance_pct", float, 0.0, 100.0),
            "min_historical_wr": ("min_historical_win_rate_pct", float, 0.0, 100.0),
            "perf_min_trades": ("perf_tracker_min_trades", int, 1, 500),
            "min_expectancy": ("min_expectancy_pct", float, -100.0, 100.0),
            "min_recency_wr": ("min_recency_win_rate_pct", float, 0.0, 100.0),
            "min_profit_factor": ("min_profit_factor", float, 0.0, 20.0),
            "max_losing_streak": ("max_losing_streak_to_trade", int, 0, 20),
            "weak_phase_scale": ("weak_phase_position_scale", float, 0.1, 1.0),
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
            if ok:
                self._send_text(chat_id, f"🧠 {msg}")
            else:
                self._send_text(chat_id, f"⚠️ {msg}")
            return
        self._send_text(chat_id, "⚠️ Runtime-Tuning-Callback nicht angebunden.")

    def _handle_setrisk(self, chat_id: str, text: str) -> None:
        parts = self._split_command_parts(text)
        if len(parts) < 3:
            self._send_text(
                chat_id,
                "Verwendung: /setrisk <key> <value>\n"
                "Keys: risk_per_trade, max_open_risk, max_positions, "
                "max_notional, min_notional, daily_loss_limit_pct, live_daily_loss_limit_pct, coin_cooldown_minutes, strategy_cooldown_minutes, duplicate_signal_minutes"
            )
            return
        key = parts[1].strip().lower()
        value_raw = parts[2].strip()
        mapping = {
            "risk_per_trade": ("risk_per_trade_pct", float, 0.1, 10.0),
            "max_open_risk": ("max_total_open_risk_pct", float, 1.0, 50.0),
            "max_positions": ("max_positions_total", int, 1, 50),
            "max_notional": ("max_position_notional", float, 5.0, 1_000_000.0),
            "min_notional": ("min_position_notional", float, 1.0, 100_000.0),
            "daily_loss_limit_pct": ("daily_loss_limit_pct", float, 0.1, 100.0),
            "daily_loss_pct": ("daily_loss_limit_pct", float, 0.1, 100.0),
            "daily_loss": ("daily_loss_limit_pct", float, 0.1, 100.0),
            "daily_limit_pct": ("daily_loss_limit_pct", float, 0.1, 100.0),
            "live_daily_loss_limit_pct": ("live_test_daily_loss_limit_pct", float, 0.1, 100.0),
            "coin_cooldown_minutes": ("coin_cooldown_minutes", int, 0, 240),
            "strategy_cooldown_minutes": ("strategy_cooldown_minutes", int, 0, 240),
            "duplicate_signal_minutes": ("duplicate_signal_minutes", int, 0, 180),
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
            if ok:
                self._send_text(chat_id, f"🛡 {msg}")
            else:
                self._send_text(chat_id, f"⚠️ {msg}")
            return
        self._send_text(chat_id, "⚠️ Runtime-Tuning-Callback nicht angebunden.")

    @staticmethod
    def _profile_presets() -> Dict[str, Dict[str, float]]:
        return {
            "defensive": {
                "min_confidence": 58.0,
                "min_rr": 2.0,
                "brain_min_score_to_trade": 0.56,
                "brain_risky_phase_score": 0.45,
                "perf_selector_weight": 0.30,
                "reward_weight": 0.05,
                "risk_per_trade_pct": 0.6,
                "max_total_open_risk_pct": 5.0,
                "max_positions_total": 3,
            },
            "balanced": {
                "min_confidence": 46.0,
                "min_rr": 1.6,
                "brain_min_score_to_trade": 0.48,
                "brain_risky_phase_score": 0.36,
                "perf_selector_weight": 0.24,
                "reward_weight": 0.08,
                "risk_per_trade_pct": 0.9,
                "max_total_open_risk_pct": 9.0,
                "max_positions_total": 5,
            },
            "aggressive": {
                "min_confidence": 36.0,
                "min_rr": 1.3,
                "brain_min_score_to_trade": 0.38,
                "brain_risky_phase_score": 0.28,
                "perf_selector_weight": 0.18,
                "reward_weight": 0.12,
                "risk_per_trade_pct": 1.3,
                "max_total_open_risk_pct": 14.0,
                "max_positions_total": 7,
            },
            "sniper": {
                "min_confidence": 62.0,
                "min_rr": 2.4,
                "brain_min_score_to_trade": 0.60,
                "brain_risky_phase_score": 0.48,
                "perf_selector_weight": 0.32,
                "reward_weight": 0.06,
                "risk_per_trade_pct": 0.7,
                "max_total_open_risk_pct": 6.0,
                "max_positions_total": 2,
            },
            "scalping": {
                "min_confidence": 34.0,
                "min_rr": 1.2,
                "brain_min_score_to_trade": 0.34,
                "brain_risky_phase_score": 0.24,
                "perf_selector_weight": 0.14,
                "reward_weight": 0.15,
                "risk_per_trade_pct": 0.8,
                "max_total_open_risk_pct": 12.0,
                "max_positions_total": 8,
            },
            "growth": {
                # Kontinuierliches Wachstum statt schneller Peak:
                # konservativeres Risiko, strengere Qualitätsgates, moderates Pattern-Lernen.
                "min_confidence": 52.0,
                "min_rr": 1.8,
                "brain_min_score_to_trade": 0.52,
                "brain_risky_phase_score": 0.40,
                "perf_selector_weight": 0.28,
                "reward_weight": 0.07,
                "risk_per_trade_pct": 0.45,
                "max_total_open_risk_pct": 4.5,
                "max_positions_total": 3,
                "coin_cooldown_minutes": 5,
                "strategy_cooldown_minutes": 6,
                "duplicate_signal_minutes": 4,
                "min_win_chance_pct": 72.0,
                "min_historical_win_rate_pct": 25.0,
                "perf_tracker_min_trades": 25,
                "min_expectancy_pct": 8.0,
                "min_recency_win_rate_pct": 62.0,
                "min_profit_factor": 1.18,
                "max_losing_streak_to_trade": 1,
                "weak_phase_position_scale": 0.50,
                "brain_positive_pattern_enabled": 1,
                "brain_positive_pattern_window": 50,
                "brain_positive_pattern_min_trades": 10,
                "brain_positive_pattern_min_winrate_pct": 58.0,
                "brain_positive_pattern_bonus_weight": 0.06,
            },
            "continuous": {
                "min_confidence": 52.0,
                "min_rr": 1.8,
                "brain_min_score_to_trade": 0.52,
                "brain_risky_phase_score": 0.40,
                "perf_selector_weight": 0.28,
                "reward_weight": 0.07,
                "risk_per_trade_pct": 0.45,
                "max_total_open_risk_pct": 4.5,
                "max_positions_total": 3,
                "coin_cooldown_minutes": 5,
                "strategy_cooldown_minutes": 6,
                "duplicate_signal_minutes": 4,
                "min_win_chance_pct": 72.0,
                "min_historical_win_rate_pct": 25.0,
                "perf_tracker_min_trades": 25,
                "min_expectancy_pct": 8.0,
                "min_recency_win_rate_pct": 62.0,
                "min_profit_factor": 1.18,
                "max_losing_streak_to_trade": 1,
                "weak_phase_position_scale": 0.50,
                "brain_positive_pattern_enabled": 1,
                "brain_positive_pattern_window": 50,
                "brain_positive_pattern_min_trades": 10,
                "brain_positive_pattern_min_winrate_pct": 58.0,
                "brain_positive_pattern_bonus_weight": 0.06,
            },
            "hf75": {
                # Ziel: mehr Entries bei großer Universe-Scanrate, aber 75%-Qualitätsgate
                # + positives Erwartungswert-Gate (mathematischer Vorteil je Trade).
                "min_confidence": 32.0,
                "min_rr": 1.15,
                "brain_min_score_to_trade": 0.24,
                "brain_risky_phase_score": 0.18,
                "perf_selector_weight": 0.10,
                "reward_weight": 0.12,
                "risk_per_trade_pct": 0.7,
                "max_total_open_risk_pct": 12.0,
                "max_positions_total": 10,
                "coin_cooldown_minutes": 2,
                "strategy_cooldown_minutes": 1,
                "duplicate_signal_minutes": 1,
                "min_win_chance_pct": 75.0,
                "min_historical_win_rate_pct": 0.0,
                "perf_tracker_min_trades": 12,
                "min_expectancy_pct": 5.0,
                "min_recency_win_rate_pct": 60.0,
                "min_profit_factor": 1.05,
                "max_losing_streak_to_trade": 2,
                "weak_phase_position_scale": 0.7,
            },
            "highfreq75": {
                "min_confidence": 32.0,
                "min_rr": 1.15,
                "brain_min_score_to_trade": 0.24,
                "brain_risky_phase_score": 0.18,
                "perf_selector_weight": 0.10,
                "reward_weight": 0.12,
                "risk_per_trade_pct": 0.7,
                "max_total_open_risk_pct": 12.0,
                "max_positions_total": 10,
                "coin_cooldown_minutes": 2,
                "strategy_cooldown_minutes": 1,
                "duplicate_signal_minutes": 1,
                "min_win_chance_pct": 75.0,
                "min_historical_win_rate_pct": 0.0,
                "perf_tracker_min_trades": 12,
                "min_expectancy_pct": 5.0,
                "min_recency_win_rate_pct": 60.0,
                "min_profit_factor": 1.05,
                "max_losing_streak_to_trade": 2,
                "weak_phase_position_scale": 0.7,
            },
        }

    def _send_profiles(self, chat_id: str) -> None:
        presets = self._profile_presets()
        lines = [
            "🎚 <b>Luxus Profile</b>",
            "Wähle per <code>/setprofile &lt;name&gt;</code>:",
            "Neu: <code>growth</code> / <code>continuous</code> = stabiles, kontinuierliches Wachstum",
            "Neu: <code>hf75</code> / <code>highfreq75</code> = mehr Entries + 75% Qualitätsgate",
            "",
        ]
        for name, values in presets.items():
            lines.append(
                f"• <b>{name}</b> → "
                f"risk={values['risk_per_trade_pct']}% | "
                f"open_risk={values['max_total_open_risk_pct']}% | "
                f"min_conf={values['min_confidence']} | "
                f"min_rr={values['min_rr']}"
            )
        lines.append("")
        lines.append("Beispiel: <code>/setprofile defensive</code>")
        self._send_text(chat_id, "\n".join(lines))

    def _handle_setprofile(self, chat_id: str, text: str) -> None:
        parts = self._split_command_parts(text)
        if len(parts) < 2:
            self._send_text(
                chat_id,
                "Verwendung: /setprofile <growth|continuous|defensive|balanced|aggressive|sniper|scalping|hf75|highfreq75>\n"
                "Nutze /profiles für die Übersicht."
            )
            return
        name = parts[1].strip().lower()
        if name == "highfreq75":
            name = "hf75"
        if name in ("continuous_growth", "steady", "stable"):
            name = "growth"
        presets = self._profile_presets()
        payload = presets.get(name)
        if payload is None:
            self._send_text(chat_id, f"Unbekanntes Profil: {name}. Nutze /profiles.")
            return
        if not self._callbacks.apply_runtime_settings:
            self._send_text(chat_id, "⚠️ Runtime-Tuning-Callback nicht angebunden.")
            return
        ok, msg = self._callbacks.apply_runtime_settings(payload)
        if ok:
            runtime_state.append_log(f"TELEGRAM /setprofile {name}")
            self._send_text(
                chat_id,
                f"✅ Profil <b>{name}</b> aktiviert.\n{msg}\n\n"
                "Kontrolle mit /config und /analysis."
            )
        else:
            self._send_text(chat_id, f"⚠️ Profil konnte nicht gesetzt werden: {msg}")

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
            parts.append(
                f"• Brain Config: minScore={float(getattr(settings, 'BRAIN_MIN_SCORE_TO_TRADE', 0.45)):.3f} | "
                f"riskyPhase<{float(getattr(settings, 'BRAIN_RISKY_PHASE_SCORE', 0.35)):.3f} | "
                f"perfW={float(getattr(settings, 'PERF_SELECTOR_WEIGHT', 0.22)):.3f} | "
                f"rewardW={float(getattr(settings, 'BRAIN_REWARD_WEIGHT', 0.08)):.3f}"
            )
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

    def _send_diag(self, chat_id: str) -> None:
        """
        Kompakte Diagnose für "warum kein Trade?".
        """
        rt = self._safe_runtime_status()
        ctrl = runtime_control.get_snapshot()
        gate = rt.get("risk_gate") or {}
        selector = rt.get("selector") or {}
        brain = rt.get("brain") or {}
        last_decision = rt.get("last_decision") or {}
        app_ctx = rt.get("app_context") or {}

        startup_reason = (
            gate.get("recovery_startup_reason")
            or app_ctx.get("startup_block_reason")
            or "none"
        )
        lines = [
            "🧪 <b>Diag (No-Trade Debug)</b>",
            f"Running: <code>{rt.get('running', False)}</code> | Health: <code>{rt.get('health_status', 'n/a')}</code>",
            f"Pause/RiskOff: <code>{rt.get('paused', ctrl.get('paused'))}/{rt.get('risk_off', ctrl.get('risk_off'))}</code>",
            f"Startup-Gate: <code>{'OK' if startup_reason in ('', 'none', None) else 'BLOCKED'}</code>",
            f"Startup-Reason: <code>{startup_reason}</code>",
            f"Selector: total/actionable/eligible=<code>{selector.get('candidates_total', 'n/a')}/{selector.get('actionable', 'n/a')}/{selector.get('eligible', 'n/a')}</code>",
            f"Selector blocked: regime/perf=<code>{selector.get('blocked_regime', 'n/a')}/{selector.get('blocked_perf', 'n/a')}</code>",
            f"Brain: regime=<code>{brain.get('last_regime', 'n/a')}</code> score=<code>{brain.get('last_signal_score', 'n/a')}</code> risky=<code>{brain.get('risky_phase', 'n/a')}</code>",
            f"Risk-Gate last: <code>{gate.get('last_gate_reason', 'n/a')}</code>",
            f"Live-Gate last: <code>{gate.get('live_last_gate_reason', 'n/a')}</code>",
            f"Last Decision: <code>{last_decision.get('decision', 'n/a')}</code> | <code>{last_decision.get('reason', 'n/a')}</code>",
            f"Open Pos: <code>{rt.get('open_positions', len(rt.get('open_positions_detail') or []))}</code>",
        ]
        self._send_text(chat_id, "\n".join(lines))

    def _send_diagfull(self, chat_id: str) -> None:
        """
        Erweiterte Diagnose mit den letzten Block-/Skip-Ereignissen aus Runtime-Logs.
        """
        rt = self._safe_runtime_status()
        ctrl = runtime_control.get_snapshot()
        gate = rt.get("risk_gate") or {}
        selector = rt.get("selector") or {}
        brain = rt.get("brain") or {}
        last_decision = rt.get("last_decision") or {}
        app_ctx = rt.get("app_context") or {}
        runtime_logs = list(rt.get("recent_logs") or [])

        startup_reason = (
            gate.get("recovery_startup_reason")
            or app_ctx.get("startup_block_reason")
            or "none"
        )
        lines = [
            "🧪 <b>DiagFull (No-Trade Deep Debug)</b>",
            f"Running: <code>{rt.get('running', False)}</code> | Health: <code>{rt.get('health_status', 'n/a')}</code> | Mode: <code>{rt.get('mode', settings.TRADING_MODE)}</code>",
            f"Pause/RiskOff: <code>{rt.get('paused', ctrl.get('paused'))}/{rt.get('risk_off', ctrl.get('risk_off'))}</code>",
            f"Startup-Gate: <code>{'OK' if startup_reason in ('', 'none', None) else 'BLOCKED'}</code> | <code>{startup_reason}</code>",
            f"Selector regime=<code>{selector.get('regime', 'n/a')}</code> | total/actionable/eligible=<code>{selector.get('candidates_total', 'n/a')}/{selector.get('actionable', 'n/a')}/{selector.get('eligible', 'n/a')}</code>",
            f"Selector blocked regime/perf=<code>{selector.get('blocked_regime', 'n/a')}/{selector.get('blocked_perf', 'n/a')}</code> | winner=<code>{selector.get('winner') or 'none'}</code>",
            f"Brain regime=<code>{brain.get('last_regime', 'n/a')}</code> | score=<code>{brain.get('last_signal_score', 'n/a')}</code> | risky=<code>{brain.get('risky_phase', 'n/a')}</code>",
            f"Brain decision=<code>{brain.get('last_decision_reason', 'n/a')}</code>",
            f"Risk gate last=<code>{gate.get('last_gate_reason', 'n/a')}</code> | live=<code>{gate.get('live_last_gate_reason', 'n/a')}</code>",
            f"Last Decision: <code>{last_decision.get('decision', 'n/a')}</code> | <code>{last_decision.get('reason', 'n/a')}</code>",
            "",
            "<b>Letzte Block-/Skip-Events (max 5)</b>",
        ]
        interesting_markers = (
            "BLOCK",
            "blocked",
            "skip",
            "startup",
            "risk_off",
            "pause",
            "MIN_WIN_CHANCE",
            "selector_none",
            "brain_score_too_low",
            "brain_risky_phase_block",
            "LIVE_GATE",
            "DAILY LOSS",
        )
        filtered: List[str] = []
        for entry in reversed(runtime_logs):
            if any(marker in entry for marker in interesting_markers):
                filtered.append(entry)
            if len(filtered) >= 5:
                break
        if not filtered:
            lines.append("- <code>Keine Block-/Skip-Events im Runtime-Log gefunden.</code>")
        else:
            for item in filtered:
                lines.append(f"- <code>{item[:260]}</code>")
        self._send_text(chat_id, "\n".join(lines))

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

    @staticmethod
    def _ampel_profit_factor(pnls: List[float]) -> float:
        wins = sum(p for p in pnls if p > 0.0)
        losses = abs(sum(p for p in pnls if p <= 0.0))
        if losses <= 1e-12:
            return 99.0 if wins > 0 else 0.0
        return float(wins / losses)

    @staticmethod
    def _parse_db_timestamp(raw: Any) -> Optional[datetime]:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _collect_closed_metrics(self, lookback: int) -> Tuple[List[float], Optional[datetime]]:
        if not self._repo.available:
            return [], None
        rows = self._repo.get_recent_trades(limit=max(10, int(lookback) * 3), status="closed")
        pnls: List[float] = []
        latest_closed_at: Optional[datetime] = None
        for row in rows:
            value = row.get("pnl_abs")
            if value is None:
                continue
            try:
                pnls.append(float(value))
            except Exception:
                continue
            ts = self._parse_db_timestamp(
                row.get("timestamp_close") or row.get("updated_at") or row.get("created_at")
            )
            if ts is not None and (latest_closed_at is None or ts > latest_closed_at):
                latest_closed_at = ts
            if len(pnls) >= lookback:
                break
        return pnls, latest_closed_at

    def _compute_ampel(self, lookback: Optional[int] = None) -> Dict[str, Any]:
        lookback_n = int(lookback or int(getattr(settings, "AMPEL_WINDOW_TRADES", 50) or 50))
        lookback_n = max(10, min(lookback_n, 300))
        pnls, latest_closed_at = self._collect_closed_metrics(lookback_n)
        n = len(pnls)
        min_trades = int(getattr(settings, "AMPEL_MIN_TRADES", 20) or 20)
        stale_hours: Optional[float] = None
        if latest_closed_at is not None:
            stale_hours = max(
                0.0,
                (datetime.now(timezone.utc) - latest_closed_at).total_seconds() / 3600.0,
            )
        stale_limit_hours = float(getattr(settings, "AMPEL_STALE_DATA_HOURS", 8.0) or 0.0)
        stale_data = bool(
            stale_limit_hours > 0.0 and stale_hours is not None and stale_hours >= stale_limit_hours
        )
        stale_force_yellow = bool(getattr(settings, "AMPEL_STALE_FORCE_YELLOW", True))
        if n == 0:
            return {
                "state": "YELLOW",
                "emoji": "🟡",
                "reason": "no_closed_trades",
                "trades": 0,
                "lookback": lookback_n,
                "winrate": 0.0,
                "pf": 0.0,
                "avg_pnl": 0.0,
                "total_pnl": 0.0,
                "expectancy_r": 0.0,
                "losing_streak": 0,
                "insufficient_data": True,
                "stale_data": stale_data,
                "last_closed_trade_age_h": round(stale_hours, 2) if stale_hours is not None else None,
            }

        wins = [p for p in pnls if p > 0.0]
        losses = [p for p in pnls if p <= 0.0]
        winrate = round((len(wins) / n) * 100.0, 1)
        total_pnl = round(sum(pnls), 4)
        avg_pnl = round(total_pnl / max(1, n), 6)
        pf = round(self._ampel_profit_factor(pnls), 3)

        rr_ref = float(max(0.5, getattr(settings, "MIN_RR", 1.2) or 1.2))
        p = max(0.0, min(1.0, winrate / 100.0))
        expectancy_r = round((p * rr_ref) - (1.0 - p), 4)

        losing_streak = 0
        for v in pnls:
            if v <= 0.0:
                losing_streak += 1
            else:
                break

        green_wr = float(getattr(settings, "AMPEL_GREEN_WINRATE_PCT", 68.0) or 68.0)
        green_pf = float(getattr(settings, "AMPEL_GREEN_PF", 1.25) or 1.25)
        green_exp = float(getattr(settings, "AMPEL_GREEN_EXPECTANCY_R", 0.08) or 0.08)
        green_ls = int(getattr(settings, "AMPEL_GREEN_MAX_LOSING_STREAK", 2) or 2)

        red_wr = float(getattr(settings, "AMPEL_RED_WINRATE_PCT", 58.0) or 58.0)
        red_pf = float(getattr(settings, "AMPEL_RED_PF", 1.05) or 1.05)
        red_exp = float(getattr(settings, "AMPEL_RED_EXPECTANCY_R", 0.02) or 0.02)
        red_ls = int(getattr(settings, "AMPEL_RED_LOSING_STREAK", 5) or 5)

        if n < min_trades:
            state = "YELLOW"
            reason = f"insufficient_data:{n}/{min_trades}"
        else:
            is_green = (
                winrate >= green_wr
                and pf >= green_pf
                and avg_pnl > 0.0
                and expectancy_r >= green_exp
                and losing_streak <= green_ls
            )
            is_red = (
                winrate <= red_wr
                or pf <= red_pf
                or avg_pnl <= 0.0
                or expectancy_r <= red_exp
                or losing_streak >= red_ls
            )
            if is_green:
                state = "GREEN"
                reason = "metrics_green"
            elif is_red:
                state = "RED"
                reason = "metrics_red"
            else:
                state = "YELLOW"
                reason = "metrics_mixed"

        # Anti-Stall: Wenn die Datengrundlage alt ist, RED nicht hart erzwingen.
        # Sonst kann der Bot in einem dauerhaften "RED -> keine Entries -> keine neuen Closed-Trades"-Loop hängen.
        if state == "RED" and stale_data and stale_force_yellow:
            state = "YELLOW"
            reason = f"stale_data_force_yellow:{stale_hours:.1f}h"

        return {
            "state": state,
            "emoji": {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴"}.get(state, "⚪"),
            "reason": reason,
            "trades": n,
            "lookback": lookback_n,
            "winrate": winrate,
            "pf": pf,
            "avg_pnl": avg_pnl,
            "total_pnl": total_pnl,
            "expectancy_r": expectancy_r,
            "losing_streak": losing_streak,
            "insufficient_data": n < min_trades,
            "stale_data": stale_data,
            "last_closed_trade_age_h": round(stale_hours, 2) if stale_hours is not None else None,
        }

    def _apply_ampel_guard(self, ampel: Dict[str, Any], *, source: str) -> Tuple[bool, str]:
        state = str(ampel.get("state", "YELLOW")).upper()
        if state == "RED":
            runtime_control.pause_entries()
            runtime_control.enable_risk_off()
            runtime_state.update_engine(paused=True, risk_off=True)
            runtime_state.append_log(f"AMPEL {source} RED -> pause+risk_off")
            try:
                self._notifier.notify_bot_paused(f"ampel:{source}:red")
                self._notifier.notify_risk_off(True, f"ampel:{source}:red")
            except Exception:
                pass
            return True, "red_pause_risk_off"

        ctrl = runtime_control.get_snapshot()
        if bool(ctrl.get("paused")) or bool(ctrl.get("risk_off")):
            runtime_control.resume_entries()
            runtime_control.disable_risk_off()
            runtime_state.update_engine(paused=False, risk_off=False)
            runtime_state.append_log(f"AMPEL {source} {state} -> resume+risk_on")
            try:
                self._notifier.notify_bot_resumed(f"ampel:{source}:{state.lower()}")
                self._notifier.notify_risk_off(False, f"ampel:{source}:{state.lower()}")
            except Exception:
                pass
            return True, f"{state.lower()}_resume_risk_on"
        return False, f"{state.lower()}_no_change"

    def _format_ampel_text(self, ampel: Dict[str, Any], action: str) -> str:
        state = str(ampel.get("state", "YELLOW")).upper()
        action_txt = "Trading pausiert (ROT)" if state == "RED" else "Trading erlaubt"
        return (
            f"{ampel.get('emoji', '⚪')} <b>Ampel: {state}</b>\n"
            f"Action: <code>{action}</code> | Guard: <code>{action_txt}</code>\n"
            f"Trades: <code>{ampel.get('trades')}</code>/<code>{ampel.get('lookback')}</code>\n"
            f"DataAge(h): <code>{ampel.get('last_closed_trade_age_h')}</code> | "
            f"Stale: <code>{ampel.get('stale_data')}</code>\n"
            f"Winrate: <code>{ampel.get('winrate')}%</code> | PF: <code>{ampel.get('pf')}</code>\n"
            f"AvgPnL: <code>{ampel.get('avg_pnl')}</code> | TotalPnL: <code>{ampel.get('total_pnl')}</code>\n"
            f"Expectancy(R): <code>{ampel.get('expectancy_r')}</code> | "
            f"LosingStreak: <code>{ampel.get('losing_streak')}</code>\n"
            f"Reason: <code>{ampel.get('reason')}</code>"
        )

    def _send_ampel(self, chat_id: str) -> None:
        ampel = self._compute_ampel()
        changed, action = self._apply_ampel_guard(ampel, source="telegram_manual")
        if changed:
            self._last_ampel_auto_ts = time.monotonic()
        self._send_text(chat_id, self._format_ampel_text(ampel, action))

    def _maybe_run_ampel_auto(self, force: bool = False) -> None:
        if not self._ampel_auto_enabled and not force:
            return
        now = time.monotonic()
        if not force and (now - self._last_ampel_auto_ts < self._ampel_auto_interval_sec):
            return
        self._last_ampel_auto_ts = now
        ampel = self._compute_ampel()
        changed, action = self._apply_ampel_guard(ampel, source="ampel_auto")
        if changed and self._chat_id:
            try:
                self._send_text(
                    self._chat_id,
                    "🚦 <b>AmpelAuto Aktion</b>\n" + self._format_ampel_text(ampel, action),
                )
            except Exception:
                pass

    def _handle_ampelauto(self, chat_id: str, text: str) -> None:
        parts = self._split_command_parts(text)
        mode = parts[1].strip().lower() if len(parts) > 1 else "status"
        if mode in ("status", "state"):
            self._send_text(
                chat_id,
                "🚦 <b>AmpelAuto</b>\n"
                f"enabled=<code>{self._ampel_auto_enabled}</code>\n"
                f"interval=<code>{self._ampel_auto_interval_sec}s</code>",
            )
            return
        if mode in ("on", "enable", "1", "true"):
            self._ampel_auto_enabled = True
            runtime_state.append_log("TELEGRAM /ampelauto on")
            self._send_text(chat_id, "✅ AmpelAuto aktiviert.")
            return
        if mode in ("off", "disable", "0", "false"):
            self._ampel_auto_enabled = False
            runtime_state.append_log("TELEGRAM /ampelauto off")
            self._send_text(chat_id, "⏸ AmpelAuto deaktiviert.")
            return
        if mode in ("now", "run", "check"):
            self._last_ampel_auto_ts = 0.0
            self._maybe_run_ampel_auto(force=True)
            self._send_text(chat_id, "✅ AmpelAuto-Check ausgeführt.")
            return
        self._send_text(chat_id, "Verwendung: /ampelauto <on|off|status|now>")

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
            try:
                ok, msg = self._callbacks.request_bot_restart()
                self._send_text(chat_id, f"{'✅' if ok else '⚠️'} {msg}")
            except Exception as e:
                logger.error("Restart-Callback-Fehler: %s", e)
                self._send_text(chat_id, "⚠️ Restart-Callback fehlgeschlagen.")
            return

        # Fallback: wenn kein dedizierter Restart vorhanden ist, versuche stop+start.
        if self._callbacks.request_bot_stop and self._callbacks.request_bot_start:
            try:
                stop_result = self._callbacks.request_bot_stop()
                stop_msg = ""
                if isinstance(stop_result, tuple) and len(stop_result) == 2:
                    stop_ok, stop_txt = stop_result
                    stop_msg = f"Stop: {'ok' if stop_ok else 'warn'} ({stop_txt})"
                else:
                    stop_msg = "Stop: gesendet"

                start_ok, start_msg = self._callbacks.request_bot_start()
                icon = "✅" if start_ok else "⚠️"
                self._send_text(
                    chat_id,
                    f"{icon} Restart via Fallback (stop+start).\n{stop_msg}\nStart: {start_msg}",
                )
            except Exception as e:
                logger.error("Fallback-Restart (stop+start) fehlgeschlagen: %s", e)
                self._send_text(chat_id, "⚠️ Fallback-Restart (stop+start) fehlgeschlagen.")
            return

        self._send_text(
            chat_id,
            "⚠️ Restart nicht angebunden.\n"
            "Nutze /botstop + /botstart oder starte den Controller/Supervisor.",
        )

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

    def _autoheal_status_text(self) -> str:
        return (
            f"enabled={self._autoheal_enabled} "
            f"cooldown={self._autoheal_cooldown_sec}s "
            f"last_action_ts={int(self._last_autoheal_ts) if self._last_autoheal_ts else 0}"
        )

    def _can_autoheal_now(self) -> Tuple[bool, str]:
        now = time.monotonic()
        if now - self._last_autoheal_ts < self._autoheal_cooldown_sec:
            wait_left = int(self._autoheal_cooldown_sec - (now - self._last_autoheal_ts))
            return False, f"cooldown_active:{wait_left}s"

        if Path(settings.KILL_SWITCH_FILE).exists():
            return False, "kill_switch_active"

        ctrl = runtime_control.get_snapshot()
        if bool(ctrl.get("paused")):
            return True, "resume_entries"
        if bool(ctrl.get("risk_off")):
            return True, "disable_risk_off"
        return False, "nothing_to_heal"

    def _run_autoheal(self, source: str) -> Tuple[bool, str]:
        ok, action = self._can_autoheal_now()
        if not ok:
            return False, action

        if action == "resume_entries":
            runtime_control.resume_entries()
            runtime_state.update_engine(paused=False)
            runtime_state.append_log(f"AUTOHEAL resume_entries source={source}")
            try:
                self._notifier.notify_bot_resumed(f"autoheal:{source}")
            except Exception:
                pass
            self._last_autoheal_ts = time.monotonic()
            return True, "pause_aufgehoben"

        if action == "disable_risk_off":
            runtime_control.disable_risk_off()
            runtime_state.update_engine(risk_off=False)
            runtime_state.append_log(f"AUTOHEAL risk_on source={source}")
            try:
                self._notifier.notify_risk_off(False, f"autoheal:{source}")
            except Exception:
                pass
            self._last_autoheal_ts = time.monotonic()
            return True, "risk_off_deaktiviert"

        return False, "nothing_to_heal"

    def _handle_autoheal(self, chat_id: str, text: str) -> None:
        parts = self._split_command_parts(text)
        mode = parts[1].strip().lower() if len(parts) > 1 else "status"

        if mode in ("status", "state"):
            self._send_text(chat_id, f"🩹 AutoHeal: <code>{self._autoheal_status_text()}</code>")
            return

        if mode in ("on", "enable", "1", "true"):
            self._autoheal_enabled = True
            runtime_state.append_log("TELEGRAM /autoheal on")
            self._send_text(chat_id, f"✅ AutoHeal aktiviert.\n<code>{self._autoheal_status_text()}</code>")
            return

        if mode in ("off", "disable", "0", "false"):
            self._autoheal_enabled = False
            runtime_state.append_log("TELEGRAM /autoheal off")
            self._send_text(chat_id, f"⏸ AutoHeal deaktiviert.\n<code>{self._autoheal_status_text()}</code>")
            return

        if mode in ("now", "run", "heal"):
            ok, msg = self._run_autoheal("telegram_manual")
            if ok:
                self._send_text(chat_id, f"✅ AutoHeal ausgeführt: <code>{msg}</code>")
            else:
                self._send_text(chat_id, f"ℹ️ AutoHeal nicht ausgeführt: <code>{msg}</code>")
            return

        self._send_text(
            chat_id,
            "Verwendung: /autoheal <on|off|status|now>",
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
        parts = self._split_command_parts(text)
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
        parts = self._split_command_parts(text)
        if len(parts) < 2:
            self._send_text(
                chat_id,
                "Verwendung: /setstrategy <name>\n"
                "Beispiele: momentum_pullback, trend_continuation, range_reversion, "
                "volatility_breakout, liquidity_sweep_reversal, "
                "ema_reclaim_breakout, auto"
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

