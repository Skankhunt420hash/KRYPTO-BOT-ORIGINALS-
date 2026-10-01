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


def fetch_all_ohlcv(exchange, symbol: str, timeframe: str, since_ms: int, until_ms: int) -> list:
    """Holt alle Kerzen zwischen since_ms und until_ms, seitenweise (ccxt-Limit pro Call)."""
    all_rows = []
    cursor = since_ms
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    limit = 1000  # von den meisten Börsen akzeptiertes Maximum

    while cursor < until_ms:
        batch = None
        for attempt in range(5):
            try:
                batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=limit)
                break
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

    exchange_class = getattr(ccxt, args.exchange)
    exchange = exchange_class({"enableRateLimit": True})

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=args.years * 365.25)
    since_ms = int(since.timestamp() * 1000)
    until_ms = int(until.timestamp() * 1000)

    print(f"==> Lade {args.symbol} {args.timeframe} von {args.exchange}, {since.date()} bis {until.date()}")
    rows = fetch_all_ohlcv(exchange, args.symbol, args.timeframe, since_ms, until_ms)
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
    print(f"==> Gespeichert: {out_path}  ({len(df)} Kerzen, {span_days} Tage, "
          f"{df['timestamp'].iloc[0]} bis {df['timestamp'].iloc[-1]})")


if __name__ == "__main__":
    main()
