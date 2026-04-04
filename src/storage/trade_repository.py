"""
Trade-Repository: einziger Ort wo Trades in SQLite geschrieben/gelesen werden.

Alle public Methoden fangen Exceptions intern ab und loggen sie –
ein DB-Fehler crasht niemals den Main-Loop.
"""

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from src.storage.database import init_db, get_connection
from src.utils.logger import setup_logger
from config.settings import settings

logger = setup_logger("trade_repository")

_IS_PAPER = settings.TRADING_MODE == "paper"


def _utcnow() -> str:
    """ISO-8601 UTC-Zeitstempel für DB-Felder."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class TradeRepository:
    """
    Persistiert Trades in SQLite. Alle Methoden sind idempotent und
    sicher bei DB-Ausfall (geben None/False zurück statt Exception).

    Verwendung:
        repo = TradeRepository()          # initialisiert DB beim Start
        if repo.available:                # optional prüfen
            trade_id = repo.save_open_trade(...)
            repo.close_trade(trade_id, ...)
    """

    def __init__(self):
        self.available: bool = init_db()
        if not self.available:
            logger.warning(
                "[yellow]Trade-Persistenz deaktiviert[/yellow] – "
                "DB-Initialisierung fehlgeschlagen. Bot läuft ohne Persistenz weiter."
            )

    # ------------------------------------------------------------------
    # Schreiben: Trade öffnen
    # ------------------------------------------------------------------

    def save_open_trade(
        self,
        symbol: str,
        timeframe: str,
        strategy_name: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        position_size: float,
        rr_planned: float,
        confidence: float,
        regime: str,
        reason_open: str,
        signal_score: Optional[float] = None,
        risk_state_at_entry: Optional[Dict] = None,
        order_id: str = "",
    ) -> Optional[int]:
        """
        Speichert einen neu eröffneten Trade. Gibt die DB-ID zurück (für späteres Update)
        oder None bei Fehler.
        """
        if not self.available:
            return None

        try:
            risk_amount = round(abs(entry_price - stop_loss) * position_size, 6)
            now = _utcnow()

            sql = """
                INSERT INTO trades (
                    timestamp_open, symbol, timeframe, strategy_name, side,
                    entry_price, stop_loss, take_profit, position_size,
                    risk_amount, rr_planned, confidence, signal_score, regime, risk_state_at_entry,
                    status, reason_open, paper_mode, order_id,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    'open', ?, ?, ?,
                    ?, ?
                )
            """
            risk_state_json = (
                json.dumps(risk_state_at_entry, ensure_ascii=True)
                if isinstance(risk_state_at_entry, dict)
                else None
            )
            params = (
                now, symbol, timeframe, strategy_name, side,
                entry_price, stop_loss, take_profit, position_size,
                risk_amount, rr_planned, confidence, signal_score, regime, risk_state_json,
                reason_open, int(_IS_PAPER), order_id or "",
                now, now,
            )

            conn = get_connection()
            if conn is None:
                return None
            try:
                cursor = conn.execute(sql, params)
                conn.commit()
                trade_id = cursor.lastrowid
                logger.info(
                    f"[green]DB OPEN[/green] trade_id={trade_id} | "
                    f"{symbol} | {strategy_name} | entry={entry_price:.4f}"
                )
                return trade_id
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"[red]DB-Fehler save_open_trade ({symbol}):[/red] {e}")
            return None

    # ------------------------------------------------------------------
    # Schreiben: Trade schließen
    # ------------------------------------------------------------------

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        pnl_abs: float,
        pnl_pct: float,
        reason_close: str,
    ) -> bool:
        """
        Aktualisiert einen bestehenden Trade-Eintrag auf 'closed'.
        Gibt True zurück wenn erfolgreich.
        """
        if not self.available:
            return False

        try:
            status = "closed"
            now = _utcnow()

            sql = """
                UPDATE trades
                SET timestamp_close = ?,
                    exit_price      = ?,
                    pnl_abs         = ?,
                    pnl_pct         = ?,
                    status          = ?,
                    reason_close    = ?,
                    exit_reason     = ?,
                    updated_at      = ?
                WHERE id = ?
            """
            params = (
                now,
                exit_price,
                pnl_abs,
                pnl_pct,
                status,
                reason_close,
                reason_close,
                now,
                trade_id,
            )

            conn = get_connection()
            if conn is None:
                return False
            try:
                cursor = conn.execute(sql, params)
                conn.commit()
                if cursor.rowcount == 0:
                    logger.warning(
                        f"close_trade: trade_id={trade_id} nicht in DB gefunden "
                        f"– kein Update durchgeführt"
                    )
                    return False
                color = "green" if pnl_abs >= 0 else "red"
                logger.info(
                    f"[{color}]DB CLOSE[/{color}] trade_id={trade_id} | "
                    f"exit={exit_price:.4f} | PnL={pnl_abs:+.4f} ({pnl_pct:+.2f}%)"
                )
                return True
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"[red]DB-Fehler close_trade (id={trade_id}):[/red] {e}")
            return False

    # ------------------------------------------------------------------
    # Schreiben: Blockiertes Signal speichern (optional, für Analyse)
    # ------------------------------------------------------------------

    def save_rejected_signal(
        self,
        symbol: str,
        timeframe: str,
        strategy_name: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        rr_planned: float,
        confidence: float,
        regime: str,
        reason_rejected: str,
    ) -> bool:
        """
        Speichert ein vom Risk-Engine blockiertes Signal als 'rejected'-Eintrag.
        Nützlich für Strategie-Analyse: welche Signale wurden wie oft blockiert?
        """
        if not self.available:
            return False

        try:
            # position_size=0 für rejected (kein Trade ausgeführt) → risk_amount=0.0
            risk_amount = 0.0
            now = _utcnow()

            sql = """
                INSERT INTO trades (
                    timestamp_open, symbol, timeframe, strategy_name, side,
                    entry_price, stop_loss, take_profit, position_size,
                    risk_amount, rr_planned, confidence, regime,
                    status, reason_open, paper_mode,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, 0,
                    ?, ?, ?, ?,
                    'rejected', ?, ?,
                    ?, ?
                )
            """
            params = (
                now, symbol, timeframe, strategy_name, side,
                entry_price, stop_loss, take_profit,
                risk_amount, rr_planned, confidence, regime,
                reason_rejected, int(_IS_PAPER),
                now, now,
            )

            conn = get_connection()
            if conn is None:
                return False
            try:
                conn.execute(sql, params)
                conn.commit()
                logger.debug(
                    f"DB REJECTED | {symbol} | {strategy_name} | {reason_rejected[:60]}"
                )
                return True
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"[red]DB-Fehler save_rejected_signal ({symbol}):[/red] {e}")
            return False

    # ------------------------------------------------------------------
    # Schreiben: Offenen Trade bei Bot-Stop als cancelled markieren
    # ------------------------------------------------------------------

    def cancel_open_trade(self, trade_id: int, reason: str = "bot_stopped") -> bool:
        """
        Markiert einen offenen Trade als 'cancelled' (z.B. beim Bot-Stop).
        Verhindert dauerhaft verbleibende status='open' Einträge nach Neustart.
        """
        if not self.available:
            return False

        try:
            now = _utcnow()
            sql = """
                UPDATE trades
                SET status     = 'cancelled',
                    reason_close = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'open'
            """
            conn = get_connection()
            if conn is None:
                return False
            try:
                cursor = conn.execute(sql, (reason, now, trade_id))
                conn.commit()
                if cursor.rowcount > 0:
                    logger.info(f"DB CANCEL trade_id={trade_id} | {reason}")
                return cursor.rowcount > 0
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"[red]DB-Fehler cancel_open_trade (id={trade_id}):[/red] {e}")
            return False

    # ------------------------------------------------------------------
    # Lesen: Letzte Trades abfragen
    # ------------------------------------------------------------------

    def get_recent_trades(
        self,
        limit: int = 20,
        status: str = None,
        current_mode_only: bool = True,
    ) -> List[dict]:
        """
        Gibt die letzten N Trades als Liste von Dicts zurück.
        Optional nach Status filtern: 'open', 'closed', 'rejected'.
        """
        if not self.available:
            return []

        try:
            if status and current_mode_only:
                sql = """
                    SELECT * FROM trades
                    WHERE status = ? AND paper_mode = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """
                params = (status, int(_IS_PAPER), limit)
            elif status:
                sql = """
                    SELECT * FROM trades
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """
                params = (status, limit)
            elif current_mode_only:
                sql = """
                    SELECT * FROM trades
                    WHERE paper_mode = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """
                params = (int(_IS_PAPER), limit)
            else:
                sql = "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?"
                params = (limit,)

            conn = get_connection()
            if conn is None:
                return []
            try:
                rows = conn.execute(sql, params).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"[red]DB-Fehler get_recent_trades:[/red] {e}")
            return []

    def get_open_trades(self, limit: int = 200) -> List[dict]:
        """
        Liefert aktuell offene Trades (status='open') für den aktuellen Modus
        (paper_mode passend zu Settings) – neueste zuerst.
        """
        if not self.available:
            return []
        try:
            sql = """
                SELECT * FROM trades
                WHERE status = 'open' AND paper_mode = ?
                ORDER BY created_at DESC
                LIMIT ?
            """
            conn = get_connection()
            if conn is None:
                return []
            try:
                rows = conn.execute(sql, (int(_IS_PAPER), limit)).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"[red]DB-Fehler get_open_trades:[/red] {e}")
            return []

    # ------------------------------------------------------------------
    # Statistik-Abfrage (für CLI --status)
    # ------------------------------------------------------------------

    def get_summary_stats(self) -> dict:
        """Gibt aggregierte Statistik aus der DB zurück."""
        if not self.available:
            return {}

        try:
            sql = """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'closed')   AS closed_trades,
                    COUNT(*) FILTER (WHERE status = 'open')     AS open_trades,
                    COUNT(*) FILTER (WHERE status = 'rejected') AS rejected_trades,
                    SUM(pnl_abs) FILTER (WHERE status = 'closed') AS total_pnl,
                    AVG(pnl_abs) FILTER (WHERE status = 'closed') AS avg_pnl,
                    COUNT(*) FILTER (WHERE status='closed' AND pnl_abs > 0) AS winners,
                    COUNT(*) FILTER (WHERE status='closed' AND pnl_abs <= 0) AS losers
                FROM trades
                WHERE paper_mode = ?
            """
            conn = get_connection()
            if conn is None:
                return {}
            try:
                row = conn.execute(sql, (int(_IS_PAPER),)).fetchone()
                if not row:
                    return {}
                result = dict(row)
                closed = result.get("closed_trades") or 0
                winners = result.get("winners") or 0
                result["winrate_pct"] = round(winners / closed * 100, 1) if closed > 0 else 0.0
                result["total_pnl"] = round(result.get("total_pnl") or 0.0, 4)
                result["avg_pnl"] = round(result.get("avg_pnl") or 0.0, 4)
                return result
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"[red]DB-Fehler get_summary_stats:[/red] {e}")
            return {}
