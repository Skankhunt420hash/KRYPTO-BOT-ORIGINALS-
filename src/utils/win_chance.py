"""
Heuristische „Gewinnchance“ / Erfolgsquote (0–100 %) für UI und Telegram.

Hinweis für Nutzer: Das ist keine statistische Vorhersage des Marktes, sondern eine
zusammengefasste Kennzahl aus Strategie-Konfidenz, optional Meta-/Brain-Score und RR.
"""

from __future__ import annotations

from typing import Optional, Tuple


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
