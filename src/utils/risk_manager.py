from dataclasses import dataclass, field
from typing import Dict, Optional
from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("risk_manager")


@dataclass
class Position:
    symbol: str
    entry_price: float
    amount: float
    stop_loss: float
    take_profit: float
    side: str = "long"
    highest_price: float = field(default=0.0)
    strategy_name: str = ""  # welche Strategie diese Position eröffnet hat

    @property
    def value(self) -> float:
        return self.entry_price * self.amount

    def update_trailing_stop(self, current_price: float, trail_pct: float):
        """Passt den Trailing-Stop je nach Positionsrichtung an."""
        if self.side == "long":
            if current_price > self.highest_price:
                self.highest_price = current_price
                new_stop = current_price * (1 - trail_pct / 100)
                if new_stop > self.stop_loss:
                    self.stop_loss = new_stop
        else:  # short: SL bewegt sich nach unten mit dem Preis
            if current_price < self.highest_price:  # highest_price als lowest-Proxy
                self.highest_price = current_price
                new_stop = current_price * (1 + trail_pct / 100)
                if new_stop < self.stop_loss:
                    self.stop_loss = new_stop


class RiskManager:
    """Verwaltet Risiko, Positionsgrößen und offene Trades."""

    def __init__(self, initial_balance: float = None):
        self.balance = initial_balance or settings.PAPER_TRADING_BALANCE
        self.open_positions: Dict[str, Position] = {}
        self.max_position_pct = settings.MAX_POSITION_SIZE_PERCENT
        self.max_open_trades = settings.MAX_OPEN_TRADES
        self.stop_loss_pct = settings.STOP_LOSS_PERCENT
        self.take_profit_pct = settings.TAKE_PROFIT_PERCENT
        self.trailing_stop = settings.TRAILING_STOP

        self.total_trades = 0
        self.winning_trades = 0
        self.total_pnl = 0.0

    def can_open_trade(self, symbol: str) -> bool:
        if symbol in self.open_positions:
            logger.debug(f"Position für {symbol} bereits offen – kein neuer Trade")
            return False
        if len(self.open_positions) >= self.max_open_trades:
            logger.warning(
                f"Maximale offene Trades erreicht ({self.max_open_trades}) – Trade übersprungen"
            )
            return False
        return True

    def calculate_position_size(self, price: float) -> float:
        """Berechnet die Positionsgröße basierend auf verfügbarem Kapital."""
        risk_amount = self.balance * (self.max_position_pct / 100)
        amount = risk_amount / price
        return round(amount, 6)

    def open_position(self, symbol: str, price: float, amount: float) -> Optional[Position]:
        if not self.can_open_trade(symbol):
            return None

        stop_loss = price * (1 - self.stop_loss_pct / 100)
        take_profit = price * (1 + self.take_profit_pct / 100)

        position = Position(
            symbol=symbol,
            entry_price=price,
            amount=amount,
            stop_loss=stop_loss,
            take_profit=take_profit,
            highest_price=price,
        )
        self.open_positions[symbol] = position
        cost = price * amount
        self.balance -= cost

        logger.info(
            f"[green]Position eröffnet:[/green] {symbol} | "
            f"Preis: {price:.4f} | Menge: {amount:.6f} | "
            f"SL: {stop_loss:.4f} | TP: {take_profit:.4f}"
        )
        return position

    def close_position(self, symbol: str, current_price: float) -> Optional[float]:
        position = self.open_positions.pop(symbol, None)
        if position is None:
            return None

        # PnL-Berechnung je nach Seite:
        # LONG:  Gewinn wenn exit > entry  → (exit - entry) * amount
        # SHORT: Gewinn wenn exit < entry  → (entry - exit) * amount
        if position.side == "long":
            pnl = (current_price - position.entry_price) * position.amount
            self.balance += current_price * position.amount
        else:  # short
            pnl = (position.entry_price - current_price) * position.amount
            # Margin (entry * amount) zurück + PnL (kann negativ sein)
            self.balance += position.entry_price * position.amount + pnl

        self.total_pnl += pnl
        self.total_trades += 1

        if pnl > 0:
            self.winning_trades += 1
            color = "green"
        else:
            color = "red"

        side_label = "[LONG]" if position.side == "long" else "[SHORT]"
        logger.info(
            f"[{color}]Position geschlossen {side_label}:[/{color}] {symbol} | "
            f"Einstieg: {position.entry_price:.4f} | Ausstieg: {current_price:.4f} | "
            f"PnL: {pnl:+.4f} USDT | Balance: {self.balance:.2f} USDT"
        )
        return pnl

    def check_exit_conditions(self, symbol: str, current_price: float) -> Optional[str]:
        position = self.open_positions.get(symbol)
        if not position:
            return None

        if self.trailing_stop:
            position.update_trailing_stop(current_price, self.stop_loss_pct)

        if position.side == "long":
            # LONG: SL wenn Preis fällt, TP wenn Preis steigt
            if current_price <= position.stop_loss:
                logger.warning(
                    f"[red]STOP-LOSS [LONG][/red] {symbol} @ {current_price:.4f} "
                    f"(SL={position.stop_loss:.4f})"
                )
                return "stop_loss"
            if current_price >= position.take_profit:
                logger.info(
                    f"[green]TAKE-PROFIT [LONG][/green] {symbol} @ {current_price:.4f} "
                    f"(TP={position.take_profit:.4f})"
                )
                return "take_profit"
        else:  # short
            # SHORT: SL wenn Preis steigt, TP wenn Preis fällt
            if current_price >= position.stop_loss:
                logger.warning(
                    f"[red]STOP-LOSS [SHORT][/red] {symbol} @ {current_price:.4f} "
                    f"(SL={position.stop_loss:.4f})"
                )
                return "stop_loss"
            if current_price <= position.take_profit:
                logger.info(
                    f"[green]TAKE-PROFIT [SHORT][/green] {symbol} @ {current_price:.4f} "
                    f"(TP={position.take_profit:.4f})"
                )
                return "take_profit"

        return None

    def get_stats(self) -> dict:
        winrate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        return {
            "balance": round(self.balance, 2),
            "total_pnl": round(self.total_pnl, 4),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "winrate_pct": round(winrate, 1),
            "open_positions": len(self.open_positions),
        }
