#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, stellt Laufzeit-JSONs zurück, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
sudo systemctl stop krypto-bot 2>/dev/null || true

RUNTIME_RECOVERY_BACKUP=""
restore_runtime_recovery() {
  if [[ -n "${RUNTIME_RECOVERY_BACKUP:-}" && -f "$RUNTIME_RECOVERY_BACKUP" ]]; then
    mkdir -p data
    cp "$RUNTIME_RECOVERY_BACKUP" data/runtime_recovery.json
    rm -f "$RUNTIME_RECOVERY_BACKUP"
  fi
}
trap restore_runtime_recovery EXIT

if [[ -f data/runtime_recovery.json ]]; then
  RUNTIME_RECOVERY_BACKUP="$(mktemp)"
  cp data/runtime_recovery.json "$RUNTIME_RECOVERY_BACKUP"
fi

git fetch origin main
# Getrackte Laufzeitdateien nicht committen — zurück auf letzten Commit.
# runtime_recovery.json enthält operative Pause/Risk-Off-Locks und wird danach wiederhergestellt.
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

restore_runtime_recovery

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
