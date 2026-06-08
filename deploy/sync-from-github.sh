#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt Watchdog/Bot, sichert Laufzeit-JSONs, zieht main und startet sauber neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
TMP_DIR="$(mktemp -d)"
RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

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

start_services() {
  sudo systemctl start krypto-bot
  sudo systemctl start safety-watchdog 2>/dev/null || true
}

backup_runtime_files

sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin
# Getrackte Laufzeitdateien nicht committen — zurück auf letzten Commit
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
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
