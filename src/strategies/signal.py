from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from config.settings import settings


class Side(Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class EnhancedSignal:
    """
    Einheitliches Signal-Objekt für alle Strategien im Multi-Strategy-Modus.
    confidence: 0-100 (0 = kein Signal, 100 = maximale Überzeugung)
    rr: Risk/Reward Ratio (z.B. 2.0 = doppeltes Risiko als Reward)
    """

    strategy_name: str
    symbol: str
    timeframe: str
    side: Side
    confidence: float
    entry: float
    stop_loss: float
    take_profit: float
    rr: float
    reason: str
    timestamp: datetime = field(default_factory=_utc_now)
    regime: str = "UNKNOWN"
    volume_confirmed: bool = False

    def is_actionable(self) -> bool:
        """Signal ist ausführbar wenn Seite gesetzt, Konfidenz & RR ausreichend."""
        if self.side == Side.NONE:
            return False
        if self.entry <= 0:
            return False
        if self.confidence < float(settings.MIN_CONFIDENCE):
            return False
        if self.rr < float(settings.MIN_RR):
            return False
        # Long/Short-Konsistenz prüfen (verhindert fehlerhafte Signale).
        if self.side == Side.LONG and not (self.stop_loss < self.entry < self.take_profit):
            return False
        if self.side == Side.SHORT and not (self.stop_loss > self.entry > self.take_profit):
            return False
        return True

    @property
    def risk_pct(self) -> float:
        if self.entry <= 0:
            return 0.0
        return abs(self.entry - self.stop_loss) / self.entry * 100

    @property
    def reward_pct(self) -> float:
        if self.entry <= 0:
            return 0.0
        return abs(self.take_profit - self.entry) / self.entry * 100
