"""
Dynamische Handelsuniversen (z. B. alle Kraken-Linear-Perpetuals).

Kraken Spot (ccxt: kraken) und Kraken Futures (ccxt: krakenfutures) sind getrennt.
Perps / PF_* linear werden über krakenfutures mit unified symbols wie BTC/USD:USD abgebildet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Any

from config.settings import settings
from src.utils.logger import setup_logger

if TYPE_CHECKING:
    from src.exchange.connector import ExchangeConnector

logger = setup_logger("exchange.universe")

KRAKEN_PERPS_UNIVERSE = "kraken_perps"
BINANCE_USDM_UNIVERSE = "binance_usdm"


def trading_universe_mode() -> str:
    return (getattr(settings, "TRADING_UNIVERSE", "") or "").strip().lower()


def _manual_pair_list() -> List[str]:
    return [
        p.strip()
        for p in settings.TRADING_PAIRS
        if p.strip() and p.strip().lower() not in ("auto", "*")
    ]


def binance_usdt_linear_swap_symbols(markets: Dict[str, Any]) -> List[str]:
    """Binance USDT-M linear Perpetuals (ccxt unified, Quote USDT)."""
    out: List[str] = []
    for sym, mk in markets.items():
        if not mk.get("swap"):
            continue
        if not mk.get("linear"):
            continue
        if mk.get("quote") != "USDT":
            continue
        if mk.get("active") is False:
            continue
        out.append(sym)
    return sorted(set(out))


def kraken_linear_perp_symbols(markets: Dict[str, Any]) -> List[str]:
    """Aktive lineare Swap-/Perp-Kontrakte (ohne inverse Siedlung in Base)."""
    out: List[str] = []
    for sym, mk in markets.items():
        if not mk.get("swap"):
            continue
        if not mk.get("linear"):
            continue
        if mk.get("active") is False:
            continue
        out.append(sym)
    return sorted(set(out))


def format_pairs_for_log(pairs: List[str], max_show: int = 15) -> str:
    if len(pairs) <= max_show:
        return ", ".join(pairs)
    head = ", ".join(pairs[:max_show])
    return f"{head} … (+{len(pairs) - max_show} weitere)"


def resolve_trading_pairs(exchange: "ExchangeConnector") -> List[str]:
    """
    Ermittelt die effektive Symbol-Liste für den Lauf.

    - Ohne TRADING_UNIVERSE: wie bisher nur TRADING_PAIRS (ohne Platzhalter auto/*).
    - TRADING_UNIVERSE=kraken_perps: Kraken linear USD Perps.
    - TRADING_UNIVERSE=binance_usdm: Binance USDT-M linear Perps.
    """
    mode = trading_universe_mode()
    manual = _manual_pair_list()

    if mode == BINANCE_USDM_UNIVERSE:
        if manual:
            logger.warning(
                "TRADING_UNIVERSE=binance_usdm: %d manuelle TRADING_PAIRS werden ignoriert.",
                len(manual),
            )
        markets = exchange.get_markets_dict()
        symbols = binance_usdt_linear_swap_symbols(markets)
        if not symbols:
            raise RuntimeError(
                "TRADING_UNIVERSE=binance_usdm, aber keine USDT-linear-Swap-Märkte. "
                "EXCHANGE=binance und FUTURES_MODE=true setzen, Märkte prüfen."
            )
        max_n = int(getattr(settings, "TRADING_UNIVERSE_MAX_SYMBOLS", 0) or 0)
        if max_n > 0 and len(symbols) > max_n:
            symbols = symbols[:max_n]
            logger.warning(
                "TRADING_UNIVERSE_MAX_SYMBOLS=%d: nur die ersten %d Binance-Symbole.",
                max_n,
                max_n,
            )
        logger.info(
            "Universum [binance_usdm]: %d USDT-linear Perps – %s",
            len(symbols),
            format_pairs_for_log(symbols, max_show=8),
        )
        return symbols

    if mode == KRAKEN_PERPS_UNIVERSE:
        if manual:
            logger.warning(
                "TRADING_UNIVERSE=kraken_perps: %d manuelle TRADING_PAIRS-Einträge werden "
                "ignoriert (Universum ersetzt die Liste).",
                len(manual),
            )
        markets = exchange.get_markets_dict()
        symbols = kraken_linear_perp_symbols(markets)
        if not symbols:
            raise RuntimeError(
                "TRADING_UNIVERSE=kraken_perps, aber keine linearen Swap-Märkte gefunden. "
                "Setze EXCHANGE=krakenfutures (Kraken Futures API), prüfe Netzwerk/API."
            )
        max_n = int(getattr(settings, "TRADING_UNIVERSE_MAX_SYMBOLS", 0) or 0)
        if max_n > 0 and len(symbols) > max_n:
            symbols = symbols[:max_n]
            logger.warning(
                "TRADING_UNIVERSE_MAX_SYMBOLS=%d: nur die ersten %d Symbole (alphabetisch) werden gescannt.",
                max_n,
                max_n,
            )
        logger.info(
            "Universum [kraken_perps]: %d lineare Perpetuals – %s",
            len(symbols),
            format_pairs_for_log(symbols, max_show=8),
        )
        return symbols

    if manual:
        return manual

    # Legacy: TRADING_PAIRS ohne Platzhalter-Filter
    return [p.strip() for p in settings.TRADING_PAIRS if p.strip()]
