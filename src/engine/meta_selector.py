from typing import TYPE_CHECKING, Dict, List, Optional

from config.settings import settings
from src.strategies.signal import EnhancedSignal, Side
from src.engine.regime import Regime
from src.utils.logger import setup_logger

if TYPE_CHECKING:
    from src.engine.strategy_scorer import StrategyScorer

logger = setup_logger("meta_selector")

# Gewicht des Performance-Scores im Gesamt-Score.
# final = signal_score + (perf_score - 0.5) * PERF_WEIGHT
# Bei PERF_WEIGHT=0.15: max Anpassung = ±0.075 (konservativ)
_PERF_WEIGHT: float = settings.PERF_SELECTOR_WEIGHT

# Richtungs-Multiplikator: penalisiert Signale die gegen das Regime laufen.
# SHORT im TREND_UP = kontra-zyklisch = 0.55 Abzug auf den Fit-Score.
# LONG im TREND_DOWN = kontra-zyklisch = 0.55 Abzug.
# In RANGE / HIGH_VOL / LOW_VOL: beide Richtungen gleichwertig.
DIRECTION_MULTIPLIER: Dict[Regime, Dict[Side, float]] = {
    Regime.TREND_UP:        {Side.LONG: 1.00, Side.SHORT: 0.55, Side.NONE: 0.0},
    Regime.TREND_DOWN:      {Side.LONG: 0.55, Side.SHORT: 1.00, Side.NONE: 0.0},
    Regime.RANGE:           {Side.LONG: 1.00, Side.SHORT: 1.00, Side.NONE: 0.0},
    Regime.HIGH_VOLATILITY: {Side.LONG: 1.00, Side.SHORT: 1.00, Side.NONE: 0.0},
    Regime.LOW_VOLATILITY:  {Side.LONG: 1.00, Side.SHORT: 1.00, Side.NONE: 0.0},
}

# Regime-Fit-Tabelle: je höher der Wert, desto besser passt die Strategie zum Regime.
# Neue Strategien haben primäre Werte, Legacy-Strategien werden ebenfalls bewertet.
REGIME_STRATEGY_FIT: Dict[Regime, Dict[str, float]] = {
    Regime.TREND_UP: {
        "TrendContinuation": 1.00,
        "MomentumPullback": 0.90,
        "VolatilityBreakout": 0.50,
        "RangeReversion": 0.10,
        "RSI_EMA": 0.60,
        "MACD_Crossover": 0.70,
        "Combined": 0.65,
    },
    Regime.TREND_DOWN: {
        "TrendContinuation": 0.80,
        "MomentumPullback": 0.70,
        "VolatilityBreakout": 0.50,
        "RangeReversion": 0.10,
        "RSI_EMA": 0.40,
        "MACD_Crossover": 0.50,
        "Combined": 0.45,
    },
    Regime.RANGE: {
        "RangeReversion": 1.00,
        "MomentumPullback": 0.40,
        "VolatilityBreakout": 0.30,
        "TrendContinuation": 0.20,
        "RSI_EMA": 0.65,
        "MACD_Crossover": 0.30,
        "Combined": 0.50,
    },
    Regime.HIGH_VOLATILITY: {
        "VolatilityBreakout": 1.00,
        "MomentumPullback": 0.70,
        "TrendContinuation": 0.50,
        "RangeReversion": 0.10,
        "RSI_EMA": 0.30,
        "MACD_Crossover": 0.40,
        "Combined": 0.35,
    },
    Regime.LOW_VOLATILITY: {
        "RangeReversion": 0.85,
        "TrendContinuation": 0.30,
        "MomentumPullback": 0.20,
        "VolatilityBreakout": 0.10,
        "RSI_EMA": 0.55,
        "MACD_Crossover": 0.55,
        "Combined": 0.55,
    },
}

# Mindest-Regime-Fit damit ein Signal überhaupt in die Bewertung kommt
MIN_REGIME_FIT: float = 0.35


class MetaSelector:
    """
    Bewertet alle eingehenden Signale und wählt das beste für den aktuellen
    Markt aus.

    Basis-Score-Formel:
        signal_score = regime_fit * 0.35 + confidence_norm * 0.35
                     + rr_score * 0.20 + volume_bonus * 0.10

    Optionale Performance-Anpassung (wenn StrategyScorer übergeben):
        final_score = signal_score + (perf_score - 0.5) * PERF_WEIGHT
        → max ±0.075 bei PERF_WEIGHT=0.15 (konservativ)
        → nur aktiv wenn genug historische Trades vorhanden
    """

    def __init__(self, scorer: Optional["StrategyScorer"] = None):
        self._scorer = scorer

    def set_scorer(self, scorer: "StrategyScorer") -> None:
        """Setzt oder ersetzt den StrategyScorer (nach Initialisierung)."""
        self._scorer = scorer

    def select(
        self,
        signals: List[EnhancedSignal],
        regime: Regime,
        symbol: str,
    ) -> Optional[EnhancedSignal]:
        actionable = [
            s for s in signals
            if s.symbol == symbol and s.is_actionable()
        ]

        if not actionable:
            logger.debug(
                f"{symbol} | Kein aktionsfähiges Signal "
                f"({len(signals)} Strategien liefen, Regime: {regime.value})"
            )
            return None

        regime_fits = REGIME_STRATEGY_FIT.get(regime, {})
        scored: List[tuple] = []

        dir_mults = DIRECTION_MULTIPLIER.get(regime, {})

        for sig in actionable:
            base_fit = regime_fits.get(sig.strategy_name, 0.30)
            dir_mult = dir_mults.get(sig.side, 1.0)
            fit = base_fit * dir_mult

            if fit < MIN_REGIME_FIT:
                logger.debug(
                    f"[REGIME-FILTER] {sig.strategy_name} [{sig.side.value.upper()}] "
                    f"| {symbol} | fit={fit:.2f} (base={base_fit:.2f} × dir={dir_mult:.2f}) "
                    f"< {MIN_REGIME_FIT} – blockiert"
                )
                continue

            conf_score = sig.confidence / 100.0
            rr_score = min(sig.rr / 5.0, 1.0)
            vol_bonus = 0.10 if sig.volume_confirmed else 0.0

            signal_score = (
                fit * 0.35
                + conf_score * 0.35
                + rr_score * 0.20
                + vol_bonus * 0.10
            )

            # Performance-Anpassung (optional, konservativ)
            perf_score = 0.5   # neutral default
            perf_adj = 0.0
            if self._scorer is not None and _PERF_WEIGHT > 0:
                try:
                    perf_score = self._scorer.get_score(
                        sig.strategy_name, regime.value
                    )
                    perf_adj = (perf_score - 0.5) * _PERF_WEIGHT
                except Exception as e:
                    logger.warning(
                        f"Scorer.get_score fehlgeschlagen für {sig.strategy_name}: "
                        f"{type(e).__name__} – neutraler Score verwendet"
                    )

            total = signal_score + perf_adj
            # Speichere signal_score + perf_adj für Logging des Gewinners
            scored.append((total, sig, signal_score, perf_adj, perf_score))
            logger.debug(
                f"  {sig.strategy_name:<22} [{sig.side.value.upper():<5}] | "
                f"fit={fit:.2f} conf={sig.confidence:.0f} rr={sig.rr:.2f} "
                f"vol={'✓' if sig.volume_confirmed else '✗'} "
                f"sig={signal_score:.3f} perf={perf_score:.2f} adj={perf_adj:+.3f} "
                f"→ final={total:.3f}"
            )

        if not scored:
            logger.debug(
                f"{symbol} | Alle Signale durch Regime-Filter blockiert "
                f"(Regime: {regime.value})"
            )
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best, best_sig_score, best_perf_adj, best_perf_score = scored[0]

        # Logging: signal_score + perf_adj + final für den Gewinner
        if self._scorer is not None and _PERF_WEIGHT > 0:
            perf_tag = (
                f" | sig={best_sig_score:.3f} "
                f"perf={best_perf_score:.2f} adj={best_perf_adj:+.3f}"
            )
        else:
            perf_tag = ""

        logger.info(
            f"[cyan]META-SELECTOR[/cyan] {symbol} | "
            f"Gewinner: [bold]{best.strategy_name}[/bold] | "
            f"final={best_score:.3f}{perf_tag} | "
            f"Regime={regime.value} | Seite={best.side.value.upper()} | "
            f"conf={best.confidence:.0f} | RR={best.rr:.2f} | "
            f"{best.reason}"
        )
        return best
