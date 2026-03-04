import pandas as pd
import ta
from src.strategies.signal import EnhancedSignal, Side
from src.strategies.base_strategy import EnhancedBaseStrategy
from src.utils.logger import setup_logger

logger = setup_logger("strategy.momentum_pullback")


class MomentumPullbackStrategy(EnhancedBaseStrategy):
    """
    Momentum Pullback Breakout – LONG und SHORT

    Wartet auf einen Pullback gegen den etablierten Trend, dann Einstieg
    wenn der Pullback endet und der Trend sich fortsetzt.

    LONG  (TREND_UP):
      EMA20 > EMA50 > EMA100, RSI fiel auf 38-58 und dreht nach oben,
      Preis über EMA20.
      SL = entry - 1.5×ATR | TP = entry + 3.0×ATR

    SHORT (TREND_DOWN):
      EMA20 < EMA50 < EMA100, RSI stieg auf 42-62 (Pullback nach oben
      im Abwärtstrend) und dreht wieder nach unten, Preis unter EMA20.
      SL = entry + 1.5×ATR | TP = entry - 3.0×ATR
    """

    EMA_SHORT = 20
    EMA_MID = 50
    EMA_LONG = 100
    RSI_PULLBACK_MIN = 38
    RSI_PULLBACK_MAX = 58
    RSI_SHORT_MIN = 42   # Pullback-Zonen für SHORT (RSI war in Überkauft-Gegend)
    RSI_SHORT_MAX = 62
    ATR_SL_MULT = 1.5
    ATR_TP_MULT = 3.0

    def __init__(self):
        super().__init__("MomentumPullback")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=self.EMA_LONG + 10):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")

        try:
            df = df.copy()
            df["ema20"] = ta.trend.EMAIndicator(df["close"], window=self.EMA_SHORT).ema_indicator()
            df["ema50"] = ta.trend.EMAIndicator(df["close"], window=self.EMA_MID).ema_indicator()
            df["ema100"] = ta.trend.EMAIndicator(df["close"], window=self.EMA_LONG).ema_indicator()
            df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
            df["atr"] = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=14
            ).average_true_range()

            last = df.iloc[-1]
            prev = df.iloc[-2]

            price = float(last["close"])
            ema20 = float(last["ema20"])
            ema50 = float(last["ema50"])
            ema100 = float(last["ema100"])
            rsi_now = float(last["rsi"])
            rsi_prev = float(prev["rsi"])
            atr = float(last["atr"])

            # ── LONG Setup ────────────────────────────────────────────────
            uptrend = ema20 > ema50 > ema100
            rsi_pullback_long = self.RSI_PULLBACK_MIN <= rsi_prev <= self.RSI_PULLBACK_MAX
            rsi_turning_up = rsi_now > rsi_prev
            above_ema20 = price > ema20

            if uptrend and rsi_pullback_long and rsi_turning_up and above_ema20:
                entry = price
                sl = entry - self.ATR_SL_MULT * atr
                tp = entry + self.ATR_TP_MULT * atr
                rr = self._calc_rr(entry, sl, tp)

                trend_strength = min((ema20 - ema50) / ema50 * 100, 5.0) / 5.0
                rsi_recovery = (rsi_now - self.RSI_PULLBACK_MIN) / (100 - self.RSI_PULLBACK_MIN)
                confidence = round(trend_strength * 40 + rsi_recovery * 40 + 20, 1)
                confidence = min(confidence, 88.0)

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
                        f"[LONG] EMA-Alignment bullisch + Pullback "
                        f"(RSI {rsi_prev:.0f}→{rsi_now:.0f}) + Preis über EMA20"
                    ),
                    volume_confirmed=self._confirm_volume(df),
                )

            # ── SHORT Setup ───────────────────────────────────────────────
            downtrend = ema20 < ema50 < ema100
            rsi_pullback_short = self.RSI_SHORT_MIN <= rsi_prev <= self.RSI_SHORT_MAX
            rsi_turning_down = rsi_now < rsi_prev
            below_ema20 = price < ema20

            if downtrend and rsi_pullback_short and rsi_turning_down and below_ema20:
                entry = price
                sl = entry + self.ATR_SL_MULT * atr
                tp = entry - self.ATR_TP_MULT * atr
                rr = self._calc_rr(entry, sl, tp)

                trend_strength = min((ema50 - ema20) / ema50 * 100, 5.0) / 5.0
                rsi_rejection = (self.RSI_SHORT_MAX - rsi_now) / self.RSI_SHORT_MAX
                confidence = round(trend_strength * 40 + rsi_rejection * 40 + 20, 1)
                confidence = min(confidence, 88.0)

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
                        f"[SHORT] EMA-Alignment bärisch + Pullback "
                        f"(RSI {rsi_prev:.0f}→{rsi_now:.0f}) + Preis unter EMA20"
                    ),
                    volume_confirmed=self._confirm_volume(df),
                )

            # Kein Setup aktiv
            if uptrend:
                reason = "[LONG] Uptrend aktiv – kein Pullback-Einstieg"
            elif downtrend:
                reason = "[SHORT] Downtrend aktiv – kein Pullback-Einstieg"
            else:
                reason = (
                    f"Kein EMA-Alignment "
                    f"(EMA20={ema20:.1f} EMA50={ema50:.1f} EMA100={ema100:.1f})"
                )
            return self._no_signal(symbol, timeframe, reason)

        except Exception as e:
            logger.error(f"Fehler in MomentumPullback.analyze ({symbol}): {e}")
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
