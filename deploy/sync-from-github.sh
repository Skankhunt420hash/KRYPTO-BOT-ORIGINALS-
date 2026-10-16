#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt die Dienste, zieht main und erhält operative Laufzeit-JSONs.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
BOT_SERVICE="${BOT_SERVICE:-krypto-bot}"
WATCHDOG_SERVICE="${WATCHDOG_SERVICE:-safety-watchdog}"
RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)
BACKUP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$BACKUP_DIR"
}
trap cleanup EXIT

backup_runtime_files() {
  mkdir -p "$BACKUP_DIR/data"
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$f" ]]; then
      cp -p "$f" "$BACKUP_DIR/$f"
    fi
  done
}

restore_runtime_files() {
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$BACKUP_DIR/$f" ]]; then
      mkdir -p "$(dirname "$f")"
      cp -p "$BACKUP_DIR/$f" "$f"
    fi
  done
}

start_services() {
  sudo systemctl start "$BOT_SERVICE"
  sudo systemctl start "$WATCHDOG_SERVICE" 2>/dev/null || true
}

sudo systemctl stop "$WATCHDOG_SERVICE" 2>/dev/null || true
sudo systemctl stop "$BOT_SERVICE" 2>/dev/null || true
backup_runtime_files

git fetch origin
# Pull darf nicht an getrackten Runtime-Dateien scheitern; der operative Zustand wird danach wiederhergestellt.
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  restore_runtime_files
  start_services
  exit 1
}

restore_runtime_files

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

start_services
sudo systemctl status "$BOT_SERVICE" --no-pager || true
echo "==> Logs: sudo journalctl -u $BOT_SERVICE -n 50 --no-pager"
