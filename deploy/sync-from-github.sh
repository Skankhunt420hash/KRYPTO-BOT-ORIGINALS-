#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt Watchdog/Bot, zieht main und erhält lokale Runtime-JSONs.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

RUNTIME_FILES=(
  "data/daily_summary.json"
  "data/runtime_recovery.json"
)
BACKUP_DIR="$(mktemp -d)"
WATCHDOG_WAS_ACTIVE=false

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
  local file
  for file in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$BACKUP_DIR/$file" ]]; then
      mkdir -p "$(dirname "$file")"
      cp -p "$BACKUP_DIR/$file" "$file"
    fi
  done
}

cleanup() {
  local status=$?
  restore_runtime_files
  rm -rf "$BACKUP_DIR"
  if [[ $status -ne 0 ]]; then
    echo "Sync fehlgeschlagen — Runtime-Dateien wiederhergestellt, Dienste werden neu gestartet"
    sudo systemctl start krypto-bot 2>/dev/null || true
    if [[ "$WATCHDOG_WAS_ACTIVE" == "true" ]]; then
      sudo systemctl start safety-watchdog 2>/dev/null || true
    fi
  fi
  exit "$status"
}
trap cleanup EXIT

echo "==> Working directory: $PWD"
backup_runtime_files

if sudo systemctl is-active --quiet safety-watchdog 2>/dev/null; then
  WATCHDOG_WAS_ACTIVE=true
fi
sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin main
# Getrackte Laufzeitdateien nicht committen — zurück auf letzten Commit
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}
restore_runtime_files

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
if [[ "$WATCHDOG_WAS_ACTIVE" == "true" ]]; then
  sudo systemctl start safety-watchdog 2>/dev/null || true
fi
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
