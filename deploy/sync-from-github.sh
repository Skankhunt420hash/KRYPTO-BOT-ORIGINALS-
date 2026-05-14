#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, zieht main und bewahrt lokalen Recovery-State, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin

# runtime_recovery.json enthält operative Sperren wie /pause und /riskoff.
# Für den Pull kurz zurücksetzen, danach den lokalen Serverzustand wiederherstellen.
recovery_backup=""
if [[ -f data/runtime_recovery.json ]]; then
  recovery_backup="$(mktemp)"
  cp data/runtime_recovery.json "$recovery_backup"
fi

# Getrackte Laufzeitdateien nicht committen — kurz zurück auf letzten Commit
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  if [[ -n "$recovery_backup" && -f "$recovery_backup" ]]; then
    mkdir -p data
    cp "$recovery_backup" data/runtime_recovery.json
    rm -f "$recovery_backup"
  fi
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

if [[ -n "$recovery_backup" && -f "$recovery_backup" ]]; then
  mkdir -p data
  cp "$recovery_backup" data/runtime_recovery.json
  rm -f "$recovery_backup"
fi

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
