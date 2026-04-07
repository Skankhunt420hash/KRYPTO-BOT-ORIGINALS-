"""
Heuristische „Gewinnchance“ / Erfolgsquote (0–100 %) für UI und Telegram.

Hinweis für Nutzer: Die reine Heuristik ist keine statistische Trefferquote.
Optional wird sie mit der gemessenen Strategie-Win-Rate aus der Trade-DB gemischt
(siehe effective_entry_win_chance_pct).
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from config.settings import settings


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
