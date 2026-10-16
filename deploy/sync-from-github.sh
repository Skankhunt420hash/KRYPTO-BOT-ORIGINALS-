#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, stellt Laufzeit-JSONs zurück, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"

RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}

backup_runtime_files() {
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$f" ]]; then
      mkdir -p "$TMP_DIR/$(dirname "$f")"
      cp -p "$f" "$TMP_DIR/$f"
    fi
  done
}

restore_runtime_files() {
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$TMP_DIR/$f" ]]; then
      mkdir -p "$(dirname "$f")"
      cp -p "$TMP_DIR/$f" "$f"
    fi
  done
}

restart_services() {
  sudo systemctl start krypto-bot
  sudo systemctl start safety-watchdog 2>/dev/null || true
}

on_error() {
  rc=$?
  echo "sync fehlgeschlagen — lokale Laufzeitdateien werden wiederhergestellt"
  restore_runtime_files
  restart_services || true
  exit "$rc"
}

trap cleanup EXIT
trap on_error ERR

backup_runtime_files
sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin
# Getrackte Laufzeitdateien nicht committen — zurück auf letzten Commit
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase
restore_runtime_files

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

restart_services
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
