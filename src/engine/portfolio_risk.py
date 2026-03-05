"""
Portfolio Risk Engine

Zentrale Komponente für professionelles Portfolio-Risikomanagement.
Verantwortlich für:
  1. Position Sizing (3 Modi)
  2. Portfolio-Level Exposure-Limits
  3. Klumpenrisiko-Schutz über Symbol-Cluster

Klare Trennung der Verantwortlichkeiten:
  - Signalqualität:   MetaSelector (Scoring, Regime-Fit)
  - Trade-Freigabe:   RiskEngine (Cooldowns, Daily-Loss, Duplikat-Schutz)
  - Positionsgröße:   PortfolioRiskEngine (diese Datei)

Backtest-Kompatibilität:
  - Keine Exchange-API-Aufrufe
  - Alle Berechnungen rein auf Balance + offene Positionen
  - Kann direkt von BacktestEngine genutzt werden (TODO: Adapter)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config.settings import settings
from src.strategies.signal import EnhancedSignal, Side
from src.utils.risk_manager import Position
from src.utils.logger import setup_logger

logger = setup_logger("portfolio_risk")

# Mindest-Konfidenz für is_actionable() – muss mit signal.py übereinstimmen
_MIN_CONFIDENCE = 40.0


# ─────────────────────────────────────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PortfolioRiskConfig:
    """Alle Portfolio-Risk-Parameter auf einem Fleck."""

    # ── Position Sizing ────────────────────────────────────────────────
    sizing_mode: str = "fixed_risk_pct"
    fixed_notional_usd: float = 200.0
    risk_per_trade_pct: float = 1.0
    min_position_notional: float = 10.0
    max_position_notional: float = 5000.0
    confidence_min_scale: float = 0.5
    confidence_max_scale: float = 1.5

    # ── Portfolio-Limits ───────────────────────────────────────────────
    max_total_open_risk_pct: float = 10.0
    max_positions_total: int = 5
    max_positions_per_symbol: int = 1
    max_strategy_positions: int = 2
    max_same_direction_pct: float = 80.0

    # ── Cluster-Risiko ─────────────────────────────────────────────────
    cluster_groups: Dict[str, str] = field(default_factory=dict)
    max_cluster_risk_pct: float = 6.0


def build_config_from_settings(trading_pairs: Optional[List[str]] = None) -> PortfolioRiskConfig:
    """Erstellt PortfolioRiskConfig aus den zentralen Settings."""
    pairs = trading_pairs or settings.TRADING_PAIRS
    return PortfolioRiskConfig(
        sizing_mode=settings.POSITION_SIZING_MODE,
        fixed_notional_usd=settings.FIXED_NOTIONAL_USD,
        risk_per_trade_pct=settings.RISK_PER_TRADE_PCT,
        min_position_notional=settings.MIN_POSITION_NOTIONAL,
        max_position_notional=settings.MAX_POSITION_NOTIONAL,
        confidence_min_scale=settings.CONFIDENCE_MIN_SCALE,
        confidence_max_scale=settings.CONFIDENCE_MAX_SCALE,
        max_total_open_risk_pct=settings.MAX_TOTAL_OPEN_RISK_PCT,
        max_positions_total=settings.MAX_POSITIONS_TOTAL,
        max_positions_per_symbol=settings.MAX_POSITIONS_PER_SYMBOL,
        max_strategy_positions=settings.MAX_STRATEGY_POSITIONS,
        max_same_direction_pct=settings.MAX_SAME_DIRECTION_EXPOSURE_PCT,
        cluster_groups=_build_default_clusters(pairs),
        max_cluster_risk_pct=settings.MAX_CLUSTER_RISK_PCT,
    )


def _build_default_clusters(trading_pairs: List[str]) -> Dict[str, str]:
    """
    Heuristisches Clustering: BTC/ETH → 'majors', alles andere → 'alts'.
    Wird überschrieben wenn der Nutzer eigene Cluster konfiguriert.
    """
    clusters: Dict[str, str] = {}
    for pair in trading_pairs:
        base = pair.split("/")[0].upper() if "/" in pair else pair.upper()
        clusters[pair] = "majors" if base in ("BTC", "ETH") else "alts"
    return clusters


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio Risk Engine
# ─────────────────────────────────────────────────────────────────────────────

class PortfolioRiskEngine:
    """
    Berechnet Positionsgrößen und prüft Portfolio-weite Exposure-Limits.

    Verwendung (von RiskEngine aufgerufen):
        amount, info = engine.calculate_size(signal, balance)
        allowed, reason = engine.check_portfolio_limits(signal, balance, positions, amount)
    """

    def __init__(self, config: Optional[PortfolioRiskConfig] = None):
        self.cfg = config or build_config_from_settings()
        logger.info(
            f"PortfolioRiskEngine aktiv | "
            f"Sizing={self.cfg.sizing_mode} | "
            f"Risiko/Trade={self.cfg.risk_per_trade_pct}% | "
            f"Max-Portfolio-Risiko={self.cfg.max_total_open_risk_pct}% | "
            f"Max-Pos={self.cfg.max_positions_total} | "
            f"Cluster-Limit={self.cfg.max_cluster_risk_pct}%"
        )

    # ── Positionsgröße berechnen ──────────────────────────────────────────

    def calculate_size(
        self,
        signal: EnhancedSignal,
        balance: float,
    ) -> Tuple[float, str]:
        """
        Berechnet die Positionsgröße (Anzahl Coins).

        Returns:
            (amount, sizing_explanation)
            amount == 0.0 → Trade soll blockiert werden

        Gültig für LONG und SHORT (SL-Distanz ist immer positiv).
        """
        entry = signal.entry
        stop_loss = signal.stop_loss

        if entry <= 0:
            return 0.0, f"Ungültiger Entry-Preis: {entry}"
        if balance <= 0:
            return 0.0, f"Balance ist 0 oder negativ: {balance:.2f}"

        # SL-Distanz: immer positiv, unabhängig von Long/Short
        risk_distance = abs(entry - stop_loss)
        min_sl_distance = entry * 0.0001  # Mindest-Distanz: 0.01% des Preises
        if risk_distance < min_sl_distance:
            return 0.0, (
                f"Stop-Loss zu eng: Distanz={risk_distance:.6f} "
                f"< Minimum={min_sl_distance:.6f} – Trade blockiert"
            )

        # Modus-basierte Berechnung
        if self.cfg.sizing_mode == "fixed_notional":
            amount, expl = self._fixed_notional(entry)
        elif self.cfg.sizing_mode == "confidence_scaled":
            amount, expl = self._confidence_scaled(
                entry, risk_distance, balance, signal.confidence
            )
        else:
            # fixed_risk_pct (Default + Fallback)
            if self.cfg.sizing_mode != "fixed_risk_pct":
                logger.warning(
                    f"Unbekannter Sizing-Modus '{self.cfg.sizing_mode}', "
                    f"Fallback auf fixed_risk_pct"
                )
            amount, expl = self._fixed_risk_pct(entry, risk_distance, balance)

        if amount <= 0:
            return 0.0, f"Berechnete Menge ≤ 0: {expl}"

        # Min/Max-Grenzen (in USDT-Notional)
        notional = amount * entry
        if notional < self.cfg.min_position_notional:
            old_amount = amount
            amount = self.cfg.min_position_notional / entry
            expl += (
                f" | Unter Minimum ({notional:.2f} < {self.cfg.min_position_notional:.0f} USD) "
                f"→ auf {amount:.6f} erhöht"
            )
        elif notional > self.cfg.max_position_notional:
            amount = self.cfg.max_position_notional / entry
            expl += (
                f" | Über Maximum ({notional:.2f} > {self.cfg.max_position_notional:.0f} USD) "
                f"→ auf {amount:.6f} begrenzt"
            )

        return round(amount, 8), expl

    def _fixed_notional(self, entry: float) -> Tuple[float, str]:
        """
        Formel: amount = FIXED_NOTIONAL_USD / entry_price
        Beispiel: 200 USD / 40000 USD/BTC = 0.005 BTC
        """
        amount = self.cfg.fixed_notional_usd / entry
        return amount, (
            f"fixed_notional: {self.cfg.fixed_notional_usd:.0f} USD "
            f"/ {entry:.4f} = {amount:.6f}"
        )

    def _fixed_risk_pct(
        self, entry: float, risk_distance: float, balance: float
    ) -> Tuple[float, str]:
        """
        Formel: amount = (balance × risk_pct) / sl_distance_in_usd
        Beispiel: (10000 × 1%) / 200 USD/BTC = 0.5 BTC
                  Notional = 0.5 × 40000 = 20000 USD, Risiko = 100 USD = 1%

        Die Positionsgröße wird so gewählt, dass bei SL-Auslösung genau
        risk_per_trade_pct % des Kapitals verloren gehen.
        """
        risk_usd = balance * (self.cfg.risk_per_trade_pct / 100)
        amount = risk_usd / risk_distance
        return amount, (
            f"fixed_risk: {self.cfg.risk_per_trade_pct}% × {balance:.0f} USDT "
            f"= {risk_usd:.2f} USD / {risk_distance:.4f} SL-Dist = {amount:.6f}"
        )

    def _confidence_scaled(
        self,
        entry: float,
        risk_distance: float,
        balance: float,
        confidence: float,
    ) -> Tuple[float, str]:
        """
        Formel: scale = lerp(MIN_SCALE, MAX_SCALE, (conf - 40) / 60)
                amount = (balance × risk_pct × scale) / sl_distance

        Bei confidence=40 (Minimum):  scale=MIN_SCALE (z.B. 0.5 → halbes Risiko)
        Bei confidence=100 (Maximum): scale=MAX_SCALE (z.B. 1.5 → 1.5× Risiko)
        """
        conf_norm = max(0.0, min(1.0, (confidence - _MIN_CONFIDENCE) / (100.0 - _MIN_CONFIDENCE)))
        scale = (
            self.cfg.confidence_min_scale
            + conf_norm * (self.cfg.confidence_max_scale - self.cfg.confidence_min_scale)
        )
        risk_usd = balance * (self.cfg.risk_per_trade_pct / 100) * scale
        amount = risk_usd / risk_distance
        return amount, (
            f"confidence_scaled: conf={confidence:.0f}/100 → scale={scale:.2f} | "
            f"{self.cfg.risk_per_trade_pct}% × {balance:.0f} × {scale:.2f} "
            f"= {risk_usd:.2f} / {risk_distance:.4f} = {amount:.6f}"
        )

    # ── Portfolio-Limits prüfen ───────────────────────────────────────────

    def check_portfolio_limits(
        self,
        signal: EnhancedSignal,
        balance: float,
        open_positions: Dict[str, Position],
        proposed_amount: float,
    ) -> Tuple[bool, str]:
        """
        Prüft alle Portfolio-Level-Limits.
        Returns: (allowed: bool, reason: str)
        """
        n_open = len(open_positions)

        # 1. Max Positionen gesamt
        if n_open >= self.cfg.max_positions_total:
            return False, (
                f"PORTFOLIO MAX-POS: {n_open}/{self.cfg.max_positions_total} "
                f"Positionen offen"
            )

        # 2. Max Positionen pro Symbol
        sym_count = sum(1 for p in open_positions.values() if p.symbol == signal.symbol)
        if sym_count >= self.cfg.max_positions_per_symbol:
            return False, (
                f"PORTFOLIO SYM-LIMIT: {sym_count}/{self.cfg.max_positions_per_symbol} "
                f"Pos. in {signal.symbol}"
            )

        # 3. Max Strategie-Positionen
        strat_count = sum(
            1 for p in open_positions.values()
            if p.strategy_name == signal.strategy_name
        )
        if strat_count >= self.cfg.max_strategy_positions:
            return False, (
                f"PORTFOLIO STRAT-LIMIT: {strat_count}/{self.cfg.max_strategy_positions} "
                f"Pos. in Strategie {signal.strategy_name}"
            )

        # 4. Max gleiche Richtung (LONG/SHORT Klumpen)
        if n_open > 0:
            same_dir = sum(
                1 for p in open_positions.values()
                if p.side == signal.side.value
            )
            dir_pct = same_dir / n_open * 100
            if dir_pct >= self.cfg.max_same_direction_pct:
                return False, (
                    f"PORTFOLIO DIR-LIMIT: {dir_pct:.0f}% ({same_dir}/{n_open}) "
                    f"der Positionen sind {signal.side.value.upper()} "
                    f"(Limit: {self.cfg.max_same_direction_pct:.0f}%)"
                )

        # 5. Max Gesamt-Portfolio-Risiko
        current_risk_pct = self._total_open_risk_pct(open_positions, balance)
        new_risk_pct = self._position_risk_pct(signal, proposed_amount, balance)
        total_after = current_risk_pct + new_risk_pct
        if total_after > self.cfg.max_total_open_risk_pct:
            return False, (
                f"PORTFOLIO RISK-LIMIT: {total_after:.2f}% > {self.cfg.max_total_open_risk_pct:.1f}% "
                f"(aktuell={current_risk_pct:.2f}% + neu={new_risk_pct:.2f}%)"
            )

        # 6. Cluster-Risiko
        cluster = self.cfg.cluster_groups.get(signal.symbol, "other")
        cluster_current = self._cluster_risk_pct(cluster, open_positions, balance)
        cluster_after = cluster_current + new_risk_pct
        if cluster_after > self.cfg.max_cluster_risk_pct:
            return False, (
                f"PORTFOLIO CLUSTER-LIMIT '{cluster}': {cluster_after:.2f}% "
                f"> {self.cfg.max_cluster_risk_pct:.1f}% "
                f"(aktuell={cluster_current:.2f}% + neu={new_risk_pct:.2f}%)"
            )

        # Alles OK: detailliertes Logging
        logger.info(
            f"[green]PORTFOLIO OK[/green] {signal.symbol} [{signal.side.value.upper()}] | "
            f"Strategie: {signal.strategy_name} | "
            f"Pos: {n_open + 1}/{self.cfg.max_positions_total} | "
            f"Risiko: {total_after:.2f}%/{self.cfg.max_total_open_risk_pct:.1f}% | "
            f"Cluster '{cluster}': {cluster_after:.2f}%/{self.cfg.max_cluster_risk_pct:.1f}%"
        )
        return True, "OK"

    # ── Exposure-Berechnungen ─────────────────────────────────────────────

    def _position_risk_pct(
        self, signal: EnhancedSignal, amount: float, balance: float
    ) -> float:
        """Risiko der geplanten Position als % des Kapitals."""
        if balance <= 0 or amount <= 0:
            return 0.0
        risk_usd = abs(signal.entry - signal.stop_loss) * amount
        return (risk_usd / balance) * 100

    def _total_open_risk_pct(
        self, open_positions: Dict[str, Position], balance: float
    ) -> float:
        """Summe aller offenen SL-Risiken als % des Kapitals."""
        if balance <= 0:
            return 0.0
        total_risk = sum(
            abs(p.entry_price - p.stop_loss) * p.amount
            for p in open_positions.values()
        )
        return (total_risk / balance) * 100

    def _cluster_risk_pct(
        self,
        cluster: str,
        open_positions: Dict[str, Position],
        balance: float,
    ) -> float:
        """Risiko aller Positionen im gleichen Cluster als % des Kapitals."""
        if balance <= 0:
            return 0.0
        cluster_risk = sum(
            abs(p.entry_price - p.stop_loss) * p.amount
            for sym, p in open_positions.items()
            if self.cfg.cluster_groups.get(sym, "other") == cluster
        )
        return (cluster_risk / balance) * 100

    # ── Exposure-Snapshot (für Logging / Status) ──────────────────────────

    def get_exposure_snapshot(
        self,
        open_positions: Dict[str, Position],
        balance: float,
    ) -> dict:
        """Gibt einen aktuellen Portfolio-Exposure-Überblick zurück."""
        total_risk = self._total_open_risk_pct(open_positions, balance)
        long_count = sum(1 for p in open_positions.values() if p.side == "long")
        short_count = sum(1 for p in open_positions.values() if p.side == "short")

        known_clusters = set(self.cfg.cluster_groups.values()) | {"other"}
        cluster_risks = {
            c: round(self._cluster_risk_pct(c, open_positions, balance), 2)
            for c in known_clusters
        }

        return {
            "n_positions": len(open_positions),
            "total_risk_pct": round(total_risk, 2),
            "long_positions": long_count,
            "short_positions": short_count,
            "cluster_risks": cluster_risks,
            "balance": round(balance, 2),
        }
