from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


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
    timestamp: datetime = field(default_factory=datetime.utcnow)
    regime: str = "UNKNOWN"
    volume_confirmed: bool = False

    def is_actionable(self) -> bool:
        """Signal ist ausführbar wenn Seite gesetzt, Konfidenz & RR ausreichend."""
        return (
            self.side != Side.NONE
            and self.confidence >= 40.0
            and self.rr >= 1.5
            and self.entry > 0
        )

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
