#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt Watchdog/Bot, zieht main und erhält operative Runtime-JSONs.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
BOT_SERVICE="${BOT_SERVICE:-krypto-bot}"
SAFETY_WATCHDOG_SERVICE="${SAFETY_WATCHDOG_SERVICE:-safety-watchdog}"
RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
BACKUP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$BACKUP_DIR"
}
trap cleanup EXIT

backup_runtime_files() {
  local rel
  for rel in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$rel" ]]; then
      mkdir -p "$BACKUP_DIR/$(dirname "$rel")"
      cp -p "$rel" "$BACKUP_DIR/$rel"
    fi
  done
}

restore_runtime_files() {
  local rel
  for rel in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$BACKUP_DIR/$rel" ]]; then
      mkdir -p "$(dirname "$rel")"
      cp -p "$BACKUP_DIR/$rel" "$rel"
    fi
  done
}

restart_services() {
  sudo systemctl start "$BOT_SERVICE"
  sudo systemctl start "$SAFETY_WATCHDOG_SERVICE" 2>/dev/null || true
}

fail_sync() {
  echo "$1"
  restore_runtime_files
  restart_services
  exit 1
}

backup_runtime_files
sudo systemctl stop "$SAFETY_WATCHDOG_SERVICE" 2>/dev/null || true
sudo systemctl stop "$BOT_SERVICE" 2>/dev/null || true

git fetch origin main || fail_sync "fetch fehlgeschlagen — Runtime-Dateien wiederhergestellt, Services gestartet"
# Getrackte Laufzeitdateien nicht committen — zurück auf letzten Commit
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  fail_sync "pull fehlgeschlagen — Runtime-Dateien wiederhergestellt, Services gestartet"
}
restore_runtime_files

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt || fail_sync "pip install fehlgeschlagen — Runtime-Dateien wiederhergestellt, Services gestartet"
fi

restart_services
sudo systemctl status "$BOT_SERVICE" --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
