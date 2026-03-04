import pandas as pd
import ta
from src.strategies.signal import EnhancedSignal, Side
from src.strategies.base_strategy import EnhancedBaseStrategy
from src.utils.logger import setup_logger

logger = setup_logger("strategy.range_reversion")


class RangeReversionStrategy(EnhancedBaseStrategy):
    """
    Range Mean Reversion

    Kauft nahe der unteren Bollinger Band und nimmt Gewinn an der Mittellinie
    (Mean Reversion). Funktioniert in RANGE- und LOW_VOLATILITY-Märkten.

    Regime-Fit: RANGE (primär), LOW_VOLATILITY (sekundär)

    Bedingungen LONG:
    - Preis ≤ untere Bollinger Band (20, 2σ)
    - RSI < 38 (oversold Bestätigung)
    SL: untere BB – 1.0 × ATR
    TP: mittlere BB (20-SMA)  →  RR variabel, typisch 1.5–3.0
    """

    BB_WINDOW = 20
    BB_DEV = 2.0
    RSI_OVERSOLD = 38
    RSI_OVERBOUGHT = 62
    ATR_SL_BUFFER = 1.0

    def __init__(self):
        super().__init__("RangeReversion")

    def analyze(self, df: pd.DataFrame, symbol: str, timeframe: str) -> EnhancedSignal:
        if not self._validate_df(df, min_rows=self.BB_WINDOW + 10):
            return self._no_signal(symbol, timeframe, "Nicht genug Daten")

        try:
            df = df.copy()
            bb = ta.volatility.BollingerBands(
                df["close"], window=self.BB_WINDOW, window_dev=self.BB_DEV
            )
            df["bb_upper"] = bb.bollinger_hband()
            df["bb_lower"] = bb.bollinger_lband()
            df["bb_mid"] = bb.bollinger_mavg()
            df["bb_width"] = bb.bollinger_wband()
            df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
            df["atr"] = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=14
            ).average_true_range()

            last = df.iloc[-1]
            price = float(last["close"])
            bb_lower = float(last["bb_lower"])
            bb_upper = float(last["bb_upper"])
            bb_mid = float(last["bb_mid"])
            rsi = float(last["rsi"])
            atr = float(last["atr"])

            # LONG: Preis unter oder an unterer BB + RSI oversold
            near_lower = price <= bb_lower * 1.002
            if near_lower and rsi < self.RSI_OVERSOLD:
                entry = price
                sl = bb_lower - self.ATR_SL_BUFFER * atr
                tp = bb_mid
                rr = self._calc_rr(entry, sl, tp)

                if rr < 1.0:
                    return self._no_signal(
                        symbol, timeframe,
                        f"RR zu gering für Range-Reversion: {rr:.2f}"
                    )

                # Konfidenz: wie weit unter der BB + wie oversold
                bb_distance = (bb_lower - price) / (atr + 1e-9)
                rsi_extreme = (self.RSI_OVERSOLD - rsi) / self.RSI_OVERSOLD
                confidence = round(40.0 + bb_distance * 20 + rsi_extreme * 30, 1)
                confidence = min(confidence, 85.0)

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
                        f"Preis an/unter BB-Lower ({bb_lower:.4f}) | "
                        f"RSI={rsi:.1f} (oversold) | TP=BB-Mid ({bb_mid:.4f})"
                    ),
                    volume_confirmed=vol_ok,
                )

            return self._no_signal(
                symbol, timeframe,
                f"Kein BB-Extremwert | price={price:.4f} "
                f"BB=[{bb_lower:.4f}..{bb_upper:.4f}] RSI={rsi:.1f}"
            )

        except Exception as e:
            logger.error(f"Fehler in RangeReversion.analyze ({symbol}): {e}")
            return self._no_signal(symbol, timeframe, f"Fehler: {e}")
