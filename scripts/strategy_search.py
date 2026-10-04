#!/usr/bin/env python3
"""Ehrliche Strategie-Suche: Auswahl auf Jahr 1 (Train), Pruefung auf Jahr 2 (Test, unberuehrt).
Kosten: 0.1% Gebuehr/Seite + 0.05% Slippage/Seite. Einstieg am naechsten Open, SL gewinnt bei Gleichstand."""
import sys, glob, os, itertools, json
import numpy as np, pandas as pd
from numba import njit

FEE, SLIP = 0.001, 0.0005
COST = 2 * (FEE + SLIP)          # Rundlauf, relativ

def load(d):
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "*_1h.csv"))):
        df = pd.read_csv(f)
        out[os.path.basename(f).split("_")[0]] = df
    return out

def ema(x, n): return pd.Series(x).ewm(span=n, adjust=False).mean().values
def rsi(c, n):
    d = pd.Series(c).diff(); up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).values
def atr(h, l, c, n=14):
    pc = np.roll(c, 1); tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc))); tr[0] = h[0] - l[0]
    return pd.Series(tr).ewm(alpha=1/n, adjust=False).mean().values

def signals(df):
    o, h, l, c, v = (df[k].values.astype(float) for k in ["open", "high", "low", "close", "volume"])
    s = pd.Series(c); sig = {}
    a = atr(h, l, c); e200 = ema(c, 200); e50 = ema(c, 50); e20 = ema(c, 20)
    up, dn = c > e200, c < e200
    for n, lo, hi in [(14, 30, 70), (14, 25, 75), (14, 20, 80), (7, 20, 80), (7, 10, 90), (2, 5, 95)]:
        r = rsi(c, n)
        sig[f"RSI{n}_{lo}/{hi}"] = (r < lo, r > hi)
        sig[f"RSI{n}_{lo}/{hi}+Trend"] = ((r < lo) & up, (r > hi) & dn)
        sig[f"RSI{n}_{lo}/{hi}_Momentum"] = (r > hi, r < lo)
    for n, k in [(20, 2.0), (20, 2.5), (20, 3.0)]:
        m = s.rolling(n).mean().values; sd = s.rolling(n).std().values
        sig[f"BB{n}_{k}_Reversion"] = (c < m - k * sd, c > m + k * sd)
        sig[f"BB{n}_{k}_Reversion+Trend"] = ((c < m - k * sd) & up, (c > m + k * sd) & dn)
        sig[f"BB{n}_{k}_Breakout"] = (c > m + k * sd, c < m - k * sd)
    for n in (20, 50, 100):
        hh = pd.Series(h).rolling(n).max().shift(1).values; ll = pd.Series(l).rolling(n).min().shift(1).values
        sig[f"Donchian{n}_Breakout"] = (c > hh, c < ll)
        sig[f"Donchian{n}_Fade"] = (c < ll, c > hh)
    cr = (e20 > e50)
    sig["EMA20/50_Cross"] = (cr & ~np.roll(cr, 1), ~cr & np.roll(cr, 1))
    pull = (c < e20) & (c > e50) & (e20 > e50); pull_s = (c > e20) & (c < e50) & (e20 < e50)
    sig["Trend_Pullback"] = (pull & up, pull_s & dn)
    vr = v / pd.Series(v).rolling(48).mean().values
    for z in (2.0, 3.0):
        ret = pd.Series(c).pct_change(); rz = (ret / ret.rolling(48).std()).values
        sig[f"Volspike{z}_Reversal"] = ((vr > 2) & (rz < -z), (vr > 2) & (rz > z))
        sig[f"Drop{z}sigma_Bounce"] = (rz < -z, rz > z)
    return o, h, l, c, a, sig

@njit(cache=True)
def sim(o, h, l, c, a, ent, side, tpm, slm, H, cost, start, end):
    n = len(c); res = np.empty(n); idx = np.empty(n, np.int64); k = 0; i = start
    while i < end - 2:
        if ent[i] and a[i] > 0 and not np.isnan(a[i]):
            e = o[i + 1]; d = a[i] / e
            if side == 1:
                tp = e * (1 + tpm * d); sl = e * (1 - slm * d)
            else:
                tp = e * (1 - tpm * d); sl = e * (1 + slm * d)
            j = i + 1; ex = c[min(i + H, n - 1)]; last = min(i + H, n - 1)
            for j in range(i + 1, last + 1):
                if side == 1:
                    if l[j] <= sl: ex = sl; last = j; break
                    if h[j] >= tp: ex = tp; last = j; break
                else:
                    if h[j] >= sl: ex = sl; last = j; break
                    if l[j] <= tp: ex = tp; last = j; break
            else:
                ex = c[last]
            g = (ex - e) / e if side == 1 else (e - ex) / e
            res[k] = g - cost; idx[k] = i; k += 1
            i = last
        i += 1
    return res[:k], idx[:k]

def stats(r):
    if len(r) == 0: return dict(n=0, win=np.nan, avg=np.nan, pf=np.nan)
    w = r[r > 0].sum(); L = -r[r <= 0].sum()
    return dict(n=len(r), win=(r > 0).mean() * 100, avg=r.mean() * 100, pf=(w / L if L > 0 else np.inf))

def main():
    d = sys.argv[1]; out = sys.argv[2] if len(sys.argv) > 2 else "search_result.json"
    data = load(d); coins = list(data)
    pre = {c: signals(data[c]) for c in coins}
    n = min(len(data[c]) for c in coins); split = n // 2
    names = list(pre[coins[0]][5].keys())
    TP = [0.5, 0.75, 1, 1.5, 2, 3]; SL = [1, 1.5, 2, 3, 4, 6]; HS = [12, 24, 48, 96]
    rows = []
    for nm in names:
        for side_name, si in (("long", 0), ("short", 1)):
            side = 1 if si == 0 else -1
            for tpm, slm, H in itertools.product(TP, SL, HS):
                tr_r, te_r, per = [], [], {}
                for c in coins:
                    o, h, l, cl, a, sg = pre[c]
                    ent = np.nan_to_num(sg[nm][si]).astype(np.bool_)
                    r1, _ = sim(o, h, l, cl, a, ent, side, tpm, slm, H, COST, 210, split)
                    r2, _ = sim(o, h, l, cl, a, ent, side, tpm, slm, H, COST, split, len(cl))
                    tr_r.append(r1); te_r.append(r2); per[c] = float(r2.mean() * 100) if len(r2) else None
                R1, R2 = np.concatenate(tr_r), np.concatenate(te_r)
                s1, s2 = stats(R1), stats(R2)
                pos = sum(1 for v in per.values() if v is not None and v > 0)
                rows.append(dict(rule=nm, side=side_name, tp=tpm, sl=slm, H=H,
                                 tr_n=s1["n"], tr_win=s1["win"], tr_avg=s1["avg"], tr_pf=s1["pf"],
                                 te_n=s2["n"], te_win=s2["win"], te_avg=s2["avg"], te_pf=s2["pf"], coins_pos=pos))
    df = pd.DataFrame(rows); df.to_csv(out.replace(".json", ".csv"), index=False)
    print(f"Konfigurationen getestet: {len(df)}, Coins: {coins}, Train/Test-Split bei Kerze {split}")
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    sel = df[(df.tr_n >= 150) & (df.tr_win >= 65) & (df.tr_avg > 0)]
    print(f"\nAuf TRAIN: >=65% Winrate UND positiver Erwartungswert, n>=150: {len(sel)} Konfigs")
    if len(sel):
        print("-> davon auf TEST (unberuehrt) mit Winrate>=65% und Erwartungswert>0:",
              len(sel[(sel.te_win >= 65) & (sel.te_avg > 0)]))
        print(sel.sort_values("tr_avg", ascending=False).head(15).round(2).to_string(index=False))
    print("\nTop 15 nach TEST-Erwartungswert, wenn auch TRAIN positiv (n>=150 je Zeitraum):")
    t = df[(df.tr_n >= 150) & (df.te_n >= 150) & (df.tr_avg > 0)].sort_values("te_avg", ascending=False)
    print(t.head(15).round(2).to_string(index=False))
    print("\nBeste Winrate mit positivem Erwartungswert auf TRAIN *und* TEST (n>=150):")
    b = df[(df.tr_n >= 150) & (df.te_n >= 150) & (df.tr_avg > 0) & (df.te_avg > 0)].sort_values("te_win", ascending=False)
    print(len(b), "Konfigs"); print(b.head(15).round(2).to_string(index=False))
    print("\nHoechste Winrate ueberhaupt (Test, n>=150), egal ob Gewinn:")
    print(df[df.te_n >= 150].sort_values("te_win", ascending=False).head(10).round(2).to_string(index=False))

if __name__ == "__main__":
    main()
