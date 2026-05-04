import pandas as pd
import ta

from src.strategies.base_strategy import EnhancedBaseStrategy
from src.strategies.signal import EnhancedSignal, Side
from src.utils.logger import setup_logger

logger = setup_logger("strategy.rsi_macd_confluence")


class RSIMACDConfluenceStrategy(EnhancedBaseStrategy):
    """
    Beliebtes Setup: RSI-Rebound + MACD-Konfluenz.
    """

    def __init__(self):
        super().__init__("RSI_MACD_Confluence")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=80):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")
        try:
            data = df.copy()
            data["rsi"] = ta.momentum.RSIIndicator(data["close"], window=14).rsi()
            macd_obj = ta.trend.MACD(data["close"], window_fast=12, window_slow=26, window_sign=9)
            data["macd"] = macd_obj.macd()
            data["macd_signal"] = macd_obj.macd_signal()
            data["macd_hist"] = macd_obj.macd_diff()
            data["atr"] = ta.volatility.AverageTrueRange(
                data["high"], data["low"], data["close"], window=14
            ).average_true_range()

            last = data.iloc[-1]
            prev = data.iloc[-2]
            price = float(last["close"])
            atr = float(last["atr"])
            rsi_now = float(last["rsi"])
            rsi_prev = float(prev["rsi"])
            macd_now = float(last["macd"])
            macd_sig_now = float(last["macd_signal"])
            hist_now = float(last["macd_hist"])
            hist_prev = float(prev["macd_hist"])

            bullish = (
                rsi_prev <= 35.0
                and rsi_now > rsi_prev
                and macd_now > macd_sig_now
                and hist_now > hist_prev
            )
            if bullish:
                entry = price
                sl = entry - 1.4 * atr
                tp = entry + 2.8 * atr
                rr = self._calc_rr(entry, sl, tp)
                conf = round(48.0 + min(22.0, max(0.0, (40.0 - rsi_now))) + min(20.0, hist_now * 120.0), 1)
                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.LONG,
                    confidence=min(conf, 90.0),
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=f"[LONG] RSI-Rebound ({rsi_prev:.1f}->{rsi_now:.1f}) + MACD-Kreuz bullisch",
                    volume_confirmed=self._confirm_volume(data),
                )

            bearish = (
                rsi_prev >= 65.0
                and rsi_now < rsi_prev
                and macd_now < macd_sig_now
                and hist_now < hist_prev
            )
            if bearish:
                entry = price
                sl = entry + 1.4 * atr
                tp = entry - 2.8 * atr
                rr = self._calc_rr(entry, sl, tp)
                conf = round(48.0 + min(22.0, max(0.0, (rsi_now - 60.0))) + min(20.0, abs(hist_now) * 120.0), 1)
                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.SHORT,
                    confidence=min(conf, 90.0),
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=f"[SHORT] RSI-Fall ({rsi_prev:.1f}->{rsi_now:.1f}) + MACD-Kreuz bärisch",
                    volume_confirmed=self._confirm_volume(data),
                )

            return self._no_signal(
                symbol,
                timeframe,
                f"Keine RSI/MACD-Konfluenz | RSI={rsi_now:.1f} MACDdiff={hist_now:.4f}",
            )
        except Exception as e:
            logger.error(f"Fehler in RSIMACDConfluence.analyze ({symbol}): {e}")
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
