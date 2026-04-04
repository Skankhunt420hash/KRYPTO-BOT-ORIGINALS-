"""
Decision-Repository: persistiert Entscheidungszyklen des Bots in SQLite.
"""

from datetime import datetime, timezone
import json
from typing import Dict, List

from src.storage.database import init_db, get_connection
from src.utils.logger import setup_logger

logger = setup_logger("decision_repository")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class DecisionRepository:
    def __init__(self) -> None:
        self.available: bool = init_db()

    def save_decision(
        self,
        *,
        mode: str,
        symbol: str,
        timeframe: str,
        detected_regime: str,
        eligible_strategies: List[str],
        strategy_ranking: List[Dict],
        chosen_strategy: str,
        signal_score: float,
        risk_decision: str,
        allow_trade: bool,
        reject_reason: str,
        last_decision_reason: str,
        market_context: Dict,
    ) -> bool:
        if not self.available:
            return False
        try:
            now = _utcnow()
            sql = """
                INSERT INTO decisions (
                    timestamp, mode, symbol, timeframe, detected_regime,
                    eligible_strategies, strategy_ranking, chosen_strategy,
                    signal_score, risk_decision, allow_trade, reject_reason,
                    last_decision_reason, market_context, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?
                )
            """
            params = (
                now,
                mode,
                symbol,
                timeframe,
                detected_regime or "",
                json.dumps(eligible_strategies or [], ensure_ascii=True),
                json.dumps(strategy_ranking or [], ensure_ascii=True),
                chosen_strategy or "",
                float(signal_score or 0.0),
                risk_decision or "",
                int(bool(allow_trade)),
                reject_reason or "",
                last_decision_reason or "",
                json.dumps(market_context or {}, ensure_ascii=True),
                now,
            )
            conn = get_connection()
            if conn is None:
                return False
            try:
                conn.execute(sql, params)
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"DB-Fehler save_decision ({symbol}): {e}")
            return False

