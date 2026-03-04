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
from typing import List

import requests

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("telegram")

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_REQUEST_TIMEOUT: int = 8       # Sekunden bis Timeout
_MAX_MSGS_PER_MIN: int = 20     # Telegram-API-Schutz

# Block-Gründe, die eine Telegram-Meldung auslösen (Cooldowns nicht)
_IMPORTANT_BLOCK_PREFIXES = ("DAILY LOSS", "MAX TRADES")


class TelegramNotifier:
    """
    Verschickt strukturierte HTML-Nachrichten über die Telegram Bot-API.

    Verwendung:
        tg = TelegramNotifier()
        tg.notify_bot_start(...)
        tg.notify_trade_opened(...)
    """

    def __init__(self):
        self.enabled: bool = bool(
            settings.TELEGRAM_ENABLED
            and settings.TELEGRAM_BOT_TOKEN
            and settings.TELEGRAM_CHAT_ID
        )
        self._url = _API_URL.format(token=settings.TELEGRAM_BOT_TOKEN)
        self._chat_id = settings.TELEGRAM_CHAT_ID
        self._min_conf = settings.TELEGRAM_MIN_CONFIDENCE

        # Timestamps der letzten Sends für Rate-Limiting
        self._send_times: deque = deque(maxlen=_MAX_MSGS_PER_MIN)

        if self.enabled:
            logger.info("Telegram-Benachrichtigungen aktiv")
        else:
            logger.debug(
                "Telegram inaktiv – TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nicht gesetzt"
            )

    # ------------------------------------------------------------------
    # Internes Senden
    # ------------------------------------------------------------------

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
        result_icon = "✅" if pnl >= 0 else "❌"
        side_label = "LONG" if side == "long" else "SHORT"
        paper_tag = " <i>[PAPER]</i>" if is_paper else ""
        text = (
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
        """
        if not any(reason.upper().startswith(p) for p in _IMPORTANT_BLOCK_PREFIXES):
            return

        text = (
            f"⛔ <b>Signal blockiert</b>\n"
            f"💱 {symbol} [{side.upper()}] | {strategy}\n"
            f"📋 {reason}\n"
            f"🕐 {self._ts()}"
        )
        self.send(text)

    def notify_error(self, context: str, message: str) -> None:
        """Fehler/Warnung aus dem Bot-Betrieb."""
        text = (
            f"⚠️ <b>Bot-Fehler</b>\n"
            f"📍 {context}\n"
            f"💬 {message[:200]}\n"
            f"🕐 {self._ts()}"
        )
        self.send(text)
