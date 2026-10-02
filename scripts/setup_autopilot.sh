#!/usr/bin/env bash
# EINMALIG auf dem Server ausfuehren. Danach braucht es nie wieder manuelles
# Eintippen fuer Auftraege -- der Server holt sie sich selbst von GitHub.
#
# Aufruf:
#   bash scripts/setup_autopilot.sh <GITHUB_TOKEN>
#
# <GITHUB_TOKEN> = Fine-grained Token mit Schreibrecht NUR auf dieses Repo:
#   github.com -> Settings -> Developer settings -> Fine-grained tokens
#   -> Generate new token -> "Only select repositories" -> KRYPTO-BOT-ORIGINALS-
#   -> Repository permissions -> Contents: Read and write -> Generate
set -euo pipefail
TOKEN="${1:-}"
if [ -z "$TOKEN" ]; then
  echo "Aufruf: bash scripts/setup_autopilot.sh <GITHUB_TOKEN>"
  exit 1
fi

cd "$(dirname "$0")/.."
REPO_URL="https://x-access-token:${TOKEN}@github.com/Skankhunt420hash/KRYPTO-BOT-ORIGINALS-.git"

git remote set-url origin "$REPO_URL"
git config user.email "autopilot@nemesis.local"
git config user.name "Nemesis Autopilot (Droplet)"

git pull -q origin main --no-rebase
chmod +x scripts/autopilot.sh scripts/run_real_backtest.sh scripts/fetch_real_data.py
mkdir -p jobs/pending jobs/done jobs/archive
touch jobs/pending/.gitkeep jobs/done/.gitkeep jobs/archive/.gitkeep

# Teste, ob der Token wirklich schreiben darf, bevor der Cron drauf baut
echo "autopilot eingerichtet $(date -u +%FT%TZ)" > jobs/.setup_test
git add jobs/.setup_test jobs/pending/.gitkeep jobs/done/.gitkeep jobs/archive/.gitkeep
git commit -q -m "autopilot: Einrichtung" || true
if git push -q origin main; then
  echo "OK: Token kann schreiben, Push hat funktioniert."
else
  echo "FEHLER: Push fehlgeschlagen. Token pruefen (Contents: Read and write, richtiges Repo?)."
  exit 1
fi

# Cron-Eintrag (alle 3 Minuten), ohne Duplikate
( crontab -l 2>/dev/null | grep -v 'autopilot.sh'; \
  echo "*/3 * * * * /root/krypto-bot/scripts/autopilot.sh >> /root/krypto-bot/autopilot.log 2>&1" \
) | crontab -

echo ""
echo "=================================================================="
echo "FERTIG. Der Server prueft ab jetzt alle 3 Minuten von selbst auf"
echo "neue Auftraege (jobs/pending/) und schreibt Ergebnisse zurueck nach"
echo "GitHub (jobs/done/). Du musst hier nichts mehr eintippen."
echo "=================================================================="
