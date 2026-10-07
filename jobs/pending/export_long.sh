#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/../.."
mkdir -p data/real jobs/done/data_long
for S in BTC ETH SOL BNB XRP ADA DOGE LINK AVAX LTC DOT ATOM TRX XLM BCH; do
  for Y in 7 6 5 4 3; do
    python3 scripts/fetch_real_data.py --exchange auto --symbol "$S/USDT" --timeframe 1d --years $Y \
      --out "data/real/${S}_1d.csv" 2>&1 | grep -E "Quelle|Gespeichert"
    [ -f "data/real/${S}_1d.csv" ] && break
  done
  [ -f "data/real/${S}_1d.csv" ] && gzip -9 -c "data/real/${S}_1d.csv" > "jobs/done/data_long/${S}_1d.csv.gz"
done
ls -la jobs/done/data_long
