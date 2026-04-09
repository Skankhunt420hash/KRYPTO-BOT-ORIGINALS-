"""
Market Intelligence Engine

Aggregiert 5 externe Markt-Signale zu einem einheitlichen MarketContext:

1. Order Book Imbalance (OBI)
   Buy-Druck vs. Sell-Druck im Order Book.
   OBI > 0.6 → bullish bias | OBI < 0.4 → bearish bias

2. Funding Rate Bias (Kraken Perps)
   Positive Funding = Longs zahlen Shorts → Markt überhitzt bullish
   → Shorting hat strukturellen Vorteil
   Negative Funding = Shorts zahlen Longs → Short-Squeeze möglich

3. Multi-Timeframe Confluence
   Signal auf 5m muss mit 15m und 1h übereinstimmen.
   Confluence-Score: 0 (alle gegen) bis 3 (alle einig)

4. Liquidation Level Detection
   Berechnet Preis-Levels wo gehebelte Trader liquidiert werden.
   Trades die auf diese Levels zulaufen → erhöhtes Risiko.

5. Fear & Greed Sentiment
   Nutzt einfache Proxy-Metriken (Volatilität, Volumen-Trend, RSI)
   da kein externer API-Key benötigt wird.
   Extreme Fear → Long-Bias | Extreme Greed → Short-Bias

Alle Signale werden gecacht (kein Exchange-Spam pro Candle).
Fehler werden geloggt aber nie den Haupt-Flow blockieren.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import ta

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("market_intel")

# Cache-Ablaufzeiten in Sekunden
_CACHE_OB_SEC: int = 15       # Order Book: alle 15s neu
_CACHE_FUND_SEC: int = 300    # Funding Rate: alle 5min
_CACHE_SENT_SEC: int = 600    # Sentiment: alle 10min
_CACHE_MTF_SEC: int = 60      # Multi-TF: jede Minute


# ─────────────────────────────────────────────────────────────────────────────
# Datenstruktur: Market Context
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MarketContext:
    """Vollständiger Markt-Kontext für ein Symbol."""

    symbol: str

    # Order Book Imbalance [0.0 – 1.0]
    # > 0.6 = bullish | 0.4–0.6 = neutral | < 0.4 = bearish
    ob_imbalance: float = 0.5
    ob_bias: str = "neutral"        # "bullish" | "neutral" | "bearish"

    # Funding Rate
    funding_rate: float = 0.0       # Aktueller Funding-Rate-Wert
    funding_bias: str = "neutral"   # "long_advantage" | "short_advantage" | "neutral"

    # Multi-Timeframe Confluence [0–3]
    mtf_confluence: int = 0
    mtf_aligned: bool = False       # True wenn >= 2 Timeframes zustimmen

    # Liquidation Levels
    liq_risk: str = "low"           # "low" | "medium" | "high"
    liq_levels_nearby: List[float] = field(default_factory=list)

    # Sentiment
    sentiment: str = "neutral"      # "fear" | "neutral" | "greed"
    sentiment_score: float = 50.0   # 0 (extreme fear) – 100 (extreme greed)

    # Gesamt-Bias
    overall_bias: str = "neutral"   # "strong_long" | "long" | "neutral" | "short" | "strong_short"
    confidence_boost: float = 0.0   # -0.15 bis +0.15 Aufschlag auf Signal-Konfidenz

    def as_dict(self) -> dict:
        return {
            "ob_bias": self.ob_bias,
            "ob_imbalance": round(self.ob_imbalance, 3),
            "funding_bias": self.funding_bias,
            "funding_rate": round(self.funding_rate * 100, 4),
            "mtf_confluence": self.mtf_confluence,
            "liq_risk": self.liq_risk,
            "sentiment": self.sentiment,
            "overall_bias": self.overall_bias,
            "confidence_boost": round(self.confidence_boost, 3),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Market Intelligence Engine
# ─────────────────────────────────────────────────────────────────────────────

class MarketIntelligenceEngine:
    """
    Sammelt und aggregiert 5 Markt-Intelligenz-Signale.

    Verwendung:
        intel = MarketIntelligenceEngine(connector)
        ctx = intel.get_context("BTC/USD:USD", df_5m)
        # ctx.overall_bias, ctx.confidence_boost, etc.
    """

    def __init__(self, connector: Any) -> None:
        self._connector = connector
        self._ob_cache: Dict[str, Tuple[float, float, str]] = {}   # symbol → (ts, imbalance, bias)
        self._fund_cache: Dict[str, Tuple[float, float, str]] = {}  # symbol → (ts, rate, bias)
        self._sent_cache: Tuple[float, float, str] = (0.0, 50.0, "neutral")
        self._mtf_cache: Dict[str, Tuple[float, pd.DataFrame, pd.DataFrame]] = {}

    def get_context(self, symbol: str, df_5m: pd.DataFrame) -> MarketContext:
        """
        Erstellt vollständigen MarketContext für ein Symbol.
        Nutzt Caching um Exchange-Calls zu minimieren.
        """
        ctx = MarketContext(symbol=symbol)

        if not settings.MARKET_INTEL_ENABLED:
            return ctx

        # 1. Order Book Imbalance
        try:
            ctx.ob_imbalance, ctx.ob_bias = self._get_ob_imbalance(symbol)
        except Exception as e:
            logger.debug(f"OB-Analyse fehlgeschlagen ({symbol}): {e}")

        # 2. Funding Rate
        try:
            ctx.funding_rate, ctx.funding_bias = self._get_funding_rate(symbol)
        except Exception as e:
            logger.debug(f"Funding-Rate fehlgeschlagen ({symbol}): {e}")

        # 3. Multi-Timeframe Confluence
        try:
            ctx.mtf_confluence, ctx.mtf_aligned = self._get_mtf_confluence(
                symbol, df_5m
            )
        except Exception as e:
            logger.debug(f"MTF-Confluence fehlgeschlagen ({symbol}): {e}")

        # 4. Liquidation Level Detection
        try:
            ctx.liq_risk, ctx.liq_levels_nearby = self._detect_liquidation_risk(
                symbol, df_5m
            )
        except Exception as e:
            logger.debug(f"Liq-Detection fehlgeschlagen ({symbol}): {e}")

        # 5. Sentiment
        try:
            ctx.sentiment_score, ctx.sentiment = self._get_sentiment(df_5m)
        except Exception as e:
            logger.debug(f"Sentiment fehlgeschlagen: {e}")

        # Gesamt-Bias + Confidence-Boost berechnen
        ctx.overall_bias, ctx.confidence_boost = self._aggregate_bias(ctx)

        logger.debug(
            f"[Intel] {symbol} | "
            f"OB={ctx.ob_bias} Fund={ctx.funding_bias} "
            f"MTF={ctx.mtf_confluence}/3 Liq={ctx.liq_risk} "
            f"Sent={ctx.sentiment} → {ctx.overall_bias} "
            f"boost={ctx.confidence_boost:+.2f}"
        )
        return ctx

    # ── 1. Order Book Imbalance ───────────────────────────────────────────────

    def _get_ob_imbalance(self, symbol: str) -> Tuple[float, str]:
        """
        Berechnet Bid/Ask-Druck aus dem Order Book.
        OBI = sum(bid_volumes) / (sum(bid_volumes) + sum(ask_volumes))
        """
        now = time.time()
        cached = self._ob_cache.get(symbol)
        if cached and now - cached[0] < _CACHE_OB_SEC:
            return cached[1], cached[2]

        ob = self._connector._exchange.fetch_order_book(symbol, limit=20)

        bid_vol = sum(amount for _, amount in ob.get("bids", [])[:20])
        ask_vol = sum(amount for _, amount in ob.get("asks", [])[:20])
        total = bid_vol + ask_vol

        if total <= 0:
            return 0.5, "neutral"

        imbalance = bid_vol / total

        if imbalance > 0.62:
            bias = "bullish"
        elif imbalance < 0.38:
            bias = "bearish"
        else:
            bias = "neutral"

        self._ob_cache[symbol] = (now, imbalance, bias)
        return imbalance, bias

    # ── 2. Funding Rate ───────────────────────────────────────────────────────

    def _get_funding_rate(self, symbol: str) -> Tuple[float, str]:
        """
        Ruft aktuellen Funding Rate ab.
        Positiv = Longs zahlen → Short-Vorteil
        Negativ = Shorts zahlen → Long-Vorteil (Short-Squeeze möglich)
        """
        now = time.time()
        cached = self._fund_cache.get(symbol)
        if cached and now - cached[0] < _CACHE_FUND_SEC:
            return cached[1], cached[2]

        try:
            funding = self._connector._exchange.fetch_funding_rate(symbol)
            rate = float(funding.get("fundingRate") or 0)
        except Exception:
            rate = 0.0

        # Funding Rate Interpretation
        if rate > 0.001:      # > 0.1% → Longs stark überhitzt
            bias = "short_advantage"
        elif rate > 0.0003:   # > 0.03% → leicht long-lastig
            bias = "slight_short_advantage"
        elif rate < -0.001:   # < -0.1% → Shorts überhitzt
            bias = "long_advantage"
        elif rate < -0.0003:
            bias = "slight_long_advantage"
        else:
            bias = "neutral"

        self._fund_cache[symbol] = (now, rate, bias)
        return rate, bias

    # ── 3. Multi-Timeframe Confluence ─────────────────────────────────────────

    def _get_mtf_confluence(
        self, symbol: str, df_5m: pd.DataFrame
    ) -> Tuple[int, bool]:
        """
        Zählt wie viele Zeitrahmen in dieselbe Richtung zeigen.
        Aggregiert 5m (vorhanden), 15m und 1h (aus Exchange).

        Returns: (confluence_count 0-3, aligned bool)
        """
        now = time.time()

        # 15m und 1h aus Cache oder Exchange laden
        cached = self._mtf_cache.get(symbol)
        if cached and now - cached[0] < _CACHE_MTF_SEC:
            _, df_15m, df_1h = cached
        else:
            try:
                raw_15m = self._connector._exchange.fetch_ohlcv(
                    symbol, timeframe="15m", limit=50
                )
                raw_1h = self._connector._exchange.fetch_ohlcv(
                    symbol, timeframe="1h", limit=50
                )
                df_15m = self._raw_to_df(raw_15m)
                df_1h = self._raw_to_df(raw_1h)
                self._mtf_cache[symbol] = (now, df_15m, df_1h)
            except Exception:
                return 0, False

        # Trend-Richtung pro Zeitrahmen bestimmen (EMA9 > EMA21 = bullish)
        directions = []
        for df in [df_5m, df_15m, df_1h]:
            if df is None or len(df) < 25:
                continue
            try:
                ema9 = float(
                    ta.trend.EMAIndicator(df["close"], window=9)
                    .ema_indicator().iloc[-1]
                )
                ema21 = float(
                    ta.trend.EMAIndicator(df["close"], window=21)
                    .ema_indicator().iloc[-1]
                )
                directions.append("up" if ema9 > ema21 else "down")
            except Exception:
                pass

        if not directions:
            return 0, False

        up_count = directions.count("up")
        down_count = directions.count("down")
        confluence = max(up_count, down_count)
        aligned = confluence >= 2

        return confluence, aligned

    @staticmethod
    def _raw_to_df(raw: list) -> Optional[pd.DataFrame]:
        if not raw:
            return None
        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df.set_index("timestamp", inplace=True)
        return df

    # ── 4. Liquidation Level Detection ────────────────────────────────────────

    def _detect_liquidation_risk(
        self, symbol: str, df: pd.DataFrame
    ) -> Tuple[str, List[float]]:
        """
        Schätzt Liquidation-Levels basierend auf:
        - Swing-Hochs und -Tiefs (lokale Maxima/Minima)
        - Hohe Volumen-Cluster (wo viele Trades stattfanden)

        Wenn der aktuelle Preis nahe an einem Liquidation-Level ist → High Risk.
        """
        if df is None or len(df) < 30:
            return "low", []

        current = float(df["close"].iloc[-1])

        # Swing-Levels als Proxy für Liquidation-Cluster
        highs = df["high"].rolling(window=5, center=True).max()
        lows = df["low"].rolling(window=5, center=True).min()

        swing_highs = df["high"][df["high"] == highs].tail(10).tolist()
        swing_lows = df["low"][df["low"] == lows].tail(10).tolist()

        levels = swing_highs + swing_lows

        # Prüfe Nähe zu Levels (< 0.5% Abstand = Gefahr)
        nearby = [
            lvl for lvl in levels
            if abs(current - lvl) / current < 0.005 and lvl != current
        ]

        if len(nearby) >= 3:
            risk = "high"
        elif len(nearby) >= 1:
            risk = "medium"
        else:
            risk = "low"

        return risk, nearby[:5]

    # ── 5. Sentiment (Proxy, kein API-Key nötig) ──────────────────────────────

    def _get_sentiment(self, df: pd.DataFrame) -> Tuple[float, str]:
        """
        Berechnet Sentiment aus:
        - RSI (Überkauft/Überverkauft)
        - Volumen-Trend (steigendes Volumen = Gier)
        - Preis-Performance (5m vs. 1h)
        """
        now = time.time()
        if now - self._sent_cache[0] < _CACHE_SENT_SEC:
            return self._sent_cache[1], self._sent_cache[2]

        if df is None or len(df) < 20:
            return 50.0, "neutral"

        try:
            # RSI-Beitrag (0–40 points)
            rsi = float(
                ta.momentum.RSIIndicator(df["close"], window=14)
                .rsi().iloc[-1]
            )
            rsi_score = rsi * 0.4  # RSI 0–100 → Anteil 0–40

            # Volumen-Trend (0–30 points)
            vol_now = float(df["volume"].tail(5).mean())
            vol_prev = float(df["volume"].tail(20).head(15).mean())
            vol_ratio = vol_now / vol_prev if vol_prev > 0 else 1.0
            vol_score = min(30, vol_ratio * 15)

            # Preis-Performance (0–30 points)
            price_change = (float(df["close"].iloc[-1]) - float(df["close"].iloc[-12])) / float(df["close"].iloc[-12]) * 100
            price_score = 15 + price_change * 3  # 0 bei -5%, 30 bei +5%
            price_score = max(0, min(30, price_score))

            score = rsi_score + vol_score + price_score

            if score > 75:
                sentiment = "greed"
            elif score < 35:
                sentiment = "fear"
            else:
                sentiment = "neutral"

            self._sent_cache = (now, score, sentiment)
            return score, sentiment

        except Exception:
            return 50.0, "neutral"

    # ── Gesamt-Aggregation ────────────────────────────────────────────────────

    @staticmethod
    def _aggregate_bias(ctx: MarketContext) -> Tuple[str, float]:
        """
        Kombiniert alle Signale zu einem Overall-Bias und Confidence-Boost.

        Scoring:
        +1.5 = starkes bullisches Signal
        +1.0 = bullisches Signal
         0   = neutral
        -1.0 = bärisches Signal
        -1.5 = starkes bärisches Signal
        """
        score = 0.0

        # Order Book (gewichtet 0.3)
        if ctx.ob_bias == "bullish":
            score += 1.5 * 0.3
        elif ctx.ob_bias == "bearish":
            score -= 1.5 * 0.3

        # Funding Rate (gewichtet 0.25)
        if ctx.funding_bias == "long_advantage":
            score += 1.0 * 0.25
        elif ctx.funding_bias == "short_advantage":
            score -= 1.0 * 0.25
        elif ctx.funding_bias == "slight_long_advantage":
            score += 0.5 * 0.25
        elif ctx.funding_bias == "slight_short_advantage":
            score -= 0.5 * 0.25

        # Multi-TF Confluence (gewichtet 0.25)
        if ctx.mtf_aligned:
            score += (ctx.mtf_confluence - 1.5) * 0.25

        # Liquidation Risk (Penalty)
        if ctx.liq_risk == "high":
            score *= 0.5   # Halbiere Score wenn Liq-Risiko hoch
        elif ctx.liq_risk == "medium":
            score *= 0.75

        # Sentiment (gewichtet 0.2)
        if ctx.sentiment == "fear":
            score += 0.8 * 0.2   # Contrarian: Fear → Long-Bias
        elif ctx.sentiment == "greed":
            score -= 0.8 * 0.2   # Contrarian: Greed → Short-Bias

        # Overall Bias bestimmen
        if score >= 0.6:
            bias = "strong_long"
        elif score >= 0.2:
            bias = "long"
        elif score <= -0.6:
            bias = "strong_short"
        elif score <= -0.2:
            bias = "short"
        else:
            bias = "neutral"

        # Confidence-Boost: ±15% bei starkem Signal
        boost = max(-0.15, min(0.15, score * 0.15))

        return bias, boost
