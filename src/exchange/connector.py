import time
from typing import Optional, List, Dict, Any, Tuple

import ccxt
import pandas as pd

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("exchange")


class ExchangeConnector:
    """
    Robuste Exchange-Schicht mit klarer Trennung:
    - Read-only (mit Retry): OHLCV, Ticker, Balance, offene Orders/Positionen
    - Orderfähig (einmaliger Send, kein blindes Retry): create/cancel
    """

    def __init__(self):
        self.exchange_id = settings.EXCHANGE
        self.is_paper = settings.TRADING_MODE == "paper"
        self._exchange: Optional[ccxt.Exchange] = None
        self._markets_cache: Optional[Dict[str, Any]] = None
        self._recent_order_fingerprints: Dict[str, float] = {}
        self._read_retry_max: int = int(getattr(settings, "EXCHANGE_READ_RETRY_MAX", 2))
        self._read_retry_backoff_sec: float = float(
            getattr(settings, "EXCHANGE_READ_RETRY_BACKOFF_SEC", 1.5)
        )
        self._duplicate_window_sec: int = int(
            getattr(settings, "EXCHANGE_DUPLICATE_WINDOW_SEC", 15)
        )
        self._connect()

    # ------------------------------------------------------------------
    # Setup / Safety
    # ------------------------------------------------------------------

    def _connect(self):
        try:
            exchange_class = getattr(ccxt, self.exchange_id)
            ex_id = str(self.exchange_id).lower()
            # Kraken Perpetuals laufen über ccxt „krakenfutures“ (nicht Spot-„kraken“).
            # Bei FUTURES_MODE typischerweise Swap/Futures-Markt statt Spot.
            if ex_id == "krakenfutures" or bool(getattr(settings, "FUTURES_MODE", False)):
                default_type = "swap"
            else:
                default_type = "spot"
            opts: Dict[str, Any] = {"defaultType": default_type}
            # Binance / Kraken Futures: Zeitabgleich reduziert -1021/Invalid timestamp
            if ex_id == "binance":
                opts["adjustForTimeDifference"] = True
                opts["recvWindow"] = 60000
            elif ex_id == "krakenfutures":
                opts["adjustForTimeDifference"] = True

            config = {
                "enableRateLimit": True,
                "options": opts,
            }
            if not self.is_paper and settings.API_KEY and settings.API_SECRET:
                config["apiKey"] = settings.API_KEY
                config["secret"] = settings.API_SECRET

            self._exchange = exchange_class(config)

            if self.is_paper:
                logger.info(
                    f"[yellow]PAPER-TRADING Modus aktiv[/yellow] – "
                    f"Börse: [bold]{self.exchange_id}[/bold] (keine echten Trades)"
                )
            else:
                logger.info(
                    f"[green]LIVE-TRADING verbunden[/green] – "
                    f"Börse: [bold]{self.exchange_id}[/bold]"
                )
                if getattr(settings, "LIVE_TEST_MODE", False):
                    logger.warning(
                        "[yellow]MINI-LIVE TEST MODE AKTIV[/yellow] | "
                        "Zusätzliche harte Limits aktiv (small-size, Symbol/Strategie-Whitelist, max 1 open)."
                    )
        except Exception as e:
            logger.error(f"Verbindungsfehler zur Börse: {e}")
            raise

    @property
    def _live_orders_enabled(self) -> bool:
        return (
            settings.TRADING_MODE == "live"
            and bool(getattr(settings, "LIVE_TRADING_ENABLED", False))
            and bool(settings.API_KEY)
            and bool(settings.API_SECRET)
        )

    # ------------------------------------------------------------------
    # Read-only API (mit Retry)
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = None,
        limit: int = None,
    ) -> pd.DataFrame:
        tf = timeframe or settings.TIMEFRAME
        lim = limit or settings.CANDLE_LIMIT
        try:
            raw = self._call_with_retry(
                lambda: self._exchange.fetch_ohlcv(symbol, timeframe=tf, limit=lim),
                op=f"fetch_ohlcv:{symbol}:{tf}:{lim}",
            )
            df = pd.DataFrame(
                raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            return df
        except Exception as e:
            logger.error(f"Fehler beim Laden der OHLCV-Daten für {symbol}: {e}")
            return pd.DataFrame()

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        try:
            return self._call_with_retry(
                lambda: self._exchange.fetch_ticker(symbol),
                op=f"fetch_ticker:{symbol}",
            ) or {}
        except Exception as e:
            logger.error(f"Fehler beim Laden des Tickers für {symbol}: {e}")
            return {}

    def fetch_market_price(self, symbol: str) -> float:
        ticker = self.fetch_ticker(symbol)
        return float(ticker.get("last") or ticker.get("close") or 0.0)

    def fetch_balance(self) -> Dict[str, Any]:
        if self.is_paper:
            return {
                "USDT": {
                    "free": settings.PAPER_TRADING_BALANCE,
                    "total": settings.PAPER_TRADING_BALANCE,
                }
            }
        try:
            return self._call_with_retry(
                lambda: self._exchange.fetch_balance(),
                op="fetch_balance",
            ) or {}
        except Exception as e:
            logger.error(f"Fehler beim Laden des Kontostands: {e}")
            return {}

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        if self.is_paper:
            # Im Paper-Modus werden Orders intern simuliert; Exchange-private Endpoints
            # sind hier bewusst nicht erforderlich.
            return []
        if not hasattr(self._exchange, "fetch_open_orders"):
            return []
        try:
            if symbol:
                return self._call_with_retry(
                    lambda: self._exchange.fetch_open_orders(symbol=symbol),
                    op=f"fetch_open_orders:{symbol}",
                ) or []
            return self._call_with_retry(
                lambda: self._exchange.fetch_open_orders(),
                op="fetch_open_orders",
            ) or []
        except Exception as e:
            msg = str(e).lower()
            if "apikey" in msg or "authentication" in msg or "requires" in msg:
                logger.warning("Offene Orders nicht verfügbar (fehlende Exchange-Credentials).")
            else:
                logger.error(f"Fehler beim Laden offener Orders: {e}")
            return []

    def fetch_open_positions(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if self.is_paper:
            return []
        # Spot-Exchanges liefern häufig keine echten Positionen -> leere Liste ist OK.
        if not hasattr(self._exchange, "fetch_positions"):
            return []
        try:
            return self._call_with_retry(
                lambda: self._exchange.fetch_positions(symbols=symbols),
                op="fetch_positions",
            ) or []
        except Exception as e:
            msg = str(e).lower()
            if "apikey" in msg or "authentication" in msg or "requires" in msg:
                logger.warning("Offene Positionen nicht verfügbar (fehlende Exchange-Credentials).")
            else:
                logger.error(f"Fehler beim Laden offener Positionen: {e}")
            return []

    def fetch_symbol_info(self, symbol: str) -> Dict[str, Any]:
        market = self._get_market(symbol)
        if not market:
            return {}
        limits = market.get("limits", {})
        precision = market.get("precision", {})
        return {
            "symbol": symbol,
            "base": market.get("base"),
            "quote": market.get("quote"),
            "active": market.get("active"),
            "min_amount": (limits.get("amount") or {}).get("min"),
            "max_amount": (limits.get("amount") or {}).get("max"),
            "min_cost": (limits.get("cost") or {}).get("min"),
            "max_cost": (limits.get("cost") or {}).get("max"),
            "amount_precision": precision.get("amount"),
            "price_precision": precision.get("price"),
            "step_size": (market.get("info") or {}).get("stepSize"),
            "tick_size": (market.get("info") or {}).get("tickSize"),
        }

    def get_markets(self) -> List[str]:
        try:
            markets = self._load_markets()
            return list(markets.keys())
        except Exception as e:
            logger.error(f"Fehler beim Laden der Märkte: {e}")
            return []

    def get_markets_dict(self) -> Dict[str, Any]:
        """Vollständiges ccxt-Markt-Dict (für Universums-Auflösung, Caching wie load_markets)."""
        try:
            return dict(self._load_markets())
        except Exception as e:
            logger.error("Fehler beim Laden der Märkte (dict): %s", e)
            return {}

    # ------------------------------------------------------------------
    # Order API (orderfähig, ohne Mehrfach-Retry)
    # ------------------------------------------------------------------

    def create_market_buy_order(self, symbol: str, amount: float) -> Dict[str, Any]:
        return self._create_market_order(symbol=symbol, side="buy", amount=amount)

    def create_market_sell_order(self, symbol: str, amount: float) -> Dict[str, Any]:
        return self._create_market_order(symbol=symbol, side="sell", amount=amount)

    def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        if not order_id:
            logger.error("cancel_order: order_id fehlt.")
            return {}
        if self.is_paper:
            return {"id": order_id, "symbol": symbol, "status": "canceled"}
        if not self._live_orders_enabled:
            logger.error("cancel_order blockiert: Live-Orderfunktionen sind deaktiviert.")
            return {}
        try:
            # Cancel ist idempotenter als Create; daher kleiner Retry vertretbar.
            return self._call_with_retry(
                lambda: self._exchange.cancel_order(order_id, symbol=symbol),
                op=f"cancel_order:{symbol}:{order_id}",
            ) or {}
        except Exception as e:
            logger.error(f"Fehler beim Stornieren {order_id} ({symbol}): {e}")
            return {}

    # ------------------------------------------------------------------
    # Interne Helpers
    # ------------------------------------------------------------------

    def _create_market_order(self, *, symbol: str, side: str, amount: float) -> Dict[str, Any]:
        if side not in ("buy", "sell"):
            logger.error("Ungültige Order-Seite: %s", side)
            return {}
        if amount <= 0:
            logger.error("Ungültige Order-Menge <= 0: %s", amount)
            return {}

        if self.is_paper:
            price = self._get_paper_price(symbol)
            logger.info(
                f"[PAPER] {side.upper()} {amount:.6f} {symbol} @ {price:.4f} USDT"
            )
            return {
                "id": f"paper-{int(time.time()*1000)}",
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "price": price,
                "status": "closed",
            }

        if not self._live_orders_enabled:
            logger.error(
                "Order blockiert: Live-Modus nicht explizit freigeschaltet "
                "(TRADING_MODE=live + LIVE_TRADING_ENABLED=true + API_KEY/API_SECRET erforderlich)."
            )
            return {}

        normalized_amount, reason = self._normalize_and_validate_order(symbol, side, amount)
        if normalized_amount <= 0:
            logger.error("Order blockiert (%s): %s %s amount=%s", reason, side, symbol, amount)
            return {}
        market_price = self.fetch_market_price(symbol)
        notional = normalized_amount * market_price if market_price > 0 else 0.0
        if settings.TRADING_MODE == "live" and getattr(settings, "LIVE_TEST_MODE", False):
            live_cap = float(getattr(settings, "LIVE_MAX_POSITION_SIZE", 0.0) or 0.0)
            if live_cap <= 0:
                logger.error("Order blockiert: LIVE_TEST_MODE aktiv, aber LIVE_MAX_POSITION_SIZE ungültig.")
                return {}
            if notional > live_cap:
                logger.error(
                    "Order blockiert: Mini-Live Limit überschritten (%.2f > %.2f USDT).",
                    notional,
                    live_cap,
                )
                return {}
            logger.warning(
                "[yellow]MINI-LIVE ORDER WARNUNG[/yellow] %s %s | amount=%s | notional=%.2f/%.2f USDT",
                side.upper(),
                symbol,
                normalized_amount,
                notional,
                live_cap,
            )

        fp = self._order_fingerprint(symbol, side, normalized_amount)
        if self._is_duplicate_order(fp):
            logger.warning(
                "Duplicate-Order-Schutz: %s %s amount=%s innerhalb %ss blockiert.",
                side.upper(),
                symbol,
                normalized_amount,
                self._duplicate_window_sec,
            )
            return {}

        try:
            params = {}
            if self.exchange_id.lower() == "binance":
                params["newClientOrderId"] = f"kb-{int(time.time()*1000)}"
            order = self._exchange.create_market_order(
                symbol=symbol,
                side=side,
                amount=normalized_amount,
                params=params,
            )
            self._register_order_fingerprint(fp)
            logger.info(
                "[LIVE] ORDER OK %s %s amount=%s id=%s",
                side.upper(),
                symbol,
                normalized_amount,
                order.get("id", "n/a"),
            )
            return order or {}
        except ccxt.InsufficientFunds as e:
            logger.error("Order abgelehnt (InsufficientFunds): %s", e)
            return {}
        except ccxt.InvalidOrder as e:
            logger.error("Order abgelehnt (InvalidOrder): %s", e)
            return {}
        except ccxt.NetworkError as e:
            logger.error("Order Netzwerkfehler (kein Auto-Retry zur Mehrfachorder-Vermeidung): %s", e)
            return {}
        except ccxt.ExchangeError as e:
            logger.error("Order Exchange-Fehler: %s", e)
            return {}
        except Exception as e:
            logger.error("Unbekannter Orderfehler %s %s: %s", side, symbol, e)
            return {}

    def _load_markets(self) -> Dict[str, Any]:
        if self._markets_cache is None:
            self._markets_cache = self._call_with_retry(
                lambda: self._exchange.load_markets(),
                op="load_markets",
            ) or {}
        return self._markets_cache

    def _get_market(self, symbol: str) -> Dict[str, Any]:
        markets = self._load_markets()
        market = markets.get(symbol)
        if not market:
            logger.error("Symbol nicht gefunden: %s", symbol)
            return {}
        return market

    def _normalize_and_validate_order(
        self, symbol: str, side: str, amount: float
    ) -> Tuple[float, str]:
        market = self._get_market(symbol)
        if not market:
            return 0.0, "symbol_unknown"
        if market.get("active") is False:
            return 0.0, "market_inactive"

        try:
            normalized = float(self._exchange.amount_to_precision(symbol, amount))
        except Exception:
            normalized = float(amount)

        limits = market.get("limits", {})
        min_amount = ((limits.get("amount") or {}).get("min")) or 0.0
        min_cost = ((limits.get("cost") or {}).get("min")) or 0.0

        if normalized <= 0:
            return 0.0, "amount_precision_rounds_to_zero"
        if min_amount and normalized < float(min_amount):
            return 0.0, f"below_min_amount:{min_amount}"

        price = self.fetch_market_price(symbol)
        if price <= 0:
            return 0.0, "no_market_price"
        notional = normalized * price
        if min_cost and notional < float(min_cost):
            return 0.0, f"below_min_notional:{min_cost}"

        if not self._has_sufficient_balance(symbol, side, normalized, price):
            return 0.0, "insufficient_available_balance"
        return normalized, "ok"

    def _has_sufficient_balance(self, symbol: str, side: str, amount: float, price: float) -> bool:
        bal = self.fetch_balance() or {}
        market = self._get_market(symbol)
        base = market.get("base")
        quote = market.get("quote")
        try:
            if side == "buy":
                needed = amount * price
                free_quote = float((bal.get(quote) or {}).get("free") or 0.0)
                return free_quote >= needed
            free_base = float((bal.get(base) or {}).get("free") or 0.0)
            return free_base >= amount
        except Exception:
            return False

    def _order_fingerprint(self, symbol: str, side: str, amount: float) -> str:
        bucket = int(time.time() / self._duplicate_window_sec)
        return f"{symbol}:{side}:{amount}:{bucket}"

    def _is_duplicate_order(self, fp: str) -> bool:
        now = time.time()
        expired = [k for k, ts in self._recent_order_fingerprints.items() if now - ts > self._duplicate_window_sec]
        for k in expired:
            del self._recent_order_fingerprints[k]
        return fp in self._recent_order_fingerprints

    def _register_order_fingerprint(self, fp: str) -> None:
        self._recent_order_fingerprints[fp] = time.time()

    def _call_with_retry(self, func, *, op: str):
        last_exc = None
        for attempt in range(self._read_retry_max + 1):
            try:
                return func()
            except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.DDoSProtection) as e:
                last_exc = e
                if attempt >= self._read_retry_max:
                    break
                wait = min(self._read_retry_backoff_sec * (2 ** attempt), 8.0)
                logger.warning(
                    "%s fehlgeschlagen (%s), Retry %s/%s in %.1fs",
                    op,
                    type(e).__name__,
                    attempt + 1,
                    self._read_retry_max,
                    wait,
                )
                time.sleep(wait)
            except Exception as e:
                # Nicht-netzwerkbezogene Fehler werden nicht blind retried.
                raise e
        raise last_exc or RuntimeError(f"{op} fehlgeschlagen")

    def _get_paper_price(self, symbol: str) -> float:
        ticker = self.fetch_ticker(symbol)
        last = float(ticker.get("last", 0) or 0)
        if last > 0:
            return last

        df = self.fetch_ohlcv(symbol, limit=1)
        if not df.empty:
            close = float(df["close"].iloc[-1])
            if close > 0:
                return close

        logger.warning(f"PAPER Preis-Fallback aktiv für {symbol} (Ticker/OHLCV nicht verfügbar).")
        return 1.0
