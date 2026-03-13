import pandas as pd
import ta
from src.strategies.signal import EnhancedSignal, Side
from src.strategies.base_strategy import EnhancedBaseStrategy
from src.utils.logger import setup_logger

logger = setup_logger("strategy.trend_continuation")


class TrendContinuationStrategy(EnhancedBaseStrategy):
    """
    Trend Continuation – LONG und SHORT

    LONG  (TREND_UP):
      EMA9 > EMA21 > EMA50, MACD-Hist positiv & steigend,
      Preis berührt EMA9 von oben (kleiner Pullback) und schließt darüber.
      SL = EMA21 - 0.5×ATR  |  TP = entry + 2.5×risk

    SHORT (TREND_DOWN):
      EMA9 < EMA21 < EMA50, MACD-Hist negativ & fallend,
      Preis berührt EMA9 von unten (kleines Bounce) und schließt darunter.
      SL = EMA21 + 0.5×ATR  |  TP = entry - 2.5×risk

    Regime-Fit: TREND_UP/DOWN (primär)
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

            # ── LONG Setup ────────────────────────────────────────────────
            aligned_up = ema9 > ema21 > ema50
            macd_bullish = hist_now > 0 and hist_now > hist_prev
            low_touched_ema9 = float(last["low"]) <= ema9 * 1.001
            close_above_ema9 = price > ema9

            if aligned_up and macd_bullish and low_touched_ema9 and close_above_ema9:
                entry = price
                sl = ema21 - self.SL_EMA_BUFFER * atr
                risk = entry - sl
                tp = entry + 2.5 * risk
                rr = self._calc_rr(entry, sl, tp)

                ema_spread = (ema9 - ema50) / (ema50 + 1e-9) * 100
                trend_conf = min(ema_spread / 5.0, 1.0)
                macd_conf = min(abs(hist_now) / (abs(hist_prev2) + 1e-9) / 3.0, 1.0)
                confidence = round(45.0 + trend_conf * 30 + macd_conf * 20, 1)
                confidence = min(confidence, 90.0)

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
                        f"[LONG] EMA9>EMA21>EMA50 + MACD-Hist steigt "
                        f"({hist_prev:.4f}→{hist_now:.4f}) + Pullback zu EMA9"
                    ),
                    volume_confirmed=self._confirm_volume(df),
                )

            # ── SHORT Setup ───────────────────────────────────────────────
            aligned_down = ema9 < ema21 < ema50
            # MACD-Hist negativ und zunehmend negativ (bearisch)
            macd_bearish = hist_now < 0 and hist_now < hist_prev
            # Preis bounced kurz zur EMA9 (high berührt EMA9) und schließt darunter
            high_touched_ema9 = float(last["high"]) >= ema9 * 0.999
            close_below_ema9 = price < ema9

            if aligned_down and macd_bearish and high_touched_ema9 and close_below_ema9:
                entry = price
                sl = ema21 + self.SL_EMA_BUFFER * atr
                risk = sl - entry
                tp = entry - 2.5 * risk
                rr = self._calc_rr(entry, sl, tp)

                ema_spread = (ema50 - ema9) / (ema50 + 1e-9) * 100
                trend_conf = min(ema_spread / 5.0, 1.0)
                macd_conf = min(abs(hist_now) / (abs(hist_prev2) + 1e-9) / 3.0, 1.0)
                confidence = round(45.0 + trend_conf * 30 + macd_conf * 20, 1)
                confidence = min(confidence, 90.0)

                return EnhancedSignal(
                    strategy_name=self.name,
                    symbol=symbol,
                    timeframe=timeframe,
                    side=Side.SHORT,
                    confidence=confidence,
                    entry=entry,
                    stop_loss=sl,
                    take_profit=tp,
                    rr=rr,
                    reason=(
                        f"[SHORT] EMA9<EMA21<EMA50 + MACD-Hist fällt "
                        f"({hist_prev:.4f}→{hist_now:.4f}) + Bounce zu EMA9"
                    ),
                    volume_confirmed=self._confirm_volume(df),
                )

            # Detaillierte Ablehnungsinfo
            parts = []
            if not aligned_up and not aligned_down:
                parts.append(
                    f"Kein EMA-Alignment "
                    f"(EMA9={ema9:.1f} EMA21={ema21:.1f} EMA50={ema50:.1f})"
                )
            elif aligned_up:
                if not macd_bullish:
                    parts.append(f"MACD-Hist nicht bullisch ({hist_now:.4f})")
                if not (low_touched_ema9 and close_above_ema9):
                    parts.append(f"Kein LONG-Pullback zu EMA9 ({ema9:.4f})")
            else:
                if not macd_bearish:
                    parts.append(f"MACD-Hist nicht bärisch ({hist_now:.4f})")
                if not (high_touched_ema9 and close_below_ema9):
                    parts.append(f"Kein SHORT-Bounce zu EMA9 ({ema9:.4f})")
            return self._no_signal(symbol, timeframe, " | ".join(parts) or "Kein Signal")

        except Exception as e:
            logger.error(f"Fehler in TrendContinuation.analyze ({symbol}): {e}")
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
