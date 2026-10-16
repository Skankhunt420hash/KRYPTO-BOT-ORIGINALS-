#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, zieht main, erhält lokale Laufzeit-JSONs und startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

RUNTIME_FILES=(
  "data/daily_summary.json"
  "data/runtime_recovery.json"
)
BACKUP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$BACKUP_DIR"
}
trap cleanup EXIT

backup_runtime_files() {
  local file
  for file in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$file" ]]; then
      mkdir -p "$BACKUP_DIR/$(dirname "$file")"
      cp -p "$file" "$BACKUP_DIR/$file"
    fi
  done
}

restore_runtime_files() {
  local file backup
  for file in "${RUNTIME_FILES[@]}"; do
    backup="$BACKUP_DIR/$file"
    if [[ -f "$backup" ]]; then
      mkdir -p "$(dirname "$file")"
      cp -p "$backup" "$file"
    fi
  done
}

echo "==> Working directory: $PWD"
sudo systemctl stop krypto-bot 2>/dev/null || true

backup_runtime_files

git fetch origin main
# Getrackte Laufzeitdateien temporär zurücksetzen, damit Pulls nicht an lokalen
# Runtime-Änderungen scheitern. Danach wird der lokale Safety-/Summary-State
# wiederhergestellt.
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  restore_runtime_files
  exit 1
}

restore_runtime_files

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
