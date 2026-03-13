"""
Backtest Engine – Candle-by-Candle Simulation.

Ausführungslogik:
  1. Signal am Schluss von Kerze i erzeugt (strategy.analyze auf Fenster df[:i+1])
  2. Einstieg am Open der nächsten Kerze i+1 + Slippage (kein Look-Ahead)
  3. TP/SL werden gegen High/Low jeder Folgekerze geprüft
  4. Treffen beide SL und TP in derselben Kerze → SL bevorzugt (konservativ)

Gebühren & Slippage:
  - fee_pct:      % der Notional pro Seite (Open + Close je einmal)
  - slippage_pct: % ungünstige Preisverschiebung bei Kauf/Verkauf
                  Long-Entry:  fill = open * (1 + slippage)
                  Short-Entry: fill = open * (1 - slippage)
                  Long-Exit:   fill = raw  * (1 - slippage)
                  Short-Exit:  fill = raw  * (1 + slippage)
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.strategies.base_strategy import EnhancedBaseStrategy
from src.strategies.signal import EnhancedSignal, Side
from src.engine.regime import RegimeEngine
from src.engine.meta_selector import MetaSelector
from src.utils.logger import setup_logger

logger = setup_logger("backtest.engine")

# Kerzen die der Strategie als "Anlaufzeit" für Indikatoren dienen
# (entspricht dem längsten Lookback aller Indikatoren im Projekt)
MIN_WARMUP_CANDLES: int = 200


# ─────────────────────────────────────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    """Alle Backtest-Parameter auf einem Fleck."""

    initial_balance: float = 10_000.0
    fee_pct: float = 0.10           # % pro Seite (0.1 = typisch Spot/Maker)
    slippage_pct: float = 0.05      # % ungünstige Preis-Verschiebung
    position_size_pct: float = 2.0  # % des Kapitals pro Trade
    max_open_trades: int = 1        # Im Backtest üblicherweise 1 pro Symbol
    timeframe: str = "1h"
    symbol: str = "UNKNOWN"
    min_confidence: float = 40.0    # Mindest-Konfidenz für Signale (0-100)


# ─────────────────────────────────────────────────────────────────────────────
# Trade-Datensatz
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    """Ein abgeschlossener oder laufender Backtest-Trade."""

    id: int
    strategy_name: str
    symbol: str
    side: str                           # "long" / "short"
    entry_time: object                  # pandas Timestamp
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size: float                # Anzahl Coins
    cost: float                         # USDT-Wert bei Einstieg (vor Fees)
    rr_planned: float
    confidence: float
    regime: str
    fee_entry: float

    exit_time: Optional[object] = None  # pandas Timestamp
    exit_price: Optional[float] = None
    pnl_abs: Optional[float] = None     # nach Fees
    pnl_pct: Optional[float] = None     # relativ zu cost
    fee_exit: Optional[float] = None
    exit_reason: str = ""               # "take_profit" / "stop_loss" / "end_of_data"


# ─────────────────────────────────────────────────────────────────────────────
# Interner Positions-State
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _OpenPosition:
    signal: EnhancedSignal
    entry_price: float
    entry_time: object
    position_size: float
    cost: float
    fee_entry: float


# ─────────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Deterministischer Backtester ohne Exchange-Verbindung.

    Zwei Modi:
    - run_single(df, strategy) → Einzelstrategie
    - run_multi(df, strategies, regime_engine, selector) → Meta-Selector
    """

    def __init__(self, config: BacktestConfig):
        self.config = config
        self._reset()

    # ── Zustand zurücksetzen ──────────────────────────────────────────────

    def _reset(self) -> None:
        self.balance: float = self.config.initial_balance
        self._open_pos: Optional[_OpenPosition] = None
        self._pending_signal: Optional[EnhancedSignal] = None
        self._trades: List[BacktestTrade] = []
        self._trade_counter: int = 0

    # ── Preis-Hilfsmethoden ───────────────────────────────────────────────

    def _fill_price(self, price: float, is_buy: bool) -> float:
        """Wendet Slippage auf einen Preis an.
        is_buy=True  → kaufen = teurer  → price * (1 + slip)
        is_buy=False → verkaufen = billiger → price * (1 - slip)
        """
        s = self.config.slippage_pct / 100
        return price * (1 + s) if is_buy else price * (1 - s)

    def _fee(self, notional: float) -> float:
        return notional * (self.config.fee_pct / 100)

    # ── Trade öffnen ──────────────────────────────────────────────────────

    def _open_trade(
        self, signal: EnhancedSignal, candle_open: float, ts: object
    ) -> None:
        is_long = signal.side == Side.LONG
        fill = self._fill_price(candle_open, is_buy=is_long)
        size = round(
            self.balance * (self.config.position_size_pct / 100) / fill, 8
        )
        if size <= 0:
            return

        cost = fill * size
        fee_in = self._fee(cost)
        self.balance -= cost + fee_in

        self._open_pos = _OpenPosition(
            signal=signal,
            entry_price=fill,
            entry_time=ts,
            position_size=size,
            cost=cost,
            fee_entry=fee_in,
        )

    # ── Exit-Bedingung prüfen ─────────────────────────────────────────────

    def _check_exit(
        self, candle: pd.Series, pos: _OpenPosition
    ) -> Optional[Tuple[float, str]]:
        """
        Prüft ob SL oder TP in dieser Kerze getroffen wurden.
        Gibt (raw_exit_price, reason) oder None zurück.
        Bei gleichzeitigem Treffen: SL bevorzugt (konservatives Worst-Case).
        """
        sl = pos.signal.stop_loss
        tp = pos.signal.take_profit
        lo = float(candle["low"])
        hi = float(candle["high"])

        if pos.signal.side == Side.LONG:
            sl_hit = lo <= sl
            tp_hit = hi >= tp
        else:  # SHORT
            sl_hit = hi >= sl
            tp_hit = lo <= tp

        # Beide in einer Kerze → konservativ SL annehmen
        if sl_hit and tp_hit:
            return sl, "stop_loss"
        if sl_hit:
            return sl, "stop_loss"
        if tp_hit:
            return tp, "take_profit"
        return None

    # ── Trade schließen ───────────────────────────────────────────────────

    def _close_trade(
        self, raw_exit: float, ts: object, reason: str
    ) -> BacktestTrade:
        pos = self._open_pos
        sig = pos.signal

        # Slippage beim Schließen (Gegenrichtung zur Eröffnung)
        is_long = sig.side == Side.LONG
        exit_price = self._fill_price(raw_exit, is_buy=not is_long)
        fee_out = self._fee(exit_price * pos.position_size)

        # PnL brutto
        if is_long:
            pnl_gross = (exit_price - pos.entry_price) * pos.position_size
            self.balance += exit_price * pos.position_size
        else:  # SHORT
            pnl_gross = (pos.entry_price - exit_price) * pos.position_size
            self.balance += pos.cost + pnl_gross

        # Fees abziehen
        self.balance -= fee_out
        pnl_net = pnl_gross - pos.fee_entry - fee_out
        pnl_pct = (pnl_net / pos.cost * 100) if pos.cost > 0 else 0.0

        self._trade_counter += 1
        trade = BacktestTrade(
            id=self._trade_counter,
            strategy_name=sig.strategy_name,
            symbol=sig.symbol,
            side=sig.side.value,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            position_size=pos.position_size,
            cost=pos.cost,
            rr_planned=sig.rr,
            confidence=sig.confidence,
            regime=sig.regime,
            fee_entry=round(pos.fee_entry, 6),
            exit_time=ts,
            exit_price=round(exit_price, 8),
            pnl_abs=round(pnl_net, 6),
            pnl_pct=round(pnl_pct, 4),
            fee_exit=round(fee_out, 6),
            exit_reason=reason,
        )
        self._trades.append(trade)
        self._open_pos = None
        return trade

    # ── Validierung ───────────────────────────────────────────────────────

    def _validate_data(self, df: pd.DataFrame) -> None:
        needed = MIN_WARMUP_CANDLES + 2
        if len(df) < needed:
            raise ValueError(
                f"Zu wenig Daten für Backtest: {len(df)} Kerzen, "
                f"mindestens {needed} erforderlich (Warmup: {MIN_WARMUP_CANDLES})"
            )

    # ── Haupt-Methoden ────────────────────────────────────────────────────

    def run_single(
        self, df: pd.DataFrame, strategy: EnhancedBaseStrategy
    ) -> List[BacktestTrade]:
        """
        Candle-by-candle Backtest mit einer einzelnen Enhanced-Strategie.

        Performance-Hinweis: analyze() wird auf einem wachsenden Fenster
        aufgerufen (O(n²) Indikator-Berechnungen). Für >50k Kerzen empfiehlt
        sich ein Adapter mit vorberechneten Indikatoren (zukünftige Erweiterung).
        """
        self._validate_data(df)
        self._reset()
        n = len(df)

        for i in range(MIN_WARMUP_CANDLES, n):
            candle = df.iloc[i]
            ts = df.index[i]

            # Schritt 1: Pending Signal aus Vorkerze ausführen
            if self._pending_signal is not None and self._open_pos is None:
                self._open_trade(self._pending_signal, float(candle["open"]), ts)
                self._pending_signal = None

            # Schritt 2: Exit prüfen (SL / TP gegen High/Low)
            if self._open_pos is not None:
                result = self._check_exit(candle, self._open_pos)
                if result:
                    raw_exit, reason = result
                    self._close_trade(raw_exit, ts, reason)
                continue  # nach Exit: kein Signal auf derselben Kerze

            # Schritt 3: Signal generieren (kein offener Trade, kein Pending)
            if self._open_pos is None and self._pending_signal is None:
                window = df.iloc[: i + 1]
                try:
                    sig = strategy.analyze(
                        window, self.config.symbol, self.config.timeframe
                    )
                    if (
                        sig.is_actionable()
                        and sig.confidence >= self.config.min_confidence
                    ):
                        self._pending_signal = sig
                except Exception as e:
                    logger.debug(f"Strategie-Fehler Kerze {i}: {e}")

        # Offene Position am Ende der Daten schließen
        if self._open_pos is not None:
            last = df.iloc[-1]
            self._close_trade(float(last["close"]), df.index[-1], "end_of_data")

        logger.info(
            f"[{strategy.name}] Backtest fertig: "
            f"{self._trade_counter} Trades | "
            f"Balance: {self.balance:.2f} USDT"
        )
        return list(self._trades)

    def run_multi(
        self,
        df: pd.DataFrame,
        strategies: List[EnhancedBaseStrategy],
        regime_engine: Optional[RegimeEngine] = None,
        selector: Optional[MetaSelector] = None,
    ) -> List[BacktestTrade]:
        """
        Candle-by-candle Backtest mit Meta-Selector (alle Strategien + Regime).
        """
        self._validate_data(df)
        self._reset()
        _regime = regime_engine or RegimeEngine()
        _selector = selector or MetaSelector()
        n = len(df)

        for i in range(MIN_WARMUP_CANDLES, n):
            candle = df.iloc[i]
            ts = df.index[i]

            # Schritt 1: Pending Signal ausführen
            if self._pending_signal is not None and self._open_pos is None:
                self._open_trade(self._pending_signal, float(candle["open"]), ts)
                self._pending_signal = None

            # Schritt 2: Exit prüfen
            if self._open_pos is not None:
                result = self._check_exit(candle, self._open_pos)
                if result:
                    raw_exit, reason = result
                    self._close_trade(raw_exit, ts, reason)
                continue

            # Schritt 3: Regime + alle Strategien + MetaSelector
            if self._open_pos is None and self._pending_signal is None:
                window = df.iloc[: i + 1]
                try:
                    regime = _regime.detect(window)
                    signals = []
                    for s in strategies:
                        try:
                            sig = s.analyze(
                                window, self.config.symbol, self.config.timeframe
                            )
                            sig.regime = regime.value
                            signals.append(sig)
                        except Exception as e:
                            logger.debug(f"  {s.name} Fehler: {e}")

                    best = _selector.select(signals, regime, self.config.symbol)
                    if (
                        best is not None
                        and best.side != Side.NONE
                        and best.confidence >= self.config.min_confidence
                    ):
                        self._pending_signal = best

                except Exception as e:
                    logger.debug(f"Multi-Signal-Fehler Kerze {i}: {e}")

        # Offene Position am Ende schließen
        if self._open_pos is not None:
            last = df.iloc[-1]
            self._close_trade(float(last["close"]), df.index[-1], "end_of_data")

        strat_names = [s.name for s in strategies]
        logger.info(
            f"[Meta] Backtest fertig: {self._trade_counter} Trades | "
            f"Balance: {self.balance:.2f} USDT | "
            f"Strategien: {strat_names}"
        )
        return list(self._trades)
