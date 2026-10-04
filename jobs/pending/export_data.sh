#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/../.."
mkdir -p data/real jobs/done/data
for S in BTC ETH SOL BNB XRP ADA DOGE LINK AVAX LTC; do
  python3 scripts/fetch_real_data.py --exchange auto --symbol "$S/USDT" --timeframe 1h --years 2 \
    --out "data/real/${S}_1h.csv" 2>&1 | grep -E "Quelle|Gespeichert|FEHLER|nicht nutzbar" 
  [ -f "data/real/${S}_1h.csv" ] && gzip -9 -c "data/real/${S}_1h.csv" > "jobs/done/data/${S}_1h.csv.gz"
done
ls -la jobs/done/data
