"""
Dynamic Pair Scanner für Kraken Futures/Perpetuals

Scannt automatisch alle verfügbaren Perpetual-Kontrakte auf Kraken,
filtert nach Volumen und Liquidität, und gibt die besten Paare zurück.

Aktualisiert die Trading-Pairs alle N Minuten automatisch.
"""

import time
from typing import Dict, List, Optional, Tuple

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("pair_scanner")

# Aktualisierungsintervall in Sekunden (Standard: alle 15 Minuten)
_REFRESH_INTERVAL_SEC: int = 900


class PairScanner:
    """
    Scannt Kraken Futures nach handelbaren Perpetual-Paaren.

    Kriterien für gute Paare:
    - 24h-Volumen > MIN_VOLUME_USD (Liquidität)
    - Spread < 0.5% (kein Spread-SL-Problem)
    - Aktiv gehandelt (nicht eingestellt)

    Verwendung:
        scanner = PairScanner(connector)
        pairs = scanner.get_active_pairs()   # gibt gefilterte Liste zurück
        scanner.refresh_if_needed()           # aktualisiert falls veraltet
    """

    def __init__(self, connector) -> None:
        self._connector = connector
        self._cached_pairs: List[str] = []
        self._last_refresh: float = 0.0
        self._pair_stats: Dict[str, dict] = {}

    def get_active_pairs(self) -> List[str]:
        """Gibt aktuell gültige Pairs zurück. Cached bis nächstes Refresh."""
        self.refresh_if_needed()
        if self._cached_pairs:
            return self._cached_pairs
        # Fallback auf Settings wenn Scan scheitert
        return list(settings.TRADING_PAIRS)

    def refresh_if_needed(self) -> bool:
        """Aktualisiert Pair-Liste wenn Intervall abgelaufen. True = aktualisiert."""
        if not settings.DYNAMIC_PAIRS_ENABLED:
            return False
        if time.time() - self._last_refresh < _REFRESH_INTERVAL_SEC:
            return False
        return self.force_refresh()

    def force_refresh(self) -> bool:
        """Erzwingt sofortige Aktualisierung der Pair-Liste."""
        try:
            pairs = self._scan_kraken_perpetuals()
            if pairs:
                self._cached_pairs = pairs
                self._last_refresh = time.time()
                logger.info(
                    f"[cyan]Pair-Scanner[/cyan]: {len(pairs)} Pairs aktiv | "
                    f"Top 5: {pairs[:5]}"
                )
                return True
            else:
                logger.warning("Pair-Scanner: Keine Pairs gefunden – Fallback auf Settings")
                return False
        except Exception as e:
            logger.warning(f"Pair-Scanner Fehler: {e} – behalte aktuelle Pairs")
            return False

    def _scan_kraken_perpetuals(self) -> List[str]:
        """
        Lädt alle Kraken Futures Märkte und filtert nach Qualitätskriterien.

        Kraken Perpetual-Format: BTC/USD:USD, ETH/USD:USD, etc.
        """
        try:
            markets = self._connector._exchange.load_markets()
        except Exception as e:
            raise RuntimeError(f"Märkte konnten nicht geladen werden: {e}")

        candidates: List[Tuple[str, float]] = []  # (symbol, volume_usd)

        for symbol, market in markets.items():
            # Nur Perpetual-Futures / Swap-Kontrakte
            market_type = market.get("type", "")
            is_swap = market.get("swap", False)
            is_future = market.get("future", False)

            if not (is_swap or is_future or market_type in ("swap", "future")):
                continue

            # Nur aktive Märkte
            if not market.get("active", True):
                continue

            # Quote muss USD oder USDT sein
            quote = market.get("quote", "")
            if quote not in ("USD", "USDT", "USDTPERP"):
                continue

            # Volumen prüfen
            try:
                tickers = self._connector._exchange.fetch_ticker(symbol)
                volume_usd = float(tickers.get("quoteVolume") or tickers.get("baseVolume") or 0)

                # Fallback: baseVolume × last_price
                if volume_usd == 0:
                    base_vol = float(tickers.get("baseVolume") or 0)
                    last = float(tickers.get("last") or 0)
                    volume_usd = base_vol * last

                if volume_usd < settings.DYNAMIC_PAIRS_MIN_VOLUME_USD:
                    continue

                # Spread prüfen (verhindert VELO-artigen Spread-SL)
                bid = float(tickers.get("bid") or 0)
                ask = float(tickers.get("ask") or 0)
                if bid > 0 and ask > 0:
                    spread_pct = (ask - bid) / bid * 100
                    # Skip wenn Spread > 0.5% (SL würde sofort triggern)
                    if spread_pct > 0.5:
                        logger.debug(
                            f"  Skip {symbol}: Spread {spread_pct:.2f}% zu hoch"
                        )
                        continue

                candidates.append((symbol, volume_usd))
                self._pair_stats[symbol] = {
                    "volume_usd": volume_usd,
                    "spread_pct": spread_pct if bid > 0 else 0,
                    "last_price": float(tickers.get("last") or 0),
                }

            except Exception:
                # Ticker-Fehler → überspringen
                continue

        # Sortieren nach Volumen (liquideste zuerst)
        candidates.sort(key=lambda x: x[1], reverse=True)

        # Max Pairs begrenzen
        top_pairs = [sym for sym, _ in candidates[: settings.DYNAMIC_PAIRS_MAX]]

        # Immer BTC und ETH drin lassen falls vorhanden
        for must_have in ("BTC/USD:USD", "ETH/USD:USD", "BTC/USDT", "ETH/USDT"):
            if must_have in markets and must_have not in top_pairs:
                top_pairs.insert(0, must_have)
                if len(top_pairs) > settings.DYNAMIC_PAIRS_MAX:
                    top_pairs.pop()

        return top_pairs

    def get_stats(self) -> dict:
        """Gibt Statistiken der gescannten Pairs zurück."""
        return {
            "total_pairs": len(self._cached_pairs),
            "last_refresh_ago_min": round(
                (time.time() - self._last_refresh) / 60, 1
            ) if self._last_refresh > 0 else None,
            "pair_details": self._pair_stats,
        }
