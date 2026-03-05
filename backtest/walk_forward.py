"""
Walk-Forward Evaluation Engine

Teilt historische OHLCV-Daten in In-Sample (IS) und Out-of-Sample (OOS)
Fenster auf und testet Strategien auf beiden Teilen separat.

Wichtige Designentscheidungen:
─────────────────────────────
• KEIN Lookahead-Bias:
    - IS und OOS sind strikt zeitlich getrennt.
    - Die ersten MIN_WARMUP_CANDLES des OOS-Fensters kommen aus dem IS-Bereich
      (nur für Indikator-Warmup), generieren aber KEINE Signale (BacktestEngine
      überspringt die ersten MIN_WARMUP_CANDLES intern mit range(WARMUP, n)).
    - OOS-Statistiken fließen NICHT in die IS-Bewertung ein.

• Zwei Walk-Forward-Modi:
    - "rolling":  IS-Fenster hat feste Länge und rollt vorwärts.
    - "anchored": IS-Fenster beginnt immer am Anfang und wächst.

• Wiederverwendung:
    - BacktestEngine (run_single / run_multi) unverändert.
    - calculate_stats() aus backtest.stats unverändert.
    - Keine doppelten Strategie-Kopien.
"""

import statistics
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pandas as pd

from backtest.engine import BacktestConfig, BacktestEngine, MIN_WARMUP_CANDLES
from backtest.stats import BacktestStats, calculate_stats
from src.strategies.base_strategy import EnhancedBaseStrategy
from src.engine.regime import RegimeEngine
from src.engine.meta_selector import MetaSelector
from src.utils.logger import setup_logger

logger = setup_logger("backtest.walk_forward")


# ─────────────────────────────────────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WalkForwardConfig:
    """Parameter für den Walk-Forward-Lauf."""

    is_candles: int = 600          # Länge des IS-Fensters in Kerzen
    oos_candles: int = 200         # Länge des OOS-Fensters in Kerzen
    step_candles: Optional[int] = None   # Rollschritt (default = oos_candles)
    mode: str = "rolling"          # "rolling" | "anchored"
    min_splits: int = 2            # Mindestanzahl valider Splits
    min_trades_per_split: int = 3  # Min. Trades für einen "validen" Split

    def __post_init__(self):
        if self.step_candles is None:
            self.step_candles = self.oos_candles
        if self.mode not in ("rolling", "anchored"):
            raise ValueError(f"WFO mode muss 'rolling' oder 'anchored' sein, nicht '{self.mode}'")
        if self.is_candles < MIN_WARMUP_CANDLES + 10:
            raise ValueError(
                f"is_candles ({self.is_candles}) muss >= "
                f"{MIN_WARMUP_CANDLES + 10} sein (Warmup + Mindest-Signale)"
            )
        if self.oos_candles < 10:
            raise ValueError(f"oos_candles ({self.oos_candles}) muss >= 10 sein")
        if self.step_candles < 1:
            raise ValueError(f"step_candles muss >= 1 sein")


# ─────────────────────────────────────────────────────────────────────────────
# Ergebnis-Strukturen
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SplitResult:
    """Ergebnisse eines einzelnen IS/OOS-Splits."""

    split_idx: int                   # 1-basierter Index des Splits

    # Zeiträume
    is_start: object                 # pd.Timestamp
    is_end: object
    oos_start: object
    oos_end: object
    is_candles_actual: int
    oos_candles_actual: int          # effektive OOS-Kerzen (ohne Warmup)

    # Statistiken
    is_stats: BacktestStats
    oos_stats: BacktestStats

    # Abgeleitete Kennzahlen
    is_profitable: bool = False
    oos_profitable: bool = False
    pnl_degradation: float = 0.0    # OOS_pnl_pct / IS_pnl_pct (wenn IS > 0)
    winrate_degradation: float = 0.0 # OOS_winrate / IS_winrate (wenn IS > 0)


@dataclass
class WalkForwardSummary:
    """Aggregierte Gesamtstatistik über alle Splits."""

    n_splits: int
    n_valid_splits: int              # Splits mit >= min_trades_per_split OOS-Trades
    n_profitable_oos: int            # Splits mit positivem OOS-PnL
    n_profitable_is: int

    # OOS-Aggregate (über valide Splits)
    oos_avg_pnl_pct: float
    oos_median_pnl_pct: float
    oos_avg_winrate: float
    oos_avg_profit_factor: float
    oos_avg_max_drawdown: float
    oos_total_trades: int

    # IS-Aggregate (Referenz)
    is_avg_pnl_pct: float
    is_avg_winrate: float

    # Overfitting-Indikatoren
    consistency_score: float         # n_profitable_oos / n_valid_splits (0–1)
    pnl_degradation_ratio: float     # avg(OOS_pnl_pct) / avg(IS_pnl_pct)
    winrate_degradation_ratio: float # avg(OOS_wr) / avg(IS_wr)
    overfitting_level: str           # "gering" | "mäßig" | "hoch"
    overfitting_explanation: str     # Menschenlesbarer Hinweis


@dataclass
class WalkForwardResult:
    """Vollständiges Walk-Forward-Ergebnis."""

    wf_config: WalkForwardConfig
    bt_config: BacktestConfig
    mode_label: str                  # z.B. "TrendContinuation" oder "Multi-Strategie"
    splits: List[SplitResult]
    summary: WalkForwardSummary


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

class WalkForwardEngine:
    """
    Führt einen Walk-Forward-Test auf historischen OHLCV-Daten durch.

    Verwendung:
        engine = WalkForwardEngine(wf_config, bt_config)
        result = engine.run_single(df, strategy)
        result = engine.run_multi(df, strategies)
    """

    def __init__(self, wf_config: WalkForwardConfig, bt_config: BacktestConfig):
        self.wf_config = wf_config
        self.bt_config = bt_config

    # ── Öffentliche API ───────────────────────────────────────────────────

    def run_single(
        self,
        df: pd.DataFrame,
        strategy: EnhancedBaseStrategy,
    ) -> WalkForwardResult:
        """Walk-Forward mit einer einzelnen Enhanced-Strategie."""
        self._validate_data(df)
        splits_idx = self._generate_splits(len(df))
        split_results = []

        for k, (is_start, is_end, oos_start, oos_end) in enumerate(splits_idx, start=1):
            logger.info(
                f"Split {k}/{len(splits_idx)}: "
                f"IS [{is_start}:{is_end}] OOS [{oos_start}:{oos_end}]"
            )
            try:
                sr = self._run_split_single(df, strategy, k, is_start, is_end, oos_start, oos_end)
                split_results.append(sr)
            except Exception as e:
                logger.warning(f"Split {k} fehlgeschlagen: {e} – übersprungen")

        if len(split_results) < self.wf_config.min_splits:
            raise ValueError(
                f"Nur {len(split_results)} valide Splits generiert, "
                f"mindestens {self.wf_config.min_splits} erforderlich. "
                f"Versuche mehr Daten oder kleinere Fenster."
            )

        summary = _compute_summary(split_results, self.wf_config.min_trades_per_split)
        return WalkForwardResult(
            wf_config=self.wf_config,
            bt_config=self.bt_config,
            mode_label=strategy.name,
            splits=split_results,
            summary=summary,
        )

    def run_multi(
        self,
        df: pd.DataFrame,
        strategies: List[EnhancedBaseStrategy],
        regime_engine: Optional[RegimeEngine] = None,
        selector: Optional[MetaSelector] = None,
    ) -> WalkForwardResult:
        """Walk-Forward mit Meta-Selector über alle Strategien."""
        self._validate_data(df)
        splits_idx = self._generate_splits(len(df))
        split_results = []

        _regime = regime_engine or RegimeEngine()
        _selector = selector or MetaSelector()  # kein StrategyScorer → kein IS→OOS Leakage

        for k, (is_start, is_end, oos_start, oos_end) in enumerate(splits_idx, start=1):
            logger.info(
                f"Split {k}/{len(splits_idx)}: "
                f"IS [{is_start}:{is_end}] OOS [{oos_start}:{oos_end}]"
            )
            try:
                sr = self._run_split_multi(
                    df, strategies, _regime, _selector,
                    k, is_start, is_end, oos_start, oos_end,
                )
                split_results.append(sr)
            except Exception as e:
                logger.warning(f"Split {k} fehlgeschlagen: {e} – übersprungen")

        if len(split_results) < self.wf_config.min_splits:
            raise ValueError(
                f"Nur {len(split_results)} valide Splits generiert, "
                f"mindestens {self.wf_config.min_splits} erforderlich. "
                f"Versuche mehr Daten oder kleinere Fenster."
            )

        strat_names = " + ".join(s.name for s in strategies)
        summary = _compute_summary(split_results, self.wf_config.min_trades_per_split)
        return WalkForwardResult(
            wf_config=self.wf_config,
            bt_config=self.bt_config,
            mode_label=f"Meta-Selector ({strat_names})",
            splits=split_results,
            summary=summary,
        )

    # ── Split-Generierung ─────────────────────────────────────────────────

    def _generate_splits(self, n_candles: int) -> List[Tuple[int, int, int, int]]:
        """
        Generiert (is_start, is_end, oos_start, oos_end) Indizes.
        OOS-Fenster überlappen sich NIE (no data leakage zwischen Splits).
        """
        cfg = self.wf_config
        is_c = cfg.is_candles
        oos_c = cfg.oos_candles
        step = cfg.step_candles

        splits = []
        i = 0
        while True:
            if cfg.mode == "rolling":
                # IS-Fenster hat feste Länge und rollt um 'step' vorwärts
                is_start = i * step
                is_end = is_start + is_c
            else:
                # anchored: IS startet immer bei 0, wächst mit jedem Schritt;
                # OOS-Fenster rückt entsprechend vor (kein Loop-Bug mehr)
                is_start = 0
                is_end = is_c + i * step

            oos_start = is_end
            oos_end = oos_start + oos_c

            if oos_end > n_candles:
                break  # Nicht genug Daten für diesen Split

            splits.append((is_start, is_end, oos_start, oos_end))
            i += 1

        if not splits:
            min_needed = is_c + oos_c
            raise ValueError(
                f"Keine Splits generierbar: {n_candles} Kerzen verfügbar, "
                f"mindestens {min_needed} benötigt "
                f"(IS={is_c} + OOS={oos_c}). "
                f"Verwende mehr Daten oder kleinere Fenster."
            )

        return splits

    # ── Split ausführen (Einzel) ──────────────────────────────────────────

    def _run_split_single(
        self,
        df: pd.DataFrame,
        strategy: EnhancedBaseStrategy,
        k: int,
        is_start: int, is_end: int,
        oos_start: int, oos_end: int,
    ) -> SplitResult:
        df_is, df_oos = self._slice_windows(df, is_start, is_end, oos_start, oos_end)

        engine_is = BacktestEngine(self.bt_config)
        is_trades = engine_is.run_single(df_is, strategy)

        engine_oos = BacktestEngine(self.bt_config)
        oos_trades = engine_oos.run_single(df_oos, strategy)

        return self._build_split_result(k, df, is_start, is_end, oos_start, oos_end,
                                        is_trades, oos_trades)

    # ── Split ausführen (Multi) ───────────────────────────────────────────

    def _run_split_multi(
        self,
        df: pd.DataFrame,
        strategies: List[EnhancedBaseStrategy],
        regime_engine: RegimeEngine,
        selector: MetaSelector,
        k: int,
        is_start: int, is_end: int,
        oos_start: int, oos_end: int,
    ) -> SplitResult:
        df_is, df_oos = self._slice_windows(df, is_start, is_end, oos_start, oos_end)

        engine_is = BacktestEngine(self.bt_config)
        is_trades = engine_is.run_multi(df_is, strategies, regime_engine, selector)

        engine_oos = BacktestEngine(self.bt_config)
        oos_trades = engine_oos.run_multi(df_oos, strategies, regime_engine, selector)

        return self._build_split_result(k, df, is_start, is_end, oos_start, oos_end,
                                        is_trades, oos_trades)

    # ── Fenster zuschneiden ───────────────────────────────────────────────

    def _slice_windows(
        self,
        df: pd.DataFrame,
        is_start: int, is_end: int,
        oos_start: int, oos_end: int,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Gibt IS und OOS DataFrames zurück.

        Das OOS-Fenster bekommt die letzten MIN_WARMUP_CANDLES des IS-Bereichs
        als Warmup-Prefix vorangestellt. Die BacktestEngine überspringt diese
        intern (range(MIN_WARMUP_CANDLES, n)) → KEIN Lookahead-Bias.
        """
        df_is = df.iloc[is_start:is_end].copy()

        # OOS bekommt Warmup-Prefix aus dem IS-Ende (kein Lookahead, da IS strikt vor OOS liegt)
        warmup_start = max(is_start, is_end - MIN_WARMUP_CANDLES)
        df_oos = df.iloc[warmup_start:oos_end].copy()

        return df_is, df_oos

    # ── Ergebnis aufbauen ─────────────────────────────────────────────────

    def _build_split_result(
        self,
        k: int,
        df: pd.DataFrame,
        is_start: int, is_end: int,
        oos_start: int, oos_end: int,
        is_trades: list,
        oos_trades: list,
    ) -> SplitResult:
        is_stats = calculate_stats(is_trades, self.bt_config.initial_balance)
        oos_stats = calculate_stats(oos_trades, self.bt_config.initial_balance)

        # Degradation-Verhältnis (IS→OOS): wie viel Leistung geht verloren?
        pnl_deg = _safe_ratio(oos_stats.total_pnl_pct, is_stats.total_pnl_pct)
        wr_deg = _safe_ratio(oos_stats.winrate_pct, is_stats.winrate_pct)

        return SplitResult(
            split_idx=k,
            is_start=df.index[is_start],
            is_end=df.index[is_end - 1],
            oos_start=df.index[oos_start],
            oos_end=df.index[min(oos_end, len(df)) - 1],
            is_candles_actual=is_end - is_start,
            oos_candles_actual=oos_end - oos_start,
            is_stats=is_stats,
            oos_stats=oos_stats,
            is_profitable=is_stats.total_pnl_abs > 0,
            oos_profitable=oos_stats.total_pnl_abs > 0,
            pnl_degradation=pnl_deg,
            winrate_degradation=wr_deg,
        )

    # ── Validierung ───────────────────────────────────────────────────────

    def _validate_data(self, df: pd.DataFrame) -> None:
        cfg = self.wf_config
        min_needed = cfg.is_candles + cfg.oos_candles
        if len(df) < min_needed:
            raise ValueError(
                f"Zu wenig Daten: {len(df)} Kerzen, mindestens "
                f"{min_needed} erforderlich "
                f"(IS={cfg.is_candles} + OOS={cfg.oos_candles})."
            )


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation & Overfitting-Erkennung
# ─────────────────────────────────────────────────────────────────────────────

def _compute_summary(
    splits: List[SplitResult],
    min_trades: int,
) -> WalkForwardSummary:
    """Berechnet die Gesamtzusammenfassung über alle Splits."""

    valid = [s for s in splits if s.oos_stats.n_trades >= min_trades]

    if not valid:
        # Fallback: alle Splits (auch mit 0 Trades)
        valid = splits

    n_valid = len(valid)
    n_profitable_oos = sum(1 for s in valid if s.oos_profitable)
    n_profitable_is = sum(1 for s in valid if s.is_profitable)

    oos_pnl_pcts = [s.oos_stats.total_pnl_pct for s in valid]
    oos_winrates = [s.oos_stats.winrate_pct for s in valid]
    oos_pfs = [
        s.oos_stats.profit_factor
        for s in valid
        if s.oos_stats.profit_factor != float("inf")
    ]
    oos_dds = [s.oos_stats.max_drawdown_pct for s in valid]

    is_pnl_pcts = [s.is_stats.total_pnl_pct for s in valid]
    is_winrates = [s.is_stats.winrate_pct for s in valid]

    avg_oos_pnl = statistics.mean(oos_pnl_pcts) if oos_pnl_pcts else 0.0
    med_oos_pnl = statistics.median(oos_pnl_pcts) if oos_pnl_pcts else 0.0
    avg_oos_wr = statistics.mean(oos_winrates) if oos_winrates else 0.0
    avg_oos_pf = statistics.mean(oos_pfs) if oos_pfs else 0.0
    avg_oos_dd = statistics.mean(oos_dds) if oos_dds else 0.0

    avg_is_pnl = statistics.mean(is_pnl_pcts) if is_pnl_pcts else 0.0
    avg_is_wr = statistics.mean(is_winrates) if is_winrates else 0.0

    consistency = n_profitable_oos / n_valid if n_valid > 0 else 0.0
    pnl_deg_ratio = _safe_ratio(avg_oos_pnl, avg_is_pnl)
    wr_deg_ratio = _safe_ratio(avg_oos_wr, avg_is_wr)

    level, explanation = _overfitting_assessment(
        consistency, pnl_deg_ratio, wr_deg_ratio, n_valid
    )

    return WalkForwardSummary(
        n_splits=len(splits),
        n_valid_splits=n_valid,
        n_profitable_oos=n_profitable_oos,
        n_profitable_is=n_profitable_is,
        oos_avg_pnl_pct=round(avg_oos_pnl, 3),
        oos_median_pnl_pct=round(med_oos_pnl, 3),
        oos_avg_winrate=round(avg_oos_wr, 1),
        oos_avg_profit_factor=round(avg_oos_pf, 2),
        oos_avg_max_drawdown=round(avg_oos_dd, 2),
        oos_total_trades=sum(s.oos_stats.n_trades for s in valid),
        is_avg_pnl_pct=round(avg_is_pnl, 3),
        is_avg_winrate=round(avg_is_wr, 1),
        consistency_score=round(consistency, 3),
        pnl_degradation_ratio=round(pnl_deg_ratio, 3),
        winrate_degradation_ratio=round(wr_deg_ratio, 3),
        overfitting_level=level,
        overfitting_explanation=explanation,
    )


def _overfitting_assessment(
    consistency: float,
    pnl_ratio: float,
    wr_ratio: float,
    n_valid: int,
) -> Tuple[str, str]:
    """
    Heuristischer Overfitting-Hinweis.

    Logik:
    - consistency_score: % profitabler OOS-Splits (> 0.6 = gut, < 0.4 = schlecht)
    - pnl_degradation_ratio: OOS-PnL / IS-PnL (> 0.5 = akzeptabel, < 0 = keine Übertragung)
    - winrate_degradation_ratio: ähnlich

    Die Heuristiken sind konservativ kalibriert – ein "geringer" Hinweis bedeutet
    nicht, dass die Strategie produktionsreif ist, sondern nur dass keine starken
    Overfitting-Signale sichtbar sind.
    """
    warnings = []

    if n_valid < 3:
        warnings.append("zu wenig Splits für robuste Schlussfolgerungen")

    if consistency < 0.40:
        warnings.append(
            f"nur {consistency:.0%} der OOS-Splits profitabel "
            f"(< 40% – inkonsistente Performance)"
        )
    elif consistency < 0.60:
        warnings.append(
            f"{consistency:.0%} der OOS-Splits profitabel "
            f"(< 60% – mäßige Konsistenz)"
        )

    if pnl_ratio == 0.0:
        # _safe_ratio gibt 0.0 zurück wenn IS-PnL ≈ 0 oder IS-PnL negativ
        # (negativ/negativ wäre ein täuschend positives Verhältnis – daher explizit blockiert)
        warnings.append(
            "IS-PnL ist null oder negativ – PnL-Degradation nicht aussagekräftig berechenbar"
        )
    elif pnl_ratio < 0:
        warnings.append(
            f"OOS-PnL negativ obwohl IS-PnL positiv "
            f"(Verhältnis={pnl_ratio:.2f} – starke Verschlechterung)"
        )
    elif 0 < pnl_ratio < 0.40:
        warnings.append(
            f"OOS-PnL = {pnl_ratio:.0%} des IS-PnL "
            f"(< 40% – erhebliche Degradation)"
        )

    if wr_ratio < 0.70:
        warnings.append(
            f"OOS-Win-Rate = {wr_ratio:.0%} der IS-Win-Rate "
            f"(< 70% – signifikante Win-Rate-Verschlechterung)"
        )

    if not warnings:
        return (
            "gering",
            "Keine starken Overfitting-Signale: OOS-Performance ist konsistent "
            "und nahe an IS. Trotzdem: reale Performance kann abweichen.",
        )

    n_warn = len(warnings)
    if n_warn >= 3 or (pnl_ratio < 0 and consistency < 0.40):
        level = "hoch"
        prefix = "⚠️ Hohes Overfitting-Risiko: "
    elif n_warn >= 2 or consistency < 0.40 or pnl_ratio < 0.20:
        level = "mäßig"
        prefix = "⚠ Mäßiges Overfitting-Risiko: "
    else:
        level = "gering"
        prefix = "ℹ Leichte Overfitting-Signale: "

    return level, prefix + "; ".join(warnings) + "."


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _safe_ratio(numerator: float, denominator: float) -> float:
    """
    Berechnet numerator / denominator für PnL-Degradations-Ratios.

    Gibt 0.0 zurück wenn:
    - denominator ≈ 0 (IS-PnL nahezu null)
    - denominator < 0 (IS bereits verlierend – Ratio wäre täuschend positiv)

    Beispiel: IS=-5, OOS=-3 → ohne Guard wäre ratio=0.6 (positiv, aber irreführend).
    """
    if denominator < 1e-9:   # deckt 0, negativ und nahezu-null ab
        return 0.0
    return numerator / denominator
