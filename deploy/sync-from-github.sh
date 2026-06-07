#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt Dienste, sichert Laufzeit-JSONs, zieht main, stellt Runtime-State wieder her, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
RUNTIME_BACKUP_DIR="$(mktemp -d)"
RUNTIME_FILES=(
  "data/daily_summary.json"
  "data/runtime_recovery.json"
)

cleanup() {
  rm -rf "$RUNTIME_BACKUP_DIR"
}
trap cleanup EXIT

backup_runtime_files() {
  for file in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$file" ]]; then
      mkdir -p "$RUNTIME_BACKUP_DIR/$(dirname "$file")"
      cp -p "$file" "$RUNTIME_BACKUP_DIR/$file"
    fi
  done
}

restore_runtime_files() {
  for file in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$RUNTIME_BACKUP_DIR/$file" ]]; then
      mkdir -p "$(dirname "$file")"
      cp -p "$RUNTIME_BACKUP_DIR/$file" "$file"
    fi
  done
}

restart_services() {
  sudo systemctl start krypto-bot
  sudo systemctl start safety-watchdog 2>/dev/null || true
}

backup_runtime_files

sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin
# Getrackte Laufzeitdateien nur fuer den Pull zuruecksetzen; danach wird der
# lokale Runtime-State wiederhergestellt.
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  restore_runtime_files
  restart_services
  exit 1
}

restore_runtime_files

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

restart_services
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
