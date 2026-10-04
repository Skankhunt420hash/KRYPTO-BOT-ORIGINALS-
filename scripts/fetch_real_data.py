#!/usr/bin/env python3
"""
Lädt ECHTE historische Kerzen von einer Börse (über ccxt) und speichert sie
im CSV-Format, das backtest/data_loader.py erwartet (timestamp,open,high,low,close,volume).

Nutzt öffentliche Marktdaten-Endpunkte (kein API-Key nötig).

Beispiel:
    python3 scripts/fetch_real_data.py --exchange binance --symbol BTC/USDT --timeframe 1h --years 2
    python3 scripts/fetch_real_data.py --exchange binance --symbol ETH/USDT --timeframe 1h --years 2
    python3 scripts/fetch_real_data.py --exchange binance --symbol SOL/USDT --timeframe 1h --years 2
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt
import pandas as pd


LIMITS = {"coinbaseexchange": 300, "bitstamp": 1000, "binanceus": 1000, "binance": 1000}
# Reihenfolge: (Börse, Quote-Währung). Binance blockt US-IPs (HTTP 451).
CHAIN = [("binance", "USDT"), ("binanceus", "USDT"), ("bitstamp", "USD"), ("coinbaseexchange", "USD")]


def translate(symbol: str, quote: str) -> str:
    base = symbol.split("/")[0]
    return f"{base}/{quote}"


def probe(exchange, symbol, timeframe, since_ms):
    """Ein einziger Versuch -- permanente Fehler (451/403) sofort erkennen."""
    rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=10)
    return bool(rows) and rows[0][0] <= since_ms + 30 * 86400 * 1000


def fetch_all_ohlcv(exchange, symbol: str, timeframe: str, since_ms: int, until_ms: int) -> list:
    """Holt alle Kerzen zwischen since_ms und until_ms, seitenweise (ccxt-Limit pro Call)."""
    all_rows = []
    cursor = since_ms
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    limit = LIMITS.get(exchange.id, 1000)

    while cursor < until_ms:
        batch = None
        for attempt in range(5):
            try:
                batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=limit)
                break
            except ccxt.ExchangeNotAvailable:
                raise
            except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.DDoSProtection) as e:
                wait = 2 ** attempt
                print(f"  Netzfehler ({e}), neuer Versuch in {wait}s ...", file=sys.stderr)
                time.sleep(wait)
        if batch is None:
            raise RuntimeError(f"Konnte Kerzen ab {cursor} nicht laden (5 Versuche fehlgeschlagen).")
        if not batch:
            break

        all_rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= cursor:
            break  # keine Fortschritt mehr -> Ende der verfügbaren Historie
        cursor = last_ts + tf_ms

        done_pct = min(100.0, (cursor - since_ms) / (until_ms - since_ms) * 100)
        # Normale Zeile (kein \r) -- sonst zerschiesst die DO-Web-Konsole beim
        # Kopieren den Text, weil sie Zeilen ueberschreibt statt neu anzuhaengen.
        step_pct = int(done_pct // 10) * 10
        if step_pct != getattr(fetch_all_ohlcv, "_last_pct", {}).get(symbol, -10):
            fetch_all_ohlcv._last_pct = getattr(fetch_all_ohlcv, "_last_pct", {})
            fetch_all_ohlcv._last_pct[symbol] = step_pct
            print(f"  {symbol} {timeframe}: {len(all_rows):>6} Kerzen geladen ({done_pct:5.1f}%)", file=sys.stderr)

        if exchange.rateLimit:
            time.sleep(exchange.rateLimit / 1000)

    return all_rows


def main():
    ap = argparse.ArgumentParser(description="Echte OHLCV-Daten von einer Börse laden")
    ap.add_argument("--exchange", default="binance", help="ccxt-Börsen-ID (binance, kraken, bybit, ...)")
    ap.add_argument("--symbol", required=True, help="z.B. BTC/USDT")
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--years", type=float, default=2.0, help="Wie viele Jahre Historie (ab heute rückwärts)")
    ap.add_argument("--out", default=None, help="Ausgabe-CSV (Default: data/real/<SYMBOL>_<TF>.csv)")
    args = ap.parse_args()

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=args.years * 365.25)
    since_ms = int(since.timestamp() * 1000)
    until_ms = int(until.timestamp() * 1000)

    chain = CHAIN if args.exchange == "auto" else [(args.exchange, args.symbol.split("/")[1])]
    exchange = None
    sym = args.symbol
    for ex_id, quote in chain:
        cand = getattr(ccxt, ex_id)({"enableRateLimit": True})
        sym = translate(args.symbol, quote) if args.exchange == "auto" else args.symbol
        try:
            if probe(cand, sym, args.timeframe, since_ms):
                exchange = cand
                break
            print(f"  {ex_id}: Historie reicht nicht {args.years} Jahre zurueck -> naechste", file=sys.stderr)
        except Exception as e:
            print(f"  {ex_id} ({sym}) nicht nutzbar: {type(e).__name__}: {str(e)[:100]}", file=sys.stderr)
    if exchange is None:
        print("FEHLER: keine Boerse lieferte Daten.", file=sys.stderr)
        sys.exit(1)

    print(f"==> Lade {sym} {args.timeframe} von {exchange.id}, {since.date()} bis {until.date()}")
    rows = fetch_all_ohlcv(exchange, sym, args.timeframe, since_ms, until_ms)
    if not rows:
        print("FEHLER: keine Kerzen erhalten.", file=sys.stderr)
        sys.exit(1)

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)

    out_path = Path(args.out) if args.out else Path("data/real") / f"{args.symbol.replace('/', '_')}_{args.timeframe}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    span_days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days
    print(f"==> Quelle: {exchange.id} {sym}\n==> Gespeichert: {out_path}  ({len(df)} Kerzen, {span_days} Tage, "
          f"{df['timestamp'].iloc[0]} bis {df['timestamp'].iloc[-1]})")


if __name__ == "__main__":
    main()
