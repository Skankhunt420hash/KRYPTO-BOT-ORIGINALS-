"""
Strategy Performance Tracker

Liest geschlossene Trades aus der bestehenden SQLite-Tabelle und berechnet
Strategie-Performance-Metriken global und pro Regime.

Keine eigene Tabelle – nutzt die bestehende `trades`-Tabelle.
DB-Fehler lassen den Bot weiterlaufen (gibt leere/neutrale Metriken zurück).
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from config.settings import settings
from src.storage.database import get_connection
from src.utils.logger import setup_logger

logger = setup_logger("perf_tracker")

_IS_PAPER = settings.TRADING_MODE == "paper"


# ─────────────────────────────────────────────────────────────────────────────
# Datenstruktur
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StrategyMetrics:
    """
    Performance-Metriken für eine Strategie.
    regime = 'GLOBAL' für die gesamte Strategie-Historie,
             oder ein spezifisches Regime (z.B. 'TREND_UP').
    """

    strategy_name: str
    regime: str                     # "GLOBAL" oder Regime-Name

    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0           # Prozent (0–100)

    pnl_abs_sum: float = 0.0        # USDT Gesamt-PnL (nach Fees)
    pnl_pct_sum: float = 0.0        # %-Gesamt-PnL
    avg_win: float = 0.0            # Ø Gewinn-Trade in USDT
    avg_loss: float = 0.0           # Ø Verlust-Trade in USDT (positiver Wert)
    profit_factor: float = 0.0      # Bruttoprofit / Bruttoverlust

    max_drawdown_pct: float = 0.0   # Max Drawdown der Trade-Sequenz in %
    avg_rr_realized: float = 0.0    # Ø realisierter PnL% (Proxy für RR)

    recency_win_rate: float = 0.5   # Exponentiell-gewichtete Win-Rate (0–1)
    losing_streak: int = 0          # Aktuelle aufeinanderfolgende Verluste

    last_trade_timestamp: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Tracker
# ─────────────────────────────────────────────────────────────────────────────

class PerformanceTracker:
    """
    Berechnet Strategie-Performance-Metriken aus der bestehenden trades-Tabelle.

    Verwendung:
        tracker = PerformanceTracker()
        tracker.refresh()                          # Daten neu laden
        m = tracker.get_global("TrendContinuation")
        r = tracker.get_regime("TrendContinuation", "TREND_UP")
    """

    def __init__(
        self,
        rolling_window: Optional[int] = None,
        recency_decay: Optional[float] = None,
    ):
        self.rolling_window: int = rolling_window or settings.PERF_TRACKER_ROLLING_WINDOW
        self.recency_decay: float = recency_decay or settings.PERF_TRACKER_RECENCY_DECAY

        self._global: Dict[str, StrategyMetrics] = {}
        self._by_regime: Dict[str, Dict[str, StrategyMetrics]] = {}
        self.available: bool = False

        self.refresh()

    # ── Public API ────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Lädt Trade-Daten aus DB und berechnet alle Metriken neu."""
        try:
            trades_by_strat = self._load_trades()
            self._global = {}
            self._by_regime = {}

            for name, trades in trades_by_strat.items():
                self._global[name] = self._compute(name, "GLOBAL", trades)

                # Regime-Aufschlüsselung
                regime_groups: Dict[str, List[dict]] = {}
                for t in trades:
                    reg = (t.get("regime") or "UNKNOWN").strip() or "UNKNOWN"
                    regime_groups.setdefault(reg, []).append(t)

                self._by_regime[name] = {
                    reg: self._compute(name, reg, rt)
                    for reg, rt in regime_groups.items()
                }

            self.available = True
            total = sum(m.trade_count for m in self._global.values())
            logger.debug(
                f"PerformanceTracker aktualisiert: "
                f"{len(self._global)} Strategien, {total} Trades gesamt"
            )

        except Exception as e:
            logger.warning(f"PerformanceTracker.refresh fehlgeschlagen: {e}")
            self.available = False

    def get_global(self, strategy_name: str) -> Optional[StrategyMetrics]:
        """Globale Metriken für eine Strategie (über alle Regimes)."""
        return self._global.get(strategy_name)

    def get_regime(
        self, strategy_name: str, regime: str
    ) -> Optional[StrategyMetrics]:
        """Regime-spezifische Metriken für eine Strategie."""
        return self._by_regime.get(strategy_name, {}).get(regime)

    def all_global(self) -> Dict[str, StrategyMetrics]:
        return dict(self._global)

    def all_regime(self) -> Dict[str, Dict[str, StrategyMetrics]]:
        return dict(self._by_regime)

    def known_strategies(self) -> List[str]:
        return sorted(self._global.keys())

    # ── Interne Daten-Ladung ──────────────────────────────────────────────

    def _load_trades(self) -> Dict[str, List[dict]]:
        """
        Lädt alle abgeschlossenen Trades aus der DB.
        Sortiert nach timestamp_close ASC (älteste zuerst).
        """
        conn = get_connection()
        if conn is None:
            return {}

        try:
            rows = conn.execute(
                """
                SELECT strategy_name, regime,
                       pnl_abs, pnl_pct, rr_planned, risk_amount,
                       timestamp_close, reason_close
                FROM   trades
                WHERE  status     = 'closed'
                  AND  pnl_abs   IS NOT NULL
                  AND  paper_mode = ?
                ORDER  BY timestamp_close ASC
                """,
                (int(_IS_PAPER),),
            ).fetchall()

            result: Dict[str, List[dict]] = {}
            for row in rows:
                d = dict(row)
                name = d.get("strategy_name") or "unknown"
                result.setdefault(name, []).append(d)
            return result

        except Exception as e:
            logger.warning(f"Trades-Ladung fehlgeschlagen: {e}")
            return {}
        finally:
            conn.close()

    # ── Metriken-Berechnung ───────────────────────────────────────────────

    def _compute(
        self, name: str, regime: str, trades: List[dict]
    ) -> StrategyMetrics:
        """Berechnet StrategyMetrics aus einer Liste von Trade-Dicts."""
        if not trades:
            return StrategyMetrics(strategy_name=name, regime=regime)

        pnls = [float(t["pnl_abs"]) for t in trades if t.get("pnl_abs") is not None]
        if not pnls:
            return StrategyMetrics(strategy_name=name, regime=regime)

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        n = len(pnls)

        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (
            round(gross_profit / gross_loss, 2)
            if gross_loss > 0
            else (10.0 if wins else 0.0)
        )
        profit_factor = min(profit_factor, 99.0)

        pnl_pcts = [float(t.get("pnl_pct") or 0) for t in trades]
        avg_rr = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0.0

        recent = trades[-self.rolling_window:]
        recency_wr = _recency_win_rate(recent, self.recency_decay)
        streak = _losing_streak(pnls)
        max_dd = _max_drawdown(pnls)
        last_ts = trades[-1].get("timestamp_close", "") if trades else ""

        return StrategyMetrics(
            strategy_name=name,
            regime=regime,
            trade_count=n,
            win_count=len(wins),
            loss_count=len(losses),
            win_rate=round(len(wins) / n * 100, 1) if n else 0.0,
            pnl_abs_sum=round(sum(pnls), 4),
            pnl_pct_sum=round(sum(pnl_pcts), 2),
            avg_win=round(gross_profit / len(wins), 4) if wins else 0.0,
            avg_loss=round(gross_loss / len(losses), 4) if losses else 0.0,
            profit_factor=profit_factor,
            max_drawdown_pct=round(max_dd, 2),
            avg_rr_realized=round(avg_rr, 2),
            recency_win_rate=round(recency_wr, 4),
            losing_streak=streak,
            last_trade_timestamp=last_ts,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Reine Berechnungsfunktionen (keine I/O, gut testbar)
# ─────────────────────────────────────────────────────────────────────────────

def _max_drawdown(pnls: List[float]) -> float:
    """Max Drawdown der kumulativen PnL-Kurve in %."""
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _recency_win_rate(trades: List[dict], decay: float) -> float:
    """
    Exponentiell gewichtete Win-Rate.
    Neueste Trades (Ende der Liste) haben das höchste Gewicht.
    decay = 0.90: ein Trade von vor 10 Perioden wird mit 0.90^10 ≈ 0.35 gewichtet.
    """
    if not trades:
        return 0.5  # neutral
    n = len(trades)
    # ältester Trade: index 0 → Gewicht decay^(n-1), neuester: index n-1 → Gewicht 1.0
    weights = [decay ** (n - 1 - i) for i in range(n)]
    weighted_wins = sum(
        w * (1.0 if (float(t.get("pnl_abs") or 0)) > 0 else 0.0)
        for w, t in zip(weights, trades)
    )
    total_w = sum(weights)
    return weighted_wins / total_w if total_w > 0 else 0.5


def _losing_streak(pnls: List[float]) -> int:
    """Anzahl aufeinanderfolgender Verluste am Ende der Trade-Liste."""
    streak = 0
    for p in reversed(pnls):
        if p <= 0:
            streak += 1
        else:
            break
    return streak
