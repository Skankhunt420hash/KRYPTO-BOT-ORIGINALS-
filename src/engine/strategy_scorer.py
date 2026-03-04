"""
Strategy Scorer

Berechnet für jede Strategie einen Performance-Score [0.10, 1.00],
der als Anpassung im Meta-Selector verwendet wird.

Score-Formel (nur wenn >= MIN_TRADES Daten vorhanden):

    base = win_rate_norm * 0.30
         + profit_factor_norm * 0.30
         + recency_win_rate * 0.40

    regime_adj = (regime_win_rate - 0.5) * 0.20    → max ±0.10
                 (nur wenn >= MIN_REGIME_TRADES Daten)

    dd_penalty     = min(max_drawdown_pct / 30.0, 1.0) * 0.15  → max -0.15
    streak_penalty = min(losing_streak / 10, 1.0)     * 0.10  → max -0.10

    score = clamp(base + regime_adj - dd_penalty - streak_penalty, 0.10, 1.00)

Wenn zu wenig Daten: score = 0.5 (neutral, keine Bevorzugung / Bestrafung).

Guardrails:
- Score niemals unter 0.10 (Strategie wird nie komplett deaktiviert)
- Score niemals über 1.00
- Regime-Adjustment nur wenn genug regime-spezifische Trades
- Kein aggressives Bestrafen bei fehlenden Daten
"""

from typing import Dict, Optional

from config.settings import settings
from src.engine.performance_tracker import PerformanceTracker, StrategyMetrics
from src.utils.logger import setup_logger

logger = setup_logger("strategy_scorer")

# ── Score-Parameter ────────────────────────────────────────────────────────
# Minimum-Trades bevor ein Score angewendet wird
_MIN_TRADES_GLOBAL: int = settings.PERF_TRACKER_MIN_TRADES
_MIN_TRADES_REGIME: int = settings.PERF_TRACKER_MIN_REGIME_TRADES

# Neutral-Score bei unzureichenden Daten
_NEUTRAL: float = 0.50

# Komponentengewichte der Basis-Score
_W_WINRATE: float = 0.30
_W_PROFIT_FACTOR: float = 0.30
_W_RECENCY: float = 0.40

# Normierungsgrenzen
_MAX_PF: float = 2.5        # PF von 2.5 → normiert auf 1.0

# Regime-Adjustment
_REGIME_WEIGHT: float = 0.20  # (regime_wr - 0.5) * 0.20 → max ±0.10

# Penalties
_DD_THRESHOLD: float = 30.0   # 30% Drawdown → maximale Strafe
_MAX_DD_PENALTY: float = 0.15
_STREAK_THRESHOLD: float = 10.0  # 10 Verluste in Folge → max Strafe
_MAX_STREAK_PENALTY: float = 0.10

# Score-Grenzen
_SCORE_MIN: float = 0.10
_SCORE_MAX: float = 1.00


class StrategyScorer:
    """
    Berechnet Performance-Scores und cached sie pro Refresh-Zyklus.

    Verwendung:
        scorer = StrategyScorer(tracker)
        scorer.refresh()                           # Scores neu berechnen
        score = scorer.get_score("TrendContinuation", "TREND_UP")
        # → 0.5 wenn keine Daten, z.B. 0.63 wenn gute Performance
    """

    def __init__(self, tracker: PerformanceTracker):
        self._tracker = tracker
        self._cache: Dict[str, float] = {}

    def refresh(self) -> None:
        """Tracker aktualisieren und Score-Cache leeren."""
        self._tracker.refresh()
        self._cache.clear()
        logger.debug("StrategyScorer: Cache geleert nach Refresh")

    def get_score(self, strategy_name: str, regime: str = "GLOBAL") -> float:
        """
        Performance-Score für eine Strategie [0.10, 1.00].
        0.50 = neutral (zu wenig Daten oder exakt durchschnittlich)
        > 0.50 = besser als Durchschnitt → leichter Bonus im Meta-Selector
        < 0.50 = schlechter als Durchschnitt → leichte Strafe
        """
        key = f"{strategy_name}::{regime}"
        if key not in self._cache:
            self._cache[key] = self._compute(strategy_name, regime)
        return self._cache[key]

    def get_all_scores(self, regime: str = "GLOBAL") -> Dict[str, float]:
        """Alle bekannten Strategie-Scores für ein Regime."""
        return {
            name: self.get_score(name, regime)
            for name in self._tracker.known_strategies()
        }

    # ── Score-Berechnung ──────────────────────────────────────────────────

    def _compute(self, strategy_name: str, regime: str) -> float:
        global_m = self._tracker.get_global(strategy_name)

        # Zu wenig Daten → neutral, keine Bevorzugung oder Bestrafung
        if global_m is None or global_m.trade_count < _MIN_TRADES_GLOBAL:
            n = global_m.trade_count if global_m else 0
            logger.debug(
                f"[Scorer] {strategy_name}: zu wenig Daten "
                f"({n}/{_MIN_TRADES_GLOBAL}) → neutral {_NEUTRAL}"
            )
            return _NEUTRAL

        # ── Basis-Komponenten ──────────────────────────────────────────
        wr_score = global_m.win_rate / 100.0
        pf_score = min(global_m.profit_factor / _MAX_PF, 1.0)
        rec_score = global_m.recency_win_rate  # bereits 0-1

        base = (
            wr_score  * _W_WINRATE
            + pf_score  * _W_PROFIT_FACTOR
            + rec_score * _W_RECENCY
        )

        # ── Regime-Adjustment ──────────────────────────────────────────
        # Nur aktiv wenn: Regime != GLOBAL UND genug regime-spezifische Trades
        regime_adj = 0.0
        if regime and regime != "GLOBAL":
            regime_m = self._tracker.get_regime(strategy_name, regime)
            if regime_m and regime_m.trade_count >= _MIN_TRADES_REGIME:
                regime_adj = (regime_m.win_rate / 100.0 - 0.5) * _REGIME_WEIGHT

        # ── Penalties ──────────────────────────────────────────────────
        dd_penalty = (
            min(global_m.max_drawdown_pct / _DD_THRESHOLD, 1.0)
            * _MAX_DD_PENALTY
        )
        streak_penalty = (
            min(global_m.losing_streak / _STREAK_THRESHOLD, 1.0)
            * _MAX_STREAK_PENALTY
        )

        raw = base + regime_adj - dd_penalty - streak_penalty
        score = max(_SCORE_MIN, min(_SCORE_MAX, raw))

        logger.debug(
            f"[Scorer] {strategy_name} [{regime}] "
            f"n={global_m.trade_count} "
            f"wr={wr_score:.2f} pf={pf_score:.2f} rec={rec_score:.2f} "
            f"base={base:.3f} reg_adj={regime_adj:+.3f} "
            f"dd_pen={-dd_penalty:.3f} sk_pen={-streak_penalty:.3f} "
            f"→ score={score:.3f}"
        )
        return score
