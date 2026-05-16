"""
Heuristische „Gewinnchance“ / Erfolgsquote (0–100 %) für UI und Telegram.

Hinweis für Nutzer: Die reine Heuristik ist keine statistische Trefferquote.
Optional wird sie mit der gemessenen Strategie-Win-Rate aus der Trade-DB gemischt
(siehe effective_entry_win_chance_pct).
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from config.settings import settings


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _metric(strategy_name: str, perf_tracker: Any):
    if not strategy_name or perf_tracker is None:
        return None
    if not getattr(perf_tracker, "available", False):
        return None
    return perf_tracker.get_global(str(strategy_name).strip())


def win_chance_label(pct: float) -> str:
    """Deutsche Einordnung für die angezeigte Prozentzahl."""
    if pct >= 80.0:
        return "sehr hoch"
    if pct >= 68.0:
        return "hoch"
    if pct >= 55.0:
        return "mittel"
    return "niedrig"


def compute_trade_win_chance_pct(
    confidence_0_100: float,
    *,
    brain_score: Optional[float] = None,
    rr: Optional[float] = None,
) -> Tuple[float, str]:
    """
    Liefert (prozent_0_100, label).

    - confidence_0_100: Strategie-Konfidenz (wie im Bot, typ. 40–100).
    - brain_score: optional 0–1.5 (IntelligenceBrain / Ranking); stärkeres Gewicht im Multi-Modus.
    - rr: Risk/Reward; leichter Bonus ab ca. MIN_RR.
    """
    c = max(0.0, min(100.0, float(confidence_0_100)))

    if brain_score is not None:
        b = max(0.0, min(1.5, float(brain_score)))
        # Anteil Brain auf 0..100 skalieren (alles >1.0 zählt als „sehr stark“)
        b100 = min(100.0, 100.0 * min(b, 1.0))
        raw = 0.45 * c + 0.55 * b100
    else:
        raw = c

    if rr is not None and rr > 0:
        r = float(rr)
        # Kleiner Bonus für überdurchschnittliches RR (gedeckelt)
        raw += min(5.0, max(0.0, (r - 1.5) * 2.5))

    pct = max(0.0, min(100.0, round(raw, 1)))
    return pct, win_chance_label(pct)


def effective_entry_win_chance_pct(
    confidence_0_100: float,
    *,
    brain_score: Optional[float] = None,
    rr: Optional[float] = None,
    strategy_name: Optional[str] = None,
    perf_tracker: Any = None,
) -> Tuple[float, str]:
    """
    Heuristik + optional Mischung mit echter Roll-Win-Rate der Strategie (SQLite-Historie).

    blend = WIN_CHANCE_BLEND_ACTUAL_WR (0 = nur Heuristik).
    """
    h, label = compute_trade_win_chance_pct(
        confidence_0_100, brain_score=brain_score, rr=rr
    )
    blend = float(getattr(settings, "WIN_CHANCE_BLEND_ACTUAL_WR", 0.0) or 0.0)
    min_actual_blend = float(
        getattr(settings, "WIN_CHANCE_MIN_ACTUAL_WR_BLEND", 0.0) or 0.0
    )
    if (
        blend <= 0
        or not strategy_name
        or perf_tracker is None
        or not getattr(perf_tracker, "available", False)
    ):
        return h, label
    m = perf_tracker.get_global(str(strategy_name).strip())
    min_n = int(getattr(settings, "PERF_TRACKER_MIN_TRADES", 10) or 10)
    if not m or m.trade_count < min_n:
        return h, label
    blend = max(blend, min_actual_blend)
    blend = _clamp(blend, 0.0, 1.0)
    actual_wr = float(m.win_rate)
    eff = (1.0 - blend) * h + blend * actual_wr
    eff = max(0.0, min(100.0, round(eff, 1)))
    return eff, label


def historical_win_rate_block_reason(
    strategy_name: str,
    perf_tracker: Any,
) -> Optional[str]:
    """
    Harte Sperre: Strategie hat genug Trades, aber gemessene Win-Rate unter Schwelle.
    MIN_HISTORICAL_WIN_RATE_PCT=0 → deaktiviert.
    """
    thr = float(getattr(settings, "MIN_HISTORICAL_WIN_RATE_PCT", 0.0) or 0.0)
    if thr <= 0 or not strategy_name or perf_tracker is None:
        return None
    if not getattr(perf_tracker, "available", False):
        return None
    m = perf_tracker.get_global(str(strategy_name).strip())
    min_n = int(getattr(settings, "PERF_TRACKER_MIN_TRADES", 10) or 10)
    if not m or m.trade_count < min_n:
        return None
    if float(m.win_rate) < thr:
        return (
            f"HIST_WIN_RATE:{m.win_rate:.1f}%<{thr:.0f}% "
            f"(n={m.trade_count}, Strategie {strategy_name})"
        )
    return None


def strategy_quality_block_reason(
    strategy_name: str,
    perf_tracker: Any,
) -> Optional[str]:
    """
    Harte Qualitäts-Sperre für Strategien mit ausreichend Historie.
    Ziel: Verlustserien und statistisch schwache Setups früh blockieren.
    """
    m = _metric(strategy_name, perf_tracker)
    if m is None:
        return None

    min_n = int(getattr(settings, "PERF_TRACKER_MIN_TRADES", 10) or 10)
    if int(m.trade_count) < min_n:
        return None

    wr_floor = float(getattr(settings, "QUALITY_GATE_MIN_GLOBAL_WR_PCT", 0.0) or 0.0)
    rec_floor = float(getattr(settings, "QUALITY_GATE_MIN_RECENCY_WR_PCT", 58.0) or 58.0)
    pf_floor = float(getattr(settings, "QUALITY_GATE_MIN_PROFIT_FACTOR", 1.08) or 1.08)
    max_losing_streak = int(getattr(settings, "QUALITY_GATE_MAX_LOSING_STREAK", 3) or 3)

    wr = float(m.win_rate)
    recency_wr = float(m.recency_win_rate) * 100.0
    pf = float(m.profit_factor)
    losing_streak = int(m.losing_streak)

    if wr_floor > 0 and wr < wr_floor:
        return (
            f"QUALITY_WR:{wr:.1f}%<{wr_floor:.1f}% "
            f"(n={m.trade_count}, Strategie {strategy_name})"
        )
    if recency_wr < rec_floor:
        return (
            f"QUALITY_RECENCY_WR:{recency_wr:.1f}%<{rec_floor:.1f}% "
            f"(Strategie {strategy_name})"
        )
    if pf < pf_floor:
        return (
            f"QUALITY_PF:{pf:.2f}<{pf_floor:.2f} "
            f"(Strategie {strategy_name})"
        )
    if losing_streak >= max_losing_streak:
        return (
            f"QUALITY_LOSING_STREAK:{losing_streak}>={max_losing_streak} "
            f"(Strategie {strategy_name})"
        )
    return None


def position_size_scaler_by_quality(
    strategy_name: str,
    perf_tracker: Any,
) -> Tuple[float, str]:
    """
    Dynamische Positionsreduktion in Schwächephasen.
    1.0 = volle Größe, <1 reduziert Risiko pro Trade.
    """
    m = _metric(strategy_name, perf_tracker)
    if m is None:
        return 1.0, "QUALITY_SCALE_INACTIVE:no_metrics"

    min_n = int(getattr(settings, "PERF_TRACKER_MIN_TRADES", 10) or 10)
    if int(m.trade_count) < min_n:
        return 1.0, f"QUALITY_SCALE_INACTIVE:n<{min_n}"

    target_wr = float(getattr(settings, "TARGET_WIN_RATE_PCT", 70.0) or 70.0)
    min_factor = float(getattr(settings, "QUALITY_RISK_SCALE_MIN_FACTOR", 0.45) or 0.45)
    rec_floor = float(getattr(settings, "QUALITY_GATE_MIN_RECENCY_WR_PCT", 58.0) or 58.0)
    pf_floor = float(getattr(settings, "QUALITY_GATE_MIN_PROFIT_FACTOR", 1.08) or 1.08)
    streak_trigger = int(
        getattr(settings, "QUALITY_RISK_SCALE_LOSS_STREAK_TRIGGER", 2) or 2
    )
    dd_trigger = float(getattr(settings, "QUALITY_RISK_SCALE_DD_TRIGGER_PCT", 10.0) or 10.0)
    min_factor = _clamp(min_factor, 0.1, 1.0)

    wr = float(m.win_rate)
    recency_wr = float(m.recency_win_rate) * 100.0
    pf = float(m.profit_factor)
    losing_streak = int(m.losing_streak)

    # Wenn keine Schwäche erkennbar ist, volle Positionsgröße behalten.
    weakness_flags = (
        recency_wr < rec_floor,
        pf < pf_floor,
        losing_streak >= streak_trigger,
        float(getattr(m, "max_drawdown_pct", 0.0)) >= dd_trigger,
    )
    if not any(weakness_flags):
        return 1.0, "QUALITY_SCALE_OFF:healthy"

    wr_ratio = _clamp(wr / max(target_wr, 1e-9), 0.0, 1.2)
    rec_ratio = _clamp(recency_wr / max(target_wr, 1e-9), 0.0, 1.2)
    pf_ratio = _clamp(pf / max(pf_floor, 1e-9), 0.0, 1.2)
    streak_penalty = min(max(0, losing_streak - streak_trigger + 1) / 4.0, 1.0) * 0.35
    dd_penalty = min(
        max(0.0, float(getattr(m, "max_drawdown_pct", 0.0)) - dd_trigger)
        / max(dd_trigger, 1e-9),
        1.0,
    ) * 0.25

    quality = (wr_ratio * 0.40) + (rec_ratio * 0.35) + (pf_ratio * 0.25) - streak_penalty - dd_penalty
    quality = _clamp(quality, 0.0, 1.0)

    factor = min_factor + ((1.0 - min_factor) * quality)
    factor = round(_clamp(factor, min_factor, 1.0), 3)
    reason = (
        f"QUALITY_SCALE wr={wr:.1f}% rec={recency_wr:.1f}% pf={pf:.2f} "
        f"streak={losing_streak} factor={factor:.3f}"
    )
    return factor, reason


def strategy_adaptive_size_factor(strategy_name: str, perf_tracker: Any) -> float:
    """Kompatibilitäts-Wrapper (älterer Name)."""
    factor, _ = position_size_scaler_by_quality(strategy_name, perf_tracker)
    return factor
