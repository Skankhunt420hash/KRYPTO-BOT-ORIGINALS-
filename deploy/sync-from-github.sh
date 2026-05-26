#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt Watchdog/Bot, zieht main und bewahrt operative Laufzeit-JSONs.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"

RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)
BACKUP_DIR="$(mktemp -d)"
BOT_STOPPED=0
BOT_STARTED=0
WATCHDOG_STOPPED=0
WATCHDOG_STARTED=0

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
  restore_runtime_files
  if [[ "$BOT_STOPPED" == "1" && "$BOT_STARTED" != "1" ]]; then
    sudo systemctl start krypto-bot 2>/dev/null || true
  fi
  if [[ "$WATCHDOG_STOPPED" == "1" && "$WATCHDOG_STARTED" != "1" ]]; then
    sudo systemctl start safety-watchdog 2>/dev/null || true
  fi
  rm -rf "$BACKUP_DIR"
  exit "$status"
}
trap on_exit EXIT

sudo systemctl stop safety-watchdog 2>/dev/null || true
WATCHDOG_STOPPED=1
sudo systemctl stop krypto-bot 2>/dev/null || true
BOT_STOPPED=1
backup_runtime_files

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
BOT_STARTED=1
sudo systemctl start safety-watchdog 2>/dev/null || true
WATCHDOG_STARTED=1
sudo systemctl status krypto-bot --no-pager || true
sudo systemctl status safety-watchdog --no-pager 2>/dev/null || true
trap - EXIT
rm -rf "$BACKUP_DIR"
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
