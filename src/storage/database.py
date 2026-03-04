"""
Datenbankverbindung und Schema-Initialisierung.

Nutzt stdlib sqlite3 direkt – kein ORM-Overhead.
Die DATABASE_URL aus den Settings wird in einen Dateipfad konvertiert.
"""

import os
import sqlite3
from typing import Optional

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("database")

# ------------------------------------------------------------------
# DDL – vollständiges Trades-Schema
# ------------------------------------------------------------------

CREATE_TRADES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_open   TEXT NOT NULL,
    timestamp_close  TEXT,
    symbol           TEXT NOT NULL,
    timeframe        TEXT NOT NULL,
    strategy_name    TEXT NOT NULL,
    side             TEXT NOT NULL DEFAULT 'long',
    entry_price      REAL NOT NULL,
    stop_loss        REAL NOT NULL,
    take_profit      REAL NOT NULL,
    exit_price       REAL,
    position_size    REAL NOT NULL,
    risk_amount      REAL NOT NULL,
    rr_planned       REAL NOT NULL,
    pnl_abs          REAL,
    pnl_pct          REAL,
    status           TEXT NOT NULL DEFAULT 'open',
    reason_open      TEXT,
    reason_close     TEXT,
    confidence       REAL,
    regime           TEXT,
    paper_mode       INTEGER NOT NULL DEFAULT 1,
    order_id         TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""

# Index für häufige Abfragen
CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_trades_symbol   ON trades (symbol);",
    "CREATE INDEX IF NOT EXISTS idx_trades_status   ON trades (status);",
    "CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy_name);",
    "CREATE INDEX IF NOT EXISTS idx_trades_ts_open  ON trades (timestamp_open);",
]


def get_db_path() -> str:
    """Konvertiert sqlite:///path → path (relativ oder absolut)."""
    url = settings.DATABASE_URL
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    if url.startswith("sqlite://"):
        return url[len("sqlite://"):]
    return url


def init_db() -> bool:
    """
    Erstellt die Datenbank-Datei, die Tabelle und Indizes falls nicht vorhanden.
    Gibt True zurück wenn erfolgreich, False bei Fehler.
    Fehler werden geloggt aber nicht re-raised – der Bot soll weiterlaufen.
    """
    try:
        db_path = get_db_path()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(CREATE_TRADES_TABLE_SQL)
            for idx_sql in CREATE_INDEXES_SQL:
                conn.execute(idx_sql)
            conn.commit()
            logger.info(f"[green]Datenbank bereit:[/green] {db_path}")
            return True
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"[red]DB-Initialisierung fehlgeschlagen:[/red] {e}")
        return False


def get_connection() -> Optional[sqlite3.Connection]:
    """
    Öffnet eine Datenbankverbindung mit Row-Factory für dict-artigen Zugriff.
    Gibt None zurück wenn die Verbindung scheitert.
    """
    try:
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")   # bessere Concurrent-Performance
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn
    except Exception as e:
        logger.error(f"[red]DB-Verbindung fehlgeschlagen:[/red] {e}")
        return None
