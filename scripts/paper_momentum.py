#!/usr/bin/env python3
"""Paper-Trading (KEIN echtes Geld): Long/Short Cross-Coin-Momentum.
Regel (aus Backtest-Kandidat): woechentlich (Montag UTC) die 10 Coins nach 14-Tage-Rendite ranken,
Top 2 long, Flop 2 short, je 25% des Eigenkapitals. Kosten: 0.15%/Seite (Gebuehr+Slippage),
Short-Funding-Annahme 0.01% pro 8h auf Short-Volumen (Schaetzung, nicht gemessen).
Zustand: paper/state.json, Log: paper/trades.csv, paper/equity.csv, Uebersicht: paper/STATUS.md"""
import json, os, sys, csv
from datetime import datetime, timezone
from pathlib import Path

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LINK", "AVAX", "LTC"]
LOOKBACK, K, W = 14, 2, 0.25
COST, FUND_8H, START = 0.0015, 0.0001, 10000.0
P = Path("paper"); STATE = P / "state.json"

def fetch_prices():
    """-> (dict coin -> Liste Tages-Closes abgeschlossener Tage, dict coin -> aktueller Preis)"""
    import ccxt
    for ex_id, quote in (("binanceus", "USDT"), ("bitstamp", "USD"), ("coinbaseexchange", "USD")):
        try:
            ex = getattr(ccxt, ex_id)({"enableRateLimit": True}); hist, last = {}, {}
            for c in COINS:
                rows = ex.fetch_ohlcv(f"{c}/{quote}", "1d", limit=LOOKBACK + 5)
                today0 = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
                hist[c] = [r[4] for r in rows if r[0] < today0]
                last[c] = rows[-1][4]
            return hist, last, ex_id
        except Exception as e:
            print(f"{ex_id} fehlgeschlagen: {type(e).__name__} {str(e)[:80]}", file=sys.stderr)
    raise SystemExit("keine Boerse erreichbar")

def load():
    if STATE.exists(): return json.loads(STATE.read_text())
    return dict(cash=START, qty={c: 0.0 for c in COINS}, last_ts=None, last_rebalance=None, start_ts=None)

def equity(s, px): return s["cash"] + sum(s["qty"][c] * px[c] for c in COINS)

def step(s, hist, px, now, log):
    ts = now.timestamp()
    if s["last_ts"] is not None:  # Short-Funding seit letztem Lauf abziehen
        hrs = (ts - s["last_ts"]) / 3600
        short_notional = sum(-s["qty"][c] * px[c] for c in COINS if s["qty"][c] < 0)
        s["cash"] -= short_notional * FUND_8H * hrs / 8
    else:
        s["start_ts"] = ts
    s["last_ts"] = ts
    today = now.strftime("%Y-%m-%d")
    due = s["last_rebalance"] is None or (now.weekday() == 0 and s["last_rebalance"] != today)
    if due and all(len(hist[c]) > LOOKBACK for c in COINS):
        mom = {c: hist[c][-1] / hist[c][-1 - LOOKBACK] - 1 for c in COINS}
        rk = sorted(COINS, key=lambda c: mom[c], reverse=True)
        target = {c: 0.0 for c in COINS}
        for c in rk[:K]: target[c] = W
        for c in rk[-K:]: target[c] = -W
        E = equity(s, px)
        for c in COINS:
            want = target[c] * E / px[c]; d = want - s["qty"][c]
            if abs(d * px[c]) < 1e-6: continue
            fill = px[c] * (1 + 0.0005) if d > 0 else px[c] * (1 - 0.0005)   # Slippage 0.05%
            fee = abs(d) * fill * 0.001                                        # Gebuehr 0.1%
            s["cash"] -= d * fill + fee; s["qty"][c] = want
            log.append([now.isoformat(timespec="minutes"), c, "BUY" if d > 0 else "SELL", f"{abs(d):.6f}", f"{fill:.6f}", f"{fee:.4f}", f"{mom[c]*100:+.1f}%"])
        s["last_rebalance"] = today
        s["ranking"] = {c: round(mom[c] * 100, 1) for c in rk}
    return s

def write_status(s, px, src, now):
    E = equity(s, px); ret = (E / START - 1) * 100
    days = (now.timestamp() - s["start_ts"]) / 86400 if s["start_ts"] else 0
    pos = "\n".join(f"| {c} | {'LONG' if s['qty'][c]>0 else 'SHORT'} | {s['qty'][c]*px[c]:,.0f} USDT |" for c in COINS if abs(s["qty"][c]) > 0)
    (P / "STATUS.md").write_text(f"""# Paper-Trading Status (KEIN echtes Geld)

Strategie: Long/Short Cross-Coin-Momentum (14 Tage, Top 2 long / Flop 2 short, woechentlich Montag)
Stand: {now.strftime('%Y-%m-%d %H:%M')} UTC | Preisquelle: {src} | Laufzeit: {days:.1f} Tage

**Eigenkapital: {E:,.2f} USDT (Start {START:,.0f}) = {ret:+.2f}%**

| Coin | Seite | Volumen |
|---|---|---|
{pos}

Letztes Rebalancing: {s['last_rebalance']} | Ranking (14T-Rendite %): {s.get('ranking')}

Hinweis: Backtest-Kandidat, noch nicht bewiesen. Kosten 0.15%/Seite, Short-Funding nur geschaetzt.
""")

def main():
    P.mkdir(exist_ok=True); now = datetime.now(timezone.utc)
    hist, px, src = fetch_prices(); s = load(); log = []
    s = step(s, hist, px, now, log)
    STATE.write_text(json.dumps(s, indent=1))
    if log:
        new = not (P / "trades.csv").exists()
        with open(P / "trades.csv", "a", newline="") as f:
            w = csv.writer(f)
            if new: w.writerow(["zeit_utc", "coin", "seite", "menge", "preis", "gebuehr", "mom14"])
            w.writerows(log)
    new = not (P / "equity.csv").exists()
    with open(P / "equity.csv", "a", newline="") as f:
        w = csv.writer(f)
        if new: w.writerow(["zeit_utc", "equity"])
        w.writerow([now.isoformat(timespec="minutes"), f"{equity(s, px):.2f}"])
    write_status(s, px, src, now); print(f"Equity {equity(s, px):.2f}, Trades heute: {len(log)}")

if __name__ == "__main__":
    main()
