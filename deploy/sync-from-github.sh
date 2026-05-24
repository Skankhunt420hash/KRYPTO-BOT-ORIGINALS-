#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Zieht main, ohne operative Laufzeit-Sperren wie Pause/Risk-Off zu verlieren.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"

RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)
BACKUP_DIR="$(mktemp -d)"
SERVICE_STOPPED=0
SERVICE_STARTED=0

backup_runtime_files() {
  local file backup
  for file in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$file" ]]; then
      backup="$BACKUP_DIR/${file//\//__}"
      cp -p "$file" "$backup"
    fi
  done
}

restore_runtime_files() {
  local file backup
  for file in "${RUNTIME_FILES[@]}"; do
    backup="$BACKUP_DIR/${file//\//__}"
    if [[ -f "$backup" ]]; then
      mkdir -p "$(dirname "$file")"
      cp -p "$backup" "$file"
    fi
  done
}

on_exit() {
  local status=$?
  if [[ "$SERVICE_STOPPED" == "1" ]]; then
    restore_runtime_files
    if [[ "$SERVICE_STARTED" != "1" ]]; then
      sudo systemctl start krypto-bot 2>/dev/null || true
    fi
  fi
  rm -rf "$BACKUP_DIR"
  exit "$status"
}
trap on_exit EXIT

git fetch origin
sudo systemctl stop krypto-bot 2>/dev/null || true
SERVICE_STOPPED=1
backup_runtime_files
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
SERVICE_STARTED=1
sudo systemctl status krypto-bot --no-pager || true
trap - EXIT
rm -rf "$BACKUP_DIR"
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
