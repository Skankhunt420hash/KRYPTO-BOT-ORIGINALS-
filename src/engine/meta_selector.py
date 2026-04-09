from typing import TYPE_CHECKING, Any, Dict, List, Optional

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

    Optionale Anpassungen:
        + Performance-Score (Strategy Scorer):  ±0.075
        + RL-Gewichtung (Q-Learning):            ±0.10
        + Market Intelligence (OB/Funding/...): ±0.15 (via confidence_boost)

    Blocking-Logik:
        - MTF nicht ausgerichtet + schwaches Signal → blockieren
        - Liquidation-Risiko "high" + enger SL → blockieren
        - Market-Bias gegen Signal-Side bei starkem Kontra-Signal → blockieren
    """

    def __init__(
        self,
        scorer: Optional["StrategyScorer"] = None,
        rl_weighter: Optional[Any] = None,
    ):
        self._scorer = scorer
        self._rl: Optional[Any] = rl_weighter   # RLSignalWeighter

    def set_scorer(self, scorer: "StrategyScorer") -> None:
        """Setzt oder ersetzt den StrategyScorer (nach Initialisierung)."""
        self._scorer = scorer

    def set_rl_weighter(self, rl: Any) -> None:
        """Setzt den RL-Weighter (nach Initialisierung)."""
        self._rl = rl

    def select(
        self,
        signals: List[EnhancedSignal],
        regime: Regime,
        symbol: str,
        market_context: Optional[Any] = None,   # MarketContext (optional)
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

            # ── Market Intelligence: Blockieren wenn stark gegen Signal ────
            mkt_bias = "neutral"
            conf_boost = 0.0
            if market_context is not None:
                mkt_bias = getattr(market_context, "overall_bias", "neutral")
                conf_boost = getattr(market_context, "confidence_boost", 0.0)
                liq_risk = getattr(market_context, "liq_risk", "low")
                mtf_aligned = getattr(market_context, "mtf_aligned", True)

                side_val = sig.side.value  # "long" | "short"

                # MTF nicht ausgerichtet + Signal unter 55 Konfidenz → skip
                if not mtf_aligned and sig.confidence < 55:
                    logger.debug(
                        f"[MTF-FILTER] {sig.strategy_name} [{side_val}] | "
                        f"MTF nicht ausgerichtet + conf={sig.confidence:.0f} < 55"
                    )
                    continue

                # Starker Kontra-Bias → blockieren
                contra = (
                    (side_val == "long" and mkt_bias == "strong_short") or
                    (side_val == "short" and mkt_bias == "strong_long")
                )
                if contra and sig.confidence < 70:
                    logger.debug(
                        f"[INTEL-FILTER] {sig.strategy_name} [{side_val}] | "
                        f"Kontra-Bias={mkt_bias} + conf={sig.confidence:.0f} < 70"
                    )
                    continue

                # Liquidation-Risiko hoch: nur hochqualitative Signale
                if liq_risk == "high" and sig.confidence < 75:
                    logger.debug(
                        f"[LIQ-FILTER] {sig.strategy_name} | "
                        f"Liq-Risk=high + conf={sig.confidence:.0f} < 75"
                    )
                    continue

            # ── Performance-Anpassung (StrategyScorer) ───────────────────────
            perf_score = 0.5
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

            # ── RL-Gewichtung (Q-Learning) ────────────────────────────────────
            rl_mult = 1.0
            if self._rl is not None:
                try:
                    rl_score = self._rl.get_score(
                        strategy=sig.strategy_name,
                        regime=regime.value,
                        side=sig.side.value,
                        confidence=sig.confidence,
                        market_bias=mkt_bias,
                    )
                    # RL-Score [0.5, 1.5] → Anpassung ±0.10
                    rl_adj = (rl_score - 1.0) * 0.10
                    rl_mult = rl_score
                    perf_adj += rl_adj
                except Exception as e:
                    logger.warning(f"RL.get_score fehlgeschlagen: {type(e).__name__}")

            # ── Market Intel Confidence Boost ─────────────────────────────────
            # conf_boost ist ±0.15 basierend auf OB/Funding/Sentiment
            perf_adj += conf_boost

            total = signal_score + perf_adj
            # Speichere signal_score + perf_adj für Logging des Gewinners
            scored.append((total, sig, signal_score, perf_adj, perf_score))
            logger.debug(
                f"  {sig.strategy_name:<22} [{sig.side.value.upper():<5}] | "
                f"fit={fit:.2f} conf={sig.confidence:.0f} rr={sig.rr:.2f} "
                f"vol={'✓' if sig.volume_confirmed else '✗'} "
                f"sig={signal_score:.3f} perf={perf_score:.2f} "
                f"rl={rl_mult:.2f} mkt={mkt_bias} adj={perf_adj:+.3f} "
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
