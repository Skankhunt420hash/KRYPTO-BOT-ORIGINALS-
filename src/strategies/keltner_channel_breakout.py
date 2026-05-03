import pandas as pd
import ta

from src.strategies.base_strategy import EnhancedBaseStrategy
from src.strategies.signal import EnhancedSignal, Side
from src.utils.logger import setup_logger

logger = setup_logger("strategy.keltner_channel_breakout")


class KeltnerChannelBreakoutStrategy(EnhancedBaseStrategy):
    """
    Volatility-Trendstrategie über Keltner Channel + EMA-Trendfilter.
    """

    EMA_TREND = 55
    KC_WINDOW = 20
    KC_ATR_WINDOW = 10
    KC_MULT = 1.5

    def __init__(self):
        super().__init__("KeltnerChannelBreakout")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=90):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")
        try:
            df = df.copy()
            df["ema55"] = ta.trend.EMAIndicator(df["close"], window=self.EMA_TREND).ema_indicator()
            df["atr10"] = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=self.KC_ATR_WINDOW
            ).average_true_range()
            basis = ta.trend.EMAIndicator(df["close"], window=self.KC_WINDOW).ema_indicator()
            df["kc_mid"] = basis
            df["kc_upper"] = basis + self.KC_MULT * df["atr10"]
            df["kc_lower"] = basis - self.KC_MULT * df["atr10"]

            last = df.iloc[-1]
            prev = df.iloc[-2]
            price = float(last["close"])
            prev_close = float(prev["close"])
            ema55 = float(last["ema55"])
            atr = max(float(last["atr10"]), 1e-9)
            kc_upper = float(last["kc_upper"])
            kc_lower = float(last["kc_lower"])
            kc_mid = float(last["kc_mid"])

            bullish = price > ema55 and prev_close <= kc_upper and price > kc_upper
            bearish = price < ema55 and prev_close >= kc_lower and price < kc_lower

            if bullish:
                entry = price
                sl = min(kc_mid, entry - 1.2 * atr)
                tp = entry + 3.0 * atr
                rr = self._calc_rr(entry, sl, tp)
                conf = min(90.0, 56.0 + min((price - kc_upper) / atr, 2.0) * 12.0)
                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.LONG,
                    confidence=round(conf, 1),
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=f"[LONG] KC Breakout über Upper ({kc_upper:.4f}) + EMA55 Trendfilter",
                    volume_confirmed=self._confirm_volume(df),
                )
            if bearish:
                entry = price
                sl = max(kc_mid, entry + 1.2 * atr)
                tp = entry - 3.0 * atr
                rr = self._calc_rr(entry, sl, tp)
                conf = min(90.0, 56.0 + min((kc_lower - price) / atr, 2.0) * 12.0)
                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.SHORT,
                    confidence=round(conf, 1),
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=f"[SHORT] KC Breakdown unter Lower ({kc_lower:.4f}) + EMA55 Trendfilter",
                    volume_confirmed=self._confirm_volume(df),
                )
            return self._no_signal(
                symbol,
                timeframe,
                f"Kein KC-Breakout | close={price:.4f} upper={kc_upper:.4f} lower={kc_lower:.4f}",
            )
        except Exception as e:
            logger.error("Fehler in KeltnerChannelBreakout.analyze (%s): %s", symbol, e)
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
