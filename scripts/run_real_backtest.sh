#!/usr/bin/env bash
# Läuft auf dem Server. Lädt ECHTE Kursdaten und testet alle Strategien ehrlich.
# Aufruf (einmal, im Bot-Ordner):  bash scripts/run_real_backtest.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Code aktualisieren"
git pull origin main --no-rebase

if [[ ! -f .venv/bin/python ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

SYMBOLS=("BTC/USDT" "ETH/USDT" "SOL/USDT")
YEARS=2
TF=1h
RESULTS_DIR="results/real_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$RESULTS_DIR"

for SYM in "${SYMBOLS[@]}"; do
  echo ""
  echo "=================================================================="
  echo "==> Echte Daten holen: $SYM"
  echo "=================================================================="
  python3 scripts/fetch_real_data.py --exchange auto --symbol "$SYM" --timeframe "$TF" --years "$YEARS"
done

# Leise waehrend des Backtests: der Meta-Selector protokolliert sonst eine Zeile
# PRO KERZE (bei 17'500 Kerzen x 3 Symbole = zigtausende Zeilen) -- bremst alles
# aus und sieht in der Konsole wie ein Haenger aus. Der Endbericht laeuft nicht
# ueber den Logger und bleibt unveraendert sichtbar. Live-/Papierbetrieb ist
# davon nicht betroffen (dort bleibt LOG_LEVEL unveraendert = INFO).
export LOG_LEVEL=WARNING

for SYM in "${SYMBOLS[@]}"; do
  CSV="data/real/${SYM//\//_}_${TF}.csv"
  echo ""
  echo "=================================================================="
  echo "==> Echter Backtest (alle Strategien + Meta-Selector): $SYM"
  echo "    (laeuft leise -- bei 17.5k Kerzen dauert das ein paar Minuten)"
  echo "=================================================================="
  python3 main.py --backtest --csv "$CSV" --multi --export "$RESULTS_DIR" | tee "$RESULTS_DIR/${SYM//\//_}_report.txt"
done

echo ""
echo "=================================================================="
echo "==> Fertig. Ergebnisse liegen in: $RESULTS_DIR"
echo "=================================================================="
