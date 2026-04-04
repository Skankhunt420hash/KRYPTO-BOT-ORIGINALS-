"""
Performance-Repository für Paper-/Live-Auswertungsdaten.

Speichert:
- Snapshot-Zeitreihe (Balance/Equity/PnL/Drawdown)
- Tagesaggregation (Trades, PnL, Best/Worst-Strategie, Reject-Gründe)
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Dict, Optional

from config.settings import settings
from src.storage.database import init_db, get_connection
from src.utils.logger import setup_logger

logger = setup_logger("performance_repository")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class PerformanceRepository:
    def __init__(self) -> None:
        self.available: bool = init_db()
        self._daily_json_path = Path("data/daily_summary.json")

    def save_snapshot(
        self,
        *,
        mode: str,
        current_balance: float,
        current_equity: float,
        open_positions_count: int,
        realized_pnl_total: float,
        unrealized_pnl_total: float,
        day_pnl: float,
        total_trades: int,
        win_rate: float,
    ) -> bool:
        if not self.available:
            return False
        try:
            now = _utcnow()
            peak_equity, max_dd = self._get_last_peak_and_dd()
            peak_equity = max(peak_equity, float(current_equity))
            dd = 0.0
            if peak_equity > 0:
                dd = max(0.0, (peak_equity - float(current_equity)) / peak_equity * 100.0)
            max_dd = max(max_dd, dd)

            sql = """
                INSERT INTO performance_snapshots (
                    timestamp, mode, current_balance, current_equity, open_positions_count,
                    realized_pnl_total, unrealized_pnl_total, day_pnl, total_trades, win_rate,
                    peak_equity, max_drawdown_pct, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                now,
                mode,
                float(current_balance),
                float(current_equity),
                int(open_positions_count),
                float(realized_pnl_total),
                float(unrealized_pnl_total),
                float(day_pnl),
                int(total_trades),
                float(win_rate),
                float(peak_equity),
                float(max_dd),
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
            logger.error(f"DB-Fehler save_snapshot: {e}")
            return False

    def latest_snapshot(self, mode: Optional[str] = None) -> Dict:
        if not self.available:
            return {}
        try:
            if mode:
                sql = """
                    SELECT * FROM performance_snapshots
                    WHERE mode = ?
                    ORDER BY id DESC
                    LIMIT 1
                """
                params = (mode,)
            else:
                sql = "SELECT * FROM performance_snapshots ORDER BY id DESC LIMIT 1"
                params = ()
            conn = get_connection()
            if conn is None:
                return {}
            try:
                row = conn.execute(sql, params).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"DB-Fehler latest_snapshot: {e}")
            return {}

    def update_daily_summary(self, *, mode: str) -> Dict:
        if not self.available:
            return {}
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        summary = self._build_daily_summary(day=day, mode=mode)
        if not summary:
            return {}
        try:
            now = _utcnow()
            sql = """
                INSERT INTO daily_performance (
                    day, mode, trades_count, pnl_abs, winners, losers, win_rate,
                    best_strategy, worst_strategy, reject_reasons_top, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    mode=excluded.mode,
                    trades_count=excluded.trades_count,
                    pnl_abs=excluded.pnl_abs,
                    winners=excluded.winners,
                    losers=excluded.losers,
                    win_rate=excluded.win_rate,
                    best_strategy=excluded.best_strategy,
                    worst_strategy=excluded.worst_strategy,
                    reject_reasons_top=excluded.reject_reasons_top,
                    updated_at=excluded.updated_at
            """
            params = (
                day,
                mode,
                int(summary["trades_count"]),
                float(summary["pnl_abs"]),
                int(summary["winners"]),
                int(summary["losers"]),
                float(summary["win_rate"]),
                summary.get("best_strategy") or "",
                summary.get("worst_strategy") or "",
                json.dumps(summary.get("reject_reasons_top") or [], ensure_ascii=True),
                now,
            )
            conn = get_connection()
            if conn is None:
                return summary
            try:
                conn.execute(sql, params)
                conn.commit()
            finally:
                conn.close()
            self._write_daily_json(summary)
            return summary
        except Exception as e:
            logger.error(f"DB-Fehler update_daily_summary: {e}")
            return summary

    def latest_daily_summary(self) -> Dict:
        if not self.available:
            return {}
        try:
            sql = "SELECT * FROM daily_performance ORDER BY day DESC LIMIT 1"
            conn = get_connection()
            if conn is None:
                return {}
            try:
                row = conn.execute(sql).fetchone()
                if not row:
                    return {}
                data = dict(row)
                try:
                    data["reject_reasons_top"] = json.loads(data.get("reject_reasons_top") or "[]")
                except Exception:
                    data["reject_reasons_top"] = []
                return data
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"DB-Fehler latest_daily_summary: {e}")
            return {}

    def _get_last_peak_and_dd(self) -> tuple[float, float]:
        try:
            sql = """
                SELECT peak_equity, max_drawdown_pct
                FROM performance_snapshots
                ORDER BY id DESC
                LIMIT 1
            """
            conn = get_connection()
            if conn is None:
                return 0.0, 0.0
            try:
                row = conn.execute(sql).fetchone()
                if not row:
                    return 0.0, 0.0
                return float(row["peak_equity"] or 0.0), float(row["max_drawdown_pct"] or 0.0)
            finally:
                conn.close()
        except Exception:
            return 0.0, 0.0

    def _build_daily_summary(self, *, day: str, mode: str) -> Dict:
        """
        Tageswerte aus Trade-Tabelle (closed/rejected) aggregieren.
        """
        is_paper = int(mode == "paper")
        conn = get_connection()
        if conn is None:
            return {}
        try:
            closed_sql = """
                SELECT strategy_name, pnl_abs
                FROM trades
                WHERE status='closed'
                  AND paper_mode=?
                  AND substr(timestamp_close,1,10)=?
            """
            closed_rows = conn.execute(closed_sql, (is_paper, day)).fetchall()

            rejected_sql = """
                SELECT reason_open
                FROM trades
                WHERE status='rejected'
                  AND paper_mode=?
                  AND substr(timestamp_open,1,10)=?
            """
            rejected_rows = conn.execute(rejected_sql, (is_paper, day)).fetchall()

            trades_count = len(closed_rows)
            pnl_abs = sum(float(r["pnl_abs"] or 0.0) for r in closed_rows)
            winners = sum(1 for r in closed_rows if float(r["pnl_abs"] or 0.0) > 0)
            losers = trades_count - winners
            win_rate = (winners / trades_count * 100.0) if trades_count > 0 else 0.0

            by_strategy: Dict[str, float] = {}
            for r in closed_rows:
                s = str(r["strategy_name"] or "n/a")
                by_strategy[s] = by_strategy.get(s, 0.0) + float(r["pnl_abs"] or 0.0)
            best_strategy = max(by_strategy.items(), key=lambda x: x[1])[0] if by_strategy else ""
            worst_strategy = min(by_strategy.items(), key=lambda x: x[1])[0] if by_strategy else ""

            reasons = Counter(str(r["reason_open"] or "unknown").strip() for r in rejected_rows)
            top_reasons = [{"reason": k, "count": v} for k, v in reasons.most_common(5)]

            return {
                "day": day,
                "mode": mode,
                "trades_count": trades_count,
                "pnl_abs": round(pnl_abs, 6),
                "winners": winners,
                "losers": losers,
                "win_rate": round(win_rate, 2),
                "best_strategy": best_strategy,
                "worst_strategy": worst_strategy,
                "reject_reasons_top": top_reasons,
            }
        finally:
            conn.close()

    def _write_daily_json(self, summary: Dict) -> None:
        try:
            self._daily_json_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"days": []}
            if self._daily_json_path.exists():
                try:
                    payload = json.loads(self._daily_json_path.read_text(encoding="utf-8"))
                except Exception:
                    payload = {"days": []}
            days = [d for d in payload.get("days", []) if d.get("day") != summary.get("day")]
            days.append(summary)
            days = sorted(days, key=lambda x: x.get("day", ""), reverse=True)[:60]
            payload["days"] = days
            self._daily_json_path.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"daily_summary.json konnte nicht geschrieben werden: {e}")

