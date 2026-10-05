#!/usr/bin/env python3
"""Runde 2: Trendfolge (4h/1d), Cross-Coin-Momentum, Benchmark Buy&Hold. Train=Jahr1, Test=Jahr2."""
import sys, glob, os, itertools
import numpy as np, pandas as pd
CS = 0.0015  # Kosten je Seite (Gebuehr+Slippage)

def load(d):
    px = {}
    for f in sorted(glob.glob(os.path.join(d, "*_1h.csv"))):
        df = pd.read_csv(f); df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        px[os.path.basename(f).split("_")[0]] = df.set_index("timestamp")
    return px

def rs(df, rule):
    return df.resample(rule).agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()

def sharpe(r, ppy):
    return r.mean() / r.std() * np.sqrt(ppy) if r.std() > 0 else np.nan

def maxdd(r):
    e = (1 + r).cumprod(); return ((e / e.cummax()) - 1).min() * 100

def summarize(r, ppy):
    return dict(ret=((1 + r).prod() - 1) * 100, sharpe=sharpe(r, ppy), dd=maxdd(r), n=len(r))

def trend_pos(df, n_in, n_out, long_only):
    """Donchian: Long ab n_in-Hoch, Ausstieg bei n_out-Tief (Short spiegelbildlich). Position gilt ab naechster Kerze."""
    c, h, l = df.close, df.high, df.low
    hi_in = h.rolling(n_in).max().shift(1); lo_in = l.rolling(n_in).min().shift(1)
    hi_out = h.rolling(n_out).max().shift(1); lo_out = l.rolling(n_out).min().shift(1)
    pos = np.zeros(len(c)); p = 0
    for i in range(len(c)):
        if np.isnan(hi_in.iloc[i]) or np.isnan(hi_out.iloc[i]): pos[i] = 0; continue
        if p == 0:
            if c.iloc[i] > hi_in.iloc[i]: p = 1
            elif (not long_only) and c.iloc[i] < lo_in.iloc[i]: p = -1
        elif p == 1 and c.iloc[i] < lo_out.iloc[i]: p = 0
        elif p == -1 and c.iloc[i] > hi_out.iloc[i]: p = 0
        pos[i] = p
    return pd.Series(pos, c.index).shift(1).fillna(0)

def pnl(close, pos):
    r = close.pct_change().fillna(0) * pos
    turn = pos.diff().abs().fillna(0)
    return r - turn * CS

def split_stats(r, ppy):
    mid = r.index[len(r) // 2]
    return summarize(r[r.index < mid], ppy), summarize(r[r.index >= mid], ppy)

def main():
    px = load(sys.argv[1]); coins = list(px)
    # ---- Benchmark
    d1 = {c: rs(px[c], "1D") for c in coins}
    close1 = pd.DataFrame({c: d1[c].close for c in coins}).dropna()
    bh = close1.pct_change().fillna(0).mean(axis=1)
    a, b = split_stats(bh, 365)
    print("=== Benchmark: gleichgewichtet alle 10 Coins halten ===")
    print(f"Jahr1: {a['ret']:+.0f}% (DD {a['dd']:.0f}%) | Jahr2: {b['ret']:+.0f}% (DD {b['dd']:.0f}%)")
    print("Einzelne Coins Jahr2:", {c: round((close1[c].iloc[-1] / close1[c].iloc[len(close1)//2] - 1) * 100) for c in coins})

    # ---- Trendfolge
    print("\n=== Trendfolge Donchian, alle 10 Coins gleichgewichtet (Rendite % / Sharpe / MaxDD) ===")
    rows = []
    for tf, ppy in (("4h", 365 * 6), ("1D", 365)):
        D = {c: rs(px[c], tf) for c in coins}
        for n_in, n_out, lo in itertools.product((10, 20, 40, 60, 100), (5, 10, 20), (True, False)):
            if n_out >= n_in: continue
            R = pd.concat({c: pnl(D[c].close, trend_pos(D[c], n_in, n_out, lo)) for c in coins}, axis=1).mean(axis=1)
            a, b = split_stats(R, ppy)
            rows.append(dict(tf=tf, n_in=n_in, n_out=n_out, mode="long" if lo else "long/short",
                             tr_ret=a["ret"], tr_sh=a["sharpe"], tr_dd=a["dd"], te_ret=b["ret"], te_sh=b["sharpe"], te_dd=b["dd"]))
    T = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print("Auf Train positiv UND Test positiv:", len(T[(T.tr_ret > 0) & (T.te_ret > 0)]), "von", len(T))
    print(T.sort_values("tr_sh", ascending=False).head(8).round(1).to_string(index=False))
    print("-> Top 8 nach TRAIN-Sharpe, Spalten te_* sind der unberuehrte Test")

    # ---- Cross-Coin-Momentum (Wochen-Rebalancing)
    print("\n=== Cross-Coin-Momentum: Top-k nach Rendite der letzten L Tage, woechentlich neu ===")
    ret = close1.pct_change().fillna(0); rows = []
    for L, k, ls in itertools.product((7, 14, 30, 60, 90), (2, 3, 5), ("long", "long/short", "reversal")):
        mom = close1.pct_change(L)
        w = pd.DataFrame(0.0, index=close1.index, columns=coins)
        for i in range(0, len(close1), 7):
            if i < L: continue
            rk = mom.iloc[i].sort_values(ascending=False)
            top, bot = rk.index[:k], rk.index[-k:]
            if ls == "long": w.iloc[i, [coins.index(x) for x in top]] = 1 / k
            elif ls == "long/short":
                w.iloc[i, [coins.index(x) for x in top]] = 0.5 / k; w.iloc[i, [coins.index(x) for x in bot]] = -0.5 / k
            else:
                w.iloc[i, [coins.index(x) for x in bot]] = 0.5 / k; w.iloc[i, [coins.index(x) for x in top]] = -0.5 / k
        w = w.replace(0, np.nan)
        # Gewicht bleibt bis zum naechsten Rebalance
        reb = pd.Series(False, index=close1.index); reb.iloc[::7] = True
        w2 = pd.DataFrame(0.0, index=close1.index, columns=coins); cur = np.zeros(len(coins))
        for i in range(len(close1)):
            if reb.iloc[i] and i >= L: cur = w.iloc[i].fillna(0).values
            w2.iloc[i] = cur
        w2 = w2.shift(1).fillna(0)
        R = (w2 * ret).sum(axis=1) - w2.diff().abs().sum(axis=1).fillna(0) * CS
        a, b = split_stats(R, 365)
        rows.append(dict(L=L, k=k, mode=ls, tr_ret=a["ret"], tr_sh=a["sharpe"], te_ret=b["ret"], te_sh=b["sharpe"], te_dd=b["dd"]))
    M = pd.DataFrame(rows)
    print("Auf Train positiv UND Test positiv:", len(M[(M.tr_ret > 0) & (M.te_ret > 0)]), "von", len(M))
    print(M.sort_values("tr_sh", ascending=False).head(8).round(1).to_string(index=False))
    print("-> Top 8 nach TRAIN-Sharpe, te_* = unberuehrter Test")
    T.to_csv("jobs/done/search2_trend.csv", index=False); M.to_csv("jobs/done/search2_momentum.csv", index=False)

if __name__ == "__main__":
    main()
