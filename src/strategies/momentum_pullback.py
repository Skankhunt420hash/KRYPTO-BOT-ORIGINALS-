import pandas as pd
import ta
from src.strategies.signal import EnhancedSignal, Side
from src.strategies.base_strategy import EnhancedBaseStrategy
from src.utils.logger import setup_logger

logger = setup_logger("strategy.momentum_pullback")


class MomentumPullbackStrategy(EnhancedBaseStrategy):
    """
    Momentum Pullback Breakout – LONG und SHORT

    Marktlogik (vereinfacht):
      1. Klarer Trend-/Momentum-Impuls in eine Richtung
      2. Kleiner, "gesunder" Pullback gegen diesen Impuls (keine tiefe
         Korrektur, EMA-Struktur bleibt intakt)
      3. Bestätigter Re-Entry:
         - Break eines lokalen Hochs/Tiefs aus der Pullback-Phase
         - Preis zurück in Impulsrichtung über/unter Trigger-Level

    Ziel ist NICHT, jeden kleinen Dip zu kaufen, sondern klar erkennbare
    Trendfortsetzungen nach einem erkennbaren Impuls + Pullback zu handeln.

    LONG  (TREND_UP):
      - EMA20 > EMA50 > EMA100 (Trendfilter)
      - Impuls: Preis liegt signifikant über dem Schlusskurs vor N Kerzen
      - Pullback: RSI in 38–58 Zone, Pullback bleibt oberhalb EMA50
      - Trigger: Schlusskurs bricht lokales Hoch der Pullback-Phase
      - SL = entry - 1.5×ATR | TP = entry + 3.0×ATR

    SHORT (TREND_DOWN):
      - EMA20 < EMA50 < EMA100 (Trendfilter)
      - Impuls: Preis liegt signifikant unter dem Schlusskurs vor N Kerzen
      - Pullback: RSI in 42–62 Zone, Pullback bleibt unterhalb EMA50
      - Trigger: Schlusskurs bricht lokales Tief der Pullback-Phase
      - SL = entry + 1.5×ATR | TP = entry - 3.0×ATR
    """

    EMA_SHORT = 20
    EMA_MID = 50
    EMA_LONG = 100

    # Impuls-Erkennung: wie viele Kerzen zurück vergleichen und
    # wie stark muss der Trend in % gelaufen sein?
    IMPULSE_LOOKBACK = 20
    MIN_IMPULSE_PCT = 1.5  # z.B. +1.5% (LONG) oder -1.5% (SHORT)

    # Pullback-Fenster: über wie viele Kerzen wird die Pullback-Phase
    # betrachtet, deren lokales Hoch/Tief als Trigger-Level dient?
    PULLBACK_LOOKBACK = 5

    # RSI-Zonen für den "gesunden" Pullback (LONG) bzw. Rücksetzer (SHORT)
    RSI_PULLBACK_MIN = 38
    RSI_PULLBACK_MAX = 58
    RSI_SHORT_MIN = 42   # Pullback-Zonen für SHORT (RSI war in Überkauft-Gegend)
    RSI_SHORT_MAX = 62

    # ATR-basierte SL/TP-Multiplikatoren
    ATR_SL_MULT = 1.5
    ATR_TP_MULT = 3.0

    # Kleiner Puffer beim Break des Trigger-Levels (0.1% über/unter Hoch/Tief)
    TRIGGER_BUFFER_PCT = 0.001

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

            # Impuls-Berechnung (LONG/SHORT gemeinsam, Vorzeichen später)
            # Vergleich des aktuellen Schlusskurses mit dem Kurs vor N Kerzen.
            try:
                past_close = float(df["close"].iloc[-self.IMPULSE_LOOKBACK])
            except IndexError:
                # Fallback: nicht genug Historie für Impuls-Erkennung
                return self._no_signal(symbol, timeframe, "Nicht genug Historie für Impuls-Erkennung")

            if past_close <= 0:
                return self._no_signal(symbol, timeframe, "Ungültiger vergangener Preis für Impuls-Erkennung")

            impulse_pct = (price / past_close - 1.0) * 100.0

            # ── LONG Setup ────────────────────────────────────────────────
            uptrend = ema20 > ema50 > ema100
            strong_impulse_long = impulse_pct >= self.MIN_IMPULSE_PCT
            rsi_pullback_long = self.RSI_PULLBACK_MIN <= rsi_prev <= self.RSI_PULLBACK_MAX
            rsi_turning_up = rsi_now > rsi_prev
            # Pullback bleibt "gesund": Tiefs der letzten PULLBACK_LOOKBACK Kerzen
            # brechen EMA50 nicht signifikant.
            recent_lows = df["low"].iloc[-(self.PULLBACK_LOOKBACK + 1):-1]
            healthy_pullback_long = recent_lows.min() > ema50
            # Trigger-Level: lokales Hoch der Pullback-Phase,
            # Break mit kleinem Puffer.
            recent_high = float(df["high"].iloc[-(self.PULLBACK_LOOKBACK + 1):-1].max())
            trigger_level_long = recent_high * (1.0 + self.TRIGGER_BUFFER_PCT)
            trigger_broken_long = price >= trigger_level_long and price > ema20

            if (
                uptrend
                and strong_impulse_long
                and rsi_pullback_long
                and rsi_turning_up
                and healthy_pullback_long
                and trigger_broken_long
            ):
                entry = price
                sl = entry - self.ATR_SL_MULT * atr
                tp = entry + self.ATR_TP_MULT * atr
                rr = self._calc_rr(entry, sl, tp)

                trend_strength = min((ema20 - ema50) / ema50 * 100, 5.0) / 5.0
                rsi_recovery = (rsi_now - self.RSI_PULLBACK_MIN) / (100 - self.RSI_PULLBACK_MIN)
                # Zusätzliche Komponente: Impuls-Stärke relativ zum Mindestimpuls
                impulse_factor = min(max((impulse_pct - self.MIN_IMPULSE_PCT) / self.MIN_IMPULSE_PCT, 0.0), 2.0) / 2.0
                confidence = round(
                    trend_strength * 35
                    + rsi_recovery * 35
                    + impulse_factor * 20
                    + 10,
                    1,
                )
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
                        f"[LONG] Trendimpuls {impulse_pct:.1f}% + EMA-Alignment bullisch + "
                        f"Pullback (RSI {rsi_prev:.0f}→{rsi_now:.0f}) + "
                        f"Break über Trigger {recent_high:.4f}"
                    ),
                    volume_confirmed=self._confirm_volume(df),
                )

            # ── SHORT Setup ───────────────────────────────────────────────
            downtrend = ema20 < ema50 < ema100
            strong_impulse_short = impulse_pct <= -self.MIN_IMPULSE_PCT
            rsi_pullback_short = self.RSI_SHORT_MIN <= rsi_prev <= self.RSI_SHORT_MAX
            rsi_turning_down = rsi_now < rsi_prev
            recent_highs = df["high"].iloc[-(self.PULLBACK_LOOKBACK + 1):-1]
            healthy_pullback_short = recent_highs.max() < ema50
            recent_low = float(df["low"].iloc[-(self.PULLBACK_LOOKBACK + 1):-1].min())
            trigger_level_short = recent_low * (1.0 - self.TRIGGER_BUFFER_PCT)
            trigger_broken_short = price <= trigger_level_short and price < ema20

            if (
                downtrend
                and strong_impulse_short
                and rsi_pullback_short
                and rsi_turning_down
                and healthy_pullback_short
                and trigger_broken_short
            ):
                entry = price
                sl = entry + self.ATR_SL_MULT * atr
                tp = entry - self.ATR_TP_MULT * atr
                rr = self._calc_rr(entry, sl, tp)

                trend_strength = min((ema50 - ema20) / ema50 * 100, 5.0) / 5.0
                rsi_rejection = (self.RSI_SHORT_MAX - rsi_now) / self.RSI_SHORT_MAX
                impulse_factor = min(max((-impulse_pct - self.MIN_IMPULSE_PCT) / self.MIN_IMPULSE_PCT, 0.0), 2.0) / 2.0
                confidence = round(
                    trend_strength * 35
                    + rsi_rejection * 35
                    + impulse_factor * 20
                    + 10,
                    1,
                )
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
                        f"[SHORT] Trendimpuls {impulse_pct:.1f}% + EMA-Alignment bärisch + "
                        f"Pullback (RSI {rsi_prev:.0f}→{rsi_now:.0f}) + "
                        f"Break unter Trigger {recent_low:.4f}"
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
