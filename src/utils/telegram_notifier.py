"""
Telegram Notification Service

Sendet formatierte Bot-Ereignisse an einen Telegram-Chat.
Benötigt TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID in .env.

Verhalten:
- Wenn Token oder Chat-ID fehlen: vollständig inaktiv (kein Crash)
- Telegram-Fehler loggen, aber nie den Main-Loop unterbrechen
- Rate-Limit: max 20 Nachrichten/Minute (Telegram erlaubt ~30/s)
- Nur Signale ab TELEGRAM_MIN_CONFIDENCE erhalten Benachrichtigungen
- Block-Benachrichtigungen nur für kritische Gründe (kein Cooldown-Spam)
"""

import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List

import requests

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("telegram")

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_REQUEST_TIMEOUT: int = 8       # Sekunden bis Timeout
_MAX_MSGS_PER_MIN: int = 20     # Telegram-API-Schutz

# Block-Gründe, die eine Telegram-Meldung auslösen (Cooldowns nicht)
_IMPORTANT_BLOCK_PREFIXES = ("DAILY LOSS", "MAX TRADES")

# Mindestabstand zwischen Block-Meldungen desselben Typs (verhindert Spam
# wenn Daily-Limit über viele Zyklen/Paare hinweg aktiv bleibt)
_BLOCK_NOTIFY_COOLDOWN_S: int = 30 * 60  # 30 Minuten
_DAILY_LOSS_NOTIFY_KEY: str = "daily_loss_limit"


class TelegramNotifier:
    """
    Verschickt strukturierte HTML-Nachrichten über die Telegram Bot-API.

    Verwendung:
        tg = TelegramNotifier()
        tg.notify_bot_start(...)
        tg.notify_trade_opened(...)
    """

    def __init__(self):
        def _masked(token: str) -> str:
            if not token:
                return "missing"
            if len(token) <= 8:
                return "***"
            return f"{token[:4]}...{token[-4:]}"

        self.enabled: bool = bool(
            settings.TELEGRAM_ENABLED
            and settings.TELEGRAM_BOT_TOKEN
            and settings.TELEGRAM_CHAT_ID
        )
        self._url = _API_URL.format(token=settings.TELEGRAM_BOT_TOKEN)
        self._chat_id = settings.TELEGRAM_CHAT_ID
        self._min_conf = settings.TELEGRAM_MIN_CONFIDENCE
        self._notify_level = settings.TELEGRAM_NOTIFY_LEVEL
        self._error_cooldown = settings.TELEGRAM_ERROR_ALERT_COOLDOWN_SEC

        # Timestamps der letzten Sends für Rate-Limiting
        self._send_times: deque = deque(maxlen=_MAX_MSGS_PER_MIN)

        # Cooldown-Tracker für Block-Benachrichtigungen (verhindert Spam)
        self._last_block_notify: Dict[str, float] = {}
        self._last_error_notify: Dict[str, float] = {}

        token_state = _masked(settings.TELEGRAM_BOT_TOKEN)
        chat_state = "set" if bool(settings.TELEGRAM_CHAT_ID) else "missing"
        logger.info(
            "Telegram-Notifier Init | enabled=%s | token=%s | chat_id=%s | notify_level=%s",
            self.enabled,
            token_state,
            chat_state,
            self._notify_level,
        )

    # ------------------------------------------------------------------
    # Internes Senden
    # ------------------------------------------------------------------

    def _should_notify(self, category: str) -> bool:
        """
        category:
          - critical: harte Risk-/Safety-Events
          - trading:  Trade-Events
          - runtime:  Betriebs-/Control-Events
          - error:    Fehler/Exceptions
        """
        level = (self._notify_level or "trading").lower()
        if level == "off":
            return False
        if level == "critical":
            return category in ("critical", "error")
        if level == "trading":
            return category in ("critical", "error", "trading")
        return True  # all

    def _is_rate_limited(self) -> bool:
        now = time.monotonic()
        while self._send_times and now - self._send_times[0] > 60:
            self._send_times.popleft()
        return len(self._send_times) >= _MAX_MSGS_PER_MIN

    def send(self, text: str) -> bool:
        """
        Sendet eine HTML-Nachricht. Gibt True bei Erfolg zurück.
        Alle Fehler werden geloggt, nie re-raised.
        Das Token erscheint niemals im Log.
        """
        if not self.enabled:
            return False

        if self._is_rate_limited():
            logger.warning("Telegram: Rate-Limit erreicht – Nachricht übersprungen")
            return False

        try:
            resp = requests.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                self._send_times.append(time.monotonic())
                return True

            # Telegram gibt manchmal 429 (Too Many Requests) zurück
            logger.warning(
                f"Telegram: HTTP {resp.status_code} – Nachricht nicht gesendet"
            )
            return False

        except requests.exceptions.Timeout:
            logger.warning(
                f"Telegram: Timeout nach {_REQUEST_TIMEOUT}s – Nachricht übersprungen"
            )
            return False
        except requests.exceptions.ConnectionError:
            logger.warning("Telegram: Keine Verbindung – Nachricht übersprungen")
            return False
        except Exception as e:
            # Token NICHT im Klartext loggen
            logger.warning(f"Telegram: Sendefehler ({type(e).__name__}) – übersprungen")
            return False

    # ------------------------------------------------------------------
    # Hilfsmethode: Zeitstempel
    # ------------------------------------------------------------------

    @staticmethod
    def _ts() -> str:
        return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    # ------------------------------------------------------------------
    # Formatierte Benachrichtigungstypen
    # ------------------------------------------------------------------

    def notify_bot_start(
        self,
        mode: str,
        strategy: str,
        pairs: List[str],
        timeframe: str,
    ) -> None:
        """Bot-Start-Meldung."""
        if not self._should_notify("runtime"):
            return
        mode_icon = "📄" if mode == "paper" else "🔴"
        text = (
            f"🤖 <b>KRYPTO-BOT ORIGINALS gestartet</b>\n"
            f"{mode_icon} Modus: <b>{mode.upper()}</b>\n"
            f"📊 Strategie: {strategy}\n"
            f"💱 Paare: {', '.join(pairs)}\n"
            f"⏱ Zeitrahmen: {timeframe}\n"
            f"🕐 {self._ts()}"
        )
        self.send(text)

    def notify_bot_stop(
        self,
        balance: float,
        total_pnl: float,
        total_trades: int,
        winrate: float,
    ) -> None:
        """Bot-Stop-Meldung mit Abschluss-Statistik."""
        if not self._should_notify("runtime"):
            return
        pnl_icon = "📈" if total_pnl >= 0 else "📉"
        text = (
            f"🛑 <b>Bot gestoppt</b>\n"
            f"💰 Balance: <b>{balance:.2f} USDT</b>\n"
            f"{pnl_icon} Gesamt-PnL: <b>{total_pnl:+.4f} USDT</b>\n"
            f"📊 Trades: {total_trades} | Win-Rate: {winrate:.1f}%\n"
            f"🕐 {self._ts()}"
        )
        self.send(text)

    def notify_trade_opened(
        self,
        symbol: str,
        side: str,
        entry: float,
        sl: float,
        tp: float,
        rr: float,
        amount: float,
        strategy: str,
        confidence: float,
        regime: str,
        is_paper: bool,
    ) -> None:
        """Trade eröffnet – nur wenn confidence >= TELEGRAM_MIN_CONFIDENCE."""
        if not self._should_notify("trading"):
            return
        if confidence < self._min_conf:
            return

        arrow = "🟢 LONG" if side == "long" else "🔴 SHORT"
        paper_tag = " <i>[PAPER]</i>" if is_paper else ""
        text = (
            f"{arrow} <b>Trade eröffnet{paper_tag}</b>\n"
            f"💱 <b>{symbol}</b> | {strategy}\n"
            f"📍 Entry: <code>{entry:.4f}</code>\n"
            f"🛑 SL: <code>{sl:.4f}</code> | 🎯 TP: <code>{tp:.4f}</code>\n"
            f"⚖ RR: {rr:.2f} | Menge: {amount:.6f}\n"
            f"🔭 Regime: {regime} | Konfidenz: {confidence:.0f}/100\n"
            f"🕐 {self._ts()}"
        )
        self.send(text)

    def notify_trade_closed(
        self,
        symbol: str,
        side: str,
        entry: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        reason: str,
        strategy: str,
        is_paper: bool,
    ) -> None:
        """Position geschlossen mit PnL-Info."""
        if not self._should_notify("trading"):
            return
        result_icon = "✅" if pnl >= 0 else "❌"
        side_label = "LONG" if side == "long" else "SHORT"
        paper_tag = " <i>[PAPER]</i>" if is_paper else ""
        reason_upper = (reason or "").lower()
        trigger_prefix = ""
        if reason_upper == "stop_loss":
            trigger_prefix = "🛑 <b>Stop Loss ausgelöst</b>\n"
        elif reason_upper == "take_profit":
            trigger_prefix = "🎯 <b>Take Profit ausgelöst</b>\n"
        text = (
            f"{trigger_prefix}"
            f"{result_icon} <b>Trade geschlossen{paper_tag}</b>\n"
            f"💱 <b>{symbol}</b> [{side_label}] | {strategy}\n"
            f"📍 Entry: <code>{entry:.4f}</code> → Exit: <code>{exit_price:.4f}</code>\n"
            f"💰 PnL: <b>{pnl:+.4f} USDT ({pnl_pct:+.2f}%)</b>\n"
            f"📋 Grund: {reason}\n"
            f"🕐 {self._ts()}"
        )
        self.send(text)

    def notify_trade_blocked(
        self,
        symbol: str,
        strategy: str,
        side: str,
        reason: str,
    ) -> None:
        """
        Signal blockiert – nur bei kritischen Gründen (Daily-Loss, Max-Trades).
        Cooldowns und Duplikate werden nicht gesendet (zu viel Spam).
        Gleiche Block-Art wird max. 1× pro 30 Minuten gemeldet (Spam-Schutz).
        """
        if not self._should_notify("critical"):
            return
        matched = next(
            (p for p in _IMPORTANT_BLOCK_PREFIXES if reason.upper().startswith(p)),
            None,
        )
        if not matched:
            return

        # Spam-Schutz: gleicher Block-Typ nur einmal pro Cooldown-Fenster
        now = time.monotonic()
        if now - self._last_block_notify.get(matched, 0) < _BLOCK_NOTIFY_COOLDOWN_S:
            return
        self._last_block_notify[matched] = now

        text = (
            f"⛔ <b>Signal blockiert</b>\n"
            f"💱 {symbol} [{side.upper()}] | {strategy}\n"
            f"📋 {reason}\n"
            f"🕐 {self._ts()}"
        )
        self.send(text)

    def notify_daily_loss_limit(
        self,
        daily_loss_usdt: float,
        limit_usdt: float,
        mode: str = "paper",
    ) -> None:
        """
        Explizite Daily-Loss-Alarmmeldung mit Cooldown.
        Wird nur periodisch gesendet, um Spam bei dauerhaft aktivem Limit
        über viele Zyklen/Symbole zu verhindern.
        """
        if not self._should_notify("critical"):
            return
        now = time.monotonic()
        if now - self._last_block_notify.get(_DAILY_LOSS_NOTIFY_KEY, 0) < _BLOCK_NOTIFY_COOLDOWN_S:
            return
        self._last_block_notify[_DAILY_LOSS_NOTIFY_KEY] = now
        mode_tag = mode.upper()
        self.send(
            "🧯 <b>Tagesverlustlimit erreicht</b>\n"
            f"📄 Modus: <b>{mode_tag}</b>\n"
            f"📉 Daily Loss: <b>{daily_loss_usdt:.2f} USDT</b>\n"
            f"🛑 Limit: <b>{limit_usdt:.2f} USDT</b>\n"
            "Neue Entries bleiben blockiert, bis Risiko-Lage wieder freigegeben ist.\n"
            f"🕐 {self._ts()}"
        )

    def notify_error(self, context: str, message: str) -> None:
        """Fehler/Warnung aus dem Bot-Betrieb."""
        if not self._should_notify("error"):
            return
        key = (context or "generic").split(":")[0][:64]
        now = time.monotonic()
        if now - self._last_error_notify.get(key, 0) < self._error_cooldown:
            return
        self._last_error_notify[key] = now
        text = (
            f"⚠️ <b>Bot-Fehler</b>\n"
            f"📍 {context}\n"
            f"💬 {message[:200]}\n"
            f"🕐 {self._ts()}"
        )
        self.send(text)

    def notify_bot_paused(self, reason: str = "manuell") -> None:
        if not self._should_notify("runtime"):
            return
        self.send(
            "⏸️ <b>Bot pausiert</b>\n"
            f"📋 Grund: {reason}\n"
            f"🕐 {self._ts()}"
        )

    def notify_bot_resumed(self, reason: str = "manuell") -> None:
        if not self._should_notify("runtime"):
            return
        self.send(
            "▶️ <b>Bot fortgesetzt</b>\n"
            f"📋 Grund: {reason}\n"
            f"🕐 {self._ts()}"
        )

    def notify_risk_off(self, enabled: bool, reason: str = "") -> None:
        if not self._should_notify("critical"):
            return
        state = "AKTIV" if enabled else "DEAKTIVIERT"
        icon = "🛡️" if enabled else "🟢"
        extra = f"\n📋 {reason}" if reason else ""
        self.send(
            f"{icon} <b>Risk-Off {state}</b>{extra}\n"
            f"🕐 {self._ts()}"
        )

    def notify_strategy_changed(self, strategy: str) -> None:
        if not self._should_notify("runtime"):
            return
        self.send(
            "🧭 <b>Strategie-Priorität geändert</b>\n"
            f"📊 Neue Priorität: <code>{strategy}</code>\n"
            f"🕐 {self._ts()}"
        )
