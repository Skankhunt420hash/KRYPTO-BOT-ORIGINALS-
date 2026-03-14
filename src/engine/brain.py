from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from config.settings import settings
from src.engine.meta_selector import (
    DIRECTION_MULTIPLIER,
    MIN_REGIME_FIT,
    REGIME_STRATEGY_FIT,
    MetaSelector,
)
from src.engine.performance_tracker import PerformanceTracker
from src.engine.regime import Regime
from src.engine.runtime_control import runtime_control
from src.engine.strategy_scorer import StrategyScorer
from src.strategies.signal import EnhancedSignal, Side
from src.utils.logger import setup_logger

logger = setup_logger("intelligence_brain")


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


class IntelligenceBrain:
    """
    Kontrollierte Intelligence-Schicht für adaptive Entscheidungslogik.

    WICHTIG:
    - Keine Selbstmodifikation von Code
    - Keine autonomen Parameter-Exzesse
    - Nur vorsichtige Gewichtungsanpassung über beobachtete Performance-Metriken
    """

    def __init__(
        self,
        *,
        tracker: PerformanceTracker,
        scorer: StrategyScorer,
        selector: MetaSelector,
    ) -> None:
        self._tracker = tracker
        self._scorer = scorer
        self._selector = selector
        self._last_snapshot: Dict = {
            "last_regime": "UNKNOWN",
            "last_signal_score": 0.0,
            "last_strategy_ranking": [],
            "last_decision_reason": "init",
            "risky_phase": False,
        }

    def evaluate(
        self,
        *,
        symbol: str,
        regime: Regime,
        signals: List[EnhancedSignal],
    ) -> Tuple[Optional[EnhancedSignal], Dict]:
        actionable = [
            s for s in signals if s.symbol == symbol and s.is_actionable()
        ]
        ranking = self._rank_strategies(regime=regime, signals=actionable)

        selector_winner = self._selector.select(signals, regime, symbol)
        selector_snapshot = self._selector.get_last_selection()

        min_trade_score = float(getattr(settings, "BRAIN_MIN_SCORE_TO_TRADE", 0.45))
        winner_rank = ranking[0] if ranking else {}
        risky_phase = self._is_risky_phase(regime, ranking)

        chosen = selector_winner
        decision_reason = "no_actionable_signal"
        last_signal_score = 0.0

        if selector_winner is None:
            decision_reason = f"selector_none:{regime.value}"
        else:
            best_entry = next(
                (r for r in ranking if r.get("strategy") == selector_winner.strategy_name),
                None,
            )
            if best_entry is not None:
                last_signal_score = float(best_entry.get("brain_score", 0.0))

            if risky_phase and last_signal_score < min_trade_score:
                chosen = None
                decision_reason = (
                    f"brain_risky_phase_block:{regime.value}:score={last_signal_score:.3f}"
                )
            elif last_signal_score < min_trade_score:
                chosen = None
                decision_reason = f"brain_score_too_low:{last_signal_score:.3f}"
            else:
                decision_reason = (
                    f"brain_ok:{selector_winner.strategy_name}:{last_signal_score:.3f}"
                )

        snapshot = {
            "last_regime": regime.value,
            "last_signal_score": round(last_signal_score, 4),
            "last_strategy_ranking": ranking[:6],
            "last_decision_reason": decision_reason,
            "risky_phase": risky_phase,
            "selector": selector_snapshot,
            "winner_strategy": (chosen.strategy_name if chosen else None),
            "winner_side": (chosen.side.value if chosen else None),
            "winner_confidence": (round(float(chosen.confidence), 1) if chosen else None),
            "winner_rr": (round(float(chosen.rr), 2) if chosen else None),
            "ranking_count": len(ranking),
            "min_trade_score": min_trade_score,
            "learning_mode": "controlled_adaptive_weights",
        }
        self._last_snapshot = snapshot
        return chosen, snapshot

    def get_snapshot(self) -> Dict:
        return dict(self._last_snapshot)

    def _rank_strategies(self, *, regime: Regime, signals: List[EnhancedSignal]) -> List[Dict]:
        out: List[Dict] = []
        regime_fits = REGIME_STRATEGY_FIT.get(regime, {})
        dir_mults = DIRECTION_MULTIPLIER.get(regime, {})
        preferred = (runtime_control.get_snapshot().get("preferred_strategy") or "").strip()
        pref_bonus = settings.CONTROL_STRATEGY_PRIORITY_BONUS if preferred else 0.0

        for sig in signals:
            base_fit = float(regime_fits.get(sig.strategy_name, 0.30))
            direction_fit = float(dir_mults.get(sig.side, 1.0))
            regime_fit = _clamp(base_fit * direction_fit)

            perf_score = float(self._scorer.get_score(sig.strategy_name, regime.value))
            metrics = self._tracker.get_global(sig.strategy_name)
            losing_streak = int(metrics.losing_streak) if metrics else 0
            drawdown = float(metrics.max_drawdown_pct) if metrics else 0.0
            recency_wr = float(metrics.recency_win_rate) if metrics else 0.5

            trend_quality = regime_fit
            momentum_quality = _clamp(float(sig.confidence) / 100.0)
            volatility_quality = self._volatility_quality(sig=sig, regime=regime)
            structure_quality = 1.0 if self._signal_structure_ok(sig) else 0.0
            rr_quality = _clamp(float(sig.rr) / 3.0)

            streak_penalty = min(losing_streak / 6.0, 1.0) * 0.10
            dd_penalty = min(drawdown / 35.0, 1.0) * 0.08

            brain_score = (
                trend_quality * 0.20
                + momentum_quality * 0.18
                + volatility_quality * 0.10
                + structure_quality * 0.12
                + rr_quality * 0.15
                + perf_score * 0.17
                + recency_wr * 0.08
            ) - streak_penalty - dd_penalty

            priority_adj = 0.0
            if preferred and sig.strategy_name.lower() == preferred.lower():
                priority_adj = pref_bonus
                brain_score += priority_adj

            eligible = regime_fit >= MIN_REGIME_FIT
            if settings.STRATEGY_MIN_PERF_SCORE > 0.0 and perf_score < settings.STRATEGY_MIN_PERF_SCORE:
                eligible = False

            out.append(
                {
                    "strategy": sig.strategy_name,
                    "side": sig.side.value,
                    "brain_score": round(_clamp(brain_score, 0.0, 1.5), 4),
                    "eligible": bool(eligible),
                    "components": {
                        "trend_quality": round(trend_quality, 3),
                        "momentum_quality": round(momentum_quality, 3),
                        "volatility_quality": round(volatility_quality, 3),
                        "structure_quality": round(structure_quality, 3),
                        "rr_quality": round(rr_quality, 3),
                        "perf_score": round(perf_score, 3),
                        "recency_win_rate": round(recency_wr, 3),
                        "priority_adj": round(priority_adj, 3),
                        "streak_penalty": round(streak_penalty, 3),
                        "drawdown_penalty": round(dd_penalty, 3),
                        "losing_streak": losing_streak,
                        "max_drawdown_pct": round(drawdown, 2),
                    },
                }
            )

        out.sort(key=lambda x: x.get("brain_score", 0.0), reverse=True)
        return out

    def _is_risky_phase(self, regime: Regime, ranking: List[Dict]) -> bool:
        if regime == Regime.HIGH_VOLATILITY:
            return True
        if not ranking:
            return True
        best = float(ranking[0].get("brain_score", 0.0))
        risk_threshold = float(getattr(settings, "BRAIN_RISKY_PHASE_SCORE", 0.35))
        return best < risk_threshold

    @staticmethod
    def _signal_structure_ok(sig: EnhancedSignal) -> bool:
        if sig.entry <= 0:
            return False
        if sig.side == Side.LONG:
            return sig.stop_loss < sig.entry < sig.take_profit
        if sig.side == Side.SHORT:
            return sig.stop_loss > sig.entry > sig.take_profit
        return False

    @staticmethod
    def _volatility_quality(*, sig: EnhancedSignal, regime: Regime) -> float:
        rr_score = _clamp(float(sig.rr) / 3.0)
        if regime == Regime.HIGH_VOLATILITY:
            return _clamp(0.4 + rr_score * 0.6)
        if regime == Regime.LOW_VOLATILITY:
            return _clamp(0.55 + rr_score * 0.45)
        return _clamp(0.5 + rr_score * 0.5)

