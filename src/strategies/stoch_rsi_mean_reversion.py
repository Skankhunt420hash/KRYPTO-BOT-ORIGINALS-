import pandas as pd
import ta

from src.strategies.base_strategy import EnhancedBaseStrategy
from src.strategies.signal import EnhancedSignal, Side
from src.utils.logger import setup_logger

logger = setup_logger("strategy.stoch_rsi_mean_reversion")


class StochRsiMeanReversionStrategy(EnhancedBaseStrategy):
    """
    Beliebter Ansatz: StochRSI Extrem + Mean-Reversion zurück zur BB-Mitte.
    """

    BB_WINDOW = 20
    BB_DEV = 2.0
    STOCH_WINDOW = 14
    STOCH_SMOOTH = 3
    ATR_SL = 1.0

    def __init__(self):
        super().__init__("StochRsiMeanReversion")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=70):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")

        try:
            d = df.copy()
            bb = ta.volatility.BollingerBands(d["close"], window=self.BB_WINDOW, window_dev=self.BB_DEV)
            d["bb_upper"] = bb.bollinger_hband()
            d["bb_lower"] = bb.bollinger_lband()
            d["bb_mid"] = bb.bollinger_mavg()
            stoch = ta.momentum.StochRSIIndicator(
                d["close"],
                window=self.STOCH_WINDOW,
                smooth1=self.STOCH_SMOOTH,
                smooth2=self.STOCH_SMOOTH,
            )
            d["stoch_k"] = stoch.stochrsi_k()
            d["stoch_d"] = stoch.stochrsi_d()
            d["atr"] = ta.volatility.AverageTrueRange(
                d["high"], d["low"], d["close"], window=14
            ).average_true_range()

            last = d.iloc[-1]
            prev = d.iloc[-2]
            price = float(last["close"])
            upper = float(last["bb_upper"])
            lower = float(last["bb_lower"])
            mid = float(last["bb_mid"])
            atr = float(last["atr"])
            k = float(last["stoch_k"]) * 100.0
            dline = float(last["stoch_d"]) * 100.0
            k_prev = float(prev["stoch_k"]) * 100.0

            # LONG: aus Oversold rausdrehen
            if price <= lower * 1.003 and k_prev < 20 and k > dline and k > 20:
                entry = price
                sl = entry - self.ATR_SL * atr
                tp = mid
                rr = self._calc_rr(entry, sl, tp)
                conf = min(86.0, round(44 + (20 - min(k_prev, 20)) * 0.8 + max(0, k - 20) * 0.5, 1))
                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.LONG,
                    confidence=conf,
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=(
                        f"[LONG] StochRSI Rebound ({k_prev:.1f}->{k:.1f}) am BB-Lower "
                        f"| TP BB-Mid {mid:.4f}"
                    ),
                    volume_confirmed=self._confirm_volume(d),
                )

            # SHORT: aus Overbought runterdrehen
            if price >= upper * 0.997 and k_prev > 80 and k < dline and k < 80:
                entry = price
                sl = entry + self.ATR_SL * atr
                tp = mid
                rr = self._calc_rr(entry, sl, tp)
                conf = min(86.0, round(44 + (max(k_prev, 80) - 80) * 0.8 + max(0, 80 - k) * 0.5, 1))
                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.SHORT,
                    confidence=conf,
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=(
                        f"[SHORT] StochRSI Reversal ({k_prev:.1f}->{k:.1f}) am BB-Upper "
                        f"| TP BB-Mid {mid:.4f}"
                    ),
                    volume_confirmed=self._confirm_volume(d),
                )

            return self._no_signal(
                symbol,
                timeframe,
                f"Kein StochRSI-Reversal | k={k:.1f} d={dline:.1f} BB=[{lower:.4f}..{upper:.4f}]",
            )
        except Exception as e:
            logger.error("Fehler in StochRsiMeanReversionStrategy (%s): %s", symbol, e)
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
