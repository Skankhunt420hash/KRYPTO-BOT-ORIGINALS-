import ccxt
import pandas as pd
from typing import Optional, List, Dict, Any
from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("exchange")


class ExchangeConnector:
    """Verbindet den Bot mit der gewählten Kryptobörse via ccxt."""

    def __init__(self):
        self.exchange_id = settings.EXCHANGE
        self.is_paper = settings.TRADING_MODE == "paper"
        self._exchange: Optional[ccxt.Exchange] = None
        self._connect()

    def _connect(self):
        try:
            exchange_class = getattr(ccxt, self.exchange_id)
            # In PAPER-Modus ausschließlich öffentliche Endpunkte verwenden.
            # Dadurch schlagen OHLCV/Ticker-Calls nicht wegen ungültiger Keys fehl.
            config = {
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
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
        except Exception as e:
            logger.error(f"Verbindungsfehler zur Börse: {e}")
            raise

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = None,
        limit: int = None,
    ) -> pd.DataFrame:
        tf = timeframe or settings.TIMEFRAME
        lim = limit or settings.CANDLE_LIMIT
        try:
            raw = self._exchange.fetch_ohlcv(symbol, timeframe=tf, limit=lim)
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
            return self._exchange.fetch_ticker(symbol)
        except Exception as e:
            logger.error(f"Fehler beim Laden des Tickers für {symbol}: {e}")
            return {}

    def fetch_balance(self) -> Dict[str, Any]:
        if self.is_paper:
            return {"USDT": {"free": settings.PAPER_TRADING_BALANCE, "total": settings.PAPER_TRADING_BALANCE}}
        try:
            return self._exchange.fetch_balance()
        except Exception as e:
            logger.error(f"Fehler beim Laden des Kontostands: {e}")
            return {}

    def create_market_buy_order(self, symbol: str, amount: float) -> Dict[str, Any]:
        if self.is_paper:
            price = self._get_paper_price(symbol)
            logger.info(
                f"[PAPER] KAUF {amount:.6f} {symbol} @ {price:.4f} USDT"
            )
            return {"symbol": symbol, "side": "buy", "amount": amount, "price": price, "status": "closed"}

        try:
            order = self._exchange.create_market_buy_order(symbol, amount)
            logger.info(f"KAUF-ORDER ausgeführt: {symbol} – {amount}")
            return order
        except Exception as e:
            logger.error(f"Fehler bei KAUF-ORDER {symbol}: {e}")
            return {}

    def create_market_sell_order(self, symbol: str, amount: float) -> Dict[str, Any]:
        if self.is_paper:
            price = self._get_paper_price(symbol)
            logger.info(
                f"[PAPER] VERKAUF {amount:.6f} {symbol} @ {price:.4f} USDT"
            )
            return {"symbol": symbol, "side": "sell", "amount": amount, "price": price, "status": "closed"}

        try:
            order = self._exchange.create_market_sell_order(symbol, amount)
            logger.info(f"VERKAUF-ORDER ausgeführt: {symbol} – {amount}")
            return order
        except Exception as e:
            logger.error(f"Fehler bei VERKAUF-ORDER {symbol}: {e}")
            return {}

    def get_markets(self) -> List[str]:
        try:
            markets = self._exchange.load_markets()
            return list(markets.keys())
        except Exception as e:
            logger.error(f"Fehler beim Laden der Märkte: {e}")
            return []

    def _get_paper_price(self, symbol: str) -> float:
        """
        Liefert im Paper-Modus robust einen Preis:
        1) Ticker.last
        2) Letzter Close aus OHLCV(limit=1)
        3) Fallback 1.0 (verhindert Division-/None-Fehler)
        """
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
