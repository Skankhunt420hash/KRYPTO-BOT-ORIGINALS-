#!/usr/bin/env python3
"""Runde 3: 7 Jahre Tagesdaten, Tranchen-Rebalancing (Wochentag-unabhaengig), Auswertung pro Kalenderjahr.
Hinweis: Nur Coins, die heute noch existieren (Survivorship Bias) -> Ergebnisse eher zu optimistisch fuer Long."""
import sys, glob, os, itertools
import numpy as np, pandas as pd
CS, FUND_D = 0.0015, 0.0001 * 3   # Kosten/Seite, Short-Funding pro Tag (Schaetzung)

def load(d, coins):
    px = {}
    for c in coins:
        df = pd.read_csv(f"{d}/{c}_1d.csv", parse_dates=["timestamp"]).set_index("timestamp")
        px[c] = df.close
    return pd.DataFrame(px)

def tranche_weights(target, n=7):
    return target.rolling(n, min_periods=1).mean()

def backtest(close, W):
    ret = close.pct_change().fillna(0)
    W = W.shift(1).fillna(0)
    short = (-W.clip(upper=0)).sum(axis=1)
    return (W * ret).sum(axis=1) - W.diff().abs().sum(axis=1).fillna(0) * CS - short * FUND_D

def yearly(R):
    return R.groupby(R.index.year).apply(lambda r: ((1 + r).prod() - 1) * 100)

def main():
    d = sys.argv[1]
    coins = [c for c in "BTC ETH BNB ADA DOGE LINK LTC XLM BCH ATOM".split()]
    close = load(d, coins).dropna()
    print(f"Coins: {coins}\nZeitraum: {close.index[0].date()} bis {close.index[-1].date()} ({len(close)} Tage)")
    ret = close.pct_change().fillna(0)
    bh = ret.mean(axis=1); print("\nBenchmark gleichgewichtet halten, pro Jahr %:", yearly(bh).round(0).to_dict())
    rows = []
    for L, k, mode in itertools.product((7, 14, 30, 60, 90), (2, 3), ("long", "long/short", "reversal")):
        mom = close.pct_change(L); rk = mom.rank(axis=1, ascending=False)
        top = (rk <= k).astype(float); bot = (rk > len(coins) - k).astype(float)
        if mode == "long": T = top / k
        elif mode == "long/short": T = 0.5 * top / k - 0.5 * bot / k
        else: T = 0.5 * bot / k - 0.5 * top / k
        T = T.where(mom.notna(), 0.0)
        R = backtest(close, tranche_weights(T)); y = yearly(R)
        rows.append(dict(L=L, k=k, mode=mode, total=((1 + R).prod() - 1) * 100, jahre_plus=int((y > 0).sum()), jahre=len(y),
                         worst=y.min(), best=y.max(), sharpe=R.mean() / R.std() * np.sqrt(365), **{f"y{yy}": v for yy, v in y.items()}))
    M = pd.DataFrame(rows); pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print("\n=== Momentum (Tranchen, 7 Jahre) -- alle", len(M), "Varianten, sortiert nach Sharpe ===")
    print(M.sort_values("sharpe", ascending=False).round(1).head(12).to_string(index=False))
    print("\nDurchschnitt je Modus (Gesamt% / Jahre im Plus von", M.jahre.iloc[0], "):")
    print(M.groupby("mode")[["total", "jahre_plus", "sharpe"]].mean().round(1).to_string())
    # Trendfolge: Preis > SMA(n) -> long (Tranchen nicht noetig), sonst Cash
    print("\n=== Trend-Filter pro Coin (Long wenn Close > SMA n, sonst Cash), gleichgewichtet ===")
    rows = []
    for n in (20, 50, 100, 150, 200):
        sig = (close > close.rolling(n).mean()).astype(float) / len(coins)
        R = backtest(close, sig); y = yearly(R)
        rows.append(dict(n=n, total=((1 + R).prod() - 1) * 100, jahre_plus=int((y > 0).sum()), worst=y.min(),
                         maxdd=((1 + R).cumprod() / (1 + R).cumprod().cummax() - 1).min() * 100, **{f"y{yy}": v for yy, v in y.items()}))
    print(pd.DataFrame(rows).round(0).to_string(index=False))
    M.to_csv("jobs/done/search3_momentum.csv", index=False)

if __name__ == "__main__":
    main()
