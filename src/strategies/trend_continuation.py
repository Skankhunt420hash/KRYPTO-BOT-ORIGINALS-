import pandas as pd
import ta
from src.strategies.signal import EnhancedSignal, Side
from src.strategies.base_strategy import EnhancedBaseStrategy
from src.utils.logger import setup_logger

logger = setup_logger("strategy.trend_continuation")


class TrendContinuationStrategy(EnhancedBaseStrategy):
    """
    Trend Continuation

    Folgt einem etablierten Trend durch EMA-Alignment (9 > 21 > 50),
    positivem und steigendem MACD-Histogramm und einem kurzen Pullback
    zur EMA9 als Einstiegspunkt.

    Regime-Fit: TREND_UP (primär), TREND_DOWN (sekundär)

    Bedingungen LONG:
    - EMA9 > EMA21 > EMA50 (Trend-Alignment)
    - MACD-Histogramm positiv und in den letzten 2 Balken zunehmend
    - Preis berührt/unterschreitet EMA9 leicht und schließt darüber
      (kleiner Pullback als Einstieg)
    SL: EMA21 – 0.5 × ATR
    TP: Einstieg + 2.5 × (Einstieg – SL)   →  RR ≈ 2.5
    """

    EMA_FAST = 9
    EMA_MID = 21
    EMA_SLOW = 50
    MACD_FAST = 12
    MACD_SLOW = 26
    MACD_SIGNAL = 9
    SL_EMA_BUFFER = 0.5

    def __init__(self):
        super().__init__("TrendContinuation")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=self.EMA_SLOW + self.MACD_SLOW + 5):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")

        try:
            df = df.copy()
            df["ema9"] = ta.trend.EMAIndicator(df["close"], window=self.EMA_FAST).ema_indicator()
            df["ema21"] = ta.trend.EMAIndicator(df["close"], window=self.EMA_MID).ema_indicator()
            df["ema50"] = ta.trend.EMAIndicator(df["close"], window=self.EMA_SLOW).ema_indicator()
            df["atr"] = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=14
            ).average_true_range()

            macd_obj = ta.trend.MACD(
                df["close"],
                window_fast=self.MACD_FAST,
                window_slow=self.MACD_SLOW,
                window_sign=self.MACD_SIGNAL,
            )
            df["macd_hist"] = macd_obj.macd_diff()

            last = df.iloc[-1]
            prev = df.iloc[-2]
            prev2 = df.iloc[-3]

            price = float(last["close"])
            ema9 = float(last["ema9"])
            ema21 = float(last["ema21"])
            ema50 = float(last["ema50"])
            atr = float(last["atr"])
            hist_now = float(last["macd_hist"])
            hist_prev = float(prev["macd_hist"])
            hist_prev2 = float(prev2["macd_hist"])

            # EMA-Alignment
            aligned = ema9 > ema21 > ema50

            # MACD-Histogramm positiv und zunehmend (mind. 2 aufeinanderfolgende Balken)
            macd_bullish = hist_now > 0 and hist_now > hist_prev

            # Pullback zur EMA9: Preis war unter oder an EMA9 und schliesst nun darüber
            low_touched_ema9 = float(last["low"]) <= ema9 * 1.001
            close_above_ema9 = price > ema9

            if aligned and macd_bullish and low_touched_ema9 and close_above_ema9:
                entry = price
                sl = ema21 - self.SL_EMA_BUFFER * atr
                risk = entry - sl
                tp = entry + 2.5 * risk
                rr = self._calc_rr(entry, sl, tp)

                # Konfidenz: EMA-Abstand als Trendstärke + MACD-Stärke
                ema_spread = (ema9 - ema50) / (ema50 + 1e-9) * 100
                trend_conf = min(ema_spread / 5.0, 1.0)
                macd_conf = min(abs(hist_now) / (abs(hist_prev2) + 1e-9) / 3.0, 1.0)
                confidence = round(45.0 + trend_conf * 30 + macd_conf * 20, 1)
                confidence = min(confidence, 90.0)

                vol_ok = self._confirm_volume(df)

                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.LONG,
                    confidence=confidence,
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=(
                        f"EMA9>EMA21>EMA50 + MACD-Hist steigt ({hist_prev:.4f}→{hist_now:.4f}) "
                        f"+ Pullback zu EMA9 abgeschlossen"
                    ),
                    volume_confirmed=vol_ok,
                )

            # Detaillierte Ablehnungsinfo
            parts = []
            if not aligned:
                parts.append(
                    f"EMA-Alignment fehlt (EMA9={ema9:.1f} EMA21={ema21:.1f} EMA50={ema50:.1f})"
                )
            if not macd_bullish:
                parts.append(f"MACD-Hist nicht bullisch ({hist_now:.4f})")
            if not (low_touched_ema9 and close_above_ema9):
                parts.append(f"Kein Pullback zur EMA9 ({ema9:.4f})")
            return self._no_signal(symbol, timeframe, " | ".join(parts) or "Kein Signal")

        except Exception as e:
            logger.error(f"Fehler in TrendContinuation.analyze ({symbol}): {e}")
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
